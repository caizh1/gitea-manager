import logging
from datetime import datetime

from models import db, RestoreTask, ScheduledTask


_UNSET = object()


def _clamp_percent(percent):
    return max(0, min(int(percent or 0), 100))


def update_schedule_progress(
    task_or_id,
    stage,
    label,
    percent,
    detail='',
    current_backup_id=_UNSET,
    current_restore_task_id=_UNSET,
    current_restore_index=_UNSET,
    current_restore_total=_UNSET,
    reset_current=False,
):
    task = task_or_id
    if isinstance(task_or_id, int):
        task = ScheduledTask.query.get(task_or_id)
    if not task:
        return

    if reset_current:
        task.current_backup_id = None
        task.current_restore_task_id = None
        task.current_restore_index = 0
        task.current_restore_total = 0

    task.progress_stage = stage
    task.progress_label = label
    task.progress_percent = _clamp_percent(percent)
    task.progress_detail = detail or ''
    task.progress_updated_at = datetime.utcnow()

    if current_backup_id is not _UNSET:
        task.current_backup_id = current_backup_id
    if current_restore_task_id is not _UNSET:
        task.current_restore_task_id = current_restore_task_id
    if current_restore_index is not _UNSET:
        task.current_restore_index = int(current_restore_index or 0)
    if current_restore_total is not _UNSET:
        task.current_restore_total = int(current_restore_total or 0)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logging.warning('[schedule-progress] update failed: %s', e)


def schedule_progress_for_response(task):
    progress = {
        'progress_stage': task.progress_stage,
        'progress_label': task.progress_label,
        'progress_percent': task.progress_percent or 0,
        'progress_detail': task.progress_detail,
        'progress_updated_at': task.progress_updated_at.isoformat() if task.progress_updated_at else None,
        'current_backup_id': task.current_backup_id,
        'current_restore_task_id': task.current_restore_task_id,
        'current_restore_index': task.current_restore_index or 0,
        'current_restore_total': task.current_restore_total or 0,
    }

    if task.last_status != 'running' or not task.current_restore_task_id:
        return progress

    restore_task = RestoreTask.query.get(task.current_restore_task_id)
    if not restore_task:
        return progress

    total = max(task.current_restore_total or 1, 1)
    index = min(max(task.current_restore_index or 1, 1), total)
    restore_percent = _clamp_percent(restore_task.progress_percent)
    slot = 65 / total
    overall_percent = int(round(30 + slot * (index - 1) + slot * restore_percent / 100))

    target_name = restore_task.target_server_name or ''
    detail_parts = [p for p in [target_name, restore_task.progress_label, restore_task.progress_detail] if p]
    progress.update({
        'progress_stage': restore_task.progress_stage or task.progress_stage,
        'progress_label': f'正在恢复 {index}/{total}',
        'progress_percent': min(max(overall_percent, task.progress_percent or 0), 95),
        'progress_detail': ' - '.join(detail_parts) or task.progress_detail,
        'progress_updated_at': restore_task.progress_updated_at.isoformat()
        if restore_task.progress_updated_at else progress['progress_updated_at'],
    })
    return progress
