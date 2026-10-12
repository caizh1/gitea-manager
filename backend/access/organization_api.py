"""组织入口检查与小型只读投影；业务写入仍走原有服务和回读链路。"""
import json
from urllib.parse import quote

from flask import jsonify, request
from models import db, GiteaServer, PolicyTemplate, PolicyBatch, ApprovalPolicy, CommitMessageRule
from access.errors import AccessError
from access.scopes import (require_resource, require_repo, require_organization, repositories_for,
                           visible_keys, ResourceScope)


def guard(endpoint, binding):
    data = request.get_json(silent=True) if request.method not in ('GET', 'HEAD', 'OPTIONS') else {}
    data = data if isinstance(data, dict) else {}
    for source in (request.args, data, request.view_args or {}):
        if source.get('server_id') is not None and str(source['server_id']) != str(binding.server_id):
            raise AccessError('资源不存在或无权访问', 404)
    if request.args.get('org_id') and str(request.args['org_id']) != str(binding.organization_id):
        raise AccessError('组织不存在或无权访问', 404)
    if request.args.get('org') and request.args['org'] != binding.organization_name:
        raise AccessError('组织不存在或无权访问', 404)
    if 'organization' in data:
        org = data['organization']
        if not isinstance(org, dict) or org.get('id') != binding.organization_id:
            raise AccessError('组织不存在或无权访问', 404)
        if org.get('server_id', binding.server_id) != binding.server_id:
            raise AccessError('组织不存在或无权访问', 404)
    if endpoint in {'servers.list_servers', 'repository_policies.servers', 'ai_review.servers'}:
        server = db.session.get(GiteaServer, binding.server_id)
        return jsonify([{'id': server.id, 'name': server.name, 'version': server.version}])
    if endpoint == 'repository_policies.organizations':
        return jsonify(items=[{'id': binding.organization_id, 'name': binding.organization_name,
                              'server_id': binding.server_id}], total=1, page=1, limit=50)
    if endpoint in {'repository_policies.candidate_users', 'repository_policies.candidate_teams'}:
        from services.policy_gitea import PolicyGitea
        client = PolicyGitea(db.session.get(GiteaServer, binding.server_id))
        kind = 'teams' if endpoint.endswith('candidate_teams') else 'members'
        rows = client.pages('/orgs/' + quote(binding.organization_name, safe='') + '/' + kind)
        search = request.args.get('q', '').casefold()
        rows = [row for row in rows if search in (row.get('name') or row.get('login') or '').casefold()]
        page = max(1, request.args.get('page', 1, type=int))
        items = [{'id': row['id'], 'name': row.get('login') or row.get('name'),
                  'server_id': binding.server_id, 'organization_id': binding.organization_id if kind == 'teams' else None}
                 for row in rows[(page-1)*50:page*50]]
        return jsonify(items=items, total=len(rows), page=page)
    if endpoint.startswith('repository_policies.'):
        template_id = (request.view_args or {}).get('template_id') or data.get('template_id')
        if template_id:
            require_resource('policy_template', template_id)
        if data.get('copy_from_id'):
            require_resource('policy_template', data['copy_from_id'])
        batch_id = (request.view_args or {}).get('batch_id')
        if batch_id:
            require_resource('policy_batch', batch_id)
        for repo_id in data.get('repo_ids', []):
            require_repo(binding.server_id, repo_id, binding)
        if (request.view_args or {}).get('repo_id'):
            require_repo(binding.server_id, request.view_args['repo_id'], binding)
        source = data.get('source')
        if isinstance(source, dict) and source.get('id'):
            require_repo(binding.server_id, source['id'], binding)
    if endpoint.startswith('commit_gate.'):
        rule_id = (request.view_args or {}).get('rule_id') or data.get('rule_id')
        if rule_id:
            require_resource('commit_rule', rule_id)
        if endpoint.endswith(('apply_gate', 'remove_gate_route')):
            names = {row['full_name'] for row in repositories_for(binding).values()}
            requested = data.get('repo_names') or []
            if not isinstance(requested, list) or any(not isinstance(n, str) or n not in names for n in requested):
                raise AccessError('请求包含无权操作的仓库，整批未执行', 404)
    if endpoint.startswith('approval.'):
        pid = (request.view_args or {}).get('pid')
        if pid:
            require_resource('approval_policy', pid)
        if data.get('repo_id'):
            require_repo(binding.server_id, data['repo_id'], binding)
    if endpoint.startswith('ai_review.'):
        return guard_review(endpoint, binding, data)
    if endpoint.startswith('statistics.'):
        from access.statistics import response
        return response(endpoint, binding)
    if endpoint == 'dashboard.recent_activity':
        ids = visible_keys('policy_batch')
        batches = PolicyBatch.query.filter(PolicyBatch.id.in_(list(map(int, ids)))).order_by(PolicyBatch.id.desc()).limit(5).all()
        return jsonify(recent_backups=[], recent_restores=[], recent_policies=[
            {'id': row.id, 'name': row.template_name, 'status': row.status, 'actor': row.actor} for row in batches])
    return None


def review_repository_ids():
    keys = visible_keys('ai_repository')
    return list(map(int, keys)) if keys is not None else None


def review_job_ids(state):
    keys = visible_keys('ai_job')
    if keys is None:
        return None
    # 自动任务在同一仓库服务归属下创建，快照代次由入队事务记录。
    from access.scopes import current_binding
    binding = current_binding()
    repositories = review_repository_ids()
    with state.connect() as connection:
        rows = connection.execute("SELECT id FROM jobs WHERE repository IN (SELECT value FROM json_each(?)) AND json_extract(access_scope,'$.binding_id')=? AND json_extract(access_scope,'$.instance_epoch')=?",
                                  (json.dumps(repositories), binding.id, binding.instance_epoch)).fetchall()
    return sorted(set(keys) | {row['id'] for row in rows})


def guard_review(endpoint, binding, data):
    from routes.ai_review_routes import store
    state = store()
    repository = (request.view_args or {}).get('repository') or request.args.get('repository')
    if repository:
        require_resource('ai_repository', repository)
    job_id = (request.view_args or {}).get('job')
    if job_id:
        if job_id not in review_job_ids(state):
            raise AccessError('资源不存在或无权访问', 404)
        task = state.job(job_id, public=True, evidence=False)
        require_resource('ai_repository', task['repository'])
    if endpoint == 'ai_review.config':
        return jsonify(config={'enabled': state.config(public=True)['enabled']}, health={}, supported_version='1.26.4')
    if endpoint == 'ai_review.catalog':
        rows = list(repositories_for(binding).values())
        query = request.args.get('q', '').casefold()
        rows = [r for r in rows if query in r['full_name'].casefold()]
        page = max(1, request.args.get('page', 1, type=int))
        return jsonify(items=[{'id': r['id'], 'name': r['full_name']} for r in rows[(page-1)*50:page*50]], total=len(rows))
    if endpoint == 'ai_review.update_repository':
        if set(data) - {'enabled', 'revision'}:
            raise AccessError('组织管理员只能修改仓库审查开关', 403)
        if not isinstance(data.get('enabled'), bool):
            raise AccessError('仓库开关必须是布尔值')
    return None
