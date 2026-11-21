"""在已确认的 TPM 拒绝后恢复同一审查阶段。"""
import random
import time
from email.utils import parsedate_to_datetime

from .common import encode


WAITING_STAGE = '等待模型额度'
BACKOFF = (60, 120, 300, 600, 900)
TPM_CODES = {'modelaccounttpmratelimitexceeded', 'tpm_rate_limit_exceeded'}


class DeferredModelRetry(Exception):
    """任务已持久化，下一个领取者将继续原阶段。"""


def migrate(c):
    c.execute('''CREATE TABLE IF NOT EXISTS review_model_retries (
        job TEXT NOT NULL, phase TEXT NOT NULL, attempt INTEGER NOT NULL, session_id TEXT NOT NULL,
        call_id TEXT NOT NULL UNIQUE, next_at REAL NOT NULL, remaining REAL NOT NULL,
        resume_deadline REAL, diagnostic TEXT NOT NULL, PRIMARY KEY(job,phase,attempt))''')


def retry_after(value, now):
    if not value:
        return None
    try:
        return int(value) if value.isdecimal() else max(0, parsedate_to_datetime(value).timestamp() - now)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def defer(store, job, phase, session, deadline):
    import json
    from .budgets import FINISH_MARGIN
    from .pr_agent_encoding import PROTOCOL

    if job.get('pr_agent_protocol') != PROTOCOL:
        return False
    now = time.time()
    remaining = deadline - now
    if remaining <= FINISH_MARGIN:
        return False
    with store.tx() as c:
        store._active(c, job['id'], job['owner'])
        row = c.execute("SELECT diagnostic,status,session_id FROM jobs WHERE id=?", (job['id'],)).fetchone()
        diagnostic = json.loads(row['diagnostic'] or '{}')
        if (row['status'] != 'running' or row['session_id'] != session
                or diagnostic.get('session_id') != session or diagnostic.get('source') != 'model_upstream'
                or diagnostic.get('upstream_http_status') != 429 or diagnostic.get('reason') != 'upstream_http_error'
                or str(diagnostic.get('upstream_error_code', '')).casefold() not in TPM_CODES):
            return False
        call = c.execute("SELECT 1 FROM review_native_calls WHERE call_id=? AND job=? AND session_id=? AND status='failed'",
                         (diagnostic.get('call_id'), job['id'], session)).fetchone()
        if not call:
            return False
        attempt = c.execute("SELECT count(*)+1 FROM review_model_retries WHERE job=? AND phase=?",
                            (job['id'], phase)).fetchone()[0]
        delay = diagnostic.get('retry_after_seconds')
        if delay is None:
            delay = BACKOFF[min(attempt, len(BACKOFF)) - 1]
        next_at = now + max(1, delay) + random.uniform(0, 4)
        # 所有当前任务共用全局模型配置。靠近的到期任务分散领取。
        while c.execute('''SELECT 1 FROM jobs WHERE status='queued' AND stage=?
                            AND abs(available-?)<2 LIMIT 1''', (WAITING_STAGE, next_at)).fetchone():
            next_at += 2
        c.execute("INSERT INTO review_model_retries VALUES (?,?,?,?,?,?,?,NULL,?)",
                  (job['id'], phase, attempt, session, diagnostic['call_id'], next_at, remaining, encode(diagnostic)))
        c.execute("""UPDATE jobs SET status='queued',stage=?,available=?,updated=?,lease=0,attempts=0,error=''
                     WHERE id=? AND owner=?""", (WAITING_STAGE, next_at, now, job['id'], job['owner']))
        c.execute("DELETE FROM capabilities WHERE job=? AND owner=?", (job['id'], job['owner']))
    return True


def deadline(store, job, default):
    from contextlib import closing
    with closing(store.connect()) as c:
        row = c.execute("""SELECT resume_deadline FROM review_model_retries WHERE job=?
                          ORDER BY rowid DESC LIMIT 1""", (job,)).fetchone()
    return row[0] if row and row[0] is not None else default
