from datetime import datetime
import logging

from models import db, RestoreTask


def update_restore_progress(task_or_id, stage, label, percent, detail='', **counts):
    from services.operation_progress import update_progress
    task = RestoreTask.query.get(task_or_id) if isinstance(task_or_id, int) else task_or_id
    update_progress(task, stage, label, percent, detail, **counts)
