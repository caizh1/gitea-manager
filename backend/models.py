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
    source_server_name = db.Column(db.String(100), default='')
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, default=None)

    source_server = db.relationship('GiteaServer', foreign_keys=[source_server_id], lazy=True)


class RestoreTask(db.Model):
    __tablename__ = 'restore_tasks'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    backup_id = db.Column(db.Integer, db.ForeignKey('backups.id'), nullable=False)
    target_server_id = db.Column(db.Integer, db.ForeignKey('gitea_servers.id'), nullable=False)
    target_server_name = db.Column(db.String(100), default='')
    status = db.Column(db.String(30), nullable=False, default='running')
    error_msg = db.Column(db.Text, default='')
    progress_stage = db.Column(db.String(50), default='')
    progress_label = db.Column(db.String(100), default='')
    progress_percent = db.Column(db.Integer, default=0)
    progress_detail = db.Column(db.Text, default='')
    progress_updated_at = db.Column(db.DateTime, default=None)
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
    progress_stage = db.Column(db.String(50), default='')
    progress_label = db.Column(db.String(100), default='')
    progress_percent = db.Column(db.Integer, default=0)
    progress_detail = db.Column(db.Text, default='')
    progress_updated_at = db.Column(db.DateTime, default=None)
    current_backup_id = db.Column(db.Integer, default=None)
    current_restore_task_id = db.Column(db.Integer, default=None)
    current_restore_index = db.Column(db.Integer, default=0)
    current_restore_total = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    source_server = db.relationship('GiteaServer', foreign_keys=[source_server_id], lazy=True)


class RepoStatistics(db.Model):
    __tablename__ = 'repo_statistics'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    server_id = db.Column(db.Integer, db.ForeignKey('gitea_servers.id'), nullable=False)
    repo_name = db.Column(db.String(300), nullable=False)
    commit_count = db.Column(db.Integer, default=0)
    code_lines = db.Column(db.Integer, default=0)
    doc_lines = db.Column(db.Integer, default=0)
    other_lines = db.Column(db.Integer, default=0)
    code_files = db.Column(db.Integer, default=0)
    doc_files = db.Column(db.Integer, default=0)
    other_files = db.Column(db.Integer, default=0)
    language_breakdown = db.Column(db.Text, default='{}')
    last_commit_sha = db.Column(db.String(40), default='')
    last_commit_date = db.Column(db.DateTime, default=None)
    snapshot_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class CommitStatistics(db.Model):
    __tablename__ = 'commit_statistics'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    server_id = db.Column(db.Integer, db.ForeignKey('gitea_servers.id'), nullable=False)
    period_type = db.Column(db.String(20), nullable=False)
    period_key = db.Column(db.String(20), nullable=False)
    commit_count = db.Column(db.Integer, default=0)
    repo_count = db.Column(db.Integer, default=0)
    author_count = db.Column(db.Integer, default=0)
    top_authors = db.Column(db.Text, default='[]')
    code_lines_added = db.Column(db.Integer, default=0)
    code_lines_deleted = db.Column(db.Integer, default=0)
    doc_lines_added = db.Column(db.Integer, default=0)
    doc_lines_deleted = db.Column(db.Integer, default=0)
    snapshot_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class AuthorStatistics(db.Model):
    __tablename__ = 'author_statistics'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    server_id = db.Column(db.Integer, db.ForeignKey('gitea_servers.id'), nullable=False)
    author_name = db.Column(db.String(200), nullable=False)
    author_email = db.Column(db.String(300), default='')
    repo_name = db.Column(db.String(300), nullable=False)
    commit_count = db.Column(db.Integer, default=0)
    additions = db.Column(db.Integer, default=0)
    deletions = db.Column(db.Integer, default=0)
    first_commit_date = db.Column(db.DateTime, default=None)
    last_commit_date = db.Column(db.DateTime, default=None)
    snapshot_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('server_id', 'author_name', 'repo_name', name='uq_author_repo'),
    )


class MirrorConfig(db.Model):
    __tablename__ = 'mirror_configs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    source_server_id = db.Column(db.Integer, db.ForeignKey('gitea_servers.id'), nullable=False)
    target_server_id = db.Column(db.Integer, db.ForeignKey('gitea_servers.id'), nullable=False)
    sync_mode = db.Column(db.String(20), nullable=False, default='gitea_mirror')
    sync_interval = db.Column(db.Integer, default=30)
    enabled = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(20), default='pending')
    last_sync_at = db.Column(db.DateTime, default=None)
    last_sync_status = db.Column(db.String(20), default='')
    last_sync_log = db.Column(db.Text, default='')
    total_repos = db.Column(db.Integer, default=0)
    synced_repos = db.Column(db.Integer, default=0)
    failed_repos = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    source_server = db.relationship('GiteaServer', foreign_keys=[source_server_id], lazy=True)
    target_server = db.relationship('GiteaServer', foreign_keys=[target_server_id], lazy=True)


class MirrorRepoStatus(db.Model):
    __tablename__ = 'mirror_repo_status'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    mirror_config_id = db.Column(db.Integer, db.ForeignKey('mirror_configs.id'), nullable=False)
    repo_name = db.Column(db.String(300), nullable=False)
    source_repo_id = db.Column(db.Integer, default=0)
    target_repo_id = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='pending')
    sync_mode = db.Column(db.String(20), default='')
    last_sync_at = db.Column(db.DateTime, default=None)
    error_msg = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class RestoreVerification(db.Model):
    __tablename__ = 'restore_verifications'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    restore_task_id = db.Column(db.Integer, db.ForeignKey('restore_tasks.id'), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending')
    total_repos = db.Column(db.Integer, default=0)
    matched_repos = db.Column(db.Integer, default=0)
    mismatch_repos = db.Column(db.Integer, default=0)
    mismatch_details = db.Column(db.Text, default='[]')
    verified_at = db.Column(db.DateTime, default=None)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class BackupRepoCommit(db.Model):
    __tablename__ = 'backup_repo_commits'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    backup_id = db.Column(db.Integer, db.ForeignKey('backups.id'), nullable=False)
    repo_name = db.Column(db.String(300), nullable=False)
    commit_count = db.Column(db.Integer, default=0)
    latest_commit_sha = db.Column(db.String(40), default='')
    commit_ids_hash = db.Column(db.String(64), default='')
    commit_ids = db.Column(db.Text, default='')
    collected_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class CommitMessageRule(db.Model):
    __tablename__ = 'commit_message_rules'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    server_id = db.Column(db.Integer, db.ForeignKey('gitea_servers.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    pattern = db.Column(db.Text, nullable=False)
    reject_message = db.Column(db.Text, default='')
    enabled = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=None)

    server = db.relationship('GiteaServer', foreign_keys=[server_id], lazy=True)


class CommitGateAssignment(db.Model):
    __tablename__ = 'commit_gate_assignments'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    server_id = db.Column(db.Integer, db.ForeignKey('gitea_servers.id'), nullable=False)
    repo_name = db.Column(db.String(300), nullable=False)
    rule_id = db.Column(db.Integer, db.ForeignKey('commit_message_rules.id'), nullable=False)
    install_status = db.Column(db.String(20), default='pending')
    install_log = db.Column(db.Text, default='')
    applied_at = db.Column(db.DateTime, default=None)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    server = db.relationship('GiteaServer', foreign_keys=[server_id], lazy=True)
    rule = db.relationship('CommitMessageRule', foreign_keys=[rule_id], lazy=True)

    __table_args__ = (
        db.UniqueConstraint('server_id', 'repo_name', name='uq_commit_gate_repo'),
    )


class Alert(db.Model):
    __tablename__ = 'alerts'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    alert_type = db.Column(db.String(30), nullable=False)
    server_id = db.Column(db.Integer, db.ForeignKey('gitea_servers.id'), nullable=False)
    server_name = db.Column(db.String(100), nullable=False)
    message = db.Column(db.Text, default='')
    status = db.Column(db.String(20), nullable=False, default='active')
    source_id = db.Column(db.Integer, default=0)
    resolved_at = db.Column(db.DateTime, default=None)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    server = db.relationship('GiteaServer', foreign_keys=[server_id], lazy=True)


class ScheduleLog(db.Model):
    __tablename__ = 'schedule_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    schedule_task_id = db.Column(db.Integer, db.ForeignKey('scheduled_tasks.id'), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='running')
    log = db.Column(db.Text, default='')
    backup_status = db.Column(db.String(20), default='')
    backup_id = db.Column(db.Integer, default=None)
    backup_filename = db.Column(db.String(500), default='')
    backup_error = db.Column(db.Text, default='')
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
