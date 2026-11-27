"""整仓扫描浏览器 API，统一校验仓库权限与报告下载。"""
import contextlib
import json
import re
import time
from uuid import uuid4

import requests
from flask import Blueprint, current_app, jsonify, request, send_file

from access.errors import AccessError
from access.organization_api import review_repository_ids
from access.scopes import actor_snapshot, current_binding, require_resource, server_epoch
from ai_review.authorization import guarded_client
from ai_review.common import ReviewError, endpoint, validate_model_id
from ai_review.ocr_store import ACTIVE, FORMATS, ScanStore, event, occurrences, recurrence, update
from ai_review.ocr_worker import NATIVE_FILES, forward
from models import GiteaServer, db

ocr_scan_bp = Blueprint('ocr_scan', __name__)


def store():
    return ScanStore(current_app.config.get('AI_REVIEW_DB'))


@ocr_scan_bp.errorhandler(ReviewError)
def failed(exc):
    return jsonify(error=str(exc)), 400


def payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ReviewError('扫描配置需要 JSON 对象')
    return data


def repository(state, ident, enabled=True):
    allowed = review_repository_ids()
    if allowed is not None and ident not in allowed:
        raise AccessError('资源不存在或无权访问', 404)
    result = state.repo(ident)
    if current_binding() is not None:
        require_resource('ai_repository', ident)
    if result.get('access_archived') or (enabled and not result['enabled']):
        raise AccessError('扫描仓库已停用或归属已失效', 404)
    return result


def task_for(state, ident):
    if not re.fullmatch(r'[a-f0-9]{32}', ident):
        raise AccessError('任务不存在', 404)
    task = state.target(ident)
    repo = repository(state, task['repository'])
    instance = db.session.get(GiteaServer, repo['server_id'])
    if not instance or task['specification']['epochs'].get(str(repo['server_id'])) != server_epoch(instance):
        raise AccessError('任务的源实例已更换', 404)
    return task, repo


def public_task(task, repo, detail=False):
    result = {key: task[key] for key in ('id', 'branch', 'sha', 'batch', 'state', 'created', 'updated', 'current',
              'error', 'http', 'retry_at', 'retries', 'tokens', 'reused', 'plan', 'stop_actor', 'stopped_at')}
    result.update(repository=repo['id'], repository_name=repo['repo_name'], server_id=repo['server_id'],
                  total=task['preview'].get('reviewable_count'), excluded=task['preview'].get('excluded_count', 0),
                  completed=len(task['progress'].get('files', {})), findings=task['progress'].get('findings', 0),
                  failures=len(task['progress'].get('failures', [])), model=task['config'].get('model', ''),
                  formats=task['specification']['formats'])
    if detail:
        result['scope'] = task['preview'].get('files', [])
        result['checkpoints'] = task['progress'].get('files', {})
        result['uncompleted'] = task['progress'].get('failures', [])
    return result


def specification(state, data):
    targets = data.get('targets')
    if not isinstance(targets, list) or not 1 <= len(targets) <= 50:
        raise ReviewError('请选择 1 至 50 个仓库分支')
    result = []
    seen = set()
    epochs = {}
    for value in targets:
        if not isinstance(value, dict) or type(value.get('repository')) is not int:
            raise ReviewError('仓库标识无效')
        ident, branch = value['repository'], value.get('branch')
        if (not isinstance(branch, str) or not 1 <= len(branch) <= 250
                or branch.startswith(('-', '/')) or branch.endswith(('/', '.')) or '..' in branch
                or any(ch.isspace() or ord(ch) < 32 or ch in '\\~^`:?*[' for ch in branch)):
            raise ReviewError('分支名称无效')
        if (ident, branch) in seen:
            continue
        seen.add((ident, branch))
        repo = repository(state, ident)
        instance = db.session.get(GiteaServer, repo['server_id'])
        if not instance:
            raise ReviewError('源实例不存在')
        epochs[str(repo['server_id'])] = server_epoch(instance)
        result.append({'repository': ident, 'branch': branch})
    formats = data.get('formats', ['html'])
    mode = data.get('mode', 'inherit')
    if (not isinstance(formats, list) or any(value not in FORMATS for value in formats) or not formats
            or mode not in ('inherit', 'custom')):
        raise ReviewError('报告格式或模型配置无效')
    return {'targets': result, 'epochs': epochs, 'formats': list(dict.fromkeys(formats)), 'mode': mode}


@ocr_scan_bp.get('/repositories/<int:ident>/branches')
def branches(ident):
    state = store()
    repo = repository(state, ident)
    with contextlib.closing(guarded_client(state, repo)) as client:
        details = client.repository()
        results = []
        for page in range(1, 51):
            rows = client.request('GET', client.prefix + '/branches', params={'page': page, 'limit': 50})
            results.extend(row['name'] for row in rows)
            if len(rows) < 50:
                return jsonify(default_branch=details.get('default_branch'), branches=results)
        raise ReviewError('仓库分支超过可枚举范围')


@ocr_scan_bp.get('/profiles')
def profiles():
    state = store()
    custom = state.model('custom')
    inherited = state.model('inherit')
    return jsonify(custom={'model': custom['model'], 'api_base_url': custom['api_base_url'],
                           'configured': bool(custom['api_key'])},
                   inherited={'model': inherited['model'], 'configured': bool(inherited['api_key'])})


@ocr_scan_bp.put('/profiles')
def save_profile():
    state, data = store(), payload()
    old = state.profile()
    from ai_review.host_policy import address as validate_address, approve, path

    address, _ = validate_address(data.get('api_base_url'))
    model = validate_model_id(data.get('model'))
    supplied = data.get('api_key')
    if not supplied and old.get('api_base_url') != address:
        raise ReviewError('更换端点时需要输入该端点的密钥')
    key = state.seal(supplied) if supplied else old.get('api_key')
    if not key:
        raise ReviewError('请输入模型 API Key')
    if path():
        approve({'AI_REVIEW_MODEL_HOSTS': [address]})
    address = endpoint(address, 'AI_REVIEW_MODEL_HOSTS')
    with state.tx() as c:
        c.execute('INSERT OR REPLACE INTO ocr_profiles VALUES (1,?)',
                  (json.dumps({'api_base_url': address, 'model': model, 'api_key': key}, ensure_ascii=False),))
    return profiles()


@ocr_scan_bp.post('/profiles/test')
def test_profile():
    state = store()
    mode = payload().get('mode')
    if mode not in ('inherit', 'custom'):
        raise ReviewError('扫描模型配置无效')
    config = state.model(mode)
    if not config['api_key']:
        raise ReviewError('请先保存模型配置')
    probe = {'messages': [{'role': 'user', 'content': '调用 manager_probe 工具。'}],
             'tools': [{'type': 'function', 'function': {'name': 'manager_probe', 'description': '检查工具调用',
                        'parameters': {'type': 'object', 'properties': {}}}}], 'tool_choice': 'required', 'max_tokens': 256}
    started = time.monotonic()
    try:
        status, raw, _ = forward(config, state.unseal(config['api_key']), probe)
        if status != 200:
            raise ReviewError(f'模型连接失败（HTTP {status}）')
        calls = json.loads(raw)['choices'][0]['message']['tool_calls']
        if not any(row['function']['name'] == 'manager_probe' for row in calls):
            raise ValueError()
    except requests.RequestException:
        raise ReviewError('无法连接模型服务') from None
    except (KeyError, TypeError, ValueError, IndexError):
        raise ReviewError('模型未返回所要求的工具调用') from None
    return jsonify(ok=True, model=config['model'], milliseconds=round((time.monotonic() - started)*1000))


@ocr_scan_bp.post('/previews')
def start_preview():
    state = store()
    spec = specification(state, payload())
    actor = actor_snapshot(current_binding())
    with state.tx() as c:
        batch = state.create(c, spec, actor)
    return jsonify(batch=batch), 202


@ocr_scan_bp.get('/batches/<batch>')
def batch_detail(batch):
    state = store()
    with contextlib.closing(state.connect()) as c:
        ids = [row[0] for row in c.execute('SELECT id FROM ocr_targets WHERE batch=? ORDER BY rowid', (batch,))]
    items = []
    for ident in ids:
        try:
            task, repo = task_for(state, ident)
        except AccessError:
            continue
        items.append(public_task(task, repo, detail=True))
    if not items:
        raise AccessError('批次不存在或无权访问', 404)
    return jsonify(batch=batch, items=items, now=time.time())


@ocr_scan_bp.post('/batches/<batch>/start')
def start_batch(batch):
    state = store()
    with contextlib.closing(state.connect()) as c:
        ids = [row[0] for row in c.execute('SELECT id FROM ocr_targets WHERE batch=? ORDER BY rowid', (batch,))]
    if not ids:
        raise AccessError('批次不存在或无权访问', 404)
    tasks = []
    for ident in ids:
        try:
            tasks.append(task_for(state, ident)[0])
        except AccessError:
            continue
    if not tasks:
        raise AccessError('批次不存在或无权访问', 404)
    if any(task['state'] not in ('previewed', 'attention') for task in tasks):
        raise ReviewError('请等待预览完成')
    if not any(task['state'] == 'previewed' for task in tasks):
        raise ReviewError('没有可扫描的目标')
    with state.tx() as c:
        for task in tasks:
            if state.target(task['id'], c)['state'] == 'previewed':
                update(c, task['id'], state='queued')
                event(c, task['id'], '扫描入队')
    return jsonify(batch=batch), 202


@ocr_scan_bp.get('/tasks')
def tasks():
    state = store()
    limit = max(1, min(request.args.get('limit', 50, type=int), 100))
    cursor = request.args.get('before', time.time() + 1, type=float)
    allowed = review_repository_ids()
    with contextlib.closing(state.connect()) as c:
        ids = [row[0] for row in c.execute('''SELECT id FROM ocr_targets WHERE created<?
           AND (? IS NULL OR repository IN (SELECT value FROM json_each(?))) ORDER BY created DESC,rowid DESC LIMIT ?''',
              (cursor, None if allowed is None else 1, json.dumps(allowed), limit))]
    items = []
    for ident in ids:
        try:
            task, repo = task_for(state, ident)
        except AccessError:
            continue
        items.append(public_task(task, repo))
    return jsonify(items=items, now=time.time())


@ocr_scan_bp.get('/tasks/<ident>')
def task_detail(ident):
    state = store()
    task, repo = task_for(state, ident)
    cursor = max(0, request.args.get('after', 0, type=int))
    with contextlib.closing(state.connect()) as c:
        rows = [dict(row) for row in c.execute('SELECT * FROM ocr_events WHERE target=? AND id>? ORDER BY id LIMIT 200',
                                              (ident, cursor))]
    for row in rows:
        row['detail'] = json.loads(row['detail'])
    return jsonify(task=public_task(task, repo, detail=True), events=rows, now=time.time())


@ocr_scan_bp.post('/tasks/<ident>/stop')
def stop_task(ident):
    state = store()
    task_for(state, ident)
    if payload().get('confirm') is not True:
        raise ReviewError('请确认终止扫描并清除本任务内容')
    with state.tx() as c:
        task = state.target(ident, c)
        if task['state'] in (*ACTIVE, 'attention', 'previewed'):
            update(c, ident, state='stopping', broker=None, retry_at=0, stop_actor=actor_snapshot(current_binding()).get('name'))
            event(c, ident, '用户确认终止')
        elif task['state'] != 'stopped':
            raise ReviewError('扫描已结束')
    return jsonify(ok=True), 202


@ocr_scan_bp.post('/tasks/<ident>/resume')
def resume_task(ident):
    state = store()
    task_for(state, ident)
    with state.tx() as c:
        task = state.target(ident, c)
        if task['state'] != 'attention' or not task['preview']:
            raise ReviewError('此任务当前无法恢复')
        current = state.model(task['specification']['mode'], c)
        old = task['config']
        if any(current.get(key) != old.get(key) for key in ('api_base_url', 'model', 'thinking_protocol', 'thinking_enabled')):
            raise ReviewError('模型或端点已变化，请新建扫描')
        update(c, ident, config=current, state='queued', ready=0, broker=None, error='', retry_at=0, http=0)
        event(c, ident, '人工处理后续扫')
    return jsonify(ok=True), 202


@ocr_scan_bp.get('/tasks/<ident>/report')
def report(ident):
    state = store()
    task, _ = task_for(state, ident)
    if task['state'] != 'complete':
        raise ReviewError('报告尚未生成')
    return jsonify(json.loads((state.report_dir(ident) / 'report-view.json').read_bytes()))


@ocr_scan_bp.get('/tasks/<ident>/download/<fmt>')
def download(ident, fmt):
    state = store()
    task, _ = task_for(state, ident)
    if task['state'] != 'complete' or fmt not in FORMATS:
        raise ReviewError('报告或导出格式不可用')
    name = NATIVE_FILES.get(fmt, f'report.{fmt}')
    file = state.report_dir(ident) / name
    if not file.is_file():
        raise ReviewError('此任务未生成所选导出格式')
    response = send_file(file, as_attachment=True, download_name=f'ocr-{task["sha"][:12]}-{name}', conditional=False)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def plan_projection(row):
    rule = json.loads(row['recurrence'])
    spec = json.loads(row['specification'])
    spec.pop('budget', None)
    return {'id': row['id'], 'enabled': bool(row['enabled']), 'specification': spec,
            'recurrence': rule, 'next_runs': occurrences(rule, time.time(), 3)}


@ocr_scan_bp.get('/plans')
def plans():
    state = store()
    allowed = review_repository_ids()
    actor = actor_snapshot(current_binding())
    with contextlib.closing(state.connect()) as c:
        rows = [dict(row) for row in c.execute('SELECT * FROM ocr_plans ORDER BY updated DESC')]
    items = []
    for row in rows:
        public = plan_projection(row)
        owner = json.loads(row['actor'])
        if allowed is None or (owner['binding_id'] == actor['binding_id'] and
                               all(t['repository'] in allowed for t in public['specification']['targets'])):
            items.append(public)
    return jsonify(items=items)


@ocr_scan_bp.post('/plans/preview')
def plan_times():
    return jsonify(next_runs=occurrences(recurrence(payload()), time.time(), 3))


@ocr_scan_bp.post('/plans')
def save_plan():
    state, data = store(), payload()
    spec = specification(state, data)
    rule = recurrence(data.get('recurrence'))
    ident = uuid4().hex
    actor = actor_snapshot(current_binding())
    now = time.time()
    with state.tx() as c:
        c.execute('INSERT INTO ocr_plans VALUES (?,?,?,?,?,?,?)',
                  (ident, json.dumps(actor), json.dumps(spec), json.dumps(rule), occurrences(rule, now)[0], 1, now))
    return jsonify(id=ident), 201


@ocr_scan_bp.patch('/plans/<ident>')
def set_plan(ident):
    state, data = store(), payload()
    if type(data.get('enabled')) is not bool:
        raise ReviewError('请指定定时计划状态')
    if not any(row['id'] == ident for row in plans().get_json()['items']):
        raise AccessError('计划不存在或无权访问', 404)
    with state.tx() as c:
        row = c.execute('SELECT * FROM ocr_plans WHERE id=?', (ident,)).fetchone()
        rule = json.loads(row['recurrence'])
        now = time.time()
        c.execute('UPDATE ocr_plans SET enabled=?,next_at=?,updated=? WHERE id=?',
                  (int(data['enabled']), occurrences(rule, now)[0], now, ident))
    return jsonify(ok=True)
