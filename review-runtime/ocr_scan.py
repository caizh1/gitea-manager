"""整仓扫描只调用固定版 OCR，并持久化原生会话和逐文件检查点。"""
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

from flask import jsonify, request, send_file

VERSION = 'v1.14.4'
TASK_ID = re.compile(r'[0-9a-f]{32}\Z')
BROKER_KEY = re.compile(r'[0-9a-f]{64}\Z')
ARTIFACTS = {'json': 'result.json', 'txt': 'result.txt', 'sarif': 'result.sarif', 'native': 'native.html'}
MAX_OUTPUT = 64 * 1024 * 1024
processes = {}
mutex = threading.Lock()


def load(path, default=None):
    try:
        if path.stat().st_size > MAX_OUTPUT:
            raise ValueError('扫描数据超过大小上限')
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return default


def write(path, value):
    pending = path.with_suffix('.tmp')
    with pending.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)


def root(task):
    return Path(os.environ.get('AI_REVIEW_NATIVE_ROOT', '/app/review-native')) / 'ocr' / task


def stop_marker(folder):
    return folder.parent / 'stopped' / folder.name


def source(task):
    return Path(os.environ.get('AI_REVIEW_WORKSPACE_ROOT', '/app/review-workspaces')) / 'ocr' / task / 'source'


def ocr_env(folder):
    return {'HOME': str(folder / 'home'), 'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1', 'LANG': 'C.UTF-8'}


def native_command(folder, path, *args):
    return [os.environ.get('AI_REVIEW_OCR_BINARY', '/opt/review/ocr'), *args, '--repo', str(path)]


def run(folder, path, *args, timeout=150):
    return subprocess.run(native_command(folder, path, *args), env=ocr_env(folder),
                          cwd=path, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=timeout, check=True)


def session_state(folder):
    """只接纳 OCR 写完的 JSONL 行；一份损坏的会话不能覆盖之前的检查点。"""
    base = folder / 'home/.opencodereview/sessions'
    index = {}
    failures = {}
    sessions = {}
    current = ''
    events = []
    for path in sorted(base.glob('*/*.jsonl'), key=lambda file: file.stat().st_mtime):
        if path.name == 'manager-resume.jsonl':
            continue
        files = {}
        errors = {}
        header = None
        with path.open('rb') as stream:
            for line in stream:
                if not line.endswith(b'\n'):
                    break
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                action = row.get('type')
                file = row.get('filePath') or row.get('newPath')
                if action == 'session_start':
                    header = row
                elif action in ('review_item_done', 'review_item_reused') and file and row.get('fingerprint'):
                    files[file] = row
                    errors.pop(file, None)
                elif action == 'review_item_failed' and file:
                    files.pop(file, None)
                    errors[file] = row
                elif action == 'llm_request' and file:
                    current = file
                elif action in ('tool_call', 'tool_result') and file and row.get('uuid'):
                    tool = row.get('toolName') or row.get('tool_name') or row.get('name') or ''
                    tool = tool if isinstance(tool, str) and re.fullmatch(r'[a-z_]{1,50}', tool) else ''
                    events.append({'id': row['uuid'], 'file': file,
                                   'kind': '调用 OCR 工具' if action == 'tool_call' else 'OCR 工具执行完成',
                                   'tool': tool})
        for file, row in files.items():
            index.setdefault(file, row)
            failures.pop(file, None)
        for file, row in errors.items():
            if file not in index:
                failures[file] = row
        if header:
            sessions[path.stem] = (path, header, files)
    return index, failures, sessions, current, events


def prepare_resume(folder):
    index, _, sessions, _, _ = session_state(folder)
    if not index:
        return []
    for ident, (_, _, entries) in reversed(list(sessions.items())):
        if entries.keys() == index.keys() and all(entries[key]['fingerprint'] == row['fingerprint']
                                                  for key, row in index.items()):
            return ['--resume', ident]
    file, start, _ = next(iter(sessions.values()))
    recovered = file.with_name('manager-resume.jsonl')
    with recovered.with_suffix('.tmp').open('w', encoding='utf-8') as stream:
        for row in [dict(start, sessionId='manager-resume'), *index.values()]:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    recovered.with_suffix('.tmp').replace(recovered)
    return ['--resume', 'manager-resume']


def preview(folder, path):
    target = folder / 'preview.json'
    run(folder, path, 'scan', '--preview', '--format', 'json', '--output', str(target))
    return load(target)


def finish(process, lock, folder, attempt):
    try:
        code = process.wait()
        write(folder / 'receipt.json', {'attempt': attempt, 'code': code})
    finally:
        with mutex:
            processes.pop(folder.name, None)
        lock.close()


def launch(folder, path, data):
    attempt = data['attempt']
    if not TASK_ID.fullmatch(attempt) or not BROKER_KEY.fullmatch(data['token']):
        raise ValueError('扫描授权格式无效')
    model = data['model']
    if not isinstance(model, str) or not 1 <= len(model) <= 128:
        raise ValueError('扫描模型无效')
    with mutex:
        if stop_marker(folder).exists() or folder.name in processes:
            return
        if (load(folder / 'started.json') or {}).get('attempt') == attempt:
            return
        folder.mkdir(parents=True, exist_ok=True)
        lock = (folder / 'execution.lock').open('a')
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            config = folder / 'home/.opencodereview'
            config.mkdir(mode=0o700, parents=True, exist_ok=True)
            broker = os.environ.get('AI_REVIEW_BROKER_URL', 'http://review-worker:5051/v1')
            write(config / 'config.json', {'provider': 'manager', 'language': 'zh-CN',
                  'custom_providers': {'manager': {'url': broker.removesuffix('/v1') + '/ocr/v1',
                  'protocol': 'openai', 'api_key': data['token'], 'model': model}}})
            resume = prepare_resume(folder)
            result = folder / 'result.json'
            result.unlink(missing_ok=True)
            (folder / 'receipt.json').unlink(missing_ok=True)
            for name in ('result.txt', 'result.sarif', 'native.html'):
                (folder / name).unlink(missing_ok=True)
            # OCR 会再次把复用意见送进批次去重。续扫保留已确认意见，避免模型二次改写。
            if resume:
                resume.append('--no-dedup')
            command = native_command(folder, path, 'scan', '--batch', 'none', '--concurrency', '1',
                                     '--format', 'json', '--output', str(result), *resume)
            write(folder / 'started.json', {'attempt': attempt, 'at': time.time()})
            with (folder / 'scan.log').open('wb') as err:
                process = subprocess.Popen(command, env=ocr_env(folder), cwd=path,
                                           stdin=subprocess.DEVNULL, stdout=err, stderr=err,
                                           start_new_session=True, pass_fds=(lock.fileno(),))
            processes[folder.name] = process
            threading.Thread(target=finish, args=(process, lock, folder, attempt), daemon=True).start()
        except BaseException:
            lock.close()
            raise


def stopped(folder):
    if not folder.exists():
        return True
    with (folder / 'execution.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False


def snapshot(folder):
    index, errors, sessions, current, events = session_state(folder)
    result = load(folder / 'result.json', {}) if stopped(folder) else {}
    return {'stopped': stopped(folder), 'attempt': (load(folder / 'started.json') or {}).get('attempt'),
            'receipt': load(folder / 'receipt.json'), 'session': result.get('session_id'),
            'current': current, 'files': {path: row['fingerprint'] for path, row in index.items()},
            'failures': sorted(errors), 'warnings': result.get('warnings') or [],
            'tools': events[-800:],
            'transient': any(re.search(r'\b(?:429|500|502|503|504)\b|timeout|connection refused|'
                                       r'connection reset|unexpected eof|context canceled', str(row.get('error', '')), re.I)
                             for row in errors.values()),
            'budget_exceeded': bool(result.get('summary', {}).get('budget_exceeded')),
            'summary_missing': bool(result.get('comments')) and not result.get('project_summary'),
            'findings': sum(len(row.get('comments') or []) for row in index.values()),
            'sessions': list(sessions)}


def export(folder, path, fmt):
    file = folder / ARTIFACTS[fmt]
    if file.is_file():
        return file
    resume = prepare_resume(folder)
    if not resume:
        raise ValueError('没有可导出的扫描会话')
    pending = file.with_name('.export-' + file.name)
    try:
        if fmt == 'native':
            result = load(folder / 'result.json', {})
            run(folder, path, 'session', 'export', result.get('session_id') or resume[1], '--output', str(pending))
        else:
            run(folder, path, 'scan', '--batch', 'none', '--concurrency', '1', '--no-summary', '--no-dedup',
                '--format', 'text' if fmt == 'txt' else fmt, '--output', str(pending), *resume)
        if pending.stat().st_size > MAX_OUTPUT:
            raise ValueError('扫描报告超过大小上限')
        pending.replace(file)
    finally:
        pending.unlink(missing_ok=True)
    return file


def register(app, authorized):
    @app.route('/ocr/<task>/<action>', methods=['GET', 'POST'])
    def ocr(task, action):
        if not authorized():
            return jsonify(error='执行器身份无效'), 403
        if not TASK_ID.fullmatch(task):
            return jsonify(error='任务编号无效'), 400
        folder, path = root(task), source(task)
        try:
            if request.method == 'GET' and action == 'status':
                return jsonify(snapshot(folder))
            if request.method == 'POST' and action == 'stop':
                with mutex:
                    marker = stop_marker(folder)
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    if not marker.exists():
                        write(marker, {'at': time.time()})
                    process = processes.get(task)
                    if process and process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if not stopped(folder):
                        return jsonify(stopped=False)
                    if folder.exists():
                        shutil.rmtree(folder)
                    return jsonify(stopped=True)
            if stop_marker(folder).exists():
                return jsonify(error='扫描已终止'), 410
            if not path.is_dir() or path.is_symlink():
                return jsonify(error='缺少扫描源码'), 404
            if request.method == 'POST' and action == 'preview':
                folder.mkdir(parents=True, exist_ok=True)
                return jsonify(preview(folder, path))
            if request.method == 'POST' and action == 'launch':
                launch(folder, path, request.get_json())
                return jsonify(ok=True)
            if request.method == 'GET' and action in ARTIFACTS and stopped(folder):
                file = export(folder, path, action)
                if file.stat().st_size <= MAX_OUTPUT:
                    return send_file(file, conditional=False)
            return jsonify(error='扫描执行器操作无效'), 409
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return jsonify(error='无法完成 OCR 操作，请检查源码、运行时或存储'), 503
