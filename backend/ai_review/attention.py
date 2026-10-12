"""发布待核对的只读投影；待办与阻塞关系始终来自审查队列。"""
import contextlib
import time
import json

from .common import ReviewError

PAGE_SIZE = 20
STATES = {'queued', 'running', 'publishing', 'recovering', 'publish_unknown', 'succeeded', 'failed', 'superseded'}
REASON = '等待管理员核对前次发布结果'


def migrate(c):
    existing = {r[1] for r in c.execute('PRAGMA table_info(jobs)')}
    for name, definition in {
        'reconcile_state': "TEXT NOT NULL DEFAULT ''",
        'reconcile_requested_at': 'REAL', 'reconcile_started_at': 'REAL', 'reconcile_finished_at': 'REAL',
        'reconcile_message': "TEXT NOT NULL DEFAULT ''",
        'reconcile_requested_by': "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in existing:
            c.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
    c.executescript('''
    CREATE INDEX IF NOT EXISTS jobs_attention ON jobs(status,repository,number);
    CREATE TRIGGER IF NOT EXISTS review_attention_transition AFTER UPDATE OF status ON jobs
      WHEN OLD.status!=NEW.status AND (OLD.status='publish_unknown' OR NEW.status='publish_unknown') BEGIN
      UPDATE pr_state SET progress_seq=progress_seq+1
        WHERE repository=NEW.repository AND number=NEW.number;
      UPDATE jobs SET
        reconcile_state=CASE WHEN NEW.status='publish_unknown' THEN 'needs_confirmation' ELSE 'resolved' END,
        reconcile_message=CASE WHEN NEW.status='publish_unknown' THEN NEW.error ELSE NEW.stage END,
        reconcile_finished_at=CASE WHEN NEW.status='publish_unknown' THEN NULL ELSE NEW.updated END
        WHERE id=NEW.id;
    END;
    ''')


def worker_state(c, now=None):
    now = time.time() if now is None else now
    row = c.execute("SELECT updated FROM health WHERE name='worker'").fetchone()
    available = bool(row and now - row['updated'] <= 90)
    return {'available': available, 'last_seen': row['updated'] if row else None,
            'message': '' if available else '工作进程尚未就绪或心跳已过期，发布核对暂时无法推进'}


def blocking(c, repository, number):
    row = c.execute("""SELECT count(*) AS pending_count, min(COALESCE(finished_at,updated,created)) AS since
                       FROM jobs WHERE repository=? AND number=? AND status='publish_unknown'""",
                    (repository, number)).fetchone()
    return {'required': bool(row['pending_count']), 'pending_count': row['pending_count'],
            'since': row['since'], 'repository': repository, 'number': number,
            'reason': REASON if row['pending_count'] else ''}


def reconciliation(job, worker):
    state = job.get('reconcile_state') or ('needs_confirmation' if job['status'] == 'publish_unknown' else '')
    return {'state': state, 'requested_at': job.get('reconcile_requested_at'),
            'started_at': job.get('reconcile_started_at'), 'finished_at': job.get('reconcile_finished_at'),
            'message': job.get('reconcile_message') or (job['error'] if state else ''),
            'requested_by': job.get('reconcile_requested_by', ''), 'worker': worker}


def listing(store, page=1, repository=None, number=None, allowed_jobs=None):
    # 先分别聚合待核对和排队任务，再关联；避免多个阻塞任务乘出重复排队数。
    prefix = 'WITH scoped_jobs AS (SELECT * FROM jobs'
    scope_args = []
    if allowed_jobs is not None:
        prefix += ' WHERE id IN (SELECT value FROM json_each(?))'
        scope_args.append(json.dumps(allowed_jobs))
    grouped = prefix + '''), pending AS (
        SELECT repository,number,count(*) AS pending_count,min(COALESCE(finished_at,updated,created)) AS since
        FROM scoped_jobs WHERE status='publish_unknown' GROUP BY repository,number
      ), waiting AS (
        SELECT repository,number,count(*) AS blocked_count FROM scoped_jobs WHERE status='queued' GROUP BY repository,number
      ), groups AS (
        SELECT p.*,COALESCE(w.blocked_count,0) AS blocked_count FROM pending p LEFT JOIN waiting w
          ON w.repository=p.repository AND w.number=p.number
      ) '''
    conditions, args = [], []
    for name, value in (('repository', repository), ('number', number)):
        if value is not None:
            conditions.append('g.' + name + '=?'); args.append(value)
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    with contextlib.closing(store.connect()) as c:
        c.execute('BEGIN')
        summary = dict(c.execute(grouped + '''SELECT count(*) AS pr_count,
            COALESCE(sum(blocked_count),0) AS blocked_task_count,
            COALESCE(sum(pending_count),0) AS pending_task_count FROM groups''', scope_args).fetchone())
        total = c.execute(grouped + 'SELECT count(*) FROM groups g' + where, scope_args + args).fetchone()[0]
        rows = c.execute(grouped + '''SELECT g.*,r.server_id,
            COALESCE(json_extract(r.value,'$.server_name'),'') AS server_name,
            COALESCE(json_extract(r.value,'$.repo_name'),'') AS repo_name
            FROM groups g JOIN repositories r ON r.id=g.repository''' + where + '''
            ORDER BY (g.blocked_count>0) DESC,g.since,g.repository,g.number LIMIT ? OFFSET ?''',
                         scope_args + args + [PAGE_SIZE, (page-1)*PAGE_SIZE]).fetchall()
        worker = worker_state(c)
    return {'summary': summary, 'items': [dict(row, reason=REASON) for row in rows], 'total': total,
            'page': page, 'page_size': PAGE_SIZE, 'worker': worker}


def request_reconciliation(store, job_id, actor='共享管理员'):
    with store.tx() as c:
        row = c.execute('SELECT status,reconcile_state FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ReviewError('任务不存在')
        if row['status'] != 'publish_unknown':
            if row['status'] in ('succeeded', 'failed'):
                return False
            raise ReviewError('该任务不需要核对发布结果')
        if row['reconcile_state'] in ('queued', 'checking'):
            return False
        c.execute("""UPDATE jobs SET available=0,reconcile_state='queued',reconcile_requested_at=?,
            reconcile_started_at=NULL,reconcile_finished_at=NULL,reconcile_requested_by=?,
            reconcile_message='已请求核对，等待工作进程执行' WHERE id=? AND status='publish_unknown'""",
                  (time.time(), actor, job_id))
    return True
