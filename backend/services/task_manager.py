import threading
import logging
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

                do_restore(task_id)
                task = RestoreTask.query.get(task_id)
                if task:
                    from services.alert_service import on_restore_completed
                    on_restore_completed(
                        task.target_server_id,
                        task.status == 'success',
                        task.error_msg or '',
                        task_id=task.id,
                    )

        thread = threading.Thread(target=target, daemon=True)
        thread.start()


task_manager = TaskManager()
