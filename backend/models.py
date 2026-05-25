from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class GiteaServer(db.Model):
    __tablename__ = 'gitea_servers'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='backup')
    host = db.Column(db.String(300), nullable=False)
    ssh_port = db.Column(db.Integer, nullable=False, default=22)
    ssh_user = db.Column(db.String(100), nullable=False)
    gitea_container = db.Column(db.String(200), nullable=False, default='gitea')
    pg_container = db.Column(db.String(200), nullable=False, default='gitea-postgres')
    pg_dbname = db.Column(db.String(100), nullable=False, default='gitea')
    pg_user = db.Column(db.String(100), nullable=False, default='gitea')
    gitea_port = db.Column(db.Integer, nullable=False)
    gitea_url = db.Column(db.String(300), nullable=False)
    api_token = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='unknown')
    version = db.Column(db.String(50), default='')
    repo_count = db.Column(db.Integer, default=0)
    user_count = db.Column(db.Integer, default=0)
    is_local = db.Column(db.Boolean, default=False)
    disk_usage = db.Column(db.String(100), default='')
    last_check_at = db.Column(db.DateTime, default=None)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class Backup(db.Model):
    __tablename__ = 'backups'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    source_server_id = db.Column(db.Integer, db.ForeignKey('gitea_servers.id'), nullable=False)
    filename = db.Column(db.String(500), nullable=False)
    file_path = db.Column(db.String(1000), nullable=False)
    file_size = db.Column(db.BigInteger, default=0)
    status = db.Column(db.String(30), nullable=False, default='running')
    error_msg = db.Column(db.Text, default='')
    source_api_token = db.Column(db.String(200), default='')
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, default=None)

    source_server = db.relationship('GiteaServer', foreign_keys=[source_server_id], lazy=True)


class RestoreTask(db.Model):
    __tablename__ = 'restore_tasks'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    backup_id = db.Column(db.Integer, db.ForeignKey('backups.id'), nullable=False)
    target_server_id = db.Column(db.Integer, db.ForeignKey('gitea_servers.id'), nullable=False)
    status = db.Column(db.String(30), nullable=False, default='running')
    error_msg = db.Column(db.Text, default='')
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, default=None)

    backup = db.relationship('Backup', foreign_keys=[backup_id], lazy=True)
    target_server = db.relationship('GiteaServer', foreign_keys=[target_server_id], lazy=True)


class Setting(db.Model):
    __tablename__ = 'settings'

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.String(500), nullable=False)


class User(UserMixin):
    id = 1

    def check_password(self, password):
        setting = Setting.query.get('admin_password')
        if not setting:
            return False
        return check_password_hash(setting.value, password)

    @staticmethod
    def set_password(password):
        setting = Setting.query.get('admin_password')
        if setting:
            setting.value = generate_password_hash(password)
            db.session.commit()
        else:
            try:
                db.session.add(Setting(key='admin_password', value=generate_password_hash(password)))
                db.session.commit()
            except Exception:
                db.session.rollback()


class ScheduledTask(db.Model):
    __tablename__ = 'scheduled_tasks'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)
    enabled = db.Column(db.Boolean, default=True)
    source_server_id = db.Column(db.Integer, db.ForeignKey('gitea_servers.id'), nullable=False)
    target_ids = db.Column(db.Text, default='[]')
    schedule_hour = db.Column(db.Integer, nullable=False, default=2)
    schedule_minute = db.Column(db.Integer, nullable=False, default=0)
    last_run_at = db.Column(db.DateTime, default=None)
    last_status = db.Column(db.String(20), default='')
    last_log = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    source_server = db.relationship('GiteaServer', foreign_keys=[source_server_id], lazy=True)


class ScheduleLog(db.Model):
    __tablename__ = 'schedule_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    schedule_task_id = db.Column(db.Integer, db.ForeignKey('scheduled_tasks.id'), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='running')
    log = db.Column(db.Text, default='')
    backup_status = db.Column(db.String(20), default='')
    restore_results = db.Column(db.Text, default='[]')
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, default=None)


def get_setting(key, default=''):
    s = Setting.query.get(key)
    return s.value if s else default


def set_setting(key, value):
    s = Setting.query.get(key)
    if s:
        s.value = value
    else:
        db.session.add(Setting(key=key, value=value))
    db.session.commit()
