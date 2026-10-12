"""认证和组织授权数据；与现有 Manager 数据库共用事务。"""
from datetime import datetime
from models import db


class DirectorySource(db.Model):
    __tablename__ = 'access_directory_sources'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, default='公司目录')
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    config_json = db.Column(db.Text, nullable=False, default='{}')
    password_ciphertext = db.Column(db.Text, nullable=False, default='')
    version = db.Column(db.Integer, nullable=False, default=1)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class ManagerAccount(db.Model):
    __tablename__ = 'access_accounts'
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey('access_directory_sources.id'))
    subject = db.Column(db.String(200), nullable=False)
    username = db.Column(db.String(200), nullable=False)
    display_name = db.Column(db.String(200), nullable=False, default='')
    email = db.Column(db.String(300), nullable=False, default='')
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    is_platform_admin = db.Column(db.Boolean, nullable=False, default=False)
    password_hash = db.Column(db.Text, nullable=False, default='')
    auth_version = db.Column(db.Integer, nullable=False, default=1)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    directory_dn = db.Column(db.Text, nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('source_id', 'subject', name='uq_access_directory_subject'),)


class AccessSession(db.Model):
    __tablename__ = 'access_sessions'
    token_hash = db.Column(db.String(64), primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('access_accounts.id'), nullable=False, index=True)
    auth_version = db.Column(db.Integer, nullable=False)
    source_version = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.Float, nullable=False)
    expires_at = db.Column(db.Float, nullable=False, index=True)
    last_seen_at = db.Column(db.Float, nullable=False)
    directory_checked_at = db.Column(db.Float, nullable=False, default=0)
    revoked_at = db.Column(db.Float)


class OrganizationBinding(db.Model):
    __tablename__ = 'access_organization_bindings'
    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey('gitea_servers.id'), nullable=False, index=True)
    organization_id = db.Column(db.Integer, nullable=False)
    organization_name = db.Column(db.String(200), nullable=False)
    instance_epoch = db.Column(db.String(200), nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    verified_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('server_id', 'organization_id', 'instance_epoch', name='uq_access_organization_instance'),)


class OrganizationGrant(db.Model):
    __tablename__ = 'access_organization_grants'
    account_id = db.Column(db.Integer, db.ForeignKey('access_accounts.id'), primary_key=True)
    binding_id = db.Column(db.Integer, db.ForeignKey('access_organization_bindings.id'), primary_key=True)
    version = db.Column(db.Integer, nullable=False, default=1)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    granted_by = db.Column(db.Integer, nullable=False)
    granted_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class AccessAudit(db.Model):
    __tablename__ = 'access_audits'
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer)
    actor_name = db.Column(db.String(200), nullable=False)
    action = db.Column(db.String(100), nullable=False)
    target = db.Column(db.String(200), nullable=False, default='')
    detail_json = db.Column(db.Text, nullable=False, default='{}')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class LoginThrottle(db.Model):
    __tablename__ = 'access_login_throttle'
    key = db.Column(db.String(64), primary_key=True)
    window_at = db.Column(db.Float, nullable=False)
    attempts = db.Column(db.Integer, nullable=False)
