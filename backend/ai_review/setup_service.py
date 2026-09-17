"""持久化的一次接入任务；共享管理员确认后自动准备受限机器人与事件链路。"""
import contextlib
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

from .common import ReviewError, encode, endpoint
from .gitea import Gitea
from .host_policy import address, approve, ensure
from .store import Store
from .setup_transport import ContainerAccess


def initialize(store):
    with store.tx() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS setup_jobs (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, value TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS setup_resources (id TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS setup_probes (token TEXT PRIMARY KEY, expires REAL NOT NULL, seen INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS setup_hook_receipts (repository INTEGER PRIMARY KEY, received REAL NOT NULL);
        ''')


def resource(store, key, value=None):
    with store.tx() as c:
        if value is not None:
            c.execute('INSERT INTO setup_resources VALUES (?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value', (key, encode(value)))
        row = c.execute('SELECT value FROM setup_resources WHERE id=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None


def public_job(store, ident=None):
    initialize(store)
    with contextlib.closing(store.connect()) as c:
        row = c.execute('SELECT id,status,detail,created,updated FROM setup_jobs ' + ('WHERE id=?' if ident else 'ORDER BY created DESC LIMIT 1'), (ident,) if ident else ()).fetchone()
        if not row:
            return None
        return dict(id=row['id'], status=row['status'], detail=json.loads(row['detail']), created=row['created'], updated=row['updated'])


def update(store, ident, status='running', **detail):
    with store.tx() as c:
        row = c.execute('SELECT detail FROM setup_jobs WHERE id=?', (ident,)).fetchone()
        merged = dict(json.loads(row[0]), **detail)
        c.execute('UPDATE setup_jobs SET status=?,detail=?,updated=? WHERE id=?', (status, encode(merged), time.time(), ident))


def submit(store, data, fallback_url):
    from models import db, GiteaServer
    initialize(store)
    if not isinstance(data, dict) or set(data) - {'api_base_url', 'api_key', 'model', 'repositories', 'manager_url'}:
        raise ReviewError('初始化输入格式无效')
    old = store.config()
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
        target = db.session.get(GiteaServer, row['server_id'])
        if not target:
            raise ReviewError('所选 Gitea 服务器已不存在')
        marker = (target.id, row['repo_id'])
        if marker in seen: continue
        seen.add(marker)
        targets.append(dict(row, gitea_url=address(target.gitea_url)[0]))
        hosts.append(target.gitea_url)
    previous = [r.get('webhook_url', '').split('/api/ai-review/webhooks/')[0] for r in store.repos(public=True)
                if r['server_id'] in {r['server_id'] for r in targets} and r.get('webhook_url')]
    callback = address(data.get('manager_url') or (previous[0] if previous and len(set(previous)) == 1 else fallback_url))[0]
    # 提交按钮即是管理员对这些明确地址的授权；不从 webhook 载荷追加名单。
    approve({'AI_REVIEW_GITEA_HOSTS': hosts, 'AI_REVIEW_MODEL_HOSTS': [api_url], 'AI_REVIEW_WEBHOOK_HOSTS': [callback]})
    value = dict(api_base_url=api_url, api_key=sealed, model=model, manager_url=callback, repositories=targets,
                 config_revision=old['revision'])
    with store.tx() as c:
        active = c.execute("SELECT id FROM setup_jobs WHERE status IN ('pending','running') LIMIT 1").fetchone()
        if active:
            raise ReviewError('已有接入任务正在执行，请先查看进度')
        ident = secrets.token_hex(16); now = time.time()
        c.execute('INSERT INTO setup_jobs VALUES (?,?,?,?,?,?)', (ident, 'pending', encode(value), encode({'stage': '等待接入检查', 'results': []}), now, now))
    return public_job(store, ident)


def verify_model(store, value):
    base = endpoint(value['api_base_url'], 'AI_REVIEW_MODEL_HOSTS')
    with requests.Session() as session:
        session.trust_env = False
        session.headers['Authorization'] = 'Bearer ' + store.unseal(value['api_key'])
        try:
            response = session.get(base + '/models', timeout=(5, 15), allow_redirects=False)
            if response.status_code != 200 or len(response.content) > 1024 * 1024:
                raise ReviewError('模型目录不可读取，请核对地址、Key 与访问权限')
            models = [r.get('id') for r in response.json().get('data', []) if isinstance(r, dict)]
            if value['model'] not in models:
                raise ReviewError('内网模型目录中没有所选模型，请修正模型名称')
            response = session.post(base + '/chat/completions', json={'model': value['model'], 'messages': [
                {'role': 'user', 'content': '连接验证：仅输出 JSON 对象 {"ok":true}。'}],
                'max_tokens': 128, 'stream': False, 'response_format': {'type': 'json_object'}},
                timeout=(5, 90), allow_redirects=False)
            if response.status_code != 200 or len(response.content) > 65536:
                raise ReviewError('模型实际调用未通过，请检查服务与模型权限')
            choice = response.json()['choices'][0]
            if choice.get('finish_reason') != 'stop' or choice['message'].get('tool_calls') or json.loads(choice['message']['content']) != {'ok': True}:
                raise ReviewError('模型实际调用未返回完整的结构化结果')
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
            raise ReviewError('模型调用超时或返回格式不符合要求') from None


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
    except Exception:
        raise ReviewError('callback_unreachable') from None
    finally:
        with store.tx() as c: c.execute('DELETE FROM setup_probes WHERE token=?', (token,))


def ensure_bot(store, target, installer, admin):
    key = 'bot:' + hashlib.sha256(target.gitea_url.rstrip('/').encode()).hexdigest()
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
    from models import db, GiteaServer
    from .assets.install import Installer
    from .progress_assets import build_assets
    from routes.ai_review_routes import repository_values, ensure_webhook
    ident = job['id']; value = json.loads(job['value'])
    update(store, ident, stage='检查内网模型实际调用', needs_callback=False, error='')
    verify_model(store, value)
    old = store.config()
    wanted = {k: value[k] for k in ('api_base_url', 'model')}
    same = all(old[k] == wanted[k] for k in wanted) and old['api_key'] and store.unseal(old['api_key']) == store.unseal(value['api_key'])
    if not same:
        if old['revision'] != value['config_revision']:
            raise ReviewError('模型配置已被其他操作修改，请重新检查并提交')
        store.save_config(dict(wanted, api_key=value['api_key'], revision=old['revision']))
    results = []
    steps = []
    def stage(name, action):
        update(store, ident, stage=name + '：' + action, steps=steps, results=results)
    def done(name, action):
        steps.append({'name': name, 'step': action, 'ok': True})
        update(store, ident, steps=steps)
    for selection in value['repositories']:
        target = db.session.get(GiteaServer, selection['server_id'])
        if not target or target.gitea_url.rstrip('/') != selection['gitea_url']:
            raise ReviewError('Gitea 连接在初始化期间发生变化，请重新选择')
        update(store, ident, stage='准备 ' + selection['name'], results=results)
        transport = ContainerAccess(target)
        admin = Gitea(target.gitea_url, target.api_token)
        try:
            installer = Installer(SimpleNamespace(container=target.gitea_container, user=None, custom_path=None, config=None, action='install'), runner=transport.run)
            admin.version()
            actual = admin.request('GET', '/repos/' + quote(selection['name'], safe='/'))
            if actual['id'] != selection['repo_id'] or actual['full_name'] != selection['name'] or actual.get('archived') or actual.get('mirror'):
                raise ReviewError('仓库身份变化或当前状态不支持审查')
            stage(selection['name'], '验证 Gitea 回连地址')
            callback_probe(store, installer, value['manager_url'])
            done(selection['name'], '回调地址')
            path = '/repos/' + quote(selection['name'], safe='/')
            aliases = [r for r in store.repos() if r['repo_id'] == actual['id'] and r.get('gitea_url', '').rstrip('/') == target.gitea_url.rstrip('/') and r['server_id'] != target.id]
            if aliases:
                raise ReviewError('该仓库已通过另一个服务器记录接入，请复用已有记录，未创建重复 Webhook')
            existing = next((r for r in store.repos() if r['server_id'] == target.id and r['repo_id'] == actual['id']), None)
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
            repo = store.repo(existing['id'], public=True) if existing else store.save_repo(sid, rid, checked)
            installed = ensure_webhook(store, repo['id'], value['manager_url'])
            stage(selection['name'], '验证 Webhook 实际投递')
            if not actual.get('empty'):
                verify_delivery(store, admin, path, repo['id'], installed['hook_id'], actual['default_branch'])
            else:
                raise ReviewError('空仓库尚无提交，无法验证 Webhook，请有首个提交后继续接入')
            done(selection['name'], 'Webhook 安装及投递')
            stage(selection['name'], '安装并热重载 PR 进度组件')
            rows = [r for r in store.repos(public=True) if r.get('gitea_url', '').rstrip('/') == target.gitea_url.rstrip('/')]
            with tempfile.TemporaryDirectory(prefix='审查组件-') as folder:
                with zipfile.ZipFile(build_assets(rows)) as archive: archive.extractall(folder)
                installer.asset_root = folder
                installer.execute()
            done(selection['name'], 'PR 进度组件')
            current = store.repo(repo['id'])
            if not current['enabled']:
                checked = dict(enabled=True, revision=current['revision'])
                store.save_repo(target.id, actual['id'], checked, current['id'])
            results.append({'name': selection['name'], 'ok': True, 'repository': repo['id']})
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
            admin.close(); transport.close()
    if any(row['ok'] for row in results):
        current = store.config()
        if not current['enabled']: store.save_config({'revision': current['revision'], 'enabled': True})
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
