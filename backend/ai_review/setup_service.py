"""持久化的一次接入任务；共享管理员确认后自动准备受限机器人与事件链路。"""
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import quote, urlsplit
import zipfile

import requests

from .common import ReviewError, encode, endpoint, MODEL_CONNECT_TIMEOUT
from .gitea import Gitea
from .host_policy import address, approve, ensure, gitea_address
from .store import Store
from .setup_transport import AccessError, ConnectionPool, ContainerAccess


def initialize(store):
    # 表结构由 Store 在进程第一次连接时迁移；查询路径不重复申请写锁。
    return None


def resource(store, key, value=None):
    with store.tx() as c:
        if value is not None:
            c.execute('INSERT INTO setup_resources VALUES (?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value', (key, encode(value)))
        row = c.execute('SELECT value FROM setup_resources WHERE id=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None


def public_job(store, ident=None, server_id=None):
    with contextlib.closing(store.connect()) as c:
        where, args = (' WHERE id=?', [ident]) if ident else ('', [])
        if server_id is not None and not ident:
            where, args = (" WHERE COALESCE(json_extract(value,'$.server_id'),json_extract(value,'$.repositories[0].server_id'))=?", [server_id])
        row = c.execute('SELECT * FROM setup_jobs' + where + ' ORDER BY created DESC,id DESC LIMIT 1', args).fetchone()
    if not row:
        return None
    value = json.loads(row['value'])
    targets = value['repositories']
    return dict(id=row['id'], status=row['status'], detail=json.loads(row['detail']), created=row['created'], updated=row['updated'],
                server_id=value.get('server_id', targets[0]['server_id']), server_name=value.get('server_name', ''),
                repositories=[{k: r[k] for k in ('server_id','repo_id','name')} for r in targets],
                model=value['model'], config_revision=value['config_revision'])


def resume(store, ident, data):
    job = public_job(store, ident)
    if not job:
        raise ReviewError('接入任务不存在')
    if data.get('server_id') != job['server_id']:
        raise ReviewError('继续检查必须对应原任务的 Gitea 实例，请刷新并核对目标')
    config = store.config()
    if data.get('config_revision') != config['revision']:
        raise ReviewError('模型配置已变化，请刷新后继续检查')
    with store.tx() as c:
        row = c.execute('SELECT * FROM setup_jobs WHERE id=?', (ident,)).fetchone()
        if row['status'] not in ('failed', 'partial'):
            raise ReviewError('该任务正在运行或已经完成，请刷新查看')
        if c.execute("SELECT id FROM setup_jobs WHERE status IN ('pending','running') AND id!=?", (ident,)).fetchone():
            raise ReviewError('已有接入任务正在执行')
        value = json.loads(row['value']); previous = json.loads(row['detail'])
        if {r['server_id'] for r in value['repositories']} != {job['server_id']}:
            raise ReviewError('旧任务包含多个实例，请重新选择单个实例接入')
        if all(config.get(k) for k in ('api_base_url','api_key','model')):
            value.update({k: config[k] for k in ('api_base_url','api_key','model')})
        value['config_revision'] = config['revision']
        history = previous.pop('history', [])[-9:] + [previous]
        detail = dict(attempt=previous.get('attempt', 1)+1, stage='等待继续检查', history=history, error='', results=[], steps=[])
        c.execute("UPDATE setup_jobs SET status='pending',value=?,detail=?,updated=? WHERE id=?",
                  (encode(value), encode(detail), time.time(), ident))
    return public_job(store, ident)


def update(store, ident, status='running', **detail):
    with store.tx() as c:
        row = c.execute('SELECT detail FROM setup_jobs WHERE id=?', (ident,)).fetchone()
        merged = dict(json.loads(row[0]), **detail)
        c.execute('UPDATE setup_jobs SET status=?,detail=?,updated=? WHERE id=?', (status, encode(merged), time.time(), ident))


def submit(store, data, fallback_url):
    from models import db, GiteaServer
    initialize(store)
    if not isinstance(data, dict) or set(data) - {'api_base_url', 'api_key', 'model', 'repositories', 'manager_url', 'server_id', 'config_revision'}:
        raise ReviewError('初始化输入格式无效')
    old = store.config()
    if data.get('config_revision') != old['revision']:
        raise ReviewError('模型配置已变化，请刷新后提交')
    if type(data.get('server_id')) is not int:
        raise ReviewError('请刷新页面并明确选择一个 Gitea 实例')
    if all(old.get(k) for k in ('api_base_url','api_key','model')):
        data = dict(data, api_base_url=old['api_base_url'], model=old['model'], api_key='')
    api_url = address(data.get('api_base_url') or old['api_base_url'])[0]
    model = data.get('model') or old['model'] or 'deepseek-v4-flash'
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', model):
        raise ReviewError('模型名称格式无效')
    key = data.get('api_key')
    sealed = store.seal(key) if key else old['api_key']
    if not sealed:
        raise ReviewError('首次接入需要填写模型 API Key')
    selected = data.get('repositories')
    if not isinstance(selected, list) or not 1 <= len(selected) <= 50:
        raise ReviewError('请选择 1 至 50 个仓库')
    targets = []; seen = set(); hosts = []
    for row in selected:
        if not isinstance(row, dict) or set(row) != {'server_id', 'repo_id', 'name'} or type(row['server_id']) is not int or type(row['repo_id']) is not int:
            raise ReviewError('仓库选择格式无效')
        if not isinstance(row['name'], str) or len(row['name']) > 300 or len(row['name'].split('/')) != 2:
            raise ReviewError('仓库名称格式无效')
        if row['server_id'] != data['server_id']:
            raise ReviewError('仓库必须全部属于当前选择的 Gitea 实例，未执行接入')
        target = db.session.get(GiteaServer, row['server_id'])
        if not target:
            raise ReviewError('所选 Gitea 服务器已不存在')
        marker = (target.id, row['repo_id'])
        if marker in seen: continue
        seen.add(marker)
        targets.append(dict(row, gitea_url=gitea_address(target.gitea_url)))
        hosts.append(gitea_address(target.gitea_url))
    previous = [r.get('webhook_url', '').split('/api/ai-review/webhooks/')[0] for r in store.repos(public=True)
                if r['server_id'] in {r['server_id'] for r in targets} and r.get('webhook_url')]
    callback = address(data.get('manager_url') or (previous[0] if previous and len(set(previous)) == 1 else fallback_url))[0]
    # 提交按钮即是管理员对这些明确地址的授权；不从 webhook 载荷追加名单。
    approve({'AI_REVIEW_GITEA_HOSTS': hosts, 'AI_REVIEW_MODEL_HOSTS': [api_url], 'AI_REVIEW_WEBHOOK_HOSTS': [callback]})
    from access.scopes import actor_snapshot
    value = dict(server_id=data['server_id'], server_name=target.name, api_base_url=api_url, api_key=sealed, model=model, manager_url=callback, repositories=targets,
                 config_revision=old['revision'], access_actor=actor_snapshot())
    with store.tx() as c:
        active = c.execute("SELECT id FROM setup_jobs WHERE status IN ('pending','running') LIMIT 1").fetchone()
        if active:
            raise ReviewError('已有接入任务正在执行，请先查看进度')
        ident = secrets.token_hex(16); now = time.time()
        c.execute('INSERT INTO setup_jobs VALUES (?,?,?,?,?,?)', (ident, 'pending', encode(value), encode({'stage': '等待接入检查', 'results': []}), now, now))
    return public_job(store, ident)


def verify_model(store, value):
    from .thinking import parameters, description
    from services.diagnostics import redact
    base = endpoint(value['api_base_url'], 'AI_REVIEW_MODEL_HOSTS')
    key = store.unseal(value['api_key'])
    started = time.monotonic()
    phase, path, timeout = '读取模型目录', '/models', 15
    response = None

    def fail(code, message, exc=None):
        details = {
            'diagnostic_id': secrets.token_hex(8),
            'time_utc': datetime.now(timezone.utc).isoformat(),
            'phase': phase, 'request_path': path, 'error_code': code,
            'elapsed_seconds': round(time.monotonic() - started, 3),
            'connect_timeout_seconds': MODEL_CONNECT_TIMEOUT, 'read_timeout_seconds': timeout,
            'http_status': response.status_code if response is not None else None,
            'exception_type': type(exc).__name__ if exc else '',
            'response_bytes': len(response.content) if response is not None else 0,
        }
        # 先移除本次凭据再截断，避免截断后泄漏密钥前缀。
        if response is not None:
            text = response.text.replace(key, '[已脱敏]') if key else response.text
            text = redact(text)
            details['response_summary'] = text[:2000] + ('\n[摘要已截断]' if len(text) > 2000 else '')
        else:
            details['response_summary'] = '未收到完整 HTTP 响应'
        logging.warning('AI 模型连接测试失败 %s', encode(details))
        error = ReviewError(message)
        error.connection_diagnostic = details
        raise error from None

    with requests.Session() as session:
        session.trust_env = False
        session.headers['Authorization'] = 'Bearer ' + key
        try:
            response = session.get(base + path, timeout=(MODEL_CONNECT_TIMEOUT, timeout), allow_redirects=False)
            if response.status_code != 200:
                fail('MODEL_HTTP_ERROR', f'模型目录返回 HTTP {response.status_code}，请展开查看详情')
            if len(response.content) > 1024 * 1024:
                fail('MODEL_RESPONSE_TOO_LARGE', '模型目录响应超过 1 MiB')
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                fail('MODEL_SCHEMA_ERROR', '模型目录缺少 data 数组')
            listed = value['model'] in [r.get('id') for r in payload['data'] if isinstance(r, dict)]
            phase, path, timeout, response = '模型结构化调用', '/chat/completions', 90, None
            response = session.post(base + path, json={'model': value['model'], 'messages': [
                {'role': 'user', 'content': '连接验证：仅输出 JSON 对象 {"ok":true}。'}],
                'max_tokens': max(128, value.get('thinking_token_budget', 4096) + 512) if value.get('thinking_enabled') else 128,
                'stream': False, 'response_format': {'type': 'json_object'}, **parameters(value)},
                timeout=(MODEL_CONNECT_TIMEOUT, timeout), allow_redirects=False)
            if response.status_code != 200:
                fail('MODEL_HTTP_ERROR', f'模型实际调用返回 HTTP {response.status_code}，请展开查看详情')
            if len(response.content) > 65536:
                fail('MODEL_RESPONSE_TOO_LARGE', '模型实际调用响应超过 64 KiB')
            choice = response.json()['choices'][0]
            if choice.get('finish_reason') != 'stop':
                fail('MODEL_INCOMPLETE', '模型未正常结束输出，请检查 finish_reason 与输出额度')
            if choice['message'].get('tool_calls'):
                fail('MODEL_UNEXPECTED_TOOLS', '连接测试意外返回工具调用，期望直接输出 JSON')
            phase = '解析模型输出 JSON'
            if json.loads(choice['message']['content']) != {'ok': True}:
                fail('MODEL_RESULT_MISMATCH', '模型输出与连接测试期望的 {"ok":true} 不符')
            return {'listed': listed, 'message': ('模型实际结构化调用通过' if listed else '模型实际调用通过；目录未列出该模型') + '；' + description(value)}
        except requests.exceptions.ConnectTimeout as exc:
            fail('MODEL_CONNECT_TIMEOUT', '建立模型服务连接超时', exc)
        except requests.exceptions.ReadTimeout as exc:
            fail('MODEL_READ_TIMEOUT', f'{phase}等待响应超时（读取上限 {timeout} 秒）', exc)
        except requests.exceptions.SSLError as exc:
            fail('MODEL_TLS_ERROR', '模型服务 TLS 握手或证书校验失败', exc)
        except requests.exceptions.JSONDecodeError as exc:
            fail('MODEL_INVALID_JSON', f'{phase}返回的内容不是合法 JSON', exc)
        except requests.RequestException as exc:
            fail('MODEL_NETWORK_ERROR', f'{phase}网络通信失败', exc)
        except ValueError as exc:
            fail('MODEL_INVALID_JSON', f'{phase}返回的内容不是合法 JSON', exc)
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            fail('MODEL_SCHEMA_ERROR', f'{phase}响应字段缺失或类型错误', exc)


def callback_probe(store, installer, callback):
    token = secrets.token_hex(24)
    with store.tx() as c:
        c.execute('DELETE FROM setup_probes WHERE expires<?', (time.time(),))
        c.execute('INSERT INTO setup_probes VALUES (?,?,0)', (token, time.time() + 90))
    try:
        url = endpoint(callback, 'AI_REVIEW_WEBHOOK_HOSTS') + '/api/ai-review/setup-probe/' + token
        answer = installer.exec(['wget', '-T', '10', '-q', '-O', '-', url]).strip()
        if answer != hashlib.sha256(token.encode()).hexdigest():
            raise ReviewError('回调校验内容不匹配')
        with contextlib.closing(store.connect()) as c:
            if not c.execute('SELECT seen FROM setup_probes WHERE token=?', (token,)).fetchone()[0]:
                raise ReviewError('回调没有到达当前 Manager')
    except AccessError as exc:
        if exc.code != 'REMOTE_COMMAND_FAILED':
            raise
        raise ReviewError('callback_unreachable') from None
    except Exception:
        raise ReviewError('callback_unreachable') from None
    finally:
        with store.tx() as c: c.execute('DELETE FROM setup_probes WHERE token=?', (token,))


def ensure_bot(store, target, installer, admin):
    key = 'bot:' + hashlib.sha256(gitea_address(target.gitea_url).encode()).hexdigest()
    value = resource(store, key)
    if value is None:
        nonce = secrets.token_hex(10)
        value = {'name': 'gm-review-' + nonce, 'marker': 'Gitea Manager 审查机器人 ' + nonce,
                 'phase': 'planned', 'token_name': 'gm-review-' + nonce, 'token': ''}
        resource(store, key, value)
    cli = ['gitea', '--config', installer.config, '--custom-path', installer.custom, 'admin', 'user']
    user = admin.request('GET', '/users/' + quote(value['name']), missing=True)
    if not user:
        if value['phase'] not in ('planned', 'creating'):
            raise ReviewError('已登记的机器人不存在，请核对 Gitea 账号，未重复创建')
        value['phase'] = 'creating'; resource(store, key, value)
        installer.exec(cli + ['create', '--username', value['name'], '--fullname', value['marker'],
            '--email', value['name'] + '@gitea-manager.invalid', '--random-password', '--random-password-length', '32', '--must-change-password=false'])
        user = admin.request('GET', '/users/' + quote(value['name']))
    if user.get('is_admin') or user.get('full_name') != value['marker'] or value.get('id', user['id']) != user['id']:
        raise ReviewError('机器人身份与接入记录不符，未接管已有账号')
    value['id'] = user['id']; value['phase'] = 'created'; resource(store, key, value)
    if not value['token']:
        folder = installer.custom + '/ai-review-manager-secrets'
        token_path = folder + '/' + value['token_name'] + '.token'
        # 原始令牌先写 Gitea 私有持久目录，SSH 中断后回读同一回执，不重发创建命令。
        script = '''umask 077
mkdir -p "$1" && chmod 700 "$1" || exit 70
shift
result="$1"; shift
if [ ! -f "$result" ]; then
  if [ -e "$result.pending" ]; then
    # 命令已留下完整回执时可恢复；其他不确定状态不重复生成令牌。
    [ -f "$result.tmp" ] && [ "$(wc -l < "$result.tmp")" -le 1 ] && grep -Eq '^[0-9a-f]{40}$' "$result.tmp" || exit 74
  else
    : > "$result.pending" || exit 70
    "$@" > "$result.tmp" || exit 74
  fi
  mv "$result.tmp" "$result" || exit 74
fi
cat "$result"
'''
        raw = installer.exec(['sh', '-c', script, '生成受限审查令牌', folder, token_path, *cli,
            'generate-access-token', '--username', value['name'], '--token-name', value['token_name'],
            '--scopes', 'read:user,write:repository,write:issue', '--raw']).strip()
        if not re.fullmatch('[0-9a-f]{40}', raw):
            raise ReviewError('机器人令牌回执待核对，未重复生成')
        value['token'] = store.seal(raw); resource(store, key, value)
        installer.exec(['rm', '-f', token_path, token_path + '.pending', token_path + '.tmp'])
    return value


def verify_delivery(store, admin, path, repository, hook_id, ref):
    hook_path = path + '/hooks/' + str(hook_id)
    original = admin.request('GET', hook_path)['events']
    # Gitea 1.26.4 的测试接口仅发送 push，且仍受事件订阅过滤。
    # 验证期间暂时订阅，结束即恢复；中断后 ensure_webhook 会先恢复正式事件集。
    changed = 'push' not in original
    if changed: admin.request('PATCH', hook_path, {'events': original + ['push']})
    started = time.time()
    try:
        admin.request('POST', hook_path + '/tests', params={'ref': 'refs/heads/' + ref})
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            with contextlib.closing(store.connect()) as c:
                row = c.execute('SELECT received FROM setup_hook_receipts WHERE repository=?', (repository,)).fetchone()
            if row and row[0] >= started: return
            time.sleep(1)
        raise ReviewError('Gitea 未成功投递验证事件，请检查 Gitea Webhook 主机限制与回调地址')
    finally:
        if changed:
            admin.request('PATCH', hook_path, {'events': original})
            if set(admin.request('GET', hook_path)['events']) != set(original):
                raise ReviewError('Webhook 验证后未能恢复正式事件订阅，请继续检查')


def execute(store, job):
    with ConnectionPool() as pool:
        _execute(store, job, pool)


def _execute(store, job, pool):
    from models import db, GiteaServer
    from .assets.install import Installer
    from .progress_assets import build_assets
    from routes.ai_review_routes import repository_values, ensure_webhook
    ident = job['id']; value = json.loads(job['value'])
    update(store, ident, stage='检查内网模型实际调用', needs_callback=False, error='', results=[], steps=[])
    if store.config()['revision'] != value['config_revision']:
        raise ReviewError('模型配置已变化，请继续检查以使用当前保存配置')
    verify_model(store, value)
    old = store.config()
    if old['revision'] != value['config_revision']:
        raise ReviewError('模型配置在检查期间发生变化，请继续检查以验证当前保存配置')
    expected_revision = old['revision']
    wanted = {k: value[k] for k in ('api_base_url', 'model')}
    same = all(old[k] == wanted[k] for k in wanted) and old['api_key'] and store.unseal(old['api_key']) == store.unseal(value['api_key'])
    if not same:
        if old['revision'] != value['config_revision']:
            raise ReviewError('模型配置已被其他操作修改，请重新检查并提交')
        store.save_config(dict(wanted, api_key=value['api_key'], revision=old['revision']))
        expected_revision += 1
    results = []
    steps = []
    def stage(name, action):
        nonlocal current_step
        if store.config()['revision'] != expected_revision:
            raise ReviewError('模型配置在接入期间发生变化，已停止后续操作，请继续检查')
        current_step = action
        update(store, ident, stage=name + '：' + action, steps=steps, results=results)
    def done(name, action):
        steps.append({'name': name, 'step': action, 'ok': True})
        update(store, ident, steps=steps)
    current_step = '准备容器连接'
    for index, selection in enumerate(value['repositories']):
        target = db.session.get(GiteaServer, selection['server_id'])
        if not target or gitea_address(target.gitea_url) != selection['gitea_url']:
            raise ReviewError('Gitea 连接在初始化期间发生变化，请重新选择')
        stage(selection['name'], '准备容器连接')
        transport = admin = None
        try:
            transport = ContainerAccess(target, pool)
            admin = Gitea(target.gitea_url, target.api_token)
            installer = Installer(SimpleNamespace(container=target.gitea_container, user=None, custom_path=None, config=None, action='install'), runner=transport.run)
            admin.version()
            actual = admin.request('GET', '/repos/' + quote(selection['name'], safe='/'))
            if actual['id'] != selection['repo_id'] or actual['full_name'] != selection['name'] or actual.get('archived') or actual.get('mirror'):
                raise ReviewError('仓库身份变化或当前状态不支持审查')
            stage(selection['name'], '验证 Gitea 回连地址')
            callback_probe(store, installer, value['manager_url'])
            done(selection['name'], '回调地址')
            path = '/repos/' + quote(selection['name'], safe='/')
            aliases = [r for r in store.repos() if not r.get('access_archived') and r['repo_id'] == actual['id'] and r.get('gitea_url', '').rstrip('/') == gitea_address(target.gitea_url) and r['server_id'] != target.id]
            if aliases:
                raise ReviewError('该仓库已通过另一个服务器记录接入，请复用已有记录，未创建重复 Webhook')
            existing = next((r for r in store.repos() if not r.get('access_archived') and r['server_id'] == target.id and r['repo_id'] == actual['id']), None)
            if not existing:
                stage(selection['name'], '准备专用机器人及只读权限')
                bot = ensure_bot(store, target, installer, admin)
                admin.request('PUT', path + '/collaborators/' + quote(bot['name']), {'permission': 'read'})
            # 已有人工配置的有效机器人继续使用，不替换其身份。
            token = store.unseal(existing['bot_token']) if existing else store.unseal(bot['token'])
            data = {'server_id': target.id, 'repo_name': actual['full_name'], 'bot_token': token,
                    'enabled': existing['enabled'] if existing else False, 'revision': existing['revision'] if existing else None}
            sid, rid, checked = repository_values(store, data, existing)
            done(selection['name'], '机器人与仓库权限')
            repo = store.repo(existing['id'], public=True) if existing else store.save_repo(sid, rid, checked, required_config_revision=expected_revision)
            from access.scopes import bind_review_repository
            bind_review_repository(store, repo['id'], actual, actor=value.get('access_actor', {}))
            installed = ensure_webhook(store, repo['id'], value['manager_url'])
            stage(selection['name'], '验证 Webhook 实际投递')
            if not actual.get('empty'):
                verify_delivery(store, admin, path, repo['id'], installed['hook_id'], actual['default_branch'])
            else:
                raise ReviewError('空仓库尚无提交，无法验证 Webhook，请有首个提交后继续接入')
            done(selection['name'], 'Webhook 安装及投递')
            stage(selection['name'], '安装并热重载 PR 进度组件')
            rows = [r for r in store.repos(public=True) if not r.get('access_archived') and r.get('gitea_url', '').rstrip('/') == gitea_address(target.gitea_url)]
            with tempfile.TemporaryDirectory(prefix='审查组件-') as folder:
                with zipfile.ZipFile(build_assets(rows)) as archive: archive.extractall(folder)
                installer.asset_root = folder
                installer.execute()
            done(selection['name'], 'PR 进度组件')
            current = store.repo(repo['id'])
            if current['revision'] != repo['revision']:
                raise ReviewError('仓库配置在接入期间发生变化，未覆盖管理员的新设置')
            if not current['enabled']:
                checked = dict(enabled=True, revision=current['revision'])
                store.save_repo(target.id, actual['id'], checked, current['id'], required_config_revision=expected_revision)
            results.append({'name': selection['name'], 'ok': True, 'repository': repo['id']})
            update(store, ident, results=results, steps=steps)
        except AccessError as exc:
            moment = datetime.now(timezone.utc).isoformat()
            results.append({'name': selection['name'], 'ok': False, 'error': str(exc),
                            'error_code': exc.code, 'step': current_step, 'phase': exc.phase,
                            'time_utc': moment, 'outcome_unknown': exc.unknown})
            # 严禁直接记录异常正文、命令参数、容器输出或凭据。
            logging.error('AI 接入通道失败 %s', encode({'time_utc': moment, 'job': ident, 'server': target.id,
                'repository': selection['name'], 'host': target.host, 'port': target.ssh_port,
                'step': current_step, 'phase': exc.phase, 'error_code': exc.code,
                'exception_type': exc.exception_type, 'errno': exc.errno, 'outcome_unknown': exc.unknown}))
            for remaining in value['repositories'][index + 1:]:
                results.append({'name': remaining['name'], 'ok': False, 'error': '前序容器操作中断，本次未执行',
                                'error_code': 'SETUP_STOPPED', 'step': '未执行'})
            break
        except ReviewError as exc:
            if str(exc) == 'callback_unreachable':
                results.append({'name': selection['name'], 'ok': False, 'error': 'Gitea 无法回连当前 Manager，请修正回调地址'})
                update(store, ident, needs_callback=True)
            else:
                results.append({'name': selection['name'], 'ok': False, 'error': str(exc)})
        except (ValueError, RuntimeError) as exc:
            # 安装器只提供受控中文诊断，不包含容器输出或令牌。
            results.append({'name': selection['name'], 'ok': False, 'error': str(exc)[:500]})
        finally:
            if admin is not None: admin.close()
            if transport is not None: transport.close()
    if any(row['ok'] for row in results):
        current = store.config()
        if current['revision'] != expected_revision:
            raise ReviewError('模型配置在接入期间发生变化，保留已完成步骤，请继续检查')
        if not current['enabled']: store.save_config({'revision': expected_revision, 'enabled': True})
    success = all(row['ok'] for row in results)
    update(store, ident, status='succeeded' if success else 'partial' if any(r['ok'] for r in results) else 'failed',
           stage='接入完成' if success else '部分检查未完成，请查看具体原因', results=results)


def run_pending(app):
    from services.upgrade_maintenance import active
    if active(): return
    with app.app_context():
        store = Store(app.config.get('AI_REVIEW_DB')); initialize(store)
        try:
            with store.lock('自动接入执行', blocking=False):
                with contextlib.closing(store.connect()) as c:
                    row = c.execute("SELECT * FROM setup_jobs WHERE status IN ('pending','running') ORDER BY created LIMIT 1").fetchone()
                if row:
                    try: execute(store, row)
                    except ReviewError as exc: update(store, row['id'], 'failed', stage='接入检查未完成', error=str(exc))
                    except Exception:
                        logging.error('AI 接入出现内部错误，保留阶段记录供核对')
                        update(store, row['id'], 'failed', stage='接入检查未完成', error='内部操作未完成，请查看服务器连接与接入阶段')
        except ReviewError:
            return


def start(app):
    if app.extensions.get('ai_setup_started') or os.environ.get('AI_REVIEW_SETUP_DISABLED') == '1': return
    app.extensions['ai_setup_started'] = True
    def loop():
        while True:
            try: run_pending(app)
            except Exception: logging.warning('自动接入后台暂时不可用')
            time.sleep(2)
    threading.Thread(target=loop, daemon=True, name='审查自动接入').start()
