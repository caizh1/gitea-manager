"""持久化审查占位；执行器停止确认后才释放容量与同 PR 互斥。"""
import contextlib
import logging
import json
import time

from .common import ReviewError


def migrate(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS review_execution_slots (
        job TEXT PRIMARY KEY, repository INTEGER NOT NULL, number INTEGER NOT NULL,
        owner TEXT NOT NULL, acquired REAL NOT NULL)''')
    connection.execute('CREATE INDEX IF NOT EXISTS review_slots_pr ON review_execution_slots(repository,number)')
    connection.execute('''INSERT OR IGNORE INTO review_execution_slots
        SELECT id,repository,number,owner,? FROM jobs
        WHERE status IN ('running','publishing','recovering','cancelling')''', (time.time(),))


def reserve(store, connection, job_id, owner):
    """调用方必须持有 BEGIN IMMEDIATE 事务。"""
    config = store.config(connection=connection)
    job = connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not job or not config['enabled']:
        return False
    held = connection.execute('SELECT owner FROM review_execution_slots WHERE job=?', (job_id,)).fetchone()
    if held is not None:
        if held['owner']:
            return False
        connection.execute("UPDATE review_execution_slots SET owner=? WHERE job=? AND owner=''", (owner, job_id))
        return True
    slots = connection.execute('SELECT s.*,h.value FROM review_execution_slots s JOIN jobs j ON j.id=s.job LEFT JOIN configuration_history h ON h.revision=j.config_revision').fetchall()
    limit = config['max_concurrent_reviews'] if config['engine'] == 'codex_cli' else 1
    if len(slots) >= limit or any(json.loads(row['value'] or '{}').get('engine') != 'codex_cli' for row in slots):
        return False
    if any(row['repository'] == job['repository'] and row['number'] == job['number'] for row in slots):
        return False
    saved = store.historical_config(job['config_revision'], connection)
    if saved.get('engine') != 'codex_cli' and slots:
        return False
    connection.execute('INSERT INTO review_execution_slots VALUES (?,?,?,?,?)',
                       (job_id, job['repository'], job['number'], owner, time.time()))
    return True


def status(store):
    with contextlib.closing(store.connect()) as connection:
        rows = connection.execute('''SELECT j.status,j.lease FROM review_execution_slots s
                                    JOIN jobs j ON j.id=s.job''').fetchall()
        queued = connection.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]
        config = store.config(connection=connection)
    running = sum(row['status'] in ('running', 'publishing', 'recovering') and row['lease'] >= time.time() for row in rows)
    return {'limit': config['max_concurrent_reviews'] if config['engine'] == 'codex_cli' else 1,
            'occupied': len(rows), 'running': running, 'recovering': len(rows) - running, 'queued': queued}


def reap(store):
    """独立周期回收，不在数据库事务内等待网络或进程锁。"""
    with contextlib.closing(store.connect()) as connection:
        rows = connection.execute('''SELECT j.*,s.owner AS slot_owner FROM review_execution_slots s
                                    JOIN jobs j ON j.id=s.job''').fetchall()
    for row in rows:
        job = dict(row)
        if job['status'] == 'queued' and not job['slot_owner']:
            continue
        if job['status'] in ('running', 'publishing', 'recovering') and job['lease'] >= time.time():
            continue
        try:
            with store.lock('execution-' + job['id']):
                # 领取者可能刚续租，锁内重读，按占位 owner 条件释放。
                current = store.job(job['id'], evidence=False)
                if current['status'] == 'queued':
                    # 人工继续可能先于回收到达，沿用原名额等待新 owner。
                    with store.tx() as connection:
                        connection.execute("UPDATE review_execution_slots SET owner='' WHERE job=? AND owner=?", (job['id'], job['slot_owner']))
                    continue
                if current['status'] in ('running', 'publishing', 'recovering') and current['lease'] >= time.time():
                    continue
                if current['status'] == 'running' and current['attempts'] < 3:
                    # 原 Worker 已退出，保留同一会话与目录，新的 owner 继续核对原回执。
                    with store.tx() as connection:
                        connection.execute("UPDATE jobs SET status='queued',owner='',lease=0,available=? WHERE id=? AND owner=? AND lease<?", (time.time(), job['id'], job['slot_owner'], time.time()))
                        connection.execute("UPDATE review_execution_slots SET owner='' WHERE job=? AND owner=?", (job['id'], job['slot_owner']))
                    continue
                if current.get('session_id'):
                    from .native_review import cancel_session
                    receipt = cancel_session(current['id'], current['session_id'])
                    if receipt.get('execution_stopped') is not True:
                        continue
                from .git_workspace import release_task
                release_task(store, current)
                with store.tx() as connection:
                    connection.execute('DELETE FROM review_execution_slots WHERE job=? AND owner=?',
                                       (job['id'], job['slot_owner']))
        except ReviewError:
            continue
        except Exception:
            logging.exception('审查占位回收失败，保留原占位')
