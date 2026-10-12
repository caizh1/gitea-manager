"""后台服务专用判定接口；不接收调用方自报的账号或组织权限。"""
import hmac
import json
import os
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from models import db, GiteaServer
from access.errors import AccessError
from access.scopes import (ResourceScope, current_binding, binding_for, check_task_actor,
                           require_repo, server_epoch)

internal_bp = Blueprint('access_internal', __name__)
context_bp = Blueprint('access_context', __name__)


@context_bp.get('/context')
def context():
    binding = current_binding()
    if not binding:
        return jsonify(organization=None)
    server = db.session.get(GiteaServer, binding.server_id)
    from ai_review.store import Store
    state = Store(current_app.config.get('AI_REVIEW_DB'))
    return jsonify(binding_id=binding.id, server_id=server.id, server_name=server.name,
                   organization_id=binding.organization_id, organization_name=binding.organization_name,
                   ai_review_enabled=bool(state.config(public=True)['enabled']))


@internal_bp.post('/check')
def check():
    path = os.environ.get('AI_REVIEW_AUTHORIZATION_TOKEN_FILE')
    try:
        expected = Path(path).read_text().strip() if path else ''
    except OSError:
        expected = ''
    supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')
    if len(expected) < 32 or not hmac.compare_digest(expected.encode('utf-8'), supplied.encode('utf-8')):
        raise AccessError('服务身份无效', 401)
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) - {'job_id', 'repository_id', 'action'}:
        raise AccessError('判定参数无效')
    if data.get('action') not in ('read', 'write', 'publish'):
        raise AccessError('判定动作无效')
    from ai_review.store import Store
    state = Store(current_app.config.get('AI_REVIEW_DB'))
    task = state.job(data['job_id'], public=False, evidence=False) if data.get('job_id') else None
    rid = task['repository'] if task else data.get('repository_id')
    if task and data.get('repository_id') not in (None, rid):
        raise AccessError('任务与仓库不一致', 403)
    repo = state.repo(rid)
    if not repo['enabled']:
        raise AccessError('仓库服务已停用', 403)
    scope = repo.get('access_scope') or {}
    if scope:
        binding = binding_for(scope.get('binding_id'))
        if scope.get('instance_epoch') != binding.instance_epoch:
            raise AccessError('组织绑定代次已变化', 403)
        record = ResourceScope.query.filter_by(kind='ai_repository', resource_key=str(rid), archived=False,
                                               binding_id=binding.id, instance_epoch=binding.instance_epoch).first()
        if not record:
            raise AccessError('仓库服务归属已失效', 403)
        require_repo(repo['server_id'], repo['repo_id'], binding)
        if task:
            job_scope = json.loads(task.get('access_scope') or '{}')
            if job_scope != scope:
                raise AccessError('任务原组织归属已变化', 403)
    if task and task.get('manual'):
        actor = json.loads(task.get('access_actor') or '{}')
        if not actor:
            raise AccessError('旧人工任务缺少真实操作者，请重新提交', 403)
        check_task_actor(actor, repo['server_id'], repo['repo_id'])
    return jsonify(allowed=True)
