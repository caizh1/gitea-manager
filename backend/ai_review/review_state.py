"""固定版本的单元检查点与批次额度，已持久化阶段不重复调用模型。"""
import contextlib
import json
import time

from .common import ReviewError, digest, encode
from .budgets import call_reservation


def migrate(c):
    fields = {'diagnostic': 'TEXT', 'raw_diagnostics': 'TEXT', 'batch_index': 'INTEGER NOT NULL DEFAULT 0',
              'batch_calls': 'INTEGER NOT NULL DEFAULT 0', 'batch_started': 'REAL',
              'plan_units': 'INTEGER NOT NULL DEFAULT 0', 'call_limit': 'INTEGER NOT NULL DEFAULT 0'}
    existing = {r[1] for r in c.execute('PRAGMA table_info(jobs)')}
    for name, definition in fields.items():
        if name not in existing:
            c.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS review_units (
      job TEXT NOT NULL, id TEXT NOT NULL, position INTEGER NOT NULL, input TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'pending', analysis TEXT, checks TEXT, value TEXT,
      error TEXT NOT NULL DEFAULT '', PRIMARY KEY(job,id), UNIQUE(job,position));
    CREATE TABLE IF NOT EXISTS setup_jobs (
      id TEXT PRIMARY KEY, status TEXT NOT NULL, value TEXT NOT NULL,
      detail TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL, updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS setup_resources (id TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS setup_probes (token TEXT PRIMARY KEY, expires REAL NOT NULL, seen INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS setup_hook_receipts (repository INTEGER PRIMARY KEY, received REAL NOT NULL);
    ''')


def prepare(store, job, items, calls_per_unit=2):
    rows = [(job['id'], digest([job['id'], i, item]), i, encode(item)) for i, item in enumerate(items)]
    with store.tx() as c:
        store._active(c, job['id'], job['owner'])
        previous = c.execute('SELECT id FROM review_units WHERE job=? ORDER BY position', (job['id'],)).fetchall()
        if previous and [r['id'] for r in previous] != [r[1] for r in rows]:
            raise ReviewError('固定版本的分析计划发生变化，停止恢复旧任务')
        c.executemany('INSERT OR IGNORE INTO review_units(job,id,position,input) VALUES (?,?,?,?)', rows)
        completed = c.execute("SELECT count(*) FROM review_units WHERE job=? AND state='done'", (job['id'],)).fetchone()[0]
        c.execute('UPDATE jobs SET plan_units=?,units_total=?,units_completed=?,call_limit=? WHERE id=?',
                  (len(rows), len(rows), completed, (len(rows)*calls_per_unit if calls_per_unit else -1), job['id']))
    return units(store, job['id'])


def units(store, job_id, page=None):
    with contextlib.closing(store.connect()) as c:
        sql = 'SELECT * FROM review_units WHERE job=? ORDER BY position'
        args = [job_id]
        if page is not None:
            sql += ' LIMIT 20 OFFSET ?'; args.append((page-1)*20)
        rows = [dict(r) for r in c.execute(sql, args)]
    for row in rows:
        for key in ('input', 'analysis', 'checks', 'value'):
            row[key] = json.loads(row[key]) if row[key] is not None else None
    return rows


class UnitSequence:
    """短事务分页读取计划，模型等待期间不持有 SQLite 读事务。"""
    def __init__(self,store,job):self.store,self.job=store,job
    def __len__(self):
        with contextlib.closing(self.store.connect()) as c:
            return c.execute('SELECT count(*) FROM review_units WHERE job=?',(self.job,)).fetchone()[0]
    def __iter__(self):
        page=1
        while True:
            rows=units(self.store,self.job,page=page)
            if not rows:return
            yield from rows
            page+=1


def append_plan(store,job,items,position):
    with store.tx() as c:
        store._active(c,job['id'],job['owner'])
        c.executemany('INSERT INTO review_units(job,id,position,input) VALUES (?,?,?,?)',
                      [(job['id'],digest([job['id'],position+i,item]),position+i,encode(item)) for i,item in enumerate(items)])
        c.execute('UPDATE jobs SET plan_units=?,units_total=?,call_limit=-1,batch_started=?,stage=? WHERE id=?',
                  (position+len(items),position+len(items),time.time(),f'已组织 {position+len(items)} 个分析单元',job['id']))
    return position+len(items)


def checkpoint(store, job, unit_id, state, **fields):
    if set(fields) - {'analysis', 'checks', 'value', 'error'}:
        raise ValueError('不支持的单元结果字段')
    values = [encode(v) if k != 'error' else v for k, v in fields.items()]
    with store.tx() as c:
        store._active(c, job['id'], job['owner'])
        c.execute('UPDATE review_units SET state=?' + ''.join(','+k+'=?' for k in fields) +
                  ' WHERE job=? AND id=?', [state, *values, job['id'], unit_id])
        completed = c.execute("SELECT count(*) FROM review_units WHERE job=? AND state='done'", (job['id'],)).fetchone()[0]
        c.execute('UPDATE jobs SET units_completed=?,updated=? WHERE id=?', (completed, time.time(), job['id']))


def begin_phase(store, job, config, unit_id, phase, turn=None, payload=None):
    now = time.time()
    with store.tx() as c:
        store._active(c, job['id'], job['owner'])
        row = c.execute('SELECT batch_index,batch_calls,batch_started,plan_units,call_limit FROM jobs WHERE id=?', (job['id'],)).fetchone()
        if turn is None:
            consumed = c.execute("SELECT sum(analysis IS NOT NULL)+sum(checks IS NOT NULL) FROM review_units WHERE job=?", (job['id'],)).fetchone()[0] or 0
        else:
            consumed = c.execute('SELECT count(*) FROM review_turns WHERE job=?', (job['id'],)).fetchone()[0]
            per_unit = c.execute('SELECT count(*) FROM review_turns WHERE job=? AND unit=?', (job['id'],unit_id)).fetchone()[0]
        if row['call_limit'] > 0 and consumed >= row['call_limit']:
            raise ReviewError('超过固定计划的模型调用上限')
        # 预留完整一次调用时间；新批次只由仍未处理的阶段驱动，不能空转。
        duration = call_reservation(config)
        fresh = (not row['batch_started'] or row['batch_calls'] >= config['max_calls'] or
                 now-row['batch_started']+duration > config['task_timeout_seconds'])
        batch = row['batch_index']+1 if fresh else row['batch_index']
        c.execute('UPDATE jobs SET batch_index=?,batch_calls=?,batch_started=? WHERE id=?',
                  (batch, 1 if fresh else row['batch_calls']+1, now if fresh else row['batch_started'], job['id']))
        c.execute('UPDATE review_units SET state=? WHERE job=? AND id=?', (phase, job['id'], unit_id))
        if turn is not None:
            c.execute('INSERT INTO review_turns(job,unit,seq,phase,state,input,created) VALUES (?,?,?,?,?,?,?)',
                      (job['id'],unit_id,turn,phase,'started',encode(payload),now))
    job.update(_unit_id=unit_id, _phase=phase)
    return batch


def aggregate(store, job, evidence=True):
    base = dict(job.get('result') or {})
    coverage = dict(base.get('coverage') or {})
    gaps = list(coverage.get('skipped', []))
    findings, rejected, inputs = [], [], []
    if evidence:
        rows = units(store, job['id'])
    else:
        with contextlib.closing(store.connect()) as c:
            rows = [{'state': r['state'], 'value': json.loads(r['value']) if r['value'] else {}} for r in c.execute(
                "SELECT state,json_remove(value,'$.effective_input','$.context_reads') AS value FROM review_units WHERE job=? ORDER BY position", (job['id'],))]
    base['units_failed'] = sum(r['state']=='failed' for r in rows)
    base['units_processed'] = sum(r['state'] in ('done','failed') for r in rows)
    for row in rows:
        value = row['value'] or {}
        gaps.extend(value.get('skipped', []))
        findings.extend(value.get('findings', [])); rejected.extend(value.get('rejected', []))
        if evidence and row['analysis'] is not None:
            item = value.get('effective_input') or row['input']
            inputs.append(dict(unit_id=row['id'], path=item['path'], scope=item['scope'], analysis_version=4 if item.get('context_lookup') else 3,
                               input=item, context_gaps=item.get('context_gaps', []), analysis=row['analysis'],
                               context_reads=value.get('context_reads',[]), checks=row['checks'],
                               confirmed=value.get('findings', []), rejected=value.get('rejected', [])))
    with contextlib.closing(store.connect()) as c:
        retrieval=c.execute("SELECT count(*),COALESCE(sum(json_extract(value,'$.status')='error'),0) FROM review_reads WHERE job=?",(job['id'],)).fetchone()
        rounds=c.execute("SELECT count(*),COALESCE(sum(COALESCE(json_extract(response,'$.context_request_origin'),'model')='model'),0) FROM review_turns WHERE job=? AND json_array_length(response,'$.context_requests')>0",(job['id'],)).fetchone()
        unique_requests=c.execute("SELECT count(DISTINCT json_extract(value,'$.request_id')) FROM review_reads WHERE job=?",(job['id'],)).fetchone()[0]
    base['context_retrieval']={'rounds':rounds[0],'model_rounds':rounds[1],'runtime_rounds':rounds[0]-rounds[1],
                               'operations':retrieval[0],'unique_requests':unique_requests,'failed':retrieval[1]}
    seen = set(); unique = []
    for f in findings:
        key = f['fingerprint']
        if key not in seen:
            seen.add(key); unique.append(f)
    published = {f['fingerprint']: f for f in base.get('publication', {}).get('findings', [])}
    unique = [published.get(f['fingerprint'], f) for f in unique]
    if rows:
        remaining = sum(r['state'] != 'done' for r in rows)
        if remaining:
            gaps.append({'path':'尚未完成的分析', 'reason':f'还有 {remaining}/{len(rows)} 个分析单元未完成，不能视为完整审查'})
        coverage['skipped'] = list({encode(g):g for g in gaps}.values())
        coverage['complete'] = not coverage['skipped']
    base.update(schema_version=4, findings=unique, rejected=rejected, calls=job['model_calls'], coverage=coverage)
    if evidence: base['evidence'] = inputs
    else: base.pop('evidence', None)
    return base
