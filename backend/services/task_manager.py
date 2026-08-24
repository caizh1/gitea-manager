import threading
import logging
from datetime import datetime
from models import db, Backup, RestoreTask, RestoreVerification
from services.gitea_service import do_backup, do_restore


class TaskManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def run_backup(self, backup_id):
        def target():
            from app import create_app
            app = create_app()
            with app.app_context():
                do_backup(backup_id)
                backup = Backup.query.get(backup_id)
                if backup:
                    from services.alert_service import on_backup_completed
                    on_backup_completed(
                        backup.source_server_id,
                        backup.status == 'success',
                        backup.error_msg or '',
                        backup_id=backup.id,
                    )

        thread = threading.Thread(target=target, daemon=True)
        thread.start()

    def run_restore(self, task_id):
        def target():
            from app import create_app
            app = create_app()
            with app.app_context():
                task = RestoreTask.query.get(task_id)
                if task:
                    v = RestoreVerification.query.filter_by(restore_task_id=task_id).first()
                    if not v:
                        v = RestoreVerification(
                            restore_task_id=task_id,
                            status='pending',
                            created_at=__import__('datetime').datetime.utcnow(),
                        )
                        db.session.add(v)
                        db.session.commit()

                try:
                    do_restore(task_id)
                except Exception as exc:
                    db.session.rollback()
                    message = str(exc).strip() or repr(exc)
                    error_text = f'{type(exc).__name__}: {message}'
                    task = RestoreTask.query.get(task_id)
                    if task:
                        task.status = 'failed'
                        task.error_msg = error_text[:2000]
                        task.progress_stage = 'failed'
                        task.progress_label = '恢复失败'
                        task.progress_percent = 100
                        task.progress_detail = error_text[:500]
                        task.progress_updated_at = datetime.utcnow()
                        task.completed_at = datetime.utcnow()
                        db.session.commit()
                    logging.exception('[恢复线程] 未捕获异常 - task_id=%s error=%s', task_id, error_text)
                task = RestoreTask.query.get(task_id)
                if task:
                    from services.alert_service import on_restore_completed
                    on_restore_completed(
                        task.target_server_id,
                        task.status == 'success',
                        task.error_msg or f'恢复任务 {task.id} 失败，但未记录具体错误',
                        task_id=task.id,
                    )

        thread = threading.Thread(target=target, daemon=True)
        thread.start()


task_manager = TaskManager()
