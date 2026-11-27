"""Manager 调度仓库和分支，OCR 执行文件筛选、分析与会话恢复。"""
import contextlib
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from flask import Response, jsonify, request

from . import ocr_reports
from .authorization import guarded_client
from .common import AccessRevoked, ReviewError, endpoint, valid_sha
from .git_cache import git_credentials, run_git
from .model_retries import retry_after
from .native_review import _request
from .ocr_store import DEFAULT_INPUT_TOKENS, ScanStore, event, update

TEMPORARY = {408, 425, 429, 500, 502, 503, 504}
MAX_RESPONSE = 64 * 1024 * 1024
NATIVE_FILES = {'json': 'result.json', 'txt': 'result.txt', 'sarif': 'result.sarif', 'native': 'native.html'}


def executor(ident, action, data=None, destination=None):
    with requests.Session() as session:
        session.trust_env = False
        with _request(session, 'GET' if data is None else 'POST', f'/ocr/{ident}/{action}', json=data,
                      read_timeout=165 if action in ('preview', *NATIVE_FILES) else 10, stream=True) as response:
            if response.status_code != 200:
                raise requests.RequestException(f'OCR 执行器 HTTP {response.status_code}')
            if destination:
                size = 0
                pending = destination.with_suffix('.tmp')
                try:
                    with pending.open('wb') as out:
                        for part in response.iter_content(65536):
                            size += len(part)
                            if size > MAX_RESPONSE:
                                raise ReviewError('OCR 报告超过大小上限')
                            out.write(part)
                        out.flush()
                        os.fsync(out.fileno())
                    pending.replace(destination)
                finally:
                    pending.unlink(missing_ok=True)
                return
            return response.json()


def workspace(ident):
    return Path(os.environ.get('AI_REVIEW_WORKSPACE_ROOT', '/app/review-workspaces')) / 'ocr' / ident


def allowed(store, task, repo):
    from .common import secret_file

    url = os.environ.get('AI_REVIEW_AUTHORIZATION_URL')
    if not url:
        raise AccessRevoked('未配置整仓扫描权限服务')
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(url, headers={'Authorization': 'Bearer ' + secret_file('AI_REVIEW_AUTHORIZATION_TOKEN')},
                                json={'ocr_target': task['id'], 'action': 'read'},
                                timeout=(3, 12), allow_redirects=False)
    if response.status_code in (401, 403, 404):
        raise AccessRevoked('整仓扫描的仓库或操作者权限已失效')
    if response.status_code != 200 or response.json().get('allowed') is not True:
        raise requests.RequestException('整仓扫描权限服务不可用')
    if not repo['enabled'] or repo.get('access_archived'):
        raise AccessRevoked('仓库服务已停用')


def sha_for(client, branch):
    client.repository()
    ref = client.request('GET', client.prefix + '/branches/' + quote(branch, safe=''), missing=True)
    if ref is None:
        raise ReviewError('分支不存在或已删除')
    if ref.get('name') != branch:
        raise ReviewError('返回的分支与请求不匹配')
    return valid_sha(ref.get('commit', {}).get('id'))


def prepare_source(store, task, repo):
    ident = task['id']
    folder = workspace(ident)
    target = folder / 'source'
    with contextlib.closing(guarded_client(store, repo)) as client:
        sha = task['sha'] or sha_for(client, task['branch'])
        with store.tx() as c:
            row = store.target(ident, c)
            if row['state'] == 'stopping':
                return None
            if not row['sha']:
                update(c, ident, sha=sha)
            event(c, ident, '获取固定版本源码', sha=sha)

        def active():
            if store.target(ident)['state'] == 'stopping':
                raise ReviewError('等待终止扫描')

        if target.is_dir() and run_git(['rev-parse', 'HEAD'], cwd=target).decode().strip() == sha:
            return sha
        folder.mkdir(parents=True, exist_ok=True)
        pending = folder / 'fetching'
        if pending.exists():
            shutil.rmtree(pending)
        pending.mkdir()
        run_git(['init', '-q'], cwd=pending, check=active)
        run_git(['config', 'core.symlinks', 'false'], cwd=pending, check=active)
        url = client.url + '/' + quote(repo['repo_name'], safe='/') + '.git'
        with git_credentials(client.git_token) as auth:
            run_git(['fetch', '--quiet', '--no-tags', '--depth=1', url, sha], cwd=pending, env=auth, check=active)
        run_git(['checkout', '--quiet', '--detach', sha], cwd=pending, check=active)
        for entry in run_git(['ls-tree', '-rz', 'HEAD'], cwd=pending).split(b'\0'):
            if entry.startswith(b'120000 '):
                relative = os.fsdecode(entry.split(b'\t', 1)[1])
                (pending / relative).unlink()
        if target.exists():
            shutil.rmtree(target)
        pending.replace(target)
    return sha


def snapshot_progress(task, state):
    candidates = {row['path'] for row in task['preview'].get('files', []) if row.get('will_review')}
    files = task['progress'].get('files', {}).copy()
    for path, digest in state['files'].items():
        if path in candidates:
            if path in files and files[path] != digest:
                raise ReviewError('同一文件的 OCR 检查点指纹发生变化')
            files[path] = digest
    return {'files': files, 'failures': sorted((set(state['failures']) & candidates) - files.keys()),
            'sessions': state['sessions'], 'findings': state['findings']}


def next_delay(streak):
    return min(300, 2 ** min(max(streak, 1), 9))


def postpone(c, task, message, status=503):
    now = time.time()
    ready = (max(now + 1, task['retry_at']) if task['retry_at'] and task['http'] in TEMPORARY
             else now + next_delay(task['streak'] + 1))
    update(c, task['id'], state='retry', broker=None, ready=ready, retry_at=ready, http=status, error=message,
           retries=task['retries'] + 1, streak=task['streak'] + 1)
    event(c, task['id'], '等待恢复', http=status, retry_at=ready, detail=message)


def prepare(store, task, repo):
    ident = task['id']
    if not prepare_source(store, task, repo):
        return
    with store.tx() as c:
        event(c, ident, '枚举候选文件')
    preview = executor(ident, 'preview', {'max_input_tokens': task['config'].get('max_input_tokens')})
    if not isinstance(preview.get('files'), list):
        raise ReviewError('OCR 预览结果格式无效')
    # 对照 Git 树，保留原生预览未列出的文件及其排除状态。
    output = run_git(['ls-tree', '-rz', '--name-only', 'HEAD'], cwd=workspace(ident) / 'source')
    paths = output.decode('utf-8', 'replace').strip('\0').split('\0') if output else []
    listed = {row['path'] for row in preview['files']}
    omitted = [{'path': path, 'will_review': False, 'exclude_reason': 'OCR 枚举阶段排除'} for path in paths if path not in listed]
    preview['files'].extend(omitted)
    preview['excluded_count'] += len(omitted)
    with store.tx() as c:
        row = store.target(ident, c)
        if row['state'] == 'stopping':
            return
        update(c, ident, preview=preview, state='previewed' if row['state'] == 'previewing' else 'queued',
               error='', retry_at=0, ready=0)
        event(c, ident, '预览完成', candidates=preview['reviewable_count'], excluded=preview['excluded_count'])


def publish(store, task, repo):
    ident = task['id']
    folder = store.report_dir(ident)
    folder.mkdir(parents=True, exist_ok=True)
    if task['preview']['reviewable_count']:
        for fmt, filename in NATIVE_FILES.items():
            if fmt == 'json' or fmt in task['specification']['formats']:
                if not (folder / filename).is_file():
                    executor(ident, fmt, destination=folder / filename)
        raw = json.loads((folder / 'result.json').read_bytes())
    else:
        raw = {'status': 'no_reviewable_files', 'comments': []}
        (folder / 'result.json').write_text(json.dumps(raw), encoding='utf-8')
    if task['preview']['reviewable_count'] and len(raw.get('comments') or []) != task['progress'].get('findings', 0):
        # 原生最终输出和逐文件检查点必须指向同一完整扫描结果。
        raise ReviewError('原生报告意见数与保存的检查点不一致')
    ocr_reports.save(folder, ocr_reports.normalized(raw, task, repo))
    with store.tx() as c:
        row = store.target(ident, c)
        if row['state'] == 'exporting':
            update(c, ident, state='complete', error='', broker=None)
            event(c, ident, '报告已生成')


def stop_target(store, task):
    ident = task['id']
    if not executor(ident, 'stop', {}).get('stopped'):
        return
    for path in (workspace(ident), store.report_dir(ident)):
        if path.exists():
            shutil.rmtree(path)
    with store.tx() as c:
        c.execute('DELETE FROM ocr_events WHERE target=?', (ident,))
        c.execute('DELETE FROM ocr_event_receipts WHERE target=?', (ident,))
        update(c, ident, state='stopped', broker=None, preview={}, progress={}, config={}, sha='', current='',
               error='', http=0, retries=0, retry_at=0, attempt=None, reused=None, stopped_at=time.time())


def reuse_previous(c, task):
    if not task['plan'] or not task['sha']:
        return None
    prior = c.execute('''SELECT t.id,t.state,t.config FROM ocr_targets t
                         WHERE t.repository=? AND t.branch=? AND t.sha=?
                         AND t.id!=? AND (t.state='complete' OR (t.created<? AND
                         t.state IN ('running','retry','exporting','queued','attention')))
                         ORDER BY t.created DESC''',
                      (task['repository'], task['branch'], task['sha'], task['id'], task['created'])).fetchall()
    for row in prior:
        prior_config = json.loads(row['config'])
        prior_config.pop('resume_budget', None)
        prior_config.setdefault('max_input_tokens', DEFAULT_INPUT_TOKENS)
        if prior_config != task['config']:
            continue
        if row['state'] != 'complete' or not task['specification'].get('force'):
            return row['id']
    return None


def advance(store, task):
    ident = task['id']
    state = task['state']
    if state == 'stopping':
        return stop_target(store, task)
    repo = store.repo(task['repository'])
    allowed(store, task, repo)
    if not task['preview']:
        return prepare(store, task, repo)
    if state == 'exporting':
        return publish(store, task, repo)
    if not all(task['config'].get(key) for key in ('model', 'api_base_url', 'api_key')):
        raise ReviewError('扫描模型未配置')
    native = executor(ident, 'status')
    with store.tx() as c:
        task = store.target(ident, c)
        if task['state'] == 'stopping':
            return
        reused = reuse_previous(c, task) if not task['attempt'] else None
        if reused:
            update(c, ident, state='skipped', reused=reused)
            event(c, ident, '已有相同目标任务', task=reused)
            return
        progress = snapshot_progress(task, native)
        if task['progress'] != progress or task['current'] != native['current']:
            update(c, ident, progress=progress, current=native['current'])
            before = set(task['progress'].get('files', {}))
            for file in progress['files'].keys() - before:
                event(c, ident, '已保存文件检查点', file=file)
        for notification in native.get('tools', []):
            cursor = c.execute('INSERT OR IGNORE INTO ocr_event_receipts VALUES (?,?)', (ident, notification['id']))
            if cursor.rowcount:
                event(c, ident, notification['kind'], file=notification['file'], tool=notification['tool'])
        if not native['stopped']:
            return
        total = task['preview']['reviewable_count']
        receipt = native.get('receipt') or {}
        summary_failed = (native.get('summary_missing') and native['current'] == '__scan_project_summary__'
                          and bool(task['error']))
        summary_rejected = summary_failed and task['http'] in (400, 413, 422)
        if not total or (len(progress['files']) == total and native['session']
                         and (not summary_failed or summary_rejected) and receipt.get('code') == 0):
            update(c, ident, state='exporting', broker=None, error='', http=0, retry_at=0)
            if summary_rejected:
                event(c, ident, '模型拒绝项目汇总请求，交付完整文件报告', http=task['http'])
            event(c, ident, '核对覆盖率并导出报告')
            return
        if state == 'running' and native['attempt'] == task['attempt']:
            if native.get('budget_exceeded'):
                # 旧执行器可能在升级前因累计用量退出，沿用同一任务的检查点。
                update(c, ident, state='queued', broker=None, ready=0, retry_at=0, http=0, error='')
                event(c, ident, '接续旧版暂停的扫描')
                return
            if task['http'] and task['http'] not in TEMPORARY:
                update(c, ident, state='attention', broker=None, error=task['error'])
                event(c, ident, '等待人工处理')
                return
            if (not task['http'] and receipt.get('code', -1) >= 0 and native['failures']
                    and not native.get('transient')):
                update(c, ident, state='attention', broker=None, error='部分文件未完成，请核对 OCR 失败记录')
                event(c, ident, '等待人工处理')
                return
            postpone(c, task, 'OCR 已停止，等待接续已保存的检查点', task['http'] or 503)
            return
    acquired = store.attempt(ident)
    if not acquired:
        return
    attempt, capability = acquired
    executor(ident, 'launch', {'attempt': attempt, 'token': capability, 'model': task['config']['model'],
                               'max_input_tokens': task['config'].get('max_input_tokens')})


def run(store, stop):
    from services.upgrade_maintenance import active

    scans = ScanStore(store.path)
    while not stop.wait(1):
        if active():
            continue
        try:
            with scans.lock('ocr-dispatch'):
                now = time.time()
                scans.schedule(now)
                with contextlib.closing(scans.connect()) as c:
                    # 活跃执行优先核对，然后继续报告、预览和新任务。
                    row = c.execute("""SELECT id FROM ocr_targets
                      WHERE state IN ('stopping','running') OR (ready<=? AND state IN ('exporting','previewing','queued','retry'))
                      ORDER BY CASE state WHEN 'stopping' THEN 0 WHEN 'running' THEN 1 WHEN 'exporting' THEN 2 ELSE 3 END,created LIMIT 1""", (now,)).fetchone()
                if row:
                    tick(scans, scans.target(row['id']))
        except ReviewError as exc:
            if str(exc) != '该 PR 正在由其他执行者发布':
                logging.warning('整仓扫描调度失败：%s', type(exc).__name__)
        except Exception as exc:
            logging.error('整仓扫描调度故障：%s', type(exc).__name__)


def tick(store, task):
    try:
        advance(store, task)
    except (ReviewError, requests.RequestException, OSError, ValueError) as exc:
        with store.tx() as c:
            row = store.target(task['id'], c)
            if row['state'] == 'stopping':
                return
            if isinstance(exc, AccessRevoked):
                update(c, row['id'], state='attention', broker=None, error=str(exc))
            elif isinstance(exc, requests.RequestException):
                if row['state'] in ('previewing', 'exporting', 'queued'):
                    update(c, row['id'], broker=None, ready=time.time()+15, error='等待 OCR 执行器重新连接')
                    event(c, row['id'], '等待执行器')
                else:
                    postpone(c, row, '等待 OCR 执行器或权限服务恢复')
            else:
                update(c, row['id'], state='attention', broker=None,
                       error=str(exc) if isinstance(exc, ReviewError) else '无法处理 OCR 扫描结果')
                event(c, row['id'], '等待人工处理')


def forward(config, key, data, heartbeat=None):
    destination = endpoint(config['api_base_url'], 'AI_REVIEW_MODEL_HOSTS') + '/chat/completions'
    # OCR 使用非流式原生工具协议。记录收到的模型用量，避免流式报文半途计为检查点。
    data = dict(data, model=config['model'], stream=False)
    data.pop('stream_options', None)
    if config.get('thinking_protocol') == 'deepseek' or urlsplit(destination).hostname == 'api.deepseek.com':
        data['thinking'] = {'type': 'disabled'}
    from .model_transport import open_model_stream

    transport = open_model_stream(destination, data, key, connect_timeout=8, timeout=120, heartbeat=heartbeat)
    try:
        content = bytearray()
        for part in transport.chunks():
            content.extend(part)
            if len(content) > MAX_RESPONSE:
                raise ReviewError('模型响应超过上限')
        return transport.status, bytes(content), transport.safe_headers.get('retry-after')
    finally:
        transport.close()


def register(broker, store):
    scans = ScanStore(store.path)

    @broker.post('/ocr/v1/chat/completions')
    def ocr_completion():
        from services.upgrade_maintenance import active

        capability = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if len(capability) != 64:
            return jsonify(error='扫描会话授权无效'), 401
        digest = hashlib.sha256(capability.encode()).hexdigest()
        with contextlib.closing(scans.connect()) as c:
            row = c.execute("SELECT id FROM ocr_targets WHERE broker=? AND state='running'", (digest,)).fetchone()
        if not row:
            return jsonify(error='扫描会话授权已失效'), 401
        ident = row['id']
        task = scans.target(ident)
        if active():
            return jsonify(error='系统维护中'), 503
        if task['retry_at'] > time.time():
            return jsonify(error='等待模型重新连接'), 429, {'Retry-After': str(max(1, int(task['retry_at'] - time.time()) + 1))}
        if request.content_length is None or request.content_length > 16 * 1024 * 1024:
            return jsonify(error='模型请求超过上限'), 413
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get('messages'), list) or payload.get('model') != task['config']['model']:
            return jsonify(error='OCR 模型请求格式无效'), 400
        status, content, server_delay = 503, b'{}', None
        try:
            allowed(scans, task, scans.repo(task['repository']))
            with scans.tx() as c:
                row = scans.target(ident, c)
                if row['broker'] != digest or row['state'] != 'running':
                    return jsonify(error='扫描已停止'), 401
                event(c, ident, '向模型发送 OCR 请求', file=row['current'])
            def heartbeat():
                active_task = scans.target(ident)
                if active_task['state'] != 'running' or active_task['broker'] != digest:
                    raise AccessRevoked('扫描已终止')
                if active():
                    raise requests.RequestException('系统进入维护')

            status, content, server_delay = forward(task['config'], scans.unseal(task['config']['api_key']),
                                                    payload, heartbeat=heartbeat)
        except AccessRevoked:
            status = 403
        except ReviewError:
            status = 400
        except requests.RequestException:
            pass
        headers = {}
        with scans.tx() as c:
            row = scans.target(ident, c)
            if row['broker'] != digest or row['state'] != 'running':
                return jsonify(error='扫描已停止'), 401
            if 200 <= status < 300:
                try:
                    count = json.loads(content).get('usage', {}).get('total_tokens', 0)
                    count = count if type(count) is int and 0 <= count < 100_000_000 else 0
                except (ValueError, TypeError, AttributeError):
                    count = 0
                update(c, ident, tokens=row['tokens'] + count, retry_at=0, streak=0, error='')
                if row['streak']:
                    event(c, ident, '模型连接已恢复', file=row['current'])
                else:
                    event(c, ident, '收到模型响应', file=row['current'], tokens=count)
            else:
                message = '模型请求失败（HTTP %s）' % status
                when = 0
                if status in TEMPORARY:
                    delay = retry_after(server_delay, time.time()) if server_delay else None
                    delay = max(1, min(86400, int(delay))) if delay is not None else next_delay(row['streak'] + 1)
                    headers['Retry-After'] = str(delay)
                    when = time.time() + delay
                update(c, ident, http=status, retry_at=when, retries=row['retries'] + int(bool(when)),
                       streak=row['streak'] + 1, error=message)
                event(c, ident, '模型请求异常', http=status, retry_at=when, file=row['current'], detail=message)
                return jsonify(error={'message': message, 'type': 'manager_upstream'}), status, headers
        return Response(content, status=status, mimetype='application/json')
