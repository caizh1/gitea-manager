import json
import logging
import time
from datetime import datetime

from sqlalchemy.exc import OperationalError

from models import db, RestoreStepLog


LOG_TAIL_LIMIT = 6000


def _run_with_retry(action, attempts=4):
    for index in range(attempts):
        try:
            result = action()
            db.session.commit()
            return result
        except OperationalError:
            db.session.rollback()
            if index + 1 >= attempts:
                raise
            time.sleep(0.1 * (2 ** index))


def _json_text(value):
    try:
        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        return json.dumps({'诊断信息': '指标无法序列化'}, ensure_ascii=False)


def _tail(value, limit=LOG_TAIL_LIMIT):
    text = str(value or '')
    return text[-limit:]


def start_restore_step(task_id, step_key, label, detail='', remote_job_dir=''):
    def create():
        step = RestoreStepLog(
            restore_task_id=task_id,
            step_key=step_key,
            label=label,
            status='running',
            detail=detail or '',
            metrics_json='{}',
            remote_job_dir=remote_job_dir or '',
            started_at=datetime.utcnow(),
        )
        db.session.add(step)
        return step

    return _run_with_retry(create)


def update_restore_step(step_or_id, detail=None, metrics=None, remote_job_dir=None):
    step_id = step_or_id.id if isinstance(step_or_id, RestoreStepLog) else step_or_id

    def apply_update():
        step = RestoreStepLog.query.get(step_id)
        if not step:
            return None
        if detail is not None:
            step.detail = detail
        if metrics is not None:
            step.metrics_json = _json_text(metrics)
        if remote_job_dir is not None:
            step.remote_job_dir = remote_job_dir
        elapsed = datetime.utcnow() - step.started_at
        step.elapsed_ms = max(0, int(elapsed.total_seconds() * 1000))
        return step

    try:
        return _run_with_retry(apply_update)
    except Exception:
        db.session.rollback()
        logging.warning('[恢复步骤] 更新失败 - step_id=%s', step_id, exc_info=True)
        return None


def finish_restore_step(
    step_or_id,
    status='success',
    detail='',
    exit_code=0,
    stdout_tail='',
    stderr_tail='',
    metrics=None,
):
    step_id = step_or_id.id if isinstance(step_or_id, RestoreStepLog) else step_or_id

    def apply_finish():
        step = RestoreStepLog.query.get(step_id)
        if not step:
            return None
        step.status = status
        step.detail = detail or step.detail or ''
        step.exit_code = exit_code
        step.stdout_tail = _tail(stdout_tail)
        step.stderr_tail = _tail(stderr_tail)
        if metrics is not None:
            step.metrics_json = _json_text(metrics)
        step.completed_at = datetime.utcnow()
        step.elapsed_ms = max(0, int((step.completed_at - step.started_at).total_seconds() * 1000))
        return step

    return _run_with_retry(apply_finish)


def restore_step_to_dict(step):
    try:
        metrics = json.loads(step.metrics_json or '{}')
    except Exception:
        metrics = {'诊断信息': '历史指标格式异常'}
    return {
        'id': step.id,
        'restore_task_id': step.restore_task_id,
        'step_key': step.step_key,
        'label': step.label,
        'status': step.status,
        'detail': step.detail,
        'metrics': metrics,
        'exit_code': step.exit_code,
        'stdout_tail': step.stdout_tail,
        'stderr_tail': step.stderr_tail,
        'remote_job_dir': step.remote_job_dir,
        'elapsed_ms': step.elapsed_ms or 0,
        'started_at': step.started_at.isoformat() if step.started_at else None,
        'completed_at': step.completed_at.isoformat() if step.completed_at else None,
    }
