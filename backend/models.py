from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class ApprovalPolicy(db.Model):
    __tablename__ = 'approval_policies'
    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey('gitea_servers.id'), nullable=False)
    repo_id = db.Column(db.Integer, nullable=False)
    repo_name = db.Column(db.String(300), nullable=False)
    owner_id = db.Column(db.Integer, nullable=False)
    rule_json = db.Column(db.Text, nullable=False)
    revision = db.Column(db.Integer, nullable=False, default=1)
    mode = db.Column(db.String(30), nullable=False, default='draft')
    preview_json = db.Column(db.Text, nullable=False, default='{}')
    grandfather_json = db.Column(db.Text, nullable=False, default='[]')
    hook_token_hash = db.Column(db.String(64), nullable=False, default='')
    native_uninstall_token_hash = db.Column(db.String(64), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('server_id', 'repo_id', name='uq_approval_repository'), {'sqlite_autoincrement': True})


class ApprovalAudit(db.Model):
    __tablename__ = 'approval_audits'
    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, nullable=False, index=True)
    actor = db.Column(db.String(100), nullable=False, default='共享管理员')
    action = db.Column(db.String(60), nullable=False)
    detail_json = db.Column(db.Text, nullable=False, default='{}')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class ApprovalNonce(db.Model):
    __tablename__ = 'approval_nonces'
    policy_id = db.Column(db.Integer, primary_key=True)
    nonce = db.Column(db.String(64), primary_key=True)
    expires_at = db.Column(db.Float, nullable=False, index=True)


class ApprovalViewCache(db.Model):
    """仅保存短期展示快照；不能用于 Hook 授权，也不保存浏览器会话。"""
    __tablename__ = 'approval_view_cache'
    policy_id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.Integer, primary_key=True)
    context_hash = db.Column(db.String(64), nullable=False)
    payload = db.Column(db.Text, nullable=False, default='{}')
    expires_at = db.Column(db.Float, nullable=False, default=0, index=True)
    lease = db.Column(db.String(64), nullable=False, default='')
    lease_until = db.Column(db.Float, nullable=False, default=0)


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
    connection_mode = db.Column(db.String(20), nullable=False, default='ssh')
    instance_identity = db.Column(db.String(200), nullable=False, default='')
    access_epoch = db.Column(db.String(64), nullable=False, default=lambda: __import__('uuid').uuid4().hex)
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
    diagnostic_id = db.Column(db.String(100), nullable=False, default='')
    source_api_token = db.Column(db.String(200), default='')
    source_server_name = db.Column(db.String(100), default='')
    commit_snapshot_status = db.Column(db.String(20), default='')
    commit_snapshot_repo_count = db.Column(db.Integer, default=0)
    commit_snapshot_error = db.Column(db.Text, default='')
    commit_snapshot_collected_at = db.Column(db.DateTime, default=None)
    progress_stage = db.Column(db.String(50), default='')
    progress_label = db.Column(db.String(100), default='')
    progress_percent = db.Column(db.Integer, default=0)
    progress_detail = db.Column(db.Text, default='')
    progress_updated_at = db.Column(db.DateTime, default=None)
    progress_current = db.Column(db.BigInteger, default=None)
    progress_total = db.Column(db.BigInteger, default=None)
    progress_unit = db.Column(db.String(20), default='')
    progress_item = db.Column(db.String(1000), default='')
    progress_step_started_at = db.Column(db.DateTime, default=None)
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
    diagnostic_id = db.Column(db.String(100), nullable=False, default='')
    progress_stage = db.Column(db.String(50), default='')
    progress_label = db.Column(db.String(100), default='')
    progress_percent = db.Column(db.Integer, default=0)
    progress_detail = db.Column(db.Text, default='')
    progress_updated_at = db.Column(db.DateTime, default=None)
    progress_current = db.Column(db.BigInteger, default=None)
    progress_total = db.Column(db.BigInteger, default=None)
    progress_unit = db.Column(db.String(20), default='')
    progress_item = db.Column(db.String(1000), default='')
    progress_step_started_at = db.Column(db.DateTime, default=None)
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, default=None)

    backup = db.relationship('Backup', foreign_keys=[backup_id], lazy=True)
    target_server = db.relationship('GiteaServer', foreign_keys=[target_server_id], lazy=True)


class RestoreStepLog(db.Model):
    __tablename__ = 'restore_step_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    restore_task_id = db.Column(db.Integer, db.ForeignKey('restore_tasks.id'), nullable=False)
    step_key = db.Column(db.String(50), nullable=False)
    label = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='running')
    detail = db.Column(db.Text, default='')
    metrics_json = db.Column(db.Text, default='{}')
    exit_code = db.Column(db.Integer, default=None)
    stdout_tail = db.Column(db.Text, default='')
    stderr_tail = db.Column(db.Text, default='')
    remote_job_dir = db.Column(db.String(1000), default='')
    elapsed_ms = db.Column(db.BigInteger, default=0)
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, default=None)

    restore_task = db.relationship('RestoreTask', foreign_keys=[restore_task_id], lazy=True)


class BackupStepLog(db.Model):
    __tablename__ = 'backup_step_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    backup_id = db.Column(db.Integer, db.ForeignKey('backups.id'), nullable=False)
    step_key = db.Column(db.String(50), nullable=False)
    label = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='running')
    detail = db.Column(db.Text, default='')
    metrics_json = db.Column(db.Text, default='{}')
    exit_code = db.Column(db.Integer, default=None)
    stdout_tail = db.Column(db.Text, default='')
    stderr_tail = db.Column(db.Text, default='')
    remote_job_dir = db.Column(db.String(1000), default='')
    elapsed_ms = db.Column(db.BigInteger, default=0)
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, default=None)

    backup = db.relationship('Backup', foreign_keys=[backup_id], lazy=True)


class PolicyTemplate(db.Model):
    __tablename__ = 'policy_templates'
    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, default='')
    rules_json = db.Column(db.Text, nullable=False)
    organization_id = db.Column(db.Integer)
    organization_name = db.Column(db.String(100), default='')
    identities_json = db.Column(db.Text, default='{}')
    source_json = db.Column(db.Text, default='{}')
    content_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class PolicyBatch(db.Model):
    __tablename__ = 'policy_batches'
    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, nullable=False, index=True)
    server_name = db.Column(db.String(100), nullable=False)
    connection_hash = db.Column(db.String(64), nullable=False)
    template_id = db.Column(db.Integer, nullable=False)
    template_name = db.Column(db.String(100), nullable=False)
    template_hash = db.Column(db.String(64), nullable=False)
    template_json = db.Column(db.Text, nullable=False)
    scope_json = db.Column(db.Text, default='{}')
    actor = db.Column(db.String(100), default='共享管理员', nullable=False)
    request_source = db.Column(db.String(200), default='')
    status = db.Column(db.String(30), default='preview_queued', nullable=False, index=True)
    total = db.Column(db.Integer, nullable=False)
    error = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    submitted_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)


class PolicyRepoResult(db.Model):
    __tablename__ = 'policy_repo_results'
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, nullable=False, index=True)
    repo_id = db.Column(db.Integer, nullable=False)
    repo_name = db.Column(db.String(300), default='')
    repo_identity_json = db.Column(db.Text, default='{}')
    access_json = db.Column(db.Text, default='{}')
    status = db.Column(db.String(30), default='pending', nullable=False)
    before_json = db.Column(db.Text, default='[]')
    desired_json = db.Column(db.Text, default='[]')
    actual_json = db.Column(db.Text, default=None)
    diff_json = db.Column(db.Text, default='[]')
    matches_json = db.Column(db.Text, default='[]')
    operations_json = db.Column(db.Text, default='[]')
    error = db.Column(db.Text, default='')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (db.UniqueConstraint('batch_id', 'repo_id', name='uq_policy_batch_repo'),)


class PolicyRequestSlot(db.Model):
    __tablename__ = 'policy_request_slots'
    server_id = db.Column(db.Integer, primary_key=True)
    next_at = db.Column(db.Float, default=0, nullable=False)


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
    sync_mode = db.Column(db.String(20), nullable=False, default='push_mirror')
    sync_interval = db.Column(db.Integer, default=30)
    sync_on_commit = db.Column(db.Boolean, default=True)
    enabled = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(20), default='pending')
    last_sync_at = db.Column(db.DateTime, default=None)
    last_sync_status = db.Column(db.String(20), default='')
    last_sync_log = db.Column(db.Text, default='')
    total_repos = db.Column(db.Integer, default=0)
    synced_repos = db.Column(db.Integer, default=0)
    failed_repos = db.Column(db.Integer, default=0)
    progress_stage = db.Column(db.String(50), default='')
    progress_label = db.Column(db.String(100), default='')
    progress_percent = db.Column(db.Integer, default=0)
    progress_detail = db.Column(db.Text, default='')
    progress_updated_at = db.Column(db.DateTime, default=None)
    current_repo_name = db.Column(db.String(300), default='')
    current_repo_index = db.Column(db.Integer, default=0)
    current_repo_total = db.Column(db.Integer, default=0)
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
    remote_name = db.Column(db.String(100), default='')
    status = db.Column(db.String(20), default='pending')
    sync_mode = db.Column(db.String(20), default='')
    last_sync_at = db.Column(db.DateTime, default=None)
    error_msg = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class MirrorAuditLog(db.Model):
    __tablename__ = 'mirror_audit_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    mirror_config_id = db.Column(db.Integer, db.ForeignKey('mirror_configs.id'), nullable=False)
    repo_name = db.Column(db.String(300), nullable=False, default='')
    action = db.Column(db.String(50), nullable=False)
    reason = db.Column(db.Text, default='')
    status = db.Column(db.String(20), nullable=False, default='success')
    detail = db.Column(db.Text, default='')
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
    repo_id = db.Column(db.Integer, nullable=False, default=0)
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


class IntegrationServiceToken(db.Model):
    __tablename__ = 'integration_service_tokens'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    rotated_at = db.Column(db.DateTime, default=None)
    last_used_at = db.Column(db.DateTime, default=None)


class IntegrationOutbox(db.Model):
    __tablename__ = 'integration_outbox'

    seq = db.Column(db.Integer, primary_key=True, autoincrement=True)
    event_type = db.Column(db.String(100), nullable=False, index=True)
    task_type = db.Column(db.String(30), nullable=False, index=True)
    task_id = db.Column(db.Integer, nullable=False, index=True)
    status = db.Column(db.String(30), nullable=False)
    payload_json = db.Column(db.Text, nullable=False, default='{}')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


class IntegrationAuditLog(db.Model):
    __tablename__ = 'integration_audit_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    actor = db.Column(db.String(100), nullable=False)
    action = db.Column(db.String(100), nullable=False, index=True)
    target = db.Column(db.String(200), nullable=False, default='')
    status = db.Column(db.String(20), nullable=False, default='success')
    detail = db.Column(db.Text, nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


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
