"""任务进度与事务内通知；只投影业务状态，不创建新的审查任务。"""
import json
import re
import time
from .performance import TARGET_SECONDS

RUNNING = ('running', 'publishing', 'recovering')
TERMINAL = ('succeeded', 'failed', 'superseded', 'publish_unknown')
LABELS = {'queued': '已排队', 'running': '正在审查', 'publishing': '正在发布',
          'recovering': '正在核对发布结果', 'succeeded': '审查完成', 'failed': '审查失败',
          'superseded': '版本已过期', 'publish_unknown': '发布结果待核对',
          'interrupted': '工作进程中断，等待恢复', 'stopped': '已停止审查',
          'outdated': '新提交等待审查'}


def migrate(c):
    columns = {
        'jobs': {'target_head': "TEXT NOT NULL DEFAULT ''", 'started_at': 'REAL', 'finished_at': 'REAL',
                 'progress_stage': "TEXT NOT NULL DEFAULT 'queued'", 'units_completed': 'INTEGER NOT NULL DEFAULT 0',
                 'units_total': 'INTEGER', 'review_fragment': "TEXT NOT NULL DEFAULT ''"},
        'pr_state': {'progress_seq': 'INTEGER NOT NULL DEFAULT 1', 'progress_synced': 'INTEGER NOT NULL DEFAULT 0',
                     'progress_sent_at': 'REAL NOT NULL DEFAULT 0', 'progress_next': 'REAL NOT NULL DEFAULT 0',
                     'progress_error': "TEXT NOT NULL DEFAULT ''", 'progress_failures': 'INTEGER NOT NULL DEFAULT 0',
                     'progress_creating': 'INTEGER NOT NULL DEFAULT 0'},
    }
    added = False
    for table, fields in columns.items():
        existing = {r[1] for r in c.execute('PRAGMA table_info(' + table + ')')}
        for name, definition in fields.items():
            if name not in existing:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
                added = True
    # 不回填无法确认的历史开始时间；快照仍可提供可信 SHA。
    if added:
        c.execute("UPDATE jobs SET target_head=COALESCE(json_extract(snapshot,'$.head'),''), finished_at=CASE WHEN status IN ('succeeded','failed','superseded','publish_unknown') THEN updated END")
    # 所有业务状态写入（包括恢复代码中的原始 SQL）自动进入同一事务的通知队列。
    # 不监听模型额度和普通租约更新，避免高频心跳导致刷屏。
    c.executescript('''
    CREATE TRIGGER IF NOT EXISTS progress_job_insert AFTER INSERT ON jobs BEGIN
      UPDATE pr_state SET progress_seq=progress_seq+1 WHERE repository=NEW.repository AND number=NEW.number;
    END;
    CREATE TRIGGER IF NOT EXISTS progress_job_update AFTER UPDATE OF
      status,stage,snapshot,result,review_id,error,progress_stage,units_completed,units_total,target_head,review_fragment ON jobs BEGIN
      UPDATE pr_state SET progress_seq=progress_seq+1 WHERE repository=NEW.repository AND number=NEW.number AND generation=NEW.generation;
    END;
    CREATE TRIGGER IF NOT EXISTS progress_generation AFTER UPDATE OF generation,signature ON pr_state
      WHEN OLD.generation!=NEW.generation OR OLD.signature!=NEW.signature BEGIN
      UPDATE pr_state SET progress_seq=progress_seq+1 WHERE repository=NEW.repository AND number=NEW.number;
    END;
    CREATE TRIGGER IF NOT EXISTS progress_timestamps AFTER UPDATE OF status ON jobs WHEN OLD.status!=NEW.status BEGIN
      UPDATE jobs SET
        started_at=CASE WHEN NEW.status='running' THEN COALESCE(started_at,NEW.updated) ELSE started_at END,
        finished_at=CASE WHEN NEW.status IN ('succeeded','failed','superseded','publish_unknown') THEN NEW.updated ELSE NULL END,
        progress_stage=CASE WHEN NEW.status='running' THEN 'snapshot' WHEN NEW.status='queued' THEN 'queued'
          WHEN NEW.status IN ('publishing','recovering') THEN 'publishing' ELSE NEW.status END
        WHERE id=NEW.id;
    END;
    ''')


def job_progress(job, now=None):
    now = time.time() if now is None else now
    status = job['status']
    if status in RUNNING and job['lease'] < now:
        status = 'publish_unknown' if status != 'running' else 'interrupted'
    snapshot = job.get('snapshot') or {}
    head = snapshot.get('head') or job.get('target_head', '')
    if not re.fullmatch(r'[0-9a-f]{40}', head):
        head = ''
    start, end = job.get('started_at'), job.get('finished_at')
    progress = {'status': status, 'label': LABELS.get(status, status), 'stage': job.get('progress_stage', 'queued'),
            'detail': job['stage'], 'head': head, 'received_at': job['created'], 'started_at': start,
            'finished_at': end, 'elapsed_seconds': max(0, int((end or now)-start)) if start else None,
            'units_completed': job.get('units_completed', 0), 'units_total': job.get('units_total'),
            'lease_until': job['lease'] if job['status'] in RUNNING else None}
    progress['target_seconds']=TARGET_SECONDS
    progress['target_exceeded']=bool(progress['elapsed_seconds'] is not None and progress['elapsed_seconds']>TARGET_SECONDS)
    progress['queue_seconds']=max(0,int((start or end or now)-job['created']))
    result = job.get('result') or {}
    progress['units_failed'] = result.get('units_failed',0)
    progress['units_processed'] = result.get('units_processed',job.get('units_completed',0))
    if job.get('session_id'):
        # 原生会话没有旧的“函数单元”概念，不用0/N伪造部分完成。
        coverage = result.get('coverage') or {}
        progress['native_session'] = True
        progress['units_completed'] = None
        progress['units_total'] = None
        progress['coverage'] = {key: coverage[key] for key in
                                ('files_total', 'files_reviewed', 'files_available', 'files_claimed',
                                 'files_observed', 'complete') if key in coverage}
    if status == 'succeeded' and result.get('coverage',{}).get('complete') is False:
        progress['label'] = '部分完成'
        progress['detail'] = '已发布可用意见，未完成范围请查看覆盖详情'
    diagnostic = job.get('diagnostic') or {}
    if diagnostic.get('request_started_at') is not None and diagnostic.get('timeout_seconds') is not None:
        waiting = diagnostic.get('status') == 'waiting' and status in RUNNING
        elapsed = (now-diagnostic['request_started_at']) if waiting else diagnostic.get('elapsed_seconds')
        if elapsed is None:
            elapsed = (end or job.get('updated', now))-diagnostic['request_started_at']
        progress['model_request'] = {'status':'waiting' if waiting else diagnostic.get('status'),
                                     'elapsed_seconds':max(0, int(elapsed)),
                                     'timeout_seconds':diagnostic['timeout_seconds']}
    if status == 'queued' and (job.get('attention') or {}).get('required'):
        progress.update(label='等待人工处理',stage='attention',detail=job['attention']['reason'])
    return progress


def closed_state(state):
    try:
        return bool(json.loads(state['signature'])[3])
    except (ValueError, IndexError, TypeError):
        return False
