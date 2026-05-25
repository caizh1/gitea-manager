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
                    if backup.status == 'success':
                        try:
                            from services.commit_service import collect_backup_commits
                            collect_backup_commits(backup_id)
                        except Exception as e:
                            logging.warning('[TaskManager] Commit采集失败: %s', e)

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
                    if task.status == 'success':
                        v = RestoreVerification.query.filter_by(restore_task_id=task_id).first()
                        if v:
                            v.status = 'running'
                            db.session.commit()
                        try:
                            from services.commit_service import verify_restore
                            verify_restore(task_id)
                        except Exception as e:
                            logging.warning('[TaskManager] 恢复验证失败: %s', e)
                            v = RestoreVerification.query.filter_by(restore_task_id=task_id).first()
                            if v and v.status == 'running':
                                v.status = 'failed'
                                v.verified_at = __import__('datetime').datetime.utcnow()
                                db.session.commit()

        thread = threading.Thread(target=target, daemon=True)
        thread.start()


task_manager = TaskManager()
