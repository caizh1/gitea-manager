"""AI Review 管理接口与签名 Webhook；Web 请求不执行代码审查。"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit, unquote

from flask import Blueprint, current_app, jsonify, request, session, send_file, g, Response, stream_with_context, make_response
from flask_login import current_user
import requests

from models import db, GiteaServer
from ai_review.common import ReviewError, endpoint, valid_sha
from ai_review.host_policy import gitea_address
from ai_review.gitea import Gitea
from ai_review.store import Store

ai_review_bp = Blueprint('ai_review', __name__)


def store():
    return Store(current_app.config.get('AI_REVIEW_DB'))


@ai_review_bp.before_request
def protect():
    if request.endpoint in ('ai_review.webhook', 'ai_review.setup_probe'):
        return
    if not current_user.is_authenticated:
        return jsonify(error='请先登录'), 401
    origin = request.headers.get('Origin')
    if (origin and urlsplit(origin).netloc != request.host) or request.headers.get('Sec-Fetch-Site') == 'cross-site':
        return jsonify(error='不允许跨站访问 Review 管理接口'), 403
    if request.content_length and request.content_length > 1024*1024:
        return jsonify(error='请求内容超过 1 MB'), 413
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        expected = session.get('ai_review_csrf', '')
        from access.control import csrf_valid
        if not csrf_valid() and (not expected or not hmac.compare_digest(expected.encode('utf-8'), request.headers.get('X-Review-CSRF', '').encode('utf-8'))):
            return jsonify(error='安全令牌无效，请刷新页面'), 403
        lock = store().lock('自动接入执行', blocking=False)
        try: lock.__enter__()
        except ReviewError:
            return jsonify(error='自动接入正在核验配置，请待其完成后再修改'), 409
        g.ai_setup_write_lock = lock


@ai_review_bp.teardown_request
def release_setup_write_lock(_):
    lock = g.pop('ai_setup_write_lock', None)
    if lock: lock.__exit__(None, None, None)


@ai_review_bp.after_request
def no_cache(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


@ai_review_bp.errorhandler(ReviewError)
def review_error(exc):
    return jsonify(error=str(exc), **({'diagnostic': exc.connection_diagnostic}
                   if hasattr(exc, 'connection_diagnostic') else {})), 400


@ai_review_bp.errorhandler(sqlite3.Error)
def db_error(exc):
    from ai_review.diagnostics import describe
    value = describe(exc, phase='管理接口存储')
    return jsonify(error=value['message'], error_code=value['error_code']), 503


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ReviewError('需要 JSON 对象')
    return value


def positive(value):
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 2**31-1:
        raise ReviewError('标识必须是正整数')
    return int(value)


def server(value):
    target = db.get_or_404(GiteaServer, positive(value))
    from ai_review.host_policy import path, approve, gitea_address
    if path(): approve({'AI_REVIEW_GITEA_HOSTS': [gitea_address(target.gitea_url)]})
    return target


@ai_review_bp.get('/csrf')
def csrf():
    session.setdefault('ai_review_csrf', secrets.token_urlsafe(32))
    return jsonify(token=session['ai_review_csrf'])


@ai_review_bp.get('/config')
def config():
    state = store()
    return jsonify(config=state.config(public=True), health=state.health_status(), supported_version='1.26.4', trial_versions=['1.27.3'])


@ai_review_bp.put('/config')
def save_config():
    state, data = store(), body()
    allowed = {'revision', 'enabled', 'engine', 'api_base_url', 'model', 'api_key',
               'debounce_seconds', 'max_files', 'max_calls', 'task_timeout_seconds', 'review_timeout_seconds',
               'model_request_timeout_seconds', 'model_max_output_tokens', 'model_response_max_mib', 'max_findings', 'thinking_protocol', 'thinking_enabled', 'thinking_token_budget', 'context_retrieval_budget', 'model_context_tokens', 'compaction_threshold_tokens'}
    from ai_review.token_budget import DEFAULTS as batch_defaults, validate as validate_batch
    allowed.update(batch_defaults)
    if set(data)-allowed:
        raise ReviewError('配置包含不支持的字段')
    old = state.config()
    value = dict(old, **data)
    validate_batch(value)
    if not isinstance(value['enabled'], bool) or value['engine'] != 'opencode_native':
        raise ReviewError('开关或执行引擎无效')
    for key, low, high in [('debounce_seconds', 0, 120), ('max_files', 1, 100), ('max_calls', 2, 64),
                           ('task_timeout_seconds', 60, 3600), ('review_timeout_seconds', 60, 3600),
                           ('model_request_timeout_seconds', 60, 3600), ('max_findings', 1, 30),
                           ('model_max_output_tokens', 0, 1048576), ('model_response_max_mib', 1, 64)]:
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ReviewError(f'{key} 超出允许范围 {low} 至 {high}')
    from ai_review.thinking import validate as validate_thinking
    validate_thinking(value)
    from ai_review.budgets import validate_context
    validate_context(value)
    if value['api_base_url']:
        from ai_review.host_policy import path, approve
        if path() and 'api_base_url' in data: approve({'AI_REVIEW_MODEL_HOSTS': [value['api_base_url']]})
        value['api_base_url'] = endpoint(value['api_base_url'], 'AI_REVIEW_MODEL_HOSTS')
    if not isinstance(value['model'], str) or value['model'] and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', value['model']):
        raise ReviewError('模型标识格式无效')
    if 'api_key' in data:
        value['api_key'] = state.seal(data['api_key']) if data['api_key'] else ''
    if value['enabled'] and not all(value[k] for k in ('api_base_url', 'api_key', 'model')):
        raise ReviewError('启用前请配置模型服务地址、密钥和模型')
    return jsonify(state.save_config(value))


@ai_review_bp.post('/connection-test')
def connection_test():
    from ai_review.setup_service import verify_model
    state = store(); config = state.config()
    if not config['api_key']:
        raise ReviewError('请先保存模型地址、名称和 Key')
    if body().get('token_samples') == 4:
        from ai_review.token_calibration import run
        return jsonify(ok=True, calibration=run(state, config))
    checked = verify_model(state, config)
    return jsonify(ok=True, model_available=checked['listed'], revision=config['revision'],
                   model=config['model'], message=checked['message'])


@ai_review_bp.get('/servers')
def servers():
    return jsonify([{'id': s.id, 'name': s.name, 'version': s.version} for s in GiteaServer.query.order_by(GiteaServer.id)])


@ai_review_bp.get('/catalog')
def catalog():
    target = server(request.args.get('server_id'))
    client = Gitea(target.gitea_url, target.api_token)
    try:
        rows = client.request('GET', '/repos/search', params={'page': positive(request.args.get('page', '1')), 'limit': 50,
                     'q': request.args.get('q', '')[:200], 'include_private': True})
        return jsonify(items=[{'id': r['id'], 'name': r['full_name']} for r in rows.get('data', [])])
    finally:
        client.close()


@ai_review_bp.get('/servers/<int:server_id>/progress-assets')
def progress_assets(server_id):
    target = server(server_id)
    from ai_review.progress_assets import build_assets
    repositories = [r for r in store().repos(public=True) if r['server_id'] == target.id]
    return send_file(build_assets(repositories), mimetype='application/zip', as_attachment=True,
                     download_name=f'gitea-review-progress-{target.id}.zip')


@ai_review_bp.get('/repositories')
def repositories():
    from access.organization_api import review_repository_ids
    allowed = review_repository_ids()
    return jsonify([row for row in store().repos(public=True) if allowed is None or row['id'] in allowed])


def repository_values(state, data, old=None):
    if not isinstance(data.get('enabled', False), bool):
        raise ReviewError('仓库开关必须是布尔值')
    if old:
        target = server(old['server_id'])
        name = old['repo_name']
    else:
        target = server(data.get('server_id'))
        name = data.get('repo_name')
    token = data.get('bot_token')
    if not token and old:
        token = state.unseal(old['bot_token'])
    if not isinstance(token, str) or not token:
        raise ReviewError('需要配置专用 Review 机器人令牌')
    client = Gitea(target.gitea_url, token, name)
    try:
        gitea_version=client.version()
        user = client.request('GET', '/user')
        repository = client.request('GET', client.prefix)
        permissions = repository.get('permissions', {})
        if user.get('is_admin') or permissions.get('admin') or permissions.get('push'):
            raise ReviewError('机器人不能具有管理员或代码写入权限，请使用仓库只读成员')
        if not permissions.get('pull') or repository.get('archived') or repository.get('mirror'):
            raise ReviewError('机器人缺少仓库读取权限，或仓库归档/镜像模式不支持 Review')
        try:
            client.request('GET', client.prefix+'/issues', params={'type': 'pulls', 'state': 'all', 'limit': 1})
        except ReviewError:
            raise ReviewError('机器人 Token 还需 issue 权限；请配置 read:user、write:repository 和 write:issue，账号仍保持仓库只读') from None
        if repository.get('full_name') != name or old and repository['id'] != old['repo_id']:
            raise ReviewError('仓库身份不一致')
        client.verify_review_scopes()
        return target.id, repository['id'], {
            'repo_name': name, 'server_name': target.name, 'gitea_url': gitea_address(target.gitea_url),'gitea_version':gitea_version,
            'bot_token': state.seal(token), 'bot_id': user['id'], 'bot_name': user['login'],
            'webhook_secret': old['webhook_secret'] if old else state.seal(secrets.token_urlsafe(32)),
            'enabled': data.get('enabled', False), 'hook_status': old.get('hook_status', '未安装') if old else '未安装',
            **({'revision': data.get('revision')} if old else {})}
    finally:
        client.close()


@ai_review_bp.post('/repositories')
def create_repository():
    state = store(); target, repo_id, data = repository_values(state, body())
    # Review 与 Manager 分库，先禁用落盘；归属核验失败不得留下可执行的无范围服务。
    wanted_enabled = data.get('enabled', False)
    saved = state.save_repo(target, repo_id, dict(data, enabled=False))
    from access.scopes import bind_review_repository, require_repo
    actual = require_repo(target, repo_id)
    bind_review_repository(state, saved['id'], actual)
    if wanted_enabled:
        state.save_repo(target, repo_id, {'enabled': True, 'revision': saved['revision']}, saved['id'])
    return jsonify(state.repo(saved['id'], public=True)), 201


@ai_review_bp.put('/repositories/<int:repository>')
def update_repository(repository):
    state = store(); old = state.repo(repository)
    from access.scopes import is_platform, actor_snapshot, current_binding, check_task_actor
    if not is_platform():
        check_task_actor(actor_snapshot(current_binding()), old['server_id'], old['repo_id'])
        return jsonify(state.save_repo(old['server_id'], old['repo_id'], body(), repository))
    target, repo_id, data = repository_values(state, body(), old)
    from access.scopes import bind_review_repository, require_repo
    bind_review_repository(state, repository, require_repo(target, repo_id))
    return jsonify(state.save_repo(target, repo_id, data, repository))


@ai_review_bp.post('/repositories/<int:repository>/webhook')
def install_webhook(repository):
    state = store()
    manager_url = body().get('manager_url')
    from ai_review.host_policy import path, approve
    if path(): approve({'AI_REVIEW_WEBHOOK_HOSTS': [manager_url]})
    return jsonify(ensure_webhook(state, repository, manager_url))


def ensure_webhook(state, repository, manager_url):
    repo = state.repo(repository)
    manager = endpoint(manager_url, 'AI_REVIEW_WEBHOOK_HOSTS')
    callback = manager + f'/api/ai-review/webhooks/{repository}'
    target = server(repo['server_id'])
    if gitea_address(target.gitea_url) != gitea_address(repo['gitea_url']):
        raise ReviewError('服务器连接已变化，请先更新 Review 仓库配置')
    client = Gitea(target.gitea_url, target.api_token, repo['repo_name'], repo['repo_id'])
    path = client.prefix+'/hooks'
    try:
        client.repository()
        existing = [r for r in client.pages(path) if r.get('config', {}).get('url') == callback]
        if len(existing) > 1:
            raise ReviewError('存在重复 Webhook，请先在 Gitea 中核对')
        value = {'type': 'gitea', 'active': True, 'events': ['pull_request', 'pull_request_sync', 'pull_request_comment',
                 'pull_request_review'],
                 'config': {'url': callback, 'content_type': 'json', 'secret': state.unseal(repo['webhook_secret'])}}
        if existing:
            hook = client.request('PATCH', path+'/'+str(existing[0]['id']), value)
        else:
            hook = client.request('POST', path, value)
        actual = client.request('GET', path+'/'+str(hook['id']))
        if not actual.get('active') or actual.get('config', {}).get('url') != callback or not set(value['events']).issubset(actual.get('events', [])):
            raise ReviewError('Webhook 回读验证失败')
        state.repo_status(repository, hook_id=hook['id'], hook_status='已安装并回读验证', webhook_url=callback,
                          feedback_since=repo.get('feedback_since',time.time()))
        return state.repo(repository, public=True)
    except ReviewError as exc:
        state.repo_status(repository, hook_status='安装失败或结果待核对：'+str(exc))
        raise
    finally:
        client.close()


@ai_review_bp.get('/setup')
def setup_status():
    from ai_review.setup_service import public_job
    state = store()
    return jsonify(job=public_job(state, server_id=positive(request.args['server_id']) if request.args.get('server_id') else None), automatic_available=bool(__import__('os').environ.get('AI_REVIEW_HOST_POLICY_FILE')))


@ai_review_bp.post('/setup')
def setup_submit():
    from ai_review.setup_service import submit, start
    job = submit(store(), body(), request.url_root.rstrip('/'))
    start(current_app._get_current_object())
    return jsonify(job=job), 202


@ai_review_bp.get('/setup/<ident>')
def setup_detail(ident):
    from ai_review.setup_service import public_job
    job = public_job(store(), ident)
    if not job: return jsonify(error='接入任务不存在'), 404
    return jsonify(job=job)


@ai_review_bp.post('/setup/<ident>/resume')
def setup_resume(ident):
    from ai_review.setup_service import resume, start
    job = resume(store(), ident, body())
    start(current_app._get_current_object())
    return jsonify(job=job), 202


@ai_review_bp.get('/setup-probe/<token>')
def setup_probe(token):
    if not re.fullmatch('[0-9a-f]{48}', token): return '', 404
    state = store()
    with state.tx() as c:
        try:
            row = c.execute('UPDATE setup_probes SET seen=1 WHERE token=? AND expires>?', (token, time.time()))
        except sqlite3.OperationalError:
            return '', 404
        if row.rowcount != 1: return '', 404
    return hashlib.sha256(token.encode()).hexdigest(), 200, {'Content-Type': 'text/plain; charset=utf-8'}


@ai_review_bp.get('/tasks')
def tasks():
    from access.organization_api import review_job_ids, review_repository_ids
    from ai_review.statistics import period
    state = store()
    window = period(request.args) if any(k in request.args for k in ('start', 'end', 'timezone')) else None
    return jsonify(state.jobs(positive(request.args.get('page', '1')),
        positive(request.args['repository']) if request.args.get('repository') else None,
        positive(request.args['number']) if request.args.get('number') else None, request.args.get('status'),
        allowed_jobs=review_job_ids(state), window=window, allowed_repositories=review_repository_ids()))


@ai_review_bp.get('/statistics')
def statistics():
    from access.organization_api import review_job_ids, review_repository_ids
    from ai_review.statistics import period, overview
    state = store()
    return jsonify(overview(state, period(request.args),
        positive(request.args['repository']) if request.args.get('repository') else None,
        allowed_jobs=review_job_ids(state), allowed_repositories=review_repository_ids()))


@ai_review_bp.get('/repositories/<int:repository>/pulls/<int:number>/issues')
def issue_list(repository, number):
    from ai_review.issue_lifecycle import listing
    state = store(); state.repo(repository)
    return jsonify(listing(state, repository, positive(number), positive(request.args.get('page', '1'))))


@ai_review_bp.get('/tasks/<job>/issues')
def task_issues(job):
    from ai_review.issue_lifecycle import listing
    state = store(); task = state.job(job, public=True, evidence=False)
    return jsonify(listing(state, task['repository'], task['number'], positive(request.args.get('page', '1')), job))



@ai_review_bp.get('/repositories/<int:repository>/dataset')
def dataset_list(repository):
    from ai_review.dataset import list_examples
    state=store();state.repo(repository)
    return jsonify(list_examples(state,repository,request.args.get('status'),positive(request.args.get('page','1'))))


@ai_review_bp.post('/repositories/<int:repository>/dataset/<ident>/disable')
def dataset_disable(repository,ident):
    from ai_review.dataset import disable
    state=store();state.repo(repository)
    return jsonify(disable(state,repository,ident))


@ai_review_bp.get('/repositories/<int:repository>/dataset/export')
def dataset_export(repository):
    from ai_review.dataset import export
    state=store();state.repo(repository)
    return send_file(export(state,repository,request.args.get('history')=='1'),mimetype='application/zip',
                     as_attachment=True,download_name=f'ai-review-dataset-{repository}.zip')


@ai_review_bp.get('/repositories/<int:repository>/dataset/<ident>/history')
def dataset_history(repository,ident):
    import contextlib
    state=store();state.repo(repository)
    with contextlib.closing(state.connect()) as c:
        rows=c.execute('SELECT h.value FROM review_example_history h JOIN review_examples e ON e.id=h.id WHERE e.repository=? AND e.id=? ORDER BY h.revision DESC LIMIT 100',(repository,ident)).fetchall()
    return jsonify([json.loads(row[0]) for row in rows])


@ai_review_bp.get('/tasks/<job>')
def task(job):
    return jsonify(store().job(job, public=True, evidence=request.args.get('evidence', '1') != '0'))


@ai_review_bp.get('/tokenizers')
def tokenizers():
    from ai_review.token_budget import catalog
    return jsonify(catalog(store().config()))


@ai_review_bp.get('/tasks/<job>/plan')
def task_plan(job):
    from ai_review.batch_store import get
    return jsonify(plan=get(store(), job))


def validate_plan_version(state, job):
    from ai_review.gitea import assert_version
    task = state.job(job)
    repo = state.repo(task['repository'])
    client = Gitea(repo['gitea_url'], state.unseal(repo['bot_token']), repo['repo_name'], repo['repo_id'])
    try:
        if not task.get('snapshot'):
            raise ReviewError('任务尚未生成计划')
        assert_version(client, task['number'], task['snapshot'])
    finally:
        client.close()


@ai_review_bp.post('/tasks/<job>/confirm-plan')
def confirm_plan(job):
    from ai_review.batch_store import control
    from access.scopes import actor_name
    state = store()
    validate_plan_version(state, job)
    control(state, job, body(), actor_name(), confirm=True)
    return jsonify(state.job(job, public=True))


@ai_review_bp.post('/tasks/<job>/continue')
def continue_plan(job):
    from ai_review.batch_store import control
    from access.scopes import actor_name
    state = store()
    validate_plan_version(state, job)
    control(state, job, body(), actor_name())
    return jsonify(state.job(job, public=True))


@ai_review_bp.post('/tasks/<job>/cancel')
def cancel_task(job):
    from ai_review.cancellation import request_cancel
    from access.scopes import actor_name
    state = store()
    code = request_cancel(state, job, actor_name())
    if code == 404:
        return jsonify(error='任务不存在或无权访问'), 404
    if code == 409:
        return jsonify(error='任务已进入发布阶段或已经结束，不能取消', task=state.job(job, public=True, evidence=False)), 409
    return jsonify(ok=True, task=state.job(job, public=True, evidence=False)), code


@ai_review_bp.route('/tasks/<job>/raw-diagnostics', methods=['GET', 'HEAD'])
def raw_diagnostics_download(job):
    state=store();task = state.job(job, public=True, evidence=False)
    value = task.get('raw_diagnostics')
    if not isinstance(value, dict):
        return jsonify(error='该历史任务未记录原始诊断'), 404
    if (value.get('status') == 'expired' or
            isinstance(value.get('retention_expires_at'),(int,float))
            and value['retention_expires_at'] <= time.time()):
        return jsonify(error='原始诊断已超过保留期限'), 410
    if not value.get('available'):
        code = 409 if value.get('status') in ('capturing','ready','archiving') else 404
        return jsonify(error='原始诊断尚未可用' if code == 409 else '该任务没有可下载的原始诊断'), code
    lock=state.lock('原始诊断下载',blocking=False)
    try:
        lock.__enter__()
    except ReviewError:
        return jsonify(error='已有原始诊断正在下载，请稍后重试'),429
    try:
        if request.method == 'HEAD':
            from ai_review.raw_diagnostics import worker_head
            response=make_response(worker_head(task))
        else:
            from ai_review.raw_diagnostics import worker_download
            response=make_response(worker_download(task))
        response.call_on_close(lambda: lock.__exit__(None,None,None))
        return response
    except BaseException:
        lock.__exit__(None,None,None)
        raise


def event_cursor(name):
    value = request.args.get(name)
    if value is None:
        return None
    if not value.isdigit() or int(value) > 2**63-1:
        raise ReviewError(f'{name} 需要非负整数')
    return int(value)


def event_filters():
    raw = request.args.get('type', '')
    kinds = [value for value in raw.split(',') if value]
    if len(kinds) > 20 or any(not re.fullmatch(r'[A-Za-z0-9_.:-]{1,180}', value) for value in kinds):
        raise ReviewError('事件类型过滤无效')
    query = request.args.get('q', '')
    if len(query) > 200:
        raise ReviewError('日志搜索文本超过 200 字符')
    return kinds, query


@ai_review_bp.get('/tasks/<job>/events')
def task_events(job):
    from ai_review.session_events import query
    raw_limit = request.args.get('limit', '200')
    if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 500:
        raise ReviewError('limit 需要 1 至 500')
    kinds, text = event_filters()
    return jsonify(query(store(), job, event_cursor('after'), event_cursor('before'), kinds, text, int(raw_limit), session_id=request.args.get('session_id')))


@ai_review_bp.get('/tasks/<job>/events/export')
def task_events_export(job):
    from ai_review.session_events import iter_export
    kinds, text = event_filters()
    response = Response(stream_with_context(iter_export(store(), job, kinds, text, session_id=request.args.get('session_id'))), mimetype='application/x-ndjson')
    response.headers['Content-Disposition'] = f'attachment; filename="ai-review-events-{job}.jsonl"'
    return response


@ai_review_bp.get('/tasks/<job>/units')
def task_units(job):
    from ai_review.review_state import units
    state = store(); task = state.job(job, public=True, evidence=False)
    rows = units(state, job, positive(request.args.get('page', '1')))
    return jsonify(items=[{'id': r['id'], 'position': r['position'], 'state': r['state'],
                           'path': r['input']['path'], 'scope': r['input']['scope'],
                           'context_gaps': r['input'].get('context_gaps', []), 'error': r['error']} for r in rows],
                   total=task['plan_units'])


@ai_review_bp.get('/tasks/<job>/context-reads')
def task_context_reads(job):
    import contextlib
    import json
    state=store();state.job(job,public=True,evidence=False)
    page=positive(request.args.get('page','1'))
    offset=positive(request.args['start'])-1 if 'start' in request.args else (page-1)*20
    with contextlib.closing(state.connect()) as c:
        total=c.execute('SELECT count(*) FROM review_reads WHERE job=?',(job,)).fetchone()[0]
        rows=c.execute('''SELECT r.unit,r.value FROM review_reads r JOIN review_units u ON u.job=r.job AND u.id=r.unit
                          WHERE r.job=? ORDER BY u.position,r.seq,r.position LIMIT 20 OFFSET ?''',(job,offset)).fetchall()
    return jsonify(items=[dict(json.loads(row['value']),unit=row['unit']) for row in rows],total=total)


@ai_review_bp.post('/repositories/<int:repository>/rerun')
def rerun(repository):
    state = store(); number = positive(body().get('number'))
    repo = state.repo(repository)
    if not state.config()['enabled'] or not repo['enabled']:
        raise ReviewError('请先启用全局和该仓库的 AI Review')
    # 人工重审也先固定真实 head/base。若仅写 manual 占位，后台
    # reconcile 读到稳定 PR 版本后会把刚入队的人工任务误判为旧代。
    target = server(repo['server_id'])
    client = Gitea(target.gitea_url, state.unseal(repo['bot_token']), repo['repo_name'], repo['repo_id'])
    try:
        from ai_review.gitea import version_of
        version = version_of(client.pr(number))
        from access.scopes import actor_snapshot, current_binding, check_task_actor
        actor = actor_snapshot(current_binding())
        check_task_actor(actor, repo['server_id'], repo['repo_id'])
        job = state.enqueue(repository, number, {'head': version['head'], 'base': version['base']}, manual=True, access_actor=actor)
    finally:
        client.close()
    return jsonify(job=job), 202


@ai_review_bp.post('/tasks/<job>/reconcile')
def reconcile(job):
    state = store()
    data = body()
    if data.get('confirm_unpublished') is True:
        from ai_review.worker import confirm_unpublished
        from access.scopes import actor_name
        confirm_unpublished(state, job, actor_name())
        return jsonify(ok=True, task=state.job(job, public=True))
    if data.get('confirm_unpublished') is not None and data['confirm_unpublished'] is not False:
        raise ReviewError('确认字段需要布尔值')
    from ai_review.attention import request_reconciliation
    from access.scopes import actor_name
    requested = request_reconciliation(state, job, actor_name())
    return jsonify(ok=True, requested=requested, task=state.job(job, public=True)), 202



@ai_review_bp.post('/webhooks/<int:repository>')
def webhook(repository):
    raw = request.stream.read(1024*1024+1)
    if len(raw) > 1024*1024:
        return jsonify(error='事件超过 1 MB'), 413
    state = store(); repo = state.repo(repository)
    supplied = request.headers.get('X-Gitea-Signature', '')
    expected = hmac.new(state.unseal(repo['webhook_secret']).encode(), raw, hashlib.sha256).hexdigest()
    if not re.fullmatch(r'[0-9a-f]{64}', supplied) or not hmac.compare_digest(expected, supplied):
        return jsonify(error='事件签名无效'), 403
    # 只记录已通过 HMAC 和仓库身份校验的投递，不把测试事件转成审查任务。
    try:
        payload = json.loads(raw)
        if payload.get('repository', {}).get('id') == repo['repo_id'] and payload['repository'].get('full_name') == repo['repo_name']:
            with state.tx() as c:
                c.execute('INSERT INTO setup_hook_receipts VALUES (?,?) ON CONFLICT(repository) DO UPDATE SET received=excluded.received', (repository, time.time()))
    except (ValueError, AttributeError, sqlite3.OperationalError):
        pass
    event=request.headers.get('X-Gitea-Event-Type') or request.headers.get('X-Gitea-Event','')
    if event=='pull_request_sync':event='pull_request'
    if event not in ('pull_request','issue_comment','pull_request_comment','pull_request_review_comment','pull_request_review_approved','pull_request_review_rejected'):
        return jsonify(ignored=True), 202
    delivery = request.headers.get('X-Gitea-Delivery', '')
    if not re.fullmatch(r'[A-Za-z0-9-]{1,100}', delivery):
        raise ReviewError('缺少有效事件投递标识')
    try:
        value = json.loads(raw)
        if value['repository']['id'] != repo['repo_id'] or value['repository']['full_name'] != repo['repo_name']:
            raise ReviewError('事件仓库与配置不一致')
        # Gitea API生成的URL随入口变化。仅从通过HMAC的真实PR事件登记浏览器入口，
        # 该集合只用于评论链接比对，绝不用作HTTP请求目标。
        observed=value.get('pull_request') or {}
        observed_url=urlsplit(observed.get('html_url',''))
        suffix='/'+repo['repo_name']+'/pulls/'+str(observed.get('number',''))
        if (observed.get('base',{}).get('repo',{}).get('id')==repo['repo_id'] and
                observed_url.scheme in ('http','https') and observed_url.netloc and not observed_url.username and
                not observed_url.query and not observed_url.fragment and unquote(observed_url.path).endswith(suffix)):
            entry=observed_url.scheme+'://'+observed_url.netloc+observed_url.path.rsplit('/pulls/',1)[0]
            from ai_review.dataset import observe_origin
            observe_origin(state,repository,entry)
        action = value.get('action')
        if event in ('issue_comment','pull_request_comment') or action=='reviewed':
            from ai_review.dataset import enqueue
            if not value.get('pull_request') or value['pull_request']['base']['repo']['id']!=repo['repo_id']:
                return jsonify(ignored=True),202
            if state.config()['enabled'] and repo['enabled']:
                enqueue(state,repo,delivery,value,event)
            return jsonify(accepted=True),202
        if action not in ('opened', 'synchronized', 'reopened', 'closed', 'edited'):
            return jsonify(ignored=True), 202
        pr = value['pull_request']
        number = positive(pr['number'])
        if pr['base']['repo']['id'] != repo['repo_id']:
            raise ReviewError('事件 PR 不属于目标仓库')
        signature = {'head': valid_sha(pr['head']['sha']), 'base': valid_sha(pr['base']['sha'])}
        job = state.enqueue(repository, number, signature, delivery=delivery, closed=action == 'closed')
        return jsonify(accepted=True, job=job), 202
    except (ValueError, TypeError, KeyError):
        raise ReviewError('事件内容格式错误') from None


@ai_review_bp.get('/attention')
def attention():
    from ai_review.attention import listing
    from access.organization_api import review_job_ids
    state = store()
    return jsonify(listing(state, positive(request.args.get('page', '1')),
        positive(request.args['repository']) if request.args.get('repository') else None,
        positive(request.args['number']) if request.args.get('number') else None,
        allowed_jobs=review_job_ids(state)))
