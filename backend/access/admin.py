"""仅平台管理员可执行的目录配置和组织授权。"""
import json
import time
from datetime import datetime
from functools import wraps
from urllib.parse import quote

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from werkzeug.security import check_password_hash, generate_password_hash

from models import db, GiteaServer, Setting
from access.errors import AccessError
from access.models import (DirectorySource, ManagerAccount, OrganizationBinding, OrganizationGrant)
from access.authentication import (audit, directory_config, issue_session, revoke_account_sessions,
                                   session_payload, upsert_directory_account)
from access.ldap_directory import DEFAULTS, authenticate, search_users, test_connection, validate_config
from access.secrets import encrypt

access_bp = Blueprint('access', __name__)


def platform_only(function):
    @wraps(function)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_platform_admin:
            raise AccessError('仅平台管理员可执行此操作', 403)
        return function(*args, **kwargs)
    return wrapped


@access_bp.errorhandler(AccessError)
def handle_error(error):
    db.session.rollback()
    if request.endpoint in ('access.test_directory_connection', 'access.test_directory_login') and hasattr(error, 'steps'):
        steps = error.steps
        # 目录凭据测试失败不等于当前管理会话失效，避免前端将平台管理员退出。
        return jsonify(ok=False, error=str(error), message=str(error), steps=steps), (422 if error.status == 401 else error.status)
    return jsonify(error=str(error)), error.status


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise AccessError('请求内容必须为对象')
    return value


def identifier(value):
    if isinstance(value, bool):
        raise AccessError('标识必须为正整数')
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise AccessError('标识必须为正整数') from None
    if result < 1:
        raise AccessError('标识必须为正整数')
    return result


def source_record(required=True):
    source = DirectorySource.query.order_by(DirectorySource.id).first()
    if required and not source:
        raise AccessError('请先保存目录设置', 409)
    return source


def safe_configuration(source):
    config = dict(DEFAULTS) | (json.loads(source.config_json) if source else {})
    config.pop('bind_password', None)
    config.update(id=source.id if source else None, name=source.name if source else DEFAULTS['name'],
                  enabled=source.enabled if source else False,
                  bind_password_set=bool(source and source.password_ciphertext), version=source.version if source else 0)
    return config


def candidate_configuration(data):
    source = source_record(required=False)
    existing = directory_config(source) if source else dict(DEFAULTS)
    submitted = data.get('configuration', {})
    return validate_config(submitted, existing=existing)


def account_json(account):
    return {'id': account.id, 'username': account.username, 'display_name': account.display_name,
            'email': account.email, 'source': 'ldap' if account.source_id else 'local',
            'enabled': account.enabled, 'is_platform_admin': account.is_platform_admin,
            'must_change_password': account.must_change_password,
            'grants': [row.binding_id for row in OrganizationGrant.query.filter_by(account_id=account.id, enabled=True)]}


def binding_json(binding):
    from access.scopes import server_epoch
    server = db.session.get(GiteaServer, binding.server_id)
    active = bool(server and binding.enabled and binding.instance_epoch == server_epoch(server))
    return {'binding_id': binding.id, 'server_id': binding.server_id,
            'server_name': server.name if server else '已删除实例', 'organization_id': binding.organization_id,
            'organization_name': binding.organization_name, 'active': active, 'enabled': active}


@access_bp.get('/directory')
@platform_only
def get_directory():
    return jsonify(safe_configuration(source_record(required=False)))


@access_bp.put('/directory')
@platform_only
def save_directory():
    source = source_record(required=False)
    submitted = body()
    if not request.is_secure:
        raise AccessError('保存目录认证设置必须使用 HTTPS', 403)
    if submitted.get('enabled') and ManagerAccount.query.filter_by(source_id=None, must_change_password=True).first():
        raise AccessError('请先修改本地应急管理员的默认密码', 409)
    config = validate_config(submitted, directory_config(source) if source else None)
    password = config.pop('bind_password')
    encrypted = encrypt(password)
    if source is None:
        source = DirectorySource()
        db.session.add(source)
    else:
        db.session.execute(db.update(DirectorySource).where(DirectorySource.id == source.id).values(
            version=DirectorySource.version + 1))
        db.session.refresh(source)
    source.name, source.enabled = config['name'], config['enabled']
    source.config_json = json.dumps(config, ensure_ascii=False)
    source.password_ciphertext = encrypted
    source.updated_at = datetime.utcnow()
    audit('更新目录认证配置', '公司目录', {'enabled': source.enabled, 'type': config['type']})
    db.session.commit()
    return jsonify(safe_configuration(source))


@access_bp.post('/directory/test-connection')
@platform_only
def test_directory_connection():
    if not request.is_secure:
        raise AccessError('目录配置测试必须使用 HTTPS', 403)
    try:
        steps = test_connection(candidate_configuration(body()))
    except AccessError as error:
        if not hasattr(error, 'steps'):
            error.steps = [{'stage': 'configuration', 'ok': False, 'message': str(error)}]
        raise
    return jsonify(ok=True, message='目录连接测试通过', steps=steps)


@access_bp.post('/directory/test-login')
@platform_only
def test_directory_login():
    if not request.is_secure:
        raise AccessError('目录用户认证测试必须使用 HTTPS', 403)
    try:
        data = body()
        identity = authenticate(candidate_configuration(data), data.get('username', ''), data.get('password', ''))
    except AccessError as error:
        if not hasattr(error, 'steps'):
            error.steps = [{'stage': 'configuration', 'ok': False, 'message': str(error)}]
        raise
    return jsonify(ok=True, message='目录用户认证测试通过', steps=identity.pop('stages'),
                   user={key: identity[key] for key in ('username', 'display_name', 'email', 'subject')})


@access_bp.get('/directory/users')
@platform_only
def directory_users():
    rows = search_users(directory_config(source_record()), request.args.get('q', ''))
    return jsonify(items=[{key: value for key, value in row.items() if key != 'dn'} for row in rows])


@access_bp.post('/directory/users')
@platform_only
def import_directory_user():
    username = body().get('username')
    if not isinstance(username, str) or not username:
        raise AccessError('请填写目录登录名')
    source = source_record()
    rows = search_users(directory_config(source), username)
    if len(rows) != 1:
        raise AccessError('没有找到满足登录条件的唯一目录账号', 404)
    account = upsert_directory_account(source, rows[0])
    db.session.flush()
    audit('建立目录待授权账号', account.id)
    db.session.commit()
    return jsonify(account_json(account))


@access_bp.get('/accounts')
@platform_only
def list_accounts():
    return jsonify(items=[account_json(row) for row in ManagerAccount.query.order_by(ManagerAccount.id)])


@access_bp.patch('/accounts/<int:account_id>')
@platform_only
def update_account(account_id):
    account = db.session.get(ManagerAccount, account_id)
    if not account:
        raise AccessError('账号不存在', 404)
    data = body()
    if not data or set(data) - {'enabled', 'is_platform_admin'} or any(not isinstance(value, bool) for value in data.values()):
        raise AccessError('仅允许修改启用状态和平台管理员角色')
    if account.source_id is None and (data.get('enabled') is False or data.get('is_platform_admin') is False):
        raise AccessError('不能停用或降级本地应急管理员', 409)
    db.session.execute(db.update(ManagerAccount).where(ManagerAccount.id == account.id).values(
        **data, auth_version=ManagerAccount.auth_version + 1))
    db.session.refresh(account)
    revoke_account_sessions(account.id)
    audit('更新账号权限', account.id, data)
    db.session.commit()
    return jsonify(account_json(account))


@access_bp.get('/organizations')
@platform_only
def list_organizations():
    query = OrganizationBinding.query
    if request.args.get('server_id'):
        query = query.filter_by(server_id=identifier(request.args['server_id']))
    return jsonify(items=[binding_json(row) for row in query.order_by(OrganizationBinding.server_id, OrganizationBinding.id)])


@access_bp.post('/organizations')
@platform_only
def refresh_organizations():
    from access.scopes import server_epoch
    from access.identity import organization_birth, verify_organization, register_organization
    from services.policy_gitea import PolicyGitea
    from services.policy_rules import PolicyError
    data = body()
    server = db.session.get(GiteaServer, identifier(data.get('server_id')))
    if not server:
        raise AccessError('实例不存在', 404)
    client = PolicyGitea(server)
    try:
        organizations = client.pages('/orgs')
        # 先逐项回读全部候选，避免请求半途失败产生半套授权目录。
        verified = []
        for organization in organizations:
            name = organization.get('name') or organization.get('username')
            if not isinstance(name, str) or not name:
                raise AccessError('Gitea 组织目录格式不正确', 409)
            actual = client.request('GET', '/orgs/' + quote(name, safe=''))
            if actual.get('id') != organization.get('id'):
                raise AccessError('Gitea 组织身份变化，请重新刷新', 409)
            verified.append({'id': identifier(actual.get('id')), 'name': actual.get('name') or actual.get('username') or name,
                             'created': organization_birth(client, name, actual.get('id'))})
    except PolicyError as error:
        raise AccessError(str(error), 502) from None
    epoch = server_epoch(server)
    existing = OrganizationBinding.query.filter_by(server_id=server.id).all()
    for actual in verified:
        for old in existing:
            if old.organization_id == actual['id'] and old.instance_epoch == epoch:
                verify_organization(old, actual['created'], require_record=False)
    seen, result = set(), []
    for actual in verified:
        binding = next((row for row in existing if row.organization_id == actual['id'] and row.instance_epoch == epoch), None)
        if binding is None:
            binding = OrganizationBinding(server_id=server.id, organization_id=actual['id'],
                organization_name=actual['name'], instance_epoch=epoch)
            db.session.add(binding)
        binding.organization_name = actual['name']
        binding.enabled = True
        binding.verified_at = datetime.utcnow()
        db.session.flush()
        register_organization(binding, actual['created'])
        seen.add(actual['id'])
        result.append(binding)
    for old in existing:
        if old.instance_epoch != epoch or old.organization_id not in seen:
            old.enabled = False
    audit('核验组织授权目录', server.id, {'count': len(result)})
    db.session.commit()
    from access.migration import migrate_binding
    for binding in result:
        migrate_binding(binding)
    db.session.commit()
    return jsonify(items=[binding_json(row) for row in result])


@access_bp.post('/grants')
@platform_only
def add_grant():
    from access.scopes import binding_for
    data = body()
    account = db.session.get(ManagerAccount, identifier(data.get('account_id')))
    if not account or not account.enabled:
        raise AccessError('账号不存在或已停用', 404)
    binding = binding_for(identifier(data.get('binding_id')), live=True)
    grant = db.session.get(OrganizationGrant, (account.id, binding.id))
    if grant is None:
        grant = OrganizationGrant(account_id=account.id, binding_id=binding.id, granted_by=current_user.id)
        db.session.add(grant)
    else:
        db.session.execute(db.update(OrganizationGrant).where(
            OrganizationGrant.account_id == account.id, OrganizationGrant.binding_id == binding.id).values(
                version=OrganizationGrant.version + 1, enabled=True, granted_by=current_user.id,
                granted_at=datetime.utcnow()))
    audit('授予组织管理权限', account.id, {'binding_id': binding.id})
    db.session.commit()
    return jsonify(ok=True, account=account_json(account))


@access_bp.delete('/grants/<int:account_id>/<int:binding_id>')
@platform_only
def remove_grant(account_id, binding_id):
    grant = db.session.get(OrganizationGrant, (account_id, binding_id))
    if grant:
        db.session.execute(db.update(OrganizationGrant).where(
            OrganizationGrant.account_id == account_id, OrganizationGrant.binding_id == binding_id).values(
                version=OrganizationGrant.version + 1, enabled=False))
    audit('撤销组织管理权限', account_id, {'binding_id': binding_id})
    db.session.commit()
    return jsonify(ok=True)


@access_bp.post('/sessions/revoke')
@platform_only
def revoke_sessions():
    account_id = identifier(body().get('account_id'))
    if not db.session.get(ManagerAccount, account_id):
        raise AccessError('账号不存在', 404)
    revoke_account_sessions(account_id)
    audit('撤销账号全部会话', account_id)
    db.session.commit()
    return jsonify(ok=True)


@access_bp.post('/password')
@login_required
def change_password():
    account = db.session.get(ManagerAccount, current_user.id)
    if account.source_id is not None:
        raise AccessError('目录账号密码应在公司目录中修改', 403)
    data = body()
    password = data.get('new_password', '')
    old = data.get('current_password', '')
    if not isinstance(old, str) or not check_password_hash(account.password_hash, old):
        raise AccessError('当前密码不正确', 403)
    if not isinstance(password, str) or not 12 <= len(password) <= 4096 or password == old:
        raise AccessError('新密码至少十二位，并且不能与原密码相同')
    # 两个 worker 同时验证旧密码时只允许一份修改提交，避免后提交者覆盖已生效的新密码。
    password_hash = generate_password_hash(password)
    changed = db.session.execute(db.update(ManagerAccount).where(
        ManagerAccount.id == account.id,
        ManagerAccount.password_hash == account.password_hash,
        ManagerAccount.auth_version == account.auth_version,
    ).values(password_hash=password_hash, must_change_password=False,
             auth_version=ManagerAccount.auth_version + 1),
        execution_options={'synchronize_session': False}).rowcount
    if changed != 1:
        raise AccessError('密码或账号状态已变化，请重新登录后再试', 409)
    setting = db.session.get(Setting, 'admin_password')
    setting.value = password_hash
    revoke_account_sessions(account.id)
    audit('修改本地应急管理员密码', account.id)
    db.session.commit()
    db.session.refresh(account)
    issue_session(account)
    return jsonify(session_payload() | {'ok': True})

# 默认拒绝的路由登记表；账号修改密码允许被首次改密门禁放行。
PLATFORM_ENDPOINTS = {
    'access.get_directory', 'access.save_directory', 'access.test_directory_connection',
    'access.test_directory_login', 'access.directory_users', 'access.import_directory_user',
    'access.list_accounts', 'access.update_account', 'access.list_organizations',
    'access.refresh_organizations', 'access.add_grant', 'access.remove_grant', 'access.revoke_sessions',
}
ACCOUNT_ENDPOINTS = {'access.change_password'}
