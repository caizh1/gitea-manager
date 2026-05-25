from datetime import datetime
import logging

from models import db, RestoreTask


def update_restore_progress(task_or_id, stage, label, percent, detail=''):
    task = task_or_id
    if isinstance(task_or_id, int):
        task = RestoreTask.query.get(task_or_id)
    if not task:
        return

    task.progress_stage = stage
    task.progress_label = label
    task.progress_percent = max(0, min(int(percent), 100))
    task.progress_detail = detail or ''
    task.progress_updated_at = datetime.utcnow()
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logging.warning('[restore-progress] update failed: %s', e)
