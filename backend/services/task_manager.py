import threading
from models import db, Backup, RestoreTask
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

        thread = threading.Thread(target=target, daemon=True)
        thread.start()

    def run_restore(self, task_id):
        def target():
            from app import create_app
            app = create_app()
            with app.app_context():
                do_restore(task_id)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()


task_manager = TaskManager()
