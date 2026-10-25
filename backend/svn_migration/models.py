"""迁移计划、不可变执行快照及阶段记录，与 Manager 共用事务。"""
from datetime import datetime
from models import db


class SvnSource(db.Model):
    __tablename__ = 'svn_sources'
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), nullable=False, unique=True)
    name = db.Column(db.String(200), nullable=False)
    url = db.Column(db.Text, nullable=False)
    username = db.Column(db.String(200), nullable=False)
    password_ciphertext = db.Column(db.Text, nullable=False, default='')
    ca_pem = db.Column(db.Text, nullable=False, default='')
    version = db.Column(db.Integer, nullable=False, default=1)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    tested_at = db.Column(db.DateTime)
    test_json = db.Column(db.Text, nullable=False, default='{}')
    __mapper_args__ = {'version_id_col': version, 'version_id_generator': False}


class SvnRepository(db.Model):
    __tablename__ = 'svn_repositories'
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey('svn_sources.id'), nullable=False, index=True)
    url = db.Column(db.Text, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    layout_json = db.Column(db.Text, nullable=False, default='{}')
    version = db.Column(db.Integer, nullable=False, default=1)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    inventory_json = db.Column(db.Text, nullable=False, default='{}')
    __table_args__ = (db.UniqueConstraint('source_id', 'url', name='uq_svn_source_url'),)
    __mapper_args__ = {'version_id_col': version, 'version_id_generator': False}


class SvnTargetBinding(db.Model):
    __tablename__ = 'svn_target_bindings'
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, nullable=False, index=True)
    server_id = db.Column(db.Integer, nullable=False, index=True)
    group_id = db.Column(db.Integer, nullable=False)
    group_path = db.Column(db.String(200), nullable=False)
    actor_id = db.Column(db.Integer, nullable=False)
    __table_args__ = (db.UniqueConstraint('server_id', 'group_path', name='uq_svn_target_group'),)


class SvnLayoutDetection(db.Model):
    """只读目录检测，固定来源版本和修订；确认前不修改仓库布局。"""
    __tablename__ = 'svn_layout_detections'
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey('svn_sources.id'), nullable=False, index=True)
    source_version = db.Column(db.Integer, nullable=False)
    url = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), nullable=False, default='queued', index=True)
    result_json = db.Column(db.Text, nullable=False, default='{}')
    error = db.Column(db.Text, nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)


class SvnDiscovery(db.Model):
    """实例级只读发现快照；纳管只能选择本快照中的真实项目。"""
    __tablename__ = 'svn_discoveries'
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey('svn_sources.id'), nullable=False, index=True)
    source_version = db.Column(db.Integer, nullable=False)
    url = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), nullable=False, default='queued', index=True)
    result_json = db.Column(db.Text, nullable=False, default='{}')
    error = db.Column(db.Text, nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)


class SvnMigrationPlan(db.Model):
    __tablename__ = 'svn_migration_plans'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    server_id = db.Column(db.Integer, nullable=False)
    config_json = db.Column(db.Text, nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    status = db.Column(db.String(30), nullable=False, default='draft')
    preview_json = db.Column(db.Text, nullable=False, default='{}')
    preview_hash = db.Column(db.String(64), nullable=False, default='')
    error = db.Column(db.Text, nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __mapper_args__ = {'version_id_col': version, 'version_id_generator': False}


class SvnMigrationRun(db.Model):
    __tablename__ = 'svn_migration_runs'
    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, nullable=False)
    server_id = db.Column(db.Integer, nullable=False, index=True)
    request_key = db.Column(db.String(100), nullable=False, unique=True)
    snapshot_json = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), nullable=False, default='queued', index=True)
    actor_id = db.Column(db.Integer, nullable=False)
    actor_name = db.Column(db.String(200), nullable=False)
    control = db.Column(db.String(20), nullable=False, default='')
    error = db.Column(db.Text, nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class SvnMigrationItem(db.Model):
    __tablename__ = 'svn_migration_items'
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey('svn_migration_runs.id'), nullable=False, index=True)
    repository_id = db.Column(db.Integer, nullable=False, index=True)
    position = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(30), nullable=False, default='pending')
    stage = db.Column(db.String(50), nullable=False, default='pending')
    detail = db.Column(db.Text, nullable=False, default='')
    checkpoint_json = db.Column(db.Text, nullable=False, default='{}')
    result_json = db.Column(db.Text, nullable=False, default='{}')
    __table_args__ = (db.UniqueConstraint('run_id', 'repository_id', name='uq_svn_run_repository'),)


class SvnMigrationEvent(db.Model):
    __tablename__ = 'svn_migration_events'
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, nullable=False, index=True)
    item_id = db.Column(db.Integer)
    stage = db.Column(db.String(50), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class SvnReservation(db.Model):
    """排队和暂停任务也保留占用，避免另一批接管同一来源或目标。"""
    __tablename__ = 'svn_reservations'
    key = db.Column(db.String(600), primary_key=True)
    run_id = db.Column(db.Integer, nullable=False, index=True)


class SvnWorkerHealth(db.Model):
    __tablename__ = 'svn_worker_health'
    id = db.Column(db.Integer, primary_key=True)
    updated = db.Column(db.Float, nullable=False, default=0)


class SvnPreflight(db.Model):
    """每次检查独立保存；完成后不覆盖历史结论与输入。"""
    __tablename__ = 'svn_preflights'
    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, nullable=False, index=True)
    plan_name = db.Column(db.String(200), nullable=False)
    plan_version = db.Column(db.Integer, nullable=False)
    server_id = db.Column(db.Integer, nullable=False)
    config_json = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), nullable=False, default='queued')
    preview_json = db.Column(db.Text, nullable=False, default='{}')
    error = db.Column(db.Text, nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)


class SvnPreflightRepository(db.Model):
    """仓库与预检的索引，两个页面通过同一预检编号进入详情。"""
    __tablename__ = 'svn_preflight_repositories'
    preflight_id = db.Column(db.Integer, db.ForeignKey('svn_preflights.id'), primary_key=True)
    repository_id = db.Column(db.Integer, primary_key=True, index=True)
