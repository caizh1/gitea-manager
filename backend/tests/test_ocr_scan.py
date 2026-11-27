"""用固定 OCR CLI 验证文件级持久化和多轮模型错误后恢复。"""
import importlib.util
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest
from flask import Flask
from werkzeug.serving import make_server

from ai_review import ocr_worker
from ai_review.ocr_store import ScanStore, occurrences, recurrence, update


def runtime():
    location = Path(__file__).resolve().parents[2] / 'review-runtime/ocr_scan.py'
    spec = importlib.util.spec_from_file_location('ocr_scan_runtime', location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_for(callback, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = callback()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError('未获得预期的 OCR 状态')


def test_schedule_uses_explicit_timezone():
    plan = recurrence({'frequency': 'weekly', 'weekday': 4, 'time': '13:05', 'timezone': 'Asia/Shanghai'})
    from datetime import datetime, timezone
    now = datetime(2026, 11, 27, 5, 4, tzinfo=timezone.utc).timestamp()
    assert occurrences(plan, now, 3) == [now + 60 + week * 604800 for week in range(3)]


def test_merge_resume_checkpoints_never_uses_empty_latest(tmp_path):
    module = runtime()
    directory = tmp_path / 'home/.opencodereview/sessions/repo'
    directory.mkdir(parents=True)
    for index, names in enumerate((['前面'], ['这里新增'], [])):
        header = {'type': 'session_start', 'reviewMode': 'full_scan'}
        files = [{'type': 'review_item_done', 'filePath': name, 'fingerprint': name, 'comments': []} for name in names]
        with (directory / f'{index}.jsonl').open('w') as stream:
            for value in [header, *files]:
                stream.write(json.dumps(value) + '\n')
            stream.write('{"type":')
    assert module.prepare_resume(tmp_path) == ['--resume', 'manager-resume']
    assert set(module.session_state(tmp_path)[0]) == {'前面', '这里新增'}


def test_stop_marker_blocks_late_executor_request(tmp_path, monkeypatch):
    module = runtime()
    monkeypatch.setenv('AI_REVIEW_NATIVE_ROOT', str(tmp_path / 'native'))
    monkeypatch.setenv('AI_REVIEW_WORKSPACE_ROOT', str(tmp_path / 'workspaces'))
    task = 'a' * 32
    module.root(task).mkdir(parents=True)
    module.source(task).mkdir(parents=True)
    app = Flask(__name__)
    module.register(app, lambda: True)
    with app.test_client() as client:
        response = client.post(f'/ocr/{task}/stop')
        assert response.get_json() == {'stopped': True}
        assert not module.root(task).exists()
        response = client.post(f'/ocr/{task}/launch', json={'attempt': 'b' * 32, 'token': 'c' * 64,
                                                          'model': 'fixture'})
        assert response.status_code == 410
        assert not module.root(task).exists()


def test_real_ocr_resumes_after_multiple_gateway_errors(tmp_path, monkeypatch):
    binary = os.environ.get('OCR_TEST_BINARY')
    if not binary:
        pytest.skip('设置 OCR_TEST_BINARY 后运行官方 CLI 恢复测试')
    module = runtime()
    from cryptography.fernet import Fernet
    secret = tmp_path / 'master.key'
    secret.write_bytes(Fernet.generate_key())
    for key, value in {'AI_REVIEW_NATIVE_ROOT': str(tmp_path / 'native'),
                       'AI_REVIEW_WORKSPACE_ROOT': str(tmp_path / 'workspaces'),
                       'AI_REVIEW_OCR_BINARY': binary,
                       'AI_REVIEW_MASTER_KEY_FILE': str(secret)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ocr_worker, 'allowed', lambda *_: None)
    store = ScanStore(tmp_path / 'review.db')
    with store.tx() as connection:
        spec = {'mode': 'custom', 'formats': ['html', 'txt', 'sarif', 'native'],
                'targets': [{'repository': 1, 'branch': 'main'}]}
        profile = {'api_base_url': 'https://example.com', 'model': 'fixture', 'api_key': store.seal('test-key')}
        connection.execute('INSERT INTO ocr_profiles VALUES (1,?)', (json.dumps(profile),))
        batch = store.create(connection, spec, {})
        task = connection.execute('SELECT id FROM ocr_targets WHERE batch=?', (batch,)).fetchone()[0]
        # 模拟旧计划已有预算以及曾经接近旧上限的检查点数据。
        connection.execute('UPDATE ocr_batches SET specification=? WHERE id=?',
                           (json.dumps(dict(spec, budget=1)), batch))
        update(connection, task, state='queued', tokens=99_999_990, config=dict(profile, resume_budget=2))
    repo = {'repo_name': 'qa/ocr-test', 'gitea_url': 'https://gitea.example.test'}
    monkeypatch.setattr(store.__class__, 'repo', lambda *_: repo)
    source = module.source(task)
    source.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q'], cwd=source, check=True)
    for index in range(5):
        (source / f'sample_{index}.py').write_text(
            f'def sample_{index}(values):\n    first = values[0]\n    last = values[-1]\n    return first + last\n')
    subprocess.run(['git', 'add', '.'], cwd=source, check=True)
    subprocess.run(['git', '-c', 'user.name=验收', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', '验收样本'], cwd=source, check=True)
    folder = module.root(task)
    folder.mkdir(parents=True)
    preview = module.preview(folder, source)
    with store.tx() as connection:
        update(connection, task, preview=preview, sha='1' * 40)
    calls, failures, dedup = [], [], []
    errors = iter([500, 429, 500, 429, 500, 429, 500, 429])
    allow_finish = threading.Event()
    failure_seen = threading.Event()

    def forward(config, key, body, heartbeat=None):
        text = json.dumps(body['messages'])
        matched = re.search(r'sample_(\d)', text)
        index = int(matched.group(1)) if matched and body.get('tools') else -1
        if index == 4 and not allow_finish.is_set():
            status = next(errors, 429)
            failures.append(status)
            failure_seen.set()
            return status, b'{}', '1'
        calls.append(index)
        prior = [row for row in body['messages'] if row['role'] == 'tool']
        if not prior and index >= 0:
            arguments = {'comments': [
                {'content': '空列表会引发异常', 'existing_code': 'first = values[0]'},
                {'content': '函数需要说明输入约束', 'existing_code': f'def sample_{index}(values):'},
                {'content': '负索引依赖序列类型', 'existing_code': 'last = values[-1]'},
                {'content': '两项必须支持相加', 'existing_code': 'return first + last'},
            ]}
            name = 'code_comment'
        else:
            name, arguments = 'task_done', {'state': 'DONE'}
        result = {'id': 'fixture', 'object': 'chat.completion', 'model': 'fixture',
                  'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                      {'id': f'call_{len(calls)}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}
                  ]}, 'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 30, 'completion_tokens': 10, 'total_tokens': 40}}
        if index < 0:
            content = '扫描汇总完成。'
            if '<batch_comments>' in text:
                dedup.append(allow_finish.is_set())
                ids = ['c-0', 'c-1', 'c-2', 'c-3']
                groups = ([{'members': ids}] if allow_finish.is_set() else [{'members': [ident]} for ident in ids])
                content = json.dumps({'groups': groups})
            elif allow_finish.is_set():
                return 400, json.dumps({'error': {'code': 'context_length_exceeded'}}).encode(), None
            result['choices'][0]['message'] = {'role': 'assistant', 'content': content}
            result['choices'][0]['finish_reason'] = 'stop'
        return 200, json.dumps(result).encode(), None

    monkeypatch.setattr(ocr_worker, 'forward', forward)
    app = Flask(__name__)
    ocr_worker.register(app, store)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    monkeypatch.setenv('AI_REVIEW_BROKER_URL', f'http://127.0.0.1:{server.server_port}/v1')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def execute(ident, action, data=None, destination=None):
        assert ident == task
        if action == 'status':
            return module.snapshot(folder)
        if action == 'launch':
            return module.launch(folder, source, data)
        destination.write_bytes(module.export(folder, source, action).read_bytes())

    monkeypatch.setattr(ocr_worker, 'executor', execute)

    def tick():
        current = store.target(task)
        if current['state'] == 'retry' and current['ready'] > time.time():
            return current
        ocr_worker.tick(store, current)
        return store.target(task)

    try:
        tick()
        assert failure_seen.wait(25)
        snapshot = wait_for(lambda: (found if len(found := module.snapshot(folder)['files']) == 4 else None))
        assert set(snapshot) == {f'sample_{index}.py' for index in range(4)}
        saved = wait_for(lambda: (row if (row := tick())['state'] == 'retry' else None), timeout=45)
        assert saved['progress']['files'] == snapshot
        assert len(failures) >= 6
        count = len(calls)
        # 使用新的 Manager Store 进程视角，只从持久化检查点与等待时间恢复。
        store = ScanStore(tmp_path / 'review.db')
        allow_finish.set()
        final = wait_for(lambda: (row if (row := tick())['state'] == 'complete' else None), timeout=45)
        assert len(final['progress']['files']) == preview['reviewable_count'] == 5
        assert final['tokens'] > 100_000_000
        assert all(final['progress']['files'][file] == digest for file, digest in snapshot.items())
        assert not set(calls[count:]) & {0, 1, 2, 3}
        assert {429, 500}.issubset(failures)
        output = store.report_dir(task)
        assert dedup == [False] * 4
        assert len(json.loads((output / 'result.json').read_text())['comments']) == 20
        assert (output / 'native.html').stat().st_size > 0
        assert (output / 'result.sarif').stat().st_size > 0
        assert (output / 'result.txt').stat().st_size > 0
        assert (output / 'report.html').stat().st_size > 0
        with store.tx() as connection:
            assert connection.execute('SELECT 1 FROM ocr_events WHERE target=? AND kind=?',
                                      (task, '模型拒绝项目汇总请求，交付完整文件报告')).fetchone()
            update(connection, task, state='stopping')
        used = store.target(task)['tokens']
        monkeypatch.setattr(ocr_worker, 'executor', lambda *_: {'stopped': True})
        ocr_worker.stop_target(store, store.target(task))
        assert store.target(task)['state'] == 'stopped'
        assert store.target(task)['tokens'] == used
        assert not store.report_dir(task).exists()
    finally:
        process = module.processes.get(task)
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        server.shutdown()
