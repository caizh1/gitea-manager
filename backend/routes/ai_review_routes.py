"""AI Review 管理接口与签名 Webhook；Web 请求不执行代码审查。"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit, unquote

from flask import Blueprint, current_app, jsonify, request, session, send_file, g
from flask_login import current_user
import requests

from models import db, GiteaServer
from ai_review.common import ReviewError, endpoint, valid_sha
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
        if not expected or not hmac.compare_digest(expected, request.headers.get('X-Review-CSRF', '')):
            return jsonify(error='安全令牌无效，请刷新页面'), 403
        # 请求只读核对只修改任务队列，可彼此并发；仍与配置变更/自动接入互斥。
        payload = request.get_json(silent=True) if request.endpoint == 'ai_review.reconcile' else None
        shared = isinstance(payload, dict) and payload.get('confirm_unpublished') is not True
        lock = store().lock('自动接入执行', blocking=False, shared=shared)
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
    return jsonify(error=str(exc)), 400


@ai_review_bp.errorhandler(sqlite3.Error)
def db_error(_):
    return jsonify(error='Review 存储暂时不可用，请稍后重试'), 503


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
    from ai_review.host_policy import path, approve
    if path(): approve({'AI_REVIEW_GITEA_HOSTS': [target.gitea_url]})
    return target


@ai_review_bp.get('/csrf')
def csrf():
    session.setdefault('ai_review_csrf', secrets.token_urlsafe(32))
    return jsonify(token=session['ai_review_csrf'])


@ai_review_bp.get('/config')
def config():
    state = store()
    return jsonify(config=state.config(public=True), health=state.health_status(), supported_version='1.26.4')


@ai_review_bp.put('/config')
def save_config():
    state, data = store(), body()
    allowed = {'revision', 'enabled', 'engine', 'api_base_url', 'model', 'api_key',
               'debounce_seconds', 'max_files', 'max_calls', 'task_timeout_seconds', 'max_findings'}
    if set(data)-allowed:
        raise ReviewError('配置包含不支持的字段')
    old = state.config()
    value = dict(old, **data)
    if not isinstance(value['enabled'], bool) or value['engine'] not in ('opencode_cli', 'new_api_http'):
        raise ReviewError('开关或执行引擎无效')
    for key, low, high in [('debounce_seconds', 0, 120), ('max_files', 1, 100), ('max_calls', 2, 64),
                           ('task_timeout_seconds', 60, 3600), ('max_findings', 1, 30)]:
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ReviewError(f'{key} 超出允许范围 {low} 至 {high}')
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
    state = store(); config = state.config()
    base = endpoint(config['api_base_url'], 'AI_REVIEW_MODEL_HOSTS')
    session_http = requests.Session(); session_http.trust_env = False
    try:
        response = session_http.get(base+'/models', headers={'Authorization': 'Bearer '+state.unseal(config['api_key'])},
                                    timeout=(5, 10), allow_redirects=False)
        if response.status_code != 200 or len(response.content) > 1024*1024:
            raise ReviewError(f'模型目录请求失败：HTTP {response.status_code}')
        models = [m['id'] for m in response.json().get('data', []) if isinstance(m, dict) and isinstance(m.get('id'), str)]
        return jsonify(ok=config['model'] in models, model_available=config['model'] in models,
                       message='所选模型可用' if config['model'] in models else '服务可达，但模型目录中没有所选模型', models=models[:200])
    except (requests.RequestException, ValueError):
        raise ReviewError('模型目录连接失败或响应格式错误') from None
    finally:
        session_http.close()


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
    return jsonify(store().repos(public=True))


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
        return target.id, repository['id'], {
            'repo_name': name, 'server_name': target.name, 'gitea_url': target.gitea_url,'gitea_version':gitea_version,
            'bot_token': state.seal(token), 'bot_id': user['id'], 'bot_name': user['login'],
            'webhook_secret': old['webhook_secret'] if old else state.seal(secrets.token_urlsafe(32)),
            'enabled': data.get('enabled', False), 'hook_status': old.get('hook_status', '未安装') if old else '未安装',
            **({'revision': data.get('revision')} if old else {})}
    finally:
        client.close()


@ai_review_bp.post('/repositories')
def create_repository():
    state = store(); target, repo_id, data = repository_values(state, body())
    return jsonify(state.save_repo(target, repo_id, data)), 201


@ai_review_bp.put('/repositories/<int:repository>')
def update_repository(repository):
    state = store(); old = state.repo(repository)
    target, repo_id, data = repository_values(state, body(), old)
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
    if target.gitea_url.rstrip('/') != repo['gitea_url'].rstrip('/'):
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
    return jsonify(job=public_job(state), automatic_available=bool(__import__('os').environ.get('AI_REVIEW_HOST_POLICY_FILE')))


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
    from ai_review.setup_service import public_job, start
    state = store()
    job = public_job(state, ident)
    if not job: return jsonify(error='接入任务不存在'), 404
    with state.tx() as c:
        if c.execute("SELECT id FROM setup_jobs WHERE status IN ('pending','running') AND id!=?", (ident,)).fetchone():
            raise ReviewError('已有接入任务正在执行')
        c.execute("UPDATE setup_jobs SET status='pending',updated=? WHERE id=? AND status IN ('failed','partial')", (time.time(), ident))
    start(current_app._get_current_object())
    return jsonify(job=public_job(state, ident)), 202


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
    return jsonify(store().jobs(positive(request.args.get('page', '1')),
        positive(request.args['repository']) if request.args.get('repository') else None,
        positive(request.args['number']) if request.args.get('number') else None, request.args.get('status')))


@ai_review_bp.get('/attention')
def attention():
    from ai_review.attention import listing
    return jsonify(listing(store(), positive(request.args.get('page', '1')),
        positive(request.args['repository']) if request.args.get('repository') else None,
        positive(request.args['number']) if request.args.get('number') else None))


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
    return jsonify(store().job(job, public=True))


@ai_review_bp.post('/repositories/<int:repository>/rerun')
def rerun(repository):
    state = store(); number = positive(body().get('number'))
    if not state.config()['enabled'] or not state.repo(repository)['enabled']:
        raise ReviewError('请先启用全局和该仓库的 AI Review')
    job = state.enqueue(repository, number, {'manual': secrets.token_hex(8)}, manual=True)
    return jsonify(job=job), 202


@ai_review_bp.post('/tasks/<job>/reconcile')
def reconcile(job):
    state = store()
    data = body()
    if data.get('confirm_unpublished') is True:
        from ai_review.worker import confirm_unpublished
        confirm_unpublished(state, job)
        return jsonify(ok=True, task=state.job(job, public=True))
    if data.get('confirm_unpublished') is not None and data['confirm_unpublished'] is not False:
        raise ReviewError('确认字段需要布尔值')
    from ai_review.attention import request_reconciliation
    requested = request_reconciliation(state, job)
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
