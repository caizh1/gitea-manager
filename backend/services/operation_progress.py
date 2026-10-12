"""备份和恢复共用的阶段计量；复用现有业务会话，仅在安全的提交边界调用。"""
import json
import logging
from datetime import datetime

from models import db, Backup, BackupStepLog, RestoreStepLog, IntegrationOutbox
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value
from services.diagnostics import redact, CURRENT


def progress_for_response(task):
    fields = ('stage', 'label', 'percent', 'detail', 'current', 'total', 'unit', 'item')
    result = {'progress_' + key: getattr(task, 'progress_' + key, None) for key in fields}
    for key in ('updated_at', 'step_started_at'):
        value = getattr(task, 'progress_' + key, None)
        result['progress_' + key] = value.isoformat() + 'Z' if value else None
    return result


def update_progress(task, stage, label, percent, detail='', *, current=None, total=None, unit='', item=''):
    if task is None:
        return
    now = datetime.utcnow()
    previous = task.progress_stage or ''
    changed = previous != stage
    terminal = stage in ('completed', 'failed') or ('failed' in stage and task.status == 'failed')
    if not changed and not terminal and current is not None and task.progress_updated_at:
        if (now - task.progress_updated_at).total_seconds() < 1 and (total is None or current < total):
            return
    # 先完成调用者原有的业务提交，再隔离非关键的进度写入故障。
    db.session.commit()
    original = task
    session = Session(db.engine)
    model = BackupStepLog if isinstance(task, Backup) else RestoreStepLog
    foreign_key = 'backup_id' if isinstance(task, Backup) else 'restore_task_id'
    detail, label, item = redact(detail or '')[:2000], redact(label)[:100], redact(item or '')[:1000]
    try:
        task = session.get(type(original), original.id)
        if task is None:
            return
        if changed:
            for step in session.query(model).filter_by(**{foreign_key: task.id, 'step_key': previous, 'status': 'running'}).all():
                step.status = 'failed' if 'failed' in stage or stage == 'rollback' or 'failed' in previous else 'success'
                if stage == 'failed': step.detail = detail or step.detail
                step.completed_at = now
                step.elapsed_ms = max(0, int((now - step.started_at).total_seconds() * 1000))
            if not terminal: task.progress_step_started_at = now
        if not terminal:
            step = session.query(model).filter_by(**{foreign_key: task.id, 'step_key': stage, 'status': 'running'}).first()
            if not step:
                step = model(**{foreign_key: task.id}, step_key=stage, label=label, started_at=now)
                session.add(step)
            step.label = label
            step.detail = detail
            step.metrics_json = json.dumps({'当前数量': current, '总数量': total, '单位': unit, '处理对象': item}, ensure_ascii=False)
            step.elapsed_ms = max(0, int((now - step.started_at).total_seconds() * 1000))
        else:
            for step in session.query(model).filter_by(**{foreign_key: task.id, 'status': 'running'}).all():
                step.status = 'success' if stage == 'completed' else 'failed'
                step.completed_at = now
                step.elapsed_ms = max(0, int((now - step.started_at).total_seconds() * 1000))
                if stage == 'failed':
                    step.detail = detail or step.detail
        task.progress_stage, task.progress_label = stage, label
        task.progress_percent = max(0, min(int(percent), 100))
        task.progress_detail, task.progress_updated_at = detail, now
        if not terminal:
            task.progress_current, task.progress_total = current, total
            task.progress_unit, task.progress_item = unit, item
        if changed or terminal:
            kind = 'backup' if isinstance(task, Backup) else 'restore'
            from services.integration_outbox_service import sanitize
            session.add(IntegrationOutbox(event_type=f'{kind}.{stage}', task_type=kind, task_id=task.id,
                        status=stage, payload_json=json.dumps(sanitize({'label': label, 'percent': percent, 'detail': detail}), ensure_ascii=False)))
        session.commit()
        for key in ('stage', 'label', 'percent', 'detail', 'current', 'total', 'unit', 'item', 'updated_at', 'step_started_at'):
            field = 'progress_' + key
            set_committed_value(original, field, getattr(task, field))
        report = CURRENT.get()
        if report:
            report.step = label
            if changed or terminal:
                report.event('进度', detail or label, 阶段=stage)
    except Exception:
        session.rollback()
        logging.warning('步骤进度暂时无法保存，继续原业务流程', exc_info=True)
    finally:
        session.close()


def update_backup_progress(task_or_id, stage, label, percent, detail='', **counts):
    task = Backup.query.get(task_or_id) if isinstance(task_or_id, int) else task_or_id
    update_progress(task, stage, label, percent, detail, **counts)


def finish_current_step(task, detail=''):
    """成功回退/清理是已完成的步骤，不能因整个任务失败而显示为步骤失败。"""
    model = BackupStepLog if isinstance(task, Backup) else RestoreStepLog
    key = 'backup_id' if isinstance(task, Backup) else 'restore_task_id'
    try:
        with Session(db.engine) as session:
            for step in session.query(model).filter_by(**{key: task.id, 'step_key': task.progress_stage, 'status': 'running'}).all():
                step.status = 'success'
                step.completed_at = datetime.utcnow()
                step.elapsed_ms = max(0, int((step.completed_at - step.started_at).total_seconds() * 1000))
                if detail: step.detail = redact(detail)[:2000]
            session.commit()
    except Exception:
        logging.warning('步骤结束记录暂时无法保存', exc_info=True)


class ProgressReader:
    """包装真实传输流；不改变流内容、长度和定位行为。"""
    def __init__(self, stream, callback):
        self.stream, self.callback = stream, callback
        position = stream.tell()
        stream.seek(0, 2)
        self.total = stream.tell()
        stream.seek(position)

    def read(self, size=-1):
        data = self.stream.read(size)
        self.callback(self.stream.tell(), self.total)
        return data

    def __getattr__(self, name):
        return getattr(self.stream, name)
