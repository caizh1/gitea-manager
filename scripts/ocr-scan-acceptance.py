#!/usr/bin/env python3
"""在专用 Gitea 1.26.4 容器、正式 OCR 与后台浏览器中验证扫描。"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
SOURCE = {
    'normal': ('https://github.com/sindresorhus/escape-string-regexp.git', 'v5.0.0',
               'ba9a4473850cb367936417e97f1f2191b7cc67dd'),
    'faults': ('https://github.com/pallets/click.git', '8.1.8',
               '934813e4d421071a1b3db3973c02fe2721359a6e'),
}


def runtime():
    spec = importlib.util.spec_from_file_location('ocr_executor', ROOT / 'review-runtime/ocr_scan.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def serve(mode, port):
    from flask import Flask, send_from_directory
    from werkzeug.serving import make_server

    if mode == 'web':
        from app import create_app
        app = create_app()

        def static_app(environ, start_response):
            path = environ['PATH_INFO']
            if path.startswith('/api/'):
                return app(environ, start_response)
            with app.request_context(environ):
                name = path.removeprefix('/')
                if not name.startswith('assets/'):
                    name = 'index.html'
                return send_from_directory(ROOT / 'frontend/dist', name)(environ, start_response)
        target = static_app
    elif mode == 'executor':
        target = Flask(__name__)
        token = Path(os.environ['AI_REVIEW_EXECUTOR_TOKEN_FILE']).read_text().strip()
        from flask import request
        runtime().register(target, lambda: secrets.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + token))
    else:
        from ai_review.worker import main
        return main()
    make_server('127.0.0.1', port, target, threaded=True).serve_forever()


def run(command, **options):
    result = subprocess.run(command, capture_output=True, text=True, **options)
    if result.returncode:
        info = result.stderr.strip() or result.stdout.strip()
        if '--password' in command:
            info = info.replace(command[command.index('--password')+1], '[已遮蔽]')
        raise RuntimeError('验收命令失败：' + info[-1200:])
    return result.stdout.strip()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_until(check, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            value = check()
            if value:
                return value
        except requests.RequestException:
            pass
        time.sleep(.25)
    raise RuntimeError('等待验收条件超时')


def provision(folder, docker, container, gitea, scenario):
    if run(docker + ['inspect', '--format', '{{index .Config.Labels "io.gm.qa"}}', container]) != 'ocr-scan':
        raise RuntimeError('验收只允许使用标有 io.gm.qa=ocr-scan 的专用 Gitea 容器')
    wait_until(lambda: requests.get(gitea + '/api/v1/version', timeout=2).ok)
    user = 'ocrqa' + secrets.token_hex(3)
    password = 'Qa7-' + secrets.token_urlsafe(30)
    prefix = docker + ['exec', '-u', 'git', container, 'gitea', 'admin', 'user']
    run(prefix + ['create', '--username', user, '--email', user+'@example.invalid', '--password', password,
                  '--admin', '--must-change-password=false'])
    token = run(prefix + ['generate-access-token', '--username', user, '--token-name', 'ocr-e2e',
                          '--scopes', 'all', '--raw']).splitlines()[-1]
    repo = requests.post(gitea + '/api/v1/user/repos', headers={'Authorization': 'token ' + token},
                         json={'name': scenario, 'default_branch': 'review', 'private': True}, timeout=15)
    repo.raise_for_status()
    upstream, tag, expected = SOURCE[scenario]
    source = folder / 'repository'
    run(['git', 'clone', '-q', '--single-branch', '--branch', tag, upstream, str(source)])
    actual = run(['git', 'rev-parse', 'HEAD'], cwd=source)
    if actual != expected:
        raise RuntimeError('上游真实样本提交与验收记录不一致')
    from ai_review.git_cache import GitFailure, git_credentials, run_git
    name = repo.json()['full_name']
    try:
        with git_credentials(token) as auth:
            run_git(['push', gitea + '/' + name + '.git', 'HEAD:refs/heads/review'], cwd=source, env=auth)
    except GitFailure as exc:
        raise RuntimeError('导入隔离验收仓库失败：' + exc.diagnostic['git_error']) from None
    return name, repo.json()['id'], token, actual


def seed(gitea, name, rid, key, extra=None):
    from app import create_app
    from models import GiteaServer, db, set_setting
    from ai_review.store import Store

    app = create_app()
    with app.app_context():
        server = GiteaServer(name='隔离验收 Gitea', host='127.0.0.1', ssh_user='git',
                             gitea_port=urlsplit(gitea).port, gitea_url=gitea, api_token=key, version='1.26.4')
        db.session.add(server)
        db.session.commit()
        set_setting('host_ip', '127.0.0.1')
        state = Store()
        state.save_repo(server.id, rid, {'repo_name': name, 'gitea_url': gitea, 'bot_token': state.seal(key), 'enabled': True})
        if extra:
            other_name, other_id, other_token = extra
            state.save_repo(server.id, other_id, {'repo_name': other_name, 'gitea_url': gitea,
                                                 'bot_token': state.seal(other_token), 'enabled': True})


def result_digest(record):
    return hashlib.sha256(json.dumps(record.get('comments') or [], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def fault_proxy(port, checkpoint, cancel_only=False):
    from flask import Flask, Response, jsonify, request
    from werkzeug.serving import make_server
    from ai_review.ocr_store import ScanStore

    app = Flask(__name__)
    lock = threading.Lock()
    state = {'snapshot': {}, 'codes': [], 'task': '', 'online': 0, 'waves': [], 'attempts': [],
             'early_resume_errors': 0, 'later_errors': 0}

    @app.post('/v1/chat/completions')
    def completion():
        data = request.get_json()
        probe = any(item.get('function', {}).get('name') == 'manager_probe' for item in data.get('tools', []))
        with lock:
            if not probe:
                if cancel_only:
                    state['codes'].append(429)
                    checkpoint.set()
                    return jsonify(error={'message': '验收注入限流'}), 429, {'Retry-After': '30'}
                scan = ScanStore()
                with contextlib.closing(scan.connect()) as connection:
                    row = connection.execute("SELECT id FROM ocr_targets WHERE state='running' ORDER BY created DESC LIMIT 1").fetchone()
                if row:
                    task = scan.target(row['id'])
                    native = runtime().snapshot(runtime().root(task['id']))
                    total = task['preview'].get('reviewable_count', 0)
                    files = native['files']
                    if total and len(files) * 5 >= total * 4:
                        if not state['snapshot']:
                            records = runtime().session_state(runtime().root(task['id']))[0]
                            state.update(snapshot=files, task=task['id'], total=total,
                                         findings={path: result_digest(records[path]) for path in files})
                            state['waves'].append(len(files))
                            state['attempts'].append(task['attempt'])

                        if len(files) > len(state['snapshot']):
                            if len(state['waves']) == 1:
                                state['waves'].append(len(files))
                            inject = state['later_errors'] < 8
                            state['later_errors'] += int(inject)
                        elif task['attempt'] == state['attempts'][0]:
                            # 持续到原生 OCR 进程退出。由 Manager 创建新尝试并使用原生检查点续扫。
                            inject = True
                        else:
                            if task['attempt'] not in state['attempts']:
                                state['attempts'].append(task['attempt'])
                            inject = state['early_resume_errors'] < 8
                            state['early_resume_errors'] += int(inject)
                        if inject:
                            code = (429, 500)[len(state['codes']) % 2]
                            state['codes'].append(code)
                            checkpoint.set()
                            return jsonify(error={'message': '验收注入的短暂网关故障'}), code, {'Retry-After': '1'}
        return forward_to_provider(data)

    def forward_to_provider(data):
        data['thinking'] = {'type': 'disabled'}
        response = requests.post('https://api.deepseek.com/chat/completions',
                                 headers={'Authorization': request.headers['Authorization']},
                                 json=data, timeout=(6, 120), allow_redirects=False)
        state['online'] += 1
        headers = {'Retry-After': response.headers['Retry-After']} if 'Retry-After' in response.headers else {}
        return Response(response.content, status=response.status_code, headers=headers, content_type='application/json')

    server = make_server('127.0.0.1', port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def login(browser, url, directory):
    context = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
    page = context.new_page()
    page.goto(url)
    page.locator('#login-username').fill('admin')
    page.locator('#login-password').fill(os.environ['INIT_PASSWORD'])
    with page.expect_response(lambda response: response.url.endswith('/api/login')) as response:
        page.get_by_role('button', name='登录', exact=True).click()
    if not response.value.ok:
        raise RuntimeError('Manager 验收账号登录失败')
    page.get_by_text('AI Code Review', exact=True).first.wait_for()
    return context, page


def ui_scan(page, url, model_address, key, sha, folder, deleted_target=None):
    from playwright.sync_api import expect

    page.goto(url + '/ai-reviews/ocr/models')
    page.get_by_text('OCR 专用', exact=True).click()
    page.get_by_label('API 端点', exact=True).fill(model_address)
    page.get_by_label('模型 ID', exact=True).fill('deepseek-flash')
    try:
        page.get_by_label('API Key', exact=True).fill(key)
    except Exception:
        raise RuntimeError('无法填写 API Key 字段') from None
    page.get_by_role('button', name='保存配置').click()
    expect(page.get_by_text('模型配置已保存')).to_be_visible()
    page.get_by_role('button', name='测试连接').click()
    expect(page.get_by_text('模型工具调用验证通过')).to_be_visible(timeout=45000)
    page.screenshot(path=folder / 'models.png', full_page=True)
    page.goto(url + '/ai-reviews/ocr/new')
    expect(page.get_by_text('隔离验收 Gitea').first).to_be_visible()
    page.screenshot(path=folder / 'configuration-initial.png', full_page=True)
    page.get_by_role('combobox', name='选择仓库', exact=True).locator('xpath=ancestor::div[contains(@class,"el-select__wrapper")]').click()
    if deleted_target:
        primary, removed, remove_repo = deleted_target
        page.get_by_role('option', name=primary, exact=True).click()
        page.get_by_role('option', name=removed, exact=True).click()
    else:
        page.get_by_role('option', name=re.compile(r'ocrqa')).first.click()
    page.keyboard.press('Escape')
    expect(page.get_by_role('combobox', name='选择分支', exact=True).first).to_be_visible()
    expect(page.get_by_text('review', exact=True).first).to_be_visible()
    if deleted_target:
        for repository in (primary, removed):
            form = page.locator('.el-form-item').filter(has_text=repository + ' 的分支')
            expect(form.get_by_text('review', exact=True)).to_be_visible(timeout=30000)
        remove_repo()

    page.get_by_role('combobox', name='扫描模型', exact=True).locator('xpath=ancestor::div[contains(@class,"el-select__wrapper")]').click()
    page.get_by_role('option', name='OCR 专用 · deepseek-flash').click()
    expect(page.get_by_text('每个仓库分支的 Token 预算', exact=True)).not_to_be_visible()
    for fmt in ('Markdown', 'JSON'):
        control = page.get_by_role('checkbox', name=fmt, exact=True)
        control.press('Space')
        expect(control).to_be_checked()
    options = page.locator('.ocr-config-card')
    expect(options.get_by_role('radio', name='手动')).to_be_checked()
    options.get_by_text('每周', exact=True).click()
    expect(options.get_by_role('radio', name='每周')).to_be_checked()
    expect(options.get_by_role('combobox', name='星期')).to_be_visible()
    options.get_by_text('每天', exact=True).click()
    expect(options.get_by_role('radio', name='每天')).to_be_checked()
    expect(options.get_by_label('定时执行时间')).to_be_visible()
    options.get_by_text('手动', exact=True).click()
    expect(options.get_by_label('定时执行时间')).not_to_be_visible()
    options.get_by_role('heading').click()
    options.screenshot(path=folder / 'configuration-controls.png', animations='disabled')
    desktop = page.viewport_size
    try:
        for width, columns in ((760, 3), (430, 2)):
            page.set_viewport_size({'width': width, 'height': 950})
            assert options.evaluate('node => node.getBoundingClientRect().right <= innerWidth + 1')
            assert options.locator('.ocr-format-options').evaluate(
                'node => getComputedStyle(node).gridTemplateColumns.split(" ").length') == columns
            for control in options.get_by_role('checkbox').all():
                assert control.evaluate('node => node.scrollWidth <= node.clientWidth')
        options.screenshot(path=folder / 'configuration-controls-narrow.png', animations='disabled')
    finally:
        page.set_viewport_size(desktop)
    with page.expect_request(lambda request: request.url.endswith('/api/ai-review/ocr/previews')
                             and request.method == 'POST') as submitted:
        page.get_by_role('button', name='预览范围').click()
    config = submitted.value.post_data_json
    assert config['mode'] == 'custom' and 'budget' not in config
    assert set(config['formats']) == {'html', 'md', 'json'}
    scope = page.get_by_role('region', name='文件范围预览')
    expect(scope.get_by_text('待启动', exact=True)).to_be_visible(timeout=120000)
    expect(scope.get_by_text(sha, exact=True).first).to_be_visible()
    expect(scope.get_by_role('columnheader', name='文件路径')).to_be_visible()
    expect(scope.get_by_text('将扫描', exact=True).first).to_be_visible()
    if deleted_target:
        expect(scope.get_by_text('待处理', exact=True)).to_be_visible()
    if scope.get_by_text('排除 3', exact=True).count():
        scope.get_by_text('排除 3', exact=True).click()
        expect(scope.get_by_text('文件类型未纳入', exact=True).first).to_be_visible()
        scope.get_by_text('全部 14', exact=True).click()
    page.screenshot(path=folder / 'configuration.png', full_page=True, animations='disabled')
    page.get_by_role('button', name='立即扫描').click()
    page.wait_for_url(re.compile(r'/tasks/[a-f0-9]{32}$'), timeout=30000)
    return page.url.rsplit('/', 1)[-1]


def finish_ui(browser, page, folder, result):
    from playwright.sync_api import expect

    page.get_by_role('button', name='查看报告').click(timeout=120000)
    expect(page.get_by_role('heading', name='扫描报告')).to_be_visible()
    expect(page.get_by_role('button', name='导出 HTML', exact=True)).to_be_enabled()
    metric = page.get_by_role('list', name='报告统计').get_by_role('listitem').filter(has_text='已用 Token')
    expect(metric.get_by_text(f'{result["模型用量"]:,}', exact=True)).to_be_visible()
    page.screenshot(path=folder / 'report.png', full_page=True)
    for label, name in [('HTML', 'report.html'), ('Markdown', 'report.md'), ('JSON', 'report.json')]:
        if label != 'HTML':
            page.get_by_role('button', name='其他导出格式').click()
        with page.expect_download() as download:
            page.get_by_role('button' if label == 'HTML' else 'menuitem', name='导出 ' + label, exact=True).click()
        download.value.save_as(folder / name)
    offline = browser.new_context(offline=True, viewport={'width': 1280, 'height': 800})
    local = offline.new_page()
    local.goto((folder / 'report.html').as_uri())
    expect(local.locator('.metric-primary dd')).to_have_text(f'{result["完成文件"]}/{result["候选文件"]}')
    expect(local.get_by_text(result['提交'], exact=True)).to_be_visible()
    expected = len(json.loads((folder / 'report.json').read_bytes()).get('comments') or [])
    expect(local.get_by_role('heading', name='问题清单')).to_be_visible()
    expect(local.locator('.finding:visible')).to_have_count(expected)
    counter = local.locator('#match-count')
    expect(counter).to_have_text(f'{expected} 项')
    fields = local.locator('.filters .filter-control').all()
    left, right = (field.bounding_box() for field in fields)
    assert left['y'] == right['y'] and left['height'] == right['height']
    local.screenshot(path=folder / 'offline-report.png', animations='disabled')
    search = local.get_by_label('筛选文件或问题')
    severity = local.get_by_role('combobox', name='严重程度')
    if expected:
        level = local.locator('.finding').first.get_attribute('data-level')
        subset = local.locator('.finding').evaluate_all(
            '(rows, key) => rows.filter(row => row.dataset.level === key).length', level)
        severity.select_option(level)
        expect(local.locator('.finding:visible')).to_have_count(subset)
        expect(counter).to_have_text(f'{subset} 项')
    search.fill('仅检查本地筛选')
    expect(local.locator('.finding:visible')).to_have_count(0)
    expect(local.get_by_text('没有匹配的问题')).to_be_visible()
    expect(counter).to_have_text('0 项')
    search.clear()
    severity.select_option('')
    expect(local.locator('.finding:visible')).to_have_count(expected)
    mobile = browser.new_context(offline=True, viewport={'width': 390, 'height': 844})
    narrow = mobile.new_page()
    narrow.goto((folder / 'report.html').as_uri())
    assert narrow.evaluate('document.documentElement.scrollWidth') == 390
    narrow_search, narrow_severity = narrow.locator('.filters .filter-control').all()
    assert narrow_search.bounding_box()['y'] < narrow_severity.bounding_box()['y']
    narrow.screenshot(path=folder / 'offline-report-narrow.png', animations='disabled')
    mobile.close()
    summary = local.get_by_text('OCR 汇总', exact=True)
    if summary.count():
        assert not local.locator('details').first.get_attribute('open')
        summary.click()
        expect(local.locator('.summary h3, .summary p, .summary li').first).to_be_visible()
        local.locator('.summary').scroll_into_view_if_needed()
        local.screenshot(path=folder / 'offline-summary.png', animations='disabled')
    return expected



def schedule_and_stop(browser, page, url, store, folder, completed):
    from playwright.sync_api import expect

    # 同一真实提交先创建默认计划，随后以强制重扫触发可终止的第二次扫描。
    outcomes = []
    for force in (False, True):
        page.goto(url + '/ai-reviews/ocr/new?schedule=1')
        page.get_by_role('combobox', name='选择仓库', exact=True).locator(
            'xpath=ancestor::div[contains(@class,"el-select__wrapper")]').click()
        page.get_by_role('option', name=re.compile(r'ocrqa')).first.click()
        page.keyboard.press('Escape')
        expect(page.get_by_text('review', exact=True).first).to_be_visible()
        page.get_by_role('combobox', name='扫描模型', exact=True).locator(
            'xpath=ancestor::div[contains(@class,"el-select__wrapper")]').click()
        page.get_by_role('option', name='OCR 专用 · deepseek-flash').click()
        if force:
            page.get_by_text('即使无变化也重新扫描', exact=True).click()
        when = datetime.now(ZoneInfo('Asia/Shanghai')) + timedelta(minutes=1, seconds=8)
        page.get_by_label('定时执行时间').fill(when.strftime('%H:%M'))
        with page.expect_response(lambda response: response.url.endswith('/ocr/plans') and response.request.method == 'POST') as reply:
            page.get_by_role('button', name='保存计划').click()
        assert reply.value.ok
        plan_id = reply.value.json()['id']

        def scheduled_target():
            with contextlib.closing(store.connect()) as connection:
                row = connection.execute('SELECT t.id FROM ocr_targets t JOIN ocr_batches b ON t.batch=b.id WHERE b.plan=?',
                                         (plan_id,)).fetchone()
            if row:
                job = store.target(row['id'])
                if job['state'] in ('running', 'skipped', 'attention', 'complete'):
                    return job

        job = wait_until(scheduled_target, timeout=95)
        if not force:
            assert job['state'] == 'skipped' and job['reused']
            outcomes.append('实际定时触发后跳过相同提交')
            continue
        assert job['state'] == 'running'
        page.goto(url + '/ai-reviews/ocr/tasks/' + job['id'])
        page.screenshot(path=folder / 'progress.png', full_page=True)
        page.get_by_role('button', name='终止扫描', exact=True).click()
        expect(page.get_by_role('dialog', name='确认终止扫描')).to_be_visible()
        page.screenshot(path=folder / 'confirmation.png', full_page=True, animations='disabled')
        page.get_by_role('button', name='返回', exact=True).click()
        expect(page.get_by_role('dialog', name='确认终止扫描')).not_to_be_visible()
        assert store.target(job['id'])['state'] == 'running'
        page.get_by_role('button', name='终止扫描', exact=True).click()
        page.get_by_role('button', name='确认终止', exact=True).click()
        expect(page.get_by_text('已终止', exact=True).first).to_be_visible(timeout=30000)
        stopped = store.target(job['id'])
        assert not stopped['progress'] and not stopped['preview'] and not stopped['broker']
        assert not runtime().root(job['id']).exists() and not store.report_dir(job['id']).exists()
        outcomes.append('强制重扫后通过二次确认终止并清理')
    guest = browser.new_context()
    response = guest.request.get(url + '/api/ai-review/ocr/tasks/' + completed + '/download/html')
    assert response.status == 401
    guest.close()
    outcomes.append('匿名无法下载报告')
    return outcomes


def verify_stop_waiting(page, folder, store, task_id, fault):
    from playwright.sync_api import expect

    banner = page.get_by_role('region', name='扫描重试').get_by_text('HTTP 429', exact=True)
    expect(banner).to_be_visible(timeout=30000)
    assert store.target(task_id)['retry_at'] > time.time()
    page.screenshot(path=folder / 'retry.png', animations='disabled')
    page.get_by_role('button', name='终止扫描', exact=True).click()
    page.get_by_role('button', name='返回', exact=True).click()
    assert store.target(task_id)['state'] in ('running', 'retry')
    page.get_by_role('button', name='终止扫描', exact=True).click()
    expect(page.get_by_role('dialog', name='确认终止扫描')).to_be_visible()
    page.screenshot(path=folder / 'confirmation.png', animations='disabled')
    page.get_by_role('button', name='确认终止', exact=True).click()
    expect(page.get_by_text('已终止', exact=True).first).to_be_visible(timeout=30000)
    row = store.target(task_id)
    assert not row['retry_at'] and not row['progress'] and not row['preview'] and not row['broker']
    assert not runtime().root(task_id).exists() and not store.report_dir(task_id).exists()
    before = len(fault['codes'])
    page.reload()
    expect(page.get_by_text('已终止', exact=True).first).to_be_visible(timeout=30000)
    assert before == len(fault['codes']) and store.target(task_id)['state'] == 'stopped'
    return {'任务': task_id, '模型错误': 429, '撤销凭据': True, '清理任务文件及报告': True, '已终止': True}


def accept(args):
    from cryptography.fernet import Fernet
    from playwright.sync_api import sync_playwright

    key = os.environ.get('OCR_ACCEPTANCE_DEEPSEEK_KEY')
    if not key:
        raise RuntimeError('请通过 OCR_ACCEPTANCE_DEEPSEEK_KEY 注入真实模型密钥')
    folder = Path(args.output).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    (folder / 'master.key').write_bytes(Fernet.generate_key())
    (folder / 'executor.key').write_text(secrets.token_hex(32))
    (folder / 'authorization.key').write_text(secrets.token_hex(32))
    for name in ('master.key', 'executor.key', 'authorization.key'):
        (folder / name).chmod(0o600)
    web_port, executor_port, model_port, broker_port = (free_port() for _ in range(4))
    gitea = 'http://127.0.0.1:' + str(args.gitea_port)
    url = f'http://127.0.0.1:{web_port}'
    os.environ.update(DATABASE_URL='sqlite:///' + str(folder / 'manager.db'),
                      SECRET_KEY=secrets.token_urlsafe(40), INIT_PASSWORD=secrets.token_urlsafe(27),
                      DISABLE_BACKGROUND_TASKS='1', AI_REVIEW_DB=str(folder / 'review.db'),
                      AI_REVIEW_NATIVE_ROOT=str(folder / 'native'), AI_REVIEW_WORKSPACE_ROOT=str(folder / 'workspace'),
                      AI_REVIEW_OCR_BINARY=str(Path(args.binary).resolve()), AI_REVIEW_MASTER_KEY_FILE=str(folder / 'master.key'),
                      AI_REVIEW_EXECUTOR_TOKEN_FILE=str(folder / 'executor.key'),
                      AI_REVIEW_AUTHORIZATION_TOKEN_FILE=str(folder / 'authorization.key'),
                      AI_REVIEW_AUTHORIZATION_URL=url + '/api/internal/authorization/check',
                      AI_REVIEW_EXECUTOR_URL=f'http://127.0.0.1:{executor_port}',
                      AI_REVIEW_BROKER_URL=f'http://127.0.0.1:{broker_port}/v1', AI_REVIEW_BROKER_PORT=str(broker_port),
                      AI_REVIEW_GITEA_HOSTS='127.0.0.1', AI_REVIEW_MODEL_HOSTS='api.deepseek.com,127.0.0.1')
    sample = 'faults' if args.scenario == 'faults' else 'normal'
    docker = ['docker', '--context', args.context]
    name, rid, token, sha = provision(folder, docker, args.container, gitea, sample)
    extra, deleted_target = None, None
    if args.scenario == 'multi-target':
        second = folder / 'second'
        second.mkdir()
        other_name, other_id, other_token, _ = provision(second, docker, args.container, gitea, sample)
        extra = (other_name, other_id, other_token)

        def remove_repo():
            response = requests.delete(gitea + '/api/v1/repos/' + other_name,
                                       headers={'Authorization': 'token ' + other_token}, timeout=15)
            assert response.status_code == 204

        deleted_target = (name, other_name, remove_repo)
    seed(gitea, name, rid, token, extra)
    processes = {}

    def child(mode, port):
        child_env = os.environ.copy()
        child_env.pop('OCR_ACCEPTANCE_DEEPSEEK_KEY', None)
        with (folder / (mode + '.log')).open('ab') as out:
            processes[mode] = subprocess.Popen([sys.executable, __file__, '--serve', mode, '--port', str(port)],
                                                env=child_env, stdout=out, stderr=out)

    proxy, fault = None, None
    try:
        child('web', web_port)
        child('executor', executor_port)
        child('worker', 0)
        wait_until(lambda: requests.get(url + '/api/session', timeout=3).ok)
        model = 'https://api.deepseek.com'
        event = threading.Event()
        if args.scenario in ('faults', 'faults-small', 'stop-waiting'):
            proxy, fault = fault_proxy(model_port, event, cancel_only=args.scenario == 'stop-waiting')
            model = f'http://127.0.0.1:{model_port}/v1'
        from ai_review.ocr_store import ScanStore
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=True)
            context, page = login(browser, url, folder)
            task_id = ui_scan(page, url, model, key, sha, folder, deleted_target)
            store = ScanStore()
            page.get_by_role('heading', name='扫描进度').wait_for()
            page.screenshot(path=folder / 'progress-initial.png', full_page=True)
            if args.scenario == 'stop-waiting':
                result = verify_stop_waiting(page, folder, store, task_id, fault)
                retry_folder = folder / 'fresh-task'
                retry_folder.mkdir()
                second = ui_scan(page, url, model, key, sha, retry_folder)
                assert second != task_id and not store.target(second)['progress'].get('files')
                verify_stop_waiting(page, retry_folder, store, second, fault)
                assert store.target(task_id)['state'] == 'stopped'
                result.update(新任务=second, 新任务从零开始=True)
                (folder / 'acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
                print(json.dumps(result, ensure_ascii=False))
                return
            if fault:
                wait_until(lambda: event.is_set(), timeout=1600)
                page.get_by_role('region', name='扫描重试').get_by_text(re.compile(r'^HTTP (429|500)$')).wait_for(timeout=30000)
                page.screenshot(path=folder / 'retry.png', full_page=True)
                processes['worker'].kill()
                processes['worker'].wait(timeout=10)
                child('worker', 0)
                page.reload()
            terminal = wait_until(lambda: (row if (row := store.target(task_id))['state'] in ('complete','attention') else None),
                                  timeout=1600 if fault else 360)
            if terminal['state'] != 'complete':
                raise RuntimeError('扫描没有完成：' + terminal['error'])
            files = terminal['progress']['files']
            if fault:
                assert {429, 500}.issubset(fault['codes']) and fault['task'] == task_id
                assert len(fault['attempts']) >= 2 and fault['early_resume_errors'] == 8 and fault['later_errors'] == 8
                assert len(fault['waves']) == 2 and fault['waves'][1] > fault['waves'][0]
                assert fault['snapshot'] and all(files.get(path) == value for path, value in fault['snapshot'].items())
                records = runtime().session_state(runtime().root(task_id))[0]
                assert all(result_digest(records[path]) == digest for path, digest in fault['findings'].items())
                completed = set()
                session_files = (runtime().root(task_id) / 'home/.opencodereview/sessions').glob('*/*.jsonl')
                for session in sorted(session_files, key=lambda path: path.stat().st_mtime):
                    if session.name == 'manager-resume.jsonl':
                        continue
                    in_this = set()
                    with session.open() as stream:
                        for line in stream:
                            if not line.endswith('\n'):
                                break
                            row = json.loads(line)
                            path = row.get('filePath')
                            if row.get('type') == 'llm_request' and row.get('taskType') == 'main_task':
                                assert path not in completed, f'已完成文件被重复主审：{path}'
                            if row.get('type') == 'review_item_done':
                                in_this.add(path)
                    completed.update(in_this)
                assert len(completed) == len(files)
            result = {'项目': name, '提交': sha, '模型': 'deepseek-flash', '任务': task_id,
                      '完成文件': len(files), '候选文件': terminal['preview']['reviewable_count'],
                      '模型用量': terminal['tokens'], '耗时秒': round(terminal['updated'] - terminal['created']),
                      '故障注入': fault['codes'] if fault else [], '故障时的已完成数': fault['waves'] if fault else []}
            if fault:
                result.update(启动过新OCR进程=True, 恢复后无新进度时的故障数=fault['early_resume_errors'],
                              新增检查点后的故障数=fault['later_errors'], 重复主审次数=0)
            result['发现数'] = finish_ui(browser, page, folder, result)
            if deleted_target:
                with contextlib.closing(store.connect()) as connection:
                    rows = connection.execute('SELECT state FROM ocr_targets WHERE batch=? ORDER BY state',
                                              (terminal['batch'],)).fetchall()
                assert [row['state'] for row in rows] == ['attention', 'complete']
                result['多目标隔离'] = '已删除目标待处理，其余仓库完成扫描并导出报告'
            page.goto(url + '/ai-reviews/ocr')
            page.get_by_role('heading', name='整仓扫描', exact=True).wait_for()
            page.get_by_role('columnheader', name='已用 Token').wait_for()
            page.get_by_role('cell', name=f'{terminal["tokens"]:,}', exact=True).wait_for()
            page.screenshot(path=folder / 'workbench.png', full_page=True)
            page.set_viewport_size({'width': 800, 'height': 900})
            page.screenshot(path=folder / 'workbench-narrow.png', full_page=True)
            page.set_viewport_size({'width': 1440, 'height': 1000})
            if args.scenario == 'lifecycle':
                result['计划与终止'] = schedule_and_stop(browser, page, url, store, folder, task_id)
            browser.close()
        (folder / 'acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(result, ensure_ascii=False))
    finally:
        if proxy:
            proxy.shutdown()
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
        for process in processes.values():
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', choices=('web', 'executor', 'worker'))
    parser.add_argument('--port', type=int)
    parser.add_argument('--gitea-port', type=int)
    parser.add_argument('--container', required=False)
    parser.add_argument('--context', default='colima-gm-pr-agent-qa')
    parser.add_argument('--binary')
    parser.add_argument('--output')
    parser.add_argument('--scenario', choices=(*SOURCE, 'faults-small', 'lifecycle', 'stop-waiting', 'multi-target'), default='normal')
    args = parser.parse_args()
    if args.serve:
        serve(args.serve, args.port)
    else:
        accept(args)


if __name__ == '__main__':
    main()
