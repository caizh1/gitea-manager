"""持久随机会话、LDAP 身份校验及跨进程登录限速。"""
import hashlib
import json
import re
import secrets
import time
from datetime import datetime
from flask import current_app, g, request, session
from flask_login import UserMixin, current_user, login_user, logout_user
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash

from models import db, Setting, GiteaServer
from access.errors import AccessError
from access.models import (ManagerAccount, DirectorySource, AccessSession, OrganizationBinding,
                           OrganizationGrant, AccessAudit, LoginThrottle)
from access import ldap_directory
from access.secrets import decrypt


class SessionUser(UserMixin):
    def __init__(self, account, token):
        self.id = account.id
        self.username = account.username
        self.display_name = account.display_name
        self.email = account.email
        self.source_id = account.source_id
        self.is_platform_admin = account.is_platform_admin
        self.must_change_password = account.must_change_password
        self.auth_version = account.auth_version
        self._token = token

    def get_id(self):
        return self._token


def initialize_access():
    """必须在现有数据库初始化锁内、原管理员密码建立后调用。"""
    db.create_all()
    local = ManagerAccount.query.filter_by(source_id=None, subject='local:admin').one_or_none()
    if local is None:
        setting = db.session.get(Setting, 'admin_password')
        if setting is None:
            raise RuntimeError('原管理员密码尚未初始化，拒绝开放权限系统')
        local = ManagerAccount(subject='local:admin', username='admin', display_name='平台管理员',
            is_platform_admin=True, password_hash=setting.value,
            must_change_password=check_password_hash(setting.value, 'admin123'))
        db.session.add(local)
        db.session.commit()


def directory_config(source):
    config = dict(ldap_directory.DEFAULTS) | json.loads(source.config_json)
    config.update(name=source.name, enabled=source.enabled, bind_password=decrypt(source.password_ciphertext))
    return config


def audit(action, target='', detail=None):
    actor = current_user if current_user.is_authenticated else None
    db.session.add(AccessAudit(actor_id=actor.id if actor else None,
        actor_name=actor.display_name if actor else '未登录用户', action=action,
        target=str(target), detail_json=json.dumps(detail or {}, ensure_ascii=False)))


def csrf_token():
    if not isinstance(session.get('access_csrf'), str) or len(session['access_csrf']) < 32:
        session['access_csrf'] = secrets.token_urlsafe(32)
    return session['access_csrf']


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def revoke_account_sessions(account_id):
    AccessSession.query.filter_by(account_id=account_id, revoked_at=None).update({'revoked_at': time.time()})


def _throttle(source, username):
    now = time.time()
    # 数据库原子累计，两个 Gunicorn worker 不能各自重置次数。
    for kind, value, limit in [('account', source + ':' + username.casefold(), 10),
                               ('address', request.remote_addr or '', 40)]:
        key = _hash(kind + ':' + value)
        count = db.session.execute(db.text('''INSERT INTO access_login_throttle (key,window_at,attempts)
            VALUES (:key,:now,1) ON CONFLICT(key) DO UPDATE SET
            attempts=CASE WHEN window_at < :before THEN 1 ELSE attempts+1 END,
            window_at=CASE WHEN window_at < :before THEN :now ELSE window_at END
            RETURNING attempts'''), {'key': key, 'now': now, 'before': now - 300}).scalar_one()
        db.session.commit()
        if count > limit:
            raise AccessError('登录尝试过于频繁，请五分钟后再试', 429)


def upsert_directory_account(source, identity):
    account = ManagerAccount.query.filter_by(source_id=source.id, subject=identity['subject']).one_or_none()
    if account is None:
        account = ManagerAccount(source_id=source.id, subject=identity['subject'], username=identity['username'])
        db.session.add(account)
    account.username = identity['username']
    account.display_name = identity['display_name']
    account.email = identity['email']
    account.directory_dn = identity['dn']
    return account


def issue_session(account, source=None):
    now, token = time.time(), secrets.token_urlsafe(32)
    previous = session.get('_user_id')
    if isinstance(previous, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', previous):
        old = db.session.get(AccessSession, _hash(previous))
        if old:
            old.revoked_at = now
    session.clear()
    record = AccessSession(token_hash=_hash(token), account_id=account.id, auth_version=account.auth_version,
        source_version=source.version if source else 0, created_at=now, last_seen_at=now,
        expires_at=now + 8 * 3600, directory_checked_at=now if source else 0)
    db.session.add(record)
    db.session.commit()
    user = SessionUser(account, token)
    login_user(user, remember=False, fresh=True)
    csrf_token()
    g.access_session = record
    return user


def authenticate_login(payload):
    source_name = payload.get('source', 'local')
    username = payload.get('username', 'admin' if source_name == 'local' else '')
    password = payload.get('password', '')
    if source_name not in {'local', 'ldap'} or not isinstance(username, str) or not isinstance(password, str):
        raise AccessError('登录参数不正确')
    if len(username) > 200 or len(password) > 4096:
        raise AccessError('登录参数长度超出范围')
    _throttle(source_name, username)
    source = None
    if source_name == 'local':
        account = ManagerAccount.query.filter_by(source_id=None, subject='local:admin').one_or_none()
        if not account or username != account.username or not check_password_hash(account.password_hash, password):
            raise AccessError('用户名或密码不正确', 401)
    else:
        if not request.is_secure:
            raise AccessError('目录登录必须使用 HTTPS', 403)
        source = DirectorySource.query.filter_by(enabled=True).first()
        if not source:
            raise AccessError('用户名或密码不正确', 401)
        try:
            identity = ldap_directory.authenticate(directory_config(source), username, password)
        except AccessError:
            raise AccessError('用户名或密码不正确，或目录服务暂不可用', 401) from None
        account = upsert_directory_account(source, identity)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            account = ManagerAccount.query.filter_by(source_id=source.id, subject=identity['subject']).one()
    if not account.enabled:
        raise AccessError('用户名或密码不正确', 401)
    return issue_session(account, source)


def load_session_user(token):
    # 会话查询也会写入活跃时间或刷新目录身份，维护期间必须在加载前阻断。
    from services.upgrade_maintenance import active
    if active():
        return None
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        return None
    record = db.session.get(AccessSession, _hash(token))
    now = time.time()
    if not record or record.revoked_at is not None or record.expires_at <= now or record.last_seen_at <= now - 1800:
        return None
    account = db.session.get(ManagerAccount, record.account_id)
    if not account or not account.enabled or account.auth_version != record.auth_version:
        return None
    if account.source_id:
        if not request.is_secure:
            return None
        source = db.session.get(DirectorySource, account.source_id)
        if not source or not source.enabled or source.version != record.source_version:
            return None
        if record.directory_checked_at <= now - 300:
            config = directory_config(source)
            if config['type'] == 'simple' and not (config['bind_dn'] and config['bind_password']):
                return None
            try:
                identity = ldap_directory.revalidate(config, account.username, account.subject)
            except AccessError:
                return None
            upsert_directory_account(source, identity)
            record.directory_checked_at = now
    # 不因普通读取提交其他业务事务；请求加载发生在业务执行前。
    record.last_seen_at = now
    db.session.commit()
    g.access_session = record
    return SessionUser(account, token)


def logout_session():
    token = session.get('_user_id')
    if isinstance(token, str):
        record = db.session.get(AccessSession, _hash(token))
        if record:
            record.revoked_at = time.time()
            db.session.commit()
    logout_user()
    session.clear()
    csrf_token()


def public_sources():
    return [{'id': source.id, 'source': 'ldap', 'type': 'ldap', 'name': source.name}
            for source in DirectorySource.query.filter_by(enabled=True).all()]


def visible_organizations(account):
    from access.scopes import server_epoch
    query = OrganizationBinding.query.filter_by(enabled=True)
    if not account.is_platform_admin:
        query = query.join(OrganizationGrant, OrganizationGrant.binding_id == OrganizationBinding.id).filter(
            OrganizationGrant.account_id == account.id, OrganizationGrant.enabled.is_(True))
    result = []
    for binding in query.all():
        server = db.session.get(GiteaServer, binding.server_id)
        if not server or binding.instance_epoch != server_epoch(server):
            continue
        result.append({'binding_id': binding.id, 'server_id': server.id, 'server_name': server.name,
            'organization_id': binding.organization_id, 'organization_name': binding.organization_name})
    return result


def session_payload():
    payload = {'authenticated': bool(current_user.is_authenticated), 'csrf_token': csrf_token(),
               'sources': public_sources(), 'user': None, 'organizations': [], 'capabilities': [], 'needs_config': False}
    if current_user.is_authenticated:
        payload['user'] = {key: getattr(current_user, key) for key in
            ('id', 'username', 'display_name', 'is_platform_admin', 'must_change_password')}
        payload['organizations'] = visible_organizations(current_user)
        if current_user.is_platform_admin:
            payload['capabilities'] = ['platform', 'access', 'servers', 'backups', 'restore', 'schedules',
                'mirrors', 'settings', 'repository_policies', 'commit_gates', 'ai_reviews', 'approval_rules', 'statistics', 'alerts', 'dashboard', 'management', 'svn_migrations']
            setting = db.session.get(Setting, 'host_ip')
            payload['needs_config'] = not bool(setting and setting.value)
        elif payload['organizations']:
            payload['capabilities'] = ['repository_policies', 'commit_gates', 'ai_reviews', 'approval_rules', 'statistics', 'alerts', 'dashboard']
    return payload
