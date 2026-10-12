"""组织归属与运行时授权；名称只展示，稳定 ID 与实例代次决定边界。"""
import hashlib
import json
import uuid
from datetime import datetime
from urllib.parse import quote

from flask import g, has_request_context, request
from flask_login import current_user

from models import db, GiteaServer
from access.errors import AccessError
from access.identity import organization_birth, verify_organization, verify_repository


class ResourceScope(db.Model):
    __tablename__ = 'access_resource_scopes'
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(40), nullable=False)
    resource_key = db.Column(db.String(160), nullable=False)
    binding_id = db.Column(db.Integer, nullable=False, index=True)
    server_id = db.Column(db.Integer, nullable=False)
    organization_id = db.Column(db.Integer, nullable=False)
    instance_epoch = db.Column(db.String(64), nullable=False)
    repo_ids_json = db.Column(db.Text, nullable=False, default='[]')
    actor_json = db.Column(db.Text, nullable=False, default='{}')
    archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('kind', 'resource_key', name='uq_access_resource'),)


def server_epoch(server):
    identity = [server.access_epoch, server.instance_identity, server.gitea_url.rstrip('/')]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def invalidate_server(server):
    """在整体恢复或连接身份变化前持久化失效，不能用原数字 ID 继承授权。"""
    from access.models import OrganizationBinding
    server.access_epoch = uuid.uuid4().hex
    OrganizationBinding.query.filter_by(server_id=server.id).update({'enabled': False})
    scopes = ResourceScope.query.filter_by(server_id=server.id).all()
    for row in scopes:
        row.archived = True
    db.session.commit()
    freeze_review_resources(scopes)


def freeze_review_resources(scopes):
    """先封闭 Manager 授权，再将独立 Review 库的旧服务与队列置为封存。"""
    keys = [int(row.resource_key) for row in scopes if row.kind == 'ai_repository']
    if not keys:
        return
    from flask import current_app
    from ai_review.store import Store
    state = Store(current_app.config.get('AI_REVIEW_DB'))
    for key in keys:
        state.archive_repo(key)


def is_platform():
    return bool(has_request_context() and current_user.is_authenticated and current_user.is_platform_admin)


def actor_name():
    if has_request_context() and current_user.is_authenticated:
        return f'{current_user.display_name or current_user.username}（账号 {current_user.id}）'
    return '系统任务'


def binding_for(binding_id, account=None, live=True):
    from access.models import ManagerAccount, OrganizationBinding, OrganizationGrant
    try:
        binding_id = int(binding_id)
    except (ValueError, TypeError):
        raise AccessError('组织不存在或无权访问', 404) from None
    binding = db.session.get(OrganizationBinding, binding_id, populate_existing=True)
    server = db.session.get(GiteaServer, binding.server_id, populate_existing=True) if binding else None
    if not binding or not binding.enabled or not server or binding.instance_epoch != server_epoch(server):
        raise AccessError('组织绑定已失效，请联系平台管理员重新核验', 409)
    if account is not None:
        account = db.session.get(ManagerAccount, account.id, populate_existing=True)
        if not account or not account.enabled:
            raise AccessError('账号已停用', 403)
        if not account.is_platform_admin and not OrganizationGrant.query.filter_by(account_id=account.id, binding_id=binding.id, enabled=True).first():
            raise AccessError('组织不存在或无权访问', 404)
    if live:
        from services.policy_gitea import PolicyGitea
        client = PolicyGitea(server)
        actual = client.request('GET', '/orgs/' + quote(binding.organization_name, safe=''), allow_missing=True)
        if not actual or actual.get('id') != binding.organization_id:
            invalidate_server(server)
            raise AccessError('组织身份发生变化，请联系平台管理员重新核验', 409)
        verify_organization(binding, organization_birth(client, binding.organization_name, binding.organization_id))
    return binding


def current_binding(required=True):
    if not has_request_context():
        return None
    if hasattr(g, 'access_binding'):
        return g.access_binding
    from access.models import OrganizationGrant
    raw = request.headers.get('X-Organization-ID')
    if not raw and current_user.is_authenticated and not current_user.is_platform_admin:
        grants = OrganizationGrant.query.filter_by(account_id=current_user.id, enabled=True).all()
        if len(grants) == 1:
            raw = grants[0].binding_id
    if not raw:
        if required and not is_platform():
            raise AccessError('请先选择已授权组织', 403)
        g.access_binding = None
        return None
    g.access_binding = binding_for(raw, current_user)
    return g.access_binding


def require_server(server_id):
    binding = current_binding()
    if binding and str(server_id) != str(binding.server_id):
        raise AccessError('资源不存在或无权访问', 404)
    return binding


def require_organization(server_id, organization_id):
    binding = require_server(server_id)
    if binding and str(organization_id) != str(binding.organization_id):
        raise AccessError('组织不存在或无权访问', 404)
    return binding


def repositories_for(binding):
    cache_key = ('access_repositories', binding.id)
    cache = g.setdefault('access_cache', {}) if has_request_context() else {}
    if cache_key not in cache:
        from services.policy_gitea import PolicyGitea
        server = db.session.get(GiteaServer, binding.server_id)
        rows = PolicyGitea(server).pages('/orgs/' + quote(binding.organization_name, safe='') + '/repos')
        if any(r.get('owner', {}).get('id') != binding.organization_id for r in rows):
            raise AccessError('仓库目录包含其他组织，已停止访问', 409)
        registered = False
        for repository in rows:
            registered = verify_repository(binding, repository, commit=False) or registered
        detached = detach_missing_repository_records(binding, {row['id'] for row in rows})
        if registered or detached:
            db.session.commit()
        cache[cache_key] = {r['id']: r for r in rows}
    return cache[cache_key]


def require_repo(server_id, repo_id, binding=None):
    from services.policy_gitea import PolicyGitea
    if binding is None:
        binding = require_server(server_id)
    server = db.session.get(GiteaServer, server_id)
    if not server:
        raise AccessError('资源不存在或无权访问', 404)
    repo = PolicyGitea(server).repo(int(repo_id))
    if binding and (binding.server_id != server_id or repo.get('owner', {}).get('id') != binding.organization_id):
        archive_repository(server_id, repo_id, binding.id)
        raise AccessError('资源不存在或无权访问', 404)
    if binding:
        verify_repository(binding, repo)
    return repo


def archive_repository(server_id, repo_id, binding_id):
    archived = []
    for row in ResourceScope.query.filter_by(server_id=server_id, binding_id=binding_id, archived=False).all():
        if int(repo_id) in json.loads(row.repo_ids_json):
            row.archived = True
            archived.append(row)
    detach_repository_records(server_id, [int(repo_id)], binding_id)
    db.session.commit()
    freeze_review_resources(archived)


def detach_repository_records(server_id, repo_ids, binding_id):
    """保留旧门禁与统计记录，但转回原组织也不能恢复旧的当前状态。"""
    from access.models import OrganizationBinding
    from access.statistics import RepositoryStatisticsSnapshot
    from models import CommitGateAssignment
    binding = db.session.get(OrganizationBinding, binding_id)
    if not binding or binding.server_id != server_id or not repo_ids:
        return False
    # 仓库 ID 由请求传入时可能属于其他组织；门禁必须同时证明旧规则属于本绑定。
    rules = [int(row.resource_key) for row in ResourceScope.query.filter_by(
        kind='commit_rule', binding_id=binding.id, instance_epoch=binding.instance_epoch)]
    gates = CommitGateAssignment.query.filter_by(server_id=server_id).filter(
        CommitGateAssignment.repo_id.in_(repo_ids), CommitGateAssignment.rule_id.in_(rules))
    changed = gates.update({'repo_id': 0}, synchronize_session=False)
    changed += RepositoryStatisticsSnapshot.query.filter_by(server_id=server_id,
        organization_id=binding.organization_id, instance_epoch=binding.instance_epoch).filter(
        RepositoryStatisticsSnapshot.repo_id.in_(repo_ids)).update(
            {'instance_epoch': 'archived:' + uuid.uuid4().hex}, synchronize_session=False)
    return bool(changed)


def detach_missing_repository_records(binding, current_ids):
    """完整目录回读也处理仅有门禁/统计、没有任务 ResourceScope 的旧状态。"""
    from access.statistics import RepositoryStatisticsSnapshot
    from models import CommitGateAssignment
    rules = [int(row.resource_key) for row in ResourceScope.query.filter_by(
        kind='commit_rule', binding_id=binding.id, instance_epoch=binding.instance_epoch)]
    prior = {row.repo_id for row in CommitGateAssignment.query.filter_by(server_id=binding.server_id).filter(
        CommitGateAssignment.rule_id.in_(rules), CommitGateAssignment.repo_id > 0)}
    prior.update(row.repo_id for row in RepositoryStatisticsSnapshot.query.filter_by(
        server_id=binding.server_id, organization_id=binding.organization_id,
        instance_epoch=binding.instance_epoch))
    return detach_repository_records(binding.server_id, prior - current_ids, binding.id)


def actor_snapshot(binding=None):
    from access.models import OrganizationGrant
    if not has_request_context() or not current_user.is_authenticated:
        return {}
    grant = OrganizationGrant.query.filter_by(account_id=current_user.id, binding_id=binding.id, enabled=True).first() if binding else None
    return {'account_id': current_user.id, 'name': actor_name(), 'auth_version': current_user.auth_version,
            'binding_id': binding.id if binding else None,
            'grant_version': grant.version if grant else None, 'platform': bool(current_user.is_platform_admin)}


def bind_resource(kind, key, server_id, organization_id, repo_ids=(), actor=None, migration=False):
    from access.models import OrganizationBinding
    server = db.session.get(GiteaServer, server_id)
    if not server:
        raise AccessError('实例不存在', 404)
    binding = OrganizationBinding.query.filter_by(server_id=server_id, organization_id=organization_id, enabled=True,
                                                  instance_epoch=server_epoch(server)).first()
    if not binding:
        # 未纳入组织授权的平台资源继续仅平台可见。
        return None
    row = ResourceScope.query.filter_by(kind=kind, resource_key=str(key)).first()
    if row:
        if row.archived or row.binding_id != binding.id or row.instance_epoch != server_epoch(server):
            if migration:
                return None
            raise AccessError('资源原归属已失效，不能自动转交或重新启用', 409)
        return row
    row = ResourceScope(kind=kind, resource_key=str(key), binding_id=binding.id, server_id=server_id,
                        organization_id=organization_id, instance_epoch=server_epoch(server),
                        repo_ids_json=json.dumps(sorted(set(map(int, repo_ids)))),
                        actor_json=json.dumps(actor if actor is not None else actor_snapshot(binding), ensure_ascii=False))
    db.session.add(row)
    db.session.flush()
    return row


def bind_review_repository(state, repository_id, actual, actor=None):
    """人工与自动接入共用即时归属，避免新服务等到下次组织同步才受隔离。"""
    from access.models import OrganizationBinding
    repository = state.repo(repository_id)
    server = db.session.get(GiteaServer, repository['server_id'])
    binding = OrganizationBinding.query.filter_by(server_id=server.id,
        organization_id=actual['owner']['id'], instance_epoch=server_epoch(server), enabled=True).first()
    if binding:
        binding_for(binding.id)
        verify_repository(binding, actual)
    scope = bind_resource('ai_repository', repository_id, server.id, actual['owner']['id'],
                          [actual['id']], actor=actor)
    if scope:
        state.repo_status(repository_id, access_scope={'binding_id': scope.binding_id,
            'instance_epoch': scope.instance_epoch, 'organization_id': scope.organization_id,
            'server_id': server.id})
        db.session.commit()


def require_resource(kind, key):
    binding = current_binding()
    if binding is None and is_platform():
        return ResourceScope.query.filter_by(kind=kind, resource_key=str(key)).first()
    row = ResourceScope.query.filter_by(kind=kind, resource_key=str(key), binding_id=binding.id,
                                        instance_epoch=binding.instance_epoch, archived=False).first()
    if not row:
        raise AccessError('资源不存在或无权访问', 404)
    for repo_id in json.loads(row.repo_ids_json):
        require_repo(row.server_id, repo_id, binding)
    return row


def visible_keys(kind):
    binding = current_binding()
    if binding is None:
        return None
    rows = ResourceScope.query.filter_by(kind=kind, binding_id=binding.id,
                                        instance_epoch=binding.instance_epoch, archived=False).all()
    repos = repositories_for(binding) if any(json.loads(r.repo_ids_json) for r in rows) else {}
    result, archived, missing = [], [], set()
    for row in rows:
        if all(rid in repos for rid in json.loads(row.repo_ids_json)):
            result.append(row.resource_key)
        else:
            row.archived = True
            archived.append(row)
            missing.update(rid for rid in json.loads(row.repo_ids_json) if rid not in repos)
    detach_repository_records(binding.server_id, missing, binding.id)
    db.session.commit()
    freeze_review_resources(archived)
    return result


def check_task_actor(snapshot, server_id=None, repo_id=None):
    """后台每个写入点调用；人工权限和组织服务分别验证，不依赖网页会话。"""
    from access.models import ManagerAccount, OrganizationGrant
    account = db.session.get(ManagerAccount, snapshot.get('account_id'), populate_existing=True)
    if not account or not account.enabled or account.auth_version != snapshot.get('auth_version'):
        raise AccessError('操作者身份已失效，已停止后续操作', 403)
    if snapshot.get('platform') and not account.is_platform_admin:
        raise AccessError('平台权限已撤销，已停止后续操作', 403)
    binding = binding_for(snapshot['binding_id'], account) if snapshot.get('binding_id') else None
    if not account.is_platform_admin:
        grant = OrganizationGrant.query.filter_by(account_id=account.id, binding_id=binding.id, enabled=True).first() if binding else None
        if not grant or grant.version != snapshot.get('grant_version'):
            raise AccessError('组织权限已变化，已停止后续操作', 403)
    if repo_id and binding:
        require_repo(server_id, repo_id, binding)
    return binding
