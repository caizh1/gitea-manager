"""独立来源和不可变发现记录，不修改 SVN 表与历史。"""
from datetime import datetime
from models import db


class GitMigrationSource(db.Model):
    __tablename__ = 'git_migration_sources'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    kind = db.Column(db.String(20), nullable=False)
    url = db.Column(db.Text, nullable=False)
    username = db.Column(db.String(200), nullable=False)
    auth_method = db.Column(db.String(20), nullable=False)
    secret_ciphertext = db.Column(db.Text, nullable=False)
    ca_pem = db.Column(db.Text, nullable=False, default='')
    version = db.Column(db.Integer, nullable=False, default=1)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('kind', 'url', name='uq_git_migration_source'),)
    __mapper_args__ = {'version_id_col': version}


class GitMigrationDiscovery(db.Model):
    __tablename__ = 'git_migration_discoveries'
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey('git_migration_sources.id'), nullable=False, index=True)
    source_version = db.Column(db.Integer, nullable=False)
    request_key = db.Column(db.String(80), nullable=False, unique=True)
    status = db.Column(db.String(30), nullable=False, default='queued', index=True)
    result_json = db.Column(db.Text, nullable=False, default='{}')
    error = db.Column(db.Text, nullable=False, default='')
    error_code = db.Column(db.String(50), nullable=False, default='')
    actor_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)


class GitMigrationWorkerHealth(db.Model):
    __tablename__ = 'git_migration_worker_health'
    id = db.Column(db.Integer, primary_key=True)
    heartbeat_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class GitMigrationInventory(db.Model):
    __tablename__ = 'git_migration_inventories'
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey('git_migration_sources.id'), nullable=False, index=True)
    source_version = db.Column(db.Integer, nullable=False)
    discovery_id = db.Column(db.Integer, db.ForeignKey('git_migration_discoveries.id'), nullable=False)
    project_ids_json = db.Column(db.Text, nullable=False)
    capture_code = db.Column(db.Boolean, nullable=False, default=False, server_default='0')
    request_key = db.Column(db.String(80), nullable=False, unique=True)
    status = db.Column(db.String(30), nullable=False, default='queued', index=True)
    result_json = db.Column(db.Text, nullable=False, default='{}')
    error = db.Column(db.Text, nullable=False, default='')
    error_code = db.Column(db.String(50), nullable=False, default='')
    actor_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)


class GitMigrationRun(db.Model):
    __tablename__ = 'git_migration_runs'
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey('git_migration_sources.id'), nullable=False, index=True)
    inventory_id = db.Column(db.Integer, db.ForeignKey('git_migration_inventories.id'), nullable=False)
    server_id = db.Column(db.Integer, db.ForeignKey('gitea_servers.id'), nullable=False, index=True)
    request_key = db.Column(db.String(80), nullable=False, unique=True)
    source_version = db.Column(db.Integer, nullable=False)
    actor_id = db.Column(db.Integer, nullable=False)
    plan_json = db.Column(db.Text, nullable=False)
    checkpoint_json = db.Column(db.Text, nullable=False, default='{}')
    status = db.Column(db.String(30), nullable=False, default='queued', index=True)
    stage = db.Column(db.String(80), nullable=False, default='等待执行')
    error = db.Column(db.Text, nullable=False, default='')
    error_code = db.Column(db.String(50), nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
