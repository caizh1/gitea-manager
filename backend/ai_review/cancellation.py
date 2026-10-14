"""单任务人工取消；数据库先撤权，独立工作线程核对实际停止。"""
import contextlib
import json
import time

from .common import ReviewError


def migrate(c):
    columns = {row[1] for row in c.execute('PRAGMA table_info(jobs)')}
    for name, definition in {
        'cancel_requested_at': 'REAL', 'cancel_requested_by': "TEXT NOT NULL DEFAULT ''",
        'cancel_confirmed_at': 'REAL', 'cancel_error': "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in columns:
            c.execute('ALTER TABLE jobs ADD COLUMN ' + name + ' ' + definition)
    # 旧触发器的 IF NOT EXISTS 不会更新终态集合。
    c.execute('DROP TRIGGER IF EXISTS progress_timestamps')
    from .progress_state import migrate as progress_migrate
    progress_migrate(c)


def request_cancel(store, job_id, actor):
    now = time.time()
    with store.tx() as c:
        job = c.execute('SELECT status,owner,session_id,started_at FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job:
            return 404
        if job['status'] == 'cancelled':
            return 200
        if job['status'] == 'cancelling':
            return 202
        if job['status'] not in ('queued', 'running'):
            return 409
        # 中断后重新排队的任务可能仍有原生会话，不能按尚未执行的任务直接结束。
        queued = job['status'] == 'queued' and not job['owner'] and not job['session_id'] and job['started_at'] is None
        c.execute("""UPDATE jobs SET status=?,stage=?,cancel_requested_at=?,cancel_requested_by=?,
            cancel_confirmed_at=?,cancel_error='',lease=0,updated=? WHERE id=?""",
            ('cancelled' if queued else 'cancelling', '已人工取消' if queued else '正在强制取消',
             now, actor, now if queued else None, now, job_id))
        c.execute('DELETE FROM capabilities WHERE job=?', (job_id,))
        c.execute('UPDATE review_native_bindings SET active=0 WHERE job=?', (job_id,))
    return 200 if queued else 202


def reconcile_cancel(store, job):
    """不将 404 或超时当作停止成功；执行锁证明准备线程也已退出。"""
    from .native_review import cancel_session
    from .native_workspace import cleanup
    receipt = None
    if job.get('session_id'):
        try:
            receipt = cancel_session(job['id'], job['session_id'])
        except (ReviewError, OSError):
            with store.tx() as c:
                c.execute("UPDATE jobs SET cancel_error='执行器停止状态尚未确认，恢复连接后继续核对' WHERE id=? AND status='cancelling'",
                          (job['id'],))
            return False
        if receipt.get('execution_stopped') is not True:
            return False
    try:
        with store.lock('execution-' + job['id']):
            # 取得锁后再次读状态；网络操作不能持有 SQLite 事务。
            with store.tx() as c:
                current = c.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()
                if not current or current['status'] != 'cancelling':
                    return False
                now = time.time()
                c.execute("""UPDATE jobs SET status='cancelled',stage='已人工取消',current_action='已人工取消',
                    cancel_confirmed_at=?,cancel_error='',lease=0,updated=? WHERE id=? AND status='cancelling'""",
                    (now, now, job['id']))
                if receipt:
                    from .raw_diagnostics import metadata
                    raw = metadata(receipt.get('raw_diagnostics'), job['id'], job['session_id'])
                    c.execute('UPDATE jobs SET session_status=?,final_event_seq=?,event_acked_seq=?,log_stream_closed=? WHERE id=?',
                              (receipt.get('status', 'aborted'), receipt.get('final_event_seq'),
                               receipt.get('acked_seq', 0), int(receipt.get('log_stream_closed') is True), job['id']))
                    if raw:
                        c.execute('UPDATE jobs SET raw_diagnostics=? WHERE id=?', (json.dumps(raw), job['id']))
            cleanup(job['id'])
            return True
    except ReviewError:
        # 原 owner 尚在准备/回收，下一轮继续核对，不能删除它正在使用的目录。
        return False


def run(store, stop):
    from services.upgrade_maintenance import active
    while not stop.is_set():
        try:
            if not active():
                with contextlib.closing(store.connect()) as c:
                    jobs = [dict(row) for row in c.execute("SELECT id,session_id FROM jobs WHERE status='cancelling'")]
                for job in jobs:
                    if stop.is_set() or active():
                        break
                    reconcile_cancel(store, job)
        except Exception:
            import logging
            logging.exception('单任务取消核对失败，保留取消中状态')
        stop.wait(1)
