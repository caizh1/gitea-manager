"""单管理员仓库策略中心接口。"""
import hmac
import json
import secrets
import re
from datetime import datetime
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request, session
from flask_login import current_user
from sqlalchemy import func

from models import db, GiteaServer, PolicyTemplate, PolicyBatch, PolicyRepoResult
from services.integration_outbox_service import write_audit
from services.policy_gitea import PolicyGitea, connection_hash
from services.policy_rules import PolicyError, normalize_rules, encode, fingerprint, API_FIELDS, BOOL_FIELDS, LIST_FIELDS, TEXT_FIELDS
from access.scopes import current_binding, visible_keys, bind_resource, actor_name, actor_snapshot
from services.policy_scope import organization, scoped_rules, verify_identities, template_scope, validate_repo_scope

policy_bp = Blueprint('repository_policies', __name__)


@policy_bp.before_request
def protect_policy_api():
    if not current_user.is_authenticated:
        return jsonify(error='请先登录'), 401
    origin = request.headers.get('Origin')
    if origin and urlsplit(origin).netloc != request.host or request.headers.get('Sec-Fetch-Site') == 'cross-site':
        return jsonify(error='不允许跨站访问策略接口'), 403
    if request.content_length and request.content_length > 2 * 1024 * 1024:
        return jsonify(error='请求内容超过 2 MB'), 413
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        supplied = request.headers.get('X-Policy-CSRF', '')
        expected = session.get('policy_csrf', '')
        from access.control import csrf_valid
        if not csrf_valid() and (not expected or not hmac.compare_digest(supplied, expected)):
            return jsonify(error='安全令牌无效，请刷新页面后重新操作'), 403


@policy_bp.errorhandler(PolicyError)
def policy_error(exc):
    return jsonify(error=str(exc)), 400


def positive(value, label):
    try:
        if isinstance(value, bool) or int(value) <= 0 or str(int(value)) != str(value):
            raise ValueError()
        return int(value)
    except (TypeError, ValueError):
        raise PolicyError(label + '必须是正整数') from None


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise PolicyError('需要 JSON 对象')
    return data


def server_by_id(value):
    return db.get_or_404(GiteaServer, positive(value, '实例 ID'))


def template_dict(item):
    return {'id': item.id, 'server_id': item.server_id, 'name': item.name, 'description': item.description,
            'organization': {'id': item.organization_id, 'name': item.organization_name, 'server_id': item.server_id} if item.organization_id else None,
            'identities': json.loads(item.identities_json or '{}'), 'source': json.loads(item.source_json or '{}'),
            'rules': json.loads(item.rules_json), 'content_hash': item.content_hash,
            'created_at': item.created_at.isoformat() + 'Z', 'updated_at': item.updated_at.isoformat() + 'Z'}


def batch_dict(item):
    counts = dict(db.session.query(PolicyRepoResult.status, func.count(PolicyRepoResult.id)).filter_by(batch_id=item.id).group_by(PolicyRepoResult.status).all())
    finished = item.total - counts.get('pending', 0) - counts.get('running', 0)
    return {'id': item.id, 'server_id': item.server_id, 'server_name': item.server_name, 'template_id': item.template_id,
            'organization': json.loads(item.scope_json or '{}').get('organization'),
            'template_name': item.template_name, 'actor': item.actor, 'status': item.status, 'total': item.total,
            'counts': counts, 'progress_percent': int(100 * finished / max(item.total, 1)), 'error': item.error,
            'created_at': item.created_at.isoformat() + 'Z',
            'submitted_at': item.submitted_at.isoformat() + 'Z' if item.submitted_at else None,
            'completed_at': item.completed_at.isoformat() + 'Z' if item.completed_at else None}


@policy_bp.get('/csrf')
def csrf():
    session.setdefault('policy_csrf', secrets.token_urlsafe(32))
    response = jsonify(token=session['policy_csrf'])
    response.headers['Cache-Control'] = 'no-store'
    return response


@policy_bp.get('/schema')
def schema():
    return jsonify(api_fields=API_FIELDS, bool_fields=BOOL_FIELDS, list_fields=LIST_FIELDS, text_fields=TEXT_FIELDS,
                   supported_version='1.26.4', max_rules=50, max_repositories=5000)


@policy_bp.get('/servers')
def servers():
    return jsonify([{'id': s.id, 'name': s.name, 'version': s.version} for s in GiteaServer.query.order_by(GiteaServer.id).all()])


def candidate_query(kind):
    server = server_by_id(request.args.get('server_id'))
    page = positive(request.args.get('page', '1'), '页码')
    query = request.args.get('q', '').strip()
    if page > 2000 or len(query) > 100 or any(ord(c) < 32 for c in query):
        raise PolicyError('名单查询条件超出允许范围')
    organization = request.args.get('org', '').strip()
    if kind == 'teams' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', organization):
        raise PolicyError('请先选择有效的模板所属组织')
    org_id = positive(request.args['org_id'], '组织 ID') if request.args.get('org_id') else None
    return jsonify(PolicyGitea(server).candidates(kind, query, page, organization, org_id))


@policy_bp.get('/organizations')
def organizations():
    client = PolicyGitea(server_by_id(request.args.get('server_id')))
    page = positive(request.args.get('page', '1'), '页码')
    query = request.args.get('q', '').strip()
    if page > 2000 or len(query) > 100 or any(ord(c) < 32 for c in query):
        raise PolicyError('组织查询条件超出允许范围')
    return jsonify(client.organizations(query, page))


def query_organization(client):
    return organization(client, {'id': positive(request.args.get('org_id'), '组织 ID'), 'name': request.args.get('org', '')})


@policy_bp.get('/users')
def candidate_users():
    return candidate_query('users')


@policy_bp.get('/teams')
def candidate_teams():
    return candidate_query('teams')


@policy_bp.get('/repositories')
def repositories():
    server = server_by_id(request.args.get('server_id'))
    page = positive(request.args.get('page', '1'), '页码')
    limit = min(100, positive(request.args.get('limit', '50'), '每页数量'))
    client = PolicyGitea(server)
    org = query_organization(client)
    data = client.request('GET', '/repos/search', params={'uid': org['id'], 'exclusive': True, 'page': page, 'limit': limit, 'q': request.args.get('q', '')[:200], 'include_private': True})
    items = data.get('data', [])
    if any(r.get('owner', {}).get('id') != org['id'] for r in items):
        raise PolicyError('Gitea 仓库查询返回了其他组织的仓库，已停止选择')
    return jsonify(items=[{'id': r['id'], 'full_name': r.get('full_path') or r['full_name'],
                           'compatibility_full_name': r['full_name'], 'default_branch': r.get('default_branch', ''),
                           'manageable': bool(r.get('permissions', {}).get('admin')) and not r.get('archived') and not r.get('mirror'),
                           'archived': bool(r.get('archived')), 'mirror': bool(r.get('mirror'))} for r in items],
                   total=client.total_count, page=page, limit=limit)


@policy_bp.get('/repositories/<int:repo_id>/rules')
def repository_rules(repo_id):
    client = PolicyGitea(server_by_id(request.args.get('server_id')))
    org = query_organization(client)
    repo = client.repo(repo_id)
    validate_repo_scope(client, repo, {'organization': org})
    data = client.import_rules(repo_id)
    if data['repo']['owner_id'] != org['id'] or data['repo']['full_name'].split('/')[0].lower() != org['name'].lower():
        raise PolicyError('来源仓库在导入期间发生转移或改名，请重新选择')
    rules = scoped_rules(data['rules'], org) if data['rules'] else []
    refs, rules = verify_identities(client, org, rules, resolve_names=True)
    return jsonify(repo=data['repo'], rules=rules, identities=refs, organization=org)


@policy_bp.get('/templates')
def templates():
    server = server_by_id(request.args.get('server_id'))
    query = PolicyTemplate.query.filter_by(server_id=server.id)
    allowed = visible_keys('policy_template')
    if allowed is not None:
        query = query.filter(PolicyTemplate.id.in_([int(i) for i in allowed]))
    if request.args.get('org_id'):
        query = query.filter_by(organization_id=positive(request.args['org_id'], '组织 ID'))
    elif request.args.get('legacy') == '1':
        query = query.filter(PolicyTemplate.organization_id.is_(None))
    return jsonify([template_dict(t) for t in query.order_by(PolicyTemplate.updated_at.desc()).all()])


def assign_template(item, data, original=None):
    name = data.get('name', '')
    description = data.get('description', '')
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or not isinstance(description, str) or len(description) > 2000:
        raise PolicyError('模板名称需为 1 至 100 字符，说明最多 2000 字符')
    client = PolicyGitea(server_by_id(item.server_id))
    org = organization(client, data.get('organization'))
    if original and original.organization_id and original.organization_id != org['id']:
        raise PolicyError('已有模板不能直接更换组织，请使用“复制到其他组织”')
    if original and not original.organization_id and data.get('confirm_identity_changes') is not True:
        raise PolicyError('旧模板归属组织前需要明确确认名单')
    copy_from = data.get('copy_from_id')
    if copy_from:
        source_template = db.get_or_404(PolicyTemplate, positive(copy_from, '来源模板 ID'))
        if source_template.server_id != item.server_id:
            raise PolicyError('当前不支持跨实例复制模板')
        if source_template.organization_id != org['id'] and data.get('confirm_identity_changes') is not True:
            raise PolicyError('跨组织复制需要明确重新选择并确认名单')
    rules = scoped_rules(data.get('rules'), org)
    identities, rules = verify_identities(client, org, rules, data.get('identities'))
    if original and original.organization_id and data.get('confirm_identity_changes') is not True:
        previous = json.loads(original.identities_json or '{}')
        for kind in ('users', 'teams'):
            old = {r['name'].lower(): r['id'] for r in previous.get(kind, [])}
            if any(r['name'].lower() in old and old[r['name'].lower()] != r['id'] for r in identities[kind]):
                raise PolicyError('同名名单的身份已改变，请明确核验新身份后保存')
    source = data.get('source') or {}
    if not isinstance(source, dict) or len(encode(source)) > 2000:
        raise PolicyError('来源信息格式错误')
    source = {k: source[k] for k in ('id', 'full_name', 'owner_id', 'default_branch') if k in source}
    item.organization_id, item.organization_name = org['id'], org['name']
    item.identities_json, item.source_json = encode(identities), encode(source)
    item.name, item.description, item.rules_json = name.strip(), description, encode(rules)
    item.content_hash = fingerprint([item.server_id, item.name, description, rules, org, identities, source])
    item.updated_at = datetime.utcnow()


@policy_bp.post('/templates')
def create_template():
    data = body()
    item = PolicyTemplate(server_id=server_by_id(data.get('server_id')).id)
    assign_template(item, data)
    db.session.add(item)
    db.session.flush()
    bind_resource('policy_template', item.id, item.server_id, item.organization_id)
    write_audit(actor_name(), 'policy.template.create', str(item.id), detail=item.name)
    db.session.commit()
    return jsonify(template_dict(item)), 201


@policy_bp.put('/templates/<int:template_id>')
def update_template(template_id):
    item = db.get_or_404(PolicyTemplate, template_id)
    data = body()
    if data.get('content_hash') != item.content_hash:
        return jsonify(error='模板已被修改，请刷新后编辑'), 409
    edited = PolicyTemplate(server_id=item.server_id)
    assign_template(edited, data, original=item)
    values = {key: getattr(edited, key) for key in ('name', 'description', 'rules_json', 'content_hash', 'updated_at',
              'organization_id', 'organization_name', 'identities_json', 'source_json')}
    changed = PolicyTemplate.query.filter_by(id=item.id, content_hash=data['content_hash']).update(values)
    if not changed:
        db.session.rollback()
        return jsonify(error='模板已被并发修改，请刷新后编辑'), 409
    write_audit(actor_name(), 'policy.template.update', str(item.id), detail=edited.name)
    db.session.commit()
    db.session.refresh(item)
    return jsonify(template_dict(item))


@policy_bp.delete('/templates/<int:template_id>')
def delete_template(template_id):
    item = db.get_or_404(PolicyTemplate, template_id)
    write_audit(actor_name(), 'policy.template.delete', str(item.id), detail=item.name)
    db.session.delete(item)
    db.session.commit()
    return jsonify(ok=True)


@policy_bp.post('/identities/verify')
def verify_template_identities():
    data = body()
    client = PolicyGitea(server_by_id(data.get('server_id')))
    org = organization(client, data.get('organization'))
    rules = scoped_rules(data.get('rules'), org)
    refs, rules = verify_identities(client, org, rules, data.get('identities'),
                                    resolve_names=data.get('confirm_names') is True, refresh_names=True)
    return jsonify(organization=org, rules=rules, identities=refs)


@policy_bp.post('/batches')
def preview_batch():
    data = body()
    template = db.get_or_404(PolicyTemplate, positive(data.get('template_id'), '模板 ID'))
    if data.get('content_hash') != template.content_hash:
        return jsonify(error='模板已变化，请刷新模板'), 409
    server = server_by_id(template.server_id)
    scope = template_scope(template)
    scope['access_actor'] = actor_snapshot(current_binding())
    ids = data.get('repo_ids')
    if not isinstance(ids, list) or not ids or len(ids) > 5000:
        raise PolicyError('请选择 1 至 5000 个仓库')
    ids = list(dict.fromkeys(positive(i, '仓库 ID') for i in ids))
    item = PolicyBatch(server_id=server.id, server_name=server.name, connection_hash=connection_hash(server),
                       template_id=template.id, template_name=template.name, template_hash=template.content_hash,
                       template_json=template.rules_json, scope_json=encode(scope), total=len(ids), actor=actor_name(), request_source=request.remote_addr or '')
    db.session.add(item)
    db.session.flush()
    db.session.add_all([PolicyRepoResult(batch_id=item.id, repo_id=i) for i in ids])
    bind_resource('policy_batch', item.id, item.server_id, template.organization_id, ids)
    write_audit(actor_name(), 'policy.preview.create', str(item.id), detail=f'{len(ids)} 个仓库')
    db.session.commit()
    return jsonify(batch_dict(item)), 202


@policy_bp.post('/batches/<int:batch_id>/apply')
def apply_batch(batch_id):
    item = db.get_or_404(PolicyBatch, batch_id)
    if item.submitted_at:
        return jsonify(batch_dict(item)), 200
    if not json.loads(item.scope_json or '{}').get('organization'):
        return jsonify(error='旧预览未记录组织和名单身份，请归属组织后重新预览；历史记录保持可读'), 409
    template = db.session.get(PolicyTemplate, item.template_id)
    server = db.session.get(GiteaServer, item.server_id)
    if not template or template.content_hash != item.template_hash or not server or connection_hash(server) != item.connection_hash:
        return jsonify(error='模板已修改、删除或实例连接已变化，请重新预览'), 409
    if item.status != 'ready':
        return jsonify(error='预览尚未完成或已失效'), 409
    applicable = PolicyRepoResult.query.filter(PolicyRepoResult.batch_id == item.id, PolicyRepoResult.status.in_(('changes', 'unchanged'))).count()
    if not applicable:
        return jsonify(error='没有可应用的仓库，请查看阻塞原因'), 409
    scope = json.loads(item.scope_json or '{}')
    scope['access_actor'] = actor_snapshot(current_binding())
    claimed = PolicyBatch.query.filter_by(id=item.id, status='ready', submitted_at=None).update({'status': 'apply_queued', 'submitted_at': datetime.utcnow(), 'completed_at': None,
                                                                                            'scope_json': encode(scope), 'actor': actor_name()})
    if claimed:
        PolicyRepoResult.query.filter(PolicyRepoResult.batch_id == item.id, PolicyRepoResult.status.in_(('changes', 'unchanged'))).update({'status': 'pending'})
        write_audit(actor_name(), 'policy.apply.submit', str(item.id), detail=f'{applicable} 个可应用仓库')
    db.session.commit()
    return jsonify(batch_dict(db.session.get(PolicyBatch, batch_id))), 202


@policy_bp.get('/batches')
def batches():
    page = positive(request.args.get('page', '1'), '页码')
    query = PolicyBatch.query
    allowed = visible_keys('policy_batch')
    if allowed is not None:
        query = query.filter(PolicyBatch.id.in_([int(i) for i in allowed]))
    if request.args.get('server_id'):
        query = query.filter_by(server_id=positive(request.args['server_id'], '实例 ID'))
    result = query.order_by(PolicyBatch.id.desc()).paginate(page=page, per_page=20, error_out=False)
    return jsonify(items=[batch_dict(b) for b in result.items], total=result.total)


@policy_bp.get('/batches/<int:batch_id>')
def batch_detail(batch_id):
    item = db.get_or_404(PolicyBatch, batch_id)
    managed_names = {r['rule_name'] for r in json.loads(item.template_json)}
    query = PolicyRepoResult.query.filter_by(batch_id=item.id)
    if request.args.get('status'):
        query = query.filter_by(status=request.args['status'])
    page = positive(request.args.get('page', '1'), '页码')
    result = query.order_by(PolicyRepoResult.id).paginate(page=page, per_page=50, error_out=False)
    rows = []
    for row in result.items:
        value = {k: getattr(row, k) for k in ('id', 'repo_id', 'repo_name', 'status', 'error')}
        for name in ('before', 'desired', 'actual', 'diff', 'matches', 'operations'):
            raw = getattr(row, name + '_json')
            value[name] = json.loads(raw) if raw is not None else None
        value['retained_rules'] = [r['rule_name'] for r in value['before'] if r['rule_name'] not in managed_names]
        value['repository_url'] = json.loads(row.repo_identity_json or '{}').get('html_url', '')
        rows.append(value)
    return jsonify(batch=batch_dict(item), template_snapshot=json.loads(item.template_json), scope_snapshot=json.loads(item.scope_json or '{}'), items=rows, total=result.total)
