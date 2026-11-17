"""Codex 首因、原始证据与终态发布顺序的回归。"""

import hashlib
import io
import json
from pathlib import Path
import sys
import time
import zipfile
from types import SimpleNamespace

from flask import Flask
import pytest

from ai_review.diagnostics import safe_remote
from ai_review.common import ReviewError
from ai_review.model_transport import ModelTransportError
from ai_review.responses import ResponseStream, http_error, register
from ai_review.store import Store
from ai_review.worker import _failure_diagnostic


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "review-runtime"))
from backend_responses import ResponseStream as RuntimeResponseStream
from codex_session import CodexRun
from native_session import NativeError
from raw_diagnostics import RawDiagnostics
from native_gateway import decode_upstream_error


@pytest.mark.parametrize("parser_type", [ResponseStream, RuntimeResponseStream])
def test_responses_error_keeps_structured_cause(parser_type):
    parser = parser_type(4096)
    event = {"type": "response.incomplete", "response": {
        "error": {"code": "context_length_exceeded", "message": "不应进入公开诊断的正文"},
        "incomplete_details": {"reason": "max_output_tokens"}}}
    parser.feed(("data: " + json.dumps(event) + "\n\n").encode())
    with pytest.raises((ValueError, ReviewError)) as failure:
        parser.finish()
    diagnostic = safe_remote(failure.value.diagnostic)
    assert diagnostic["event_type"] == "response.incomplete"
    assert diagnostic["upstream_error_code"] == "context_length_exceeded"
    assert diagnostic["finish_reason"] == "max_output_tokens"
    assert "不应进入" not in json.dumps(diagnostic, ensure_ascii=False)


@pytest.fixture
def gateway_client(tmp_path, monkeypatch):
    store = Store(tmp_path / "review.db")
    store.save_config({"revision": 1, "enabled": True, "debounce_seconds": 0,
                       "engine": "codex_cli"})
    repo = store.save_repo(1, 1, {"enabled": True, "gitea_url": "http://test",
                                 "repo_name": "test/code"})
    job_id = store.enqueue(repo["id"], 1, {"head": "b" * 40, "base": "a" * 40})
    job = store.claim()
    store.update(job_id, job["owner"], session_id="codex-test-session", snapshot={"codex_tools": "native"})
    with store.tx() as connection:
        connection.execute("INSERT INTO capabilities VALUES (?,?,?,?,?,?)", (
            hashlib.sha256(b"test-capability").hexdigest(), job_id, job["owner"],
            time.time() + 60, -1, store.config()["model"]))
    monkeypatch.setattr("ai_review.responses.endpoint", lambda *_: "https://example.invalid/v1")
    monkeypatch.setattr(store, "unseal", lambda _: "测试密钥")
    app = Flask(__name__)
    register(app, store)
    client = app.test_client()
    headers = {"Authorization": "Bearer test-capability", "X-Review-Session": "codex-test-session"}
    body = {"model": store.config()["model"], "stream": True, "input": [{"role": "user", "content": "审查"}]}
    return client, headers, body, store, job


@pytest.mark.parametrize("failure_kind", ["http", "timeout", "stream"])
def test_gateway_persists_cause_before_codex_exit(gateway_client, monkeypatch, failure_kind):
    client, headers, body, store, job = gateway_client

    class Transport:
        status = 429 if failure_kind == "http" else 200
        metrics = {"received_bytes": 23}

        def chunks(self, _):
            yield b'data: {"type":"response.created"}\n\n'
            raise ModelTransportError("ReadTimeout", {"received_bytes": 42})

        def close(self):
            pass

    def open_stream(*_args, **_kwargs):
        if failure_kind == "timeout":
            raise ModelTransportError("ConnectTimeout")
        return Transport()

    monkeypatch.setattr("ai_review.responses.open_model_stream", open_stream)
    response = client.post("/v1/responses", json=body, headers=headers, buffered=True)
    current = store.job(job["id"], evidence=False)
    diagnostic = current["diagnostic"]
    assert diagnostic["status"] == "failed"
    assert diagnostic["call_id"]
    assert diagnostic["session_id"] == "codex-test-session"
    if failure_kind == "http":
        assert diagnostic["upstream_http_status"] == 429
    else:
        assert diagnostic["error_code"] == "MODEL_TIMEOUT"
    if failure_kind == "stream":
        assert response.status_code == 200
        assert b'"type":"error"' in response.data
        assert diagnostic["phase"] == "response_stream"
    else:
        assert response.status_code == 400
        assert response.json["diagnostic"]["error_code"] == diagnostic["error_code"]
    error = NativeError("codex_failed")
    error.error_code = "EXECUTOR_CODEX_FAILED"
    error.diagnostic = {"source": "codex", "reason": "codex_failed", "session_id": "codex-test-session"}
    preserved, _ = _failure_diagnostic(current, error, job)
    assert preserved["error_code"] == diagnostic["error_code"]


def test_codex_error_and_stderr_are_kept_without_local_credentials(tmp_path):
    run = object.__new__(CodexRun)
    run.raw_diagnostics = RawDiagnostics(tmp_path, "job-diagnostics-123", "session-diagnostics-123")
    run.capability = "private-capability-value"
    run.gateway = SimpleNamespace(token="private-gateway-value")
    run.process = SimpleNamespace(stderr=io.BytesIO(
        "执行器错误 private-capability-value private-gateway-value\n".encode()))
    run.stderr_bytes = 0
    run._drain_codex_stderr()
    run.diagnostic = {}
    with pytest.raises(NativeError):
        run._consume({"type": "error"})
    assert safe_remote(run.diagnostic)["reason"] == "codex_failed"
    saved = (run.raw_diagnostics.root / "codex-stderr.raw").read_text()
    assert "执行器错误" in saved
    assert "private-capability-value" not in saved
    assert "private-gateway-value" not in saved
    assert run.stderr_bytes > 0


@pytest.mark.parametrize("oversized", [False, True])
def test_http_error_body_is_bounded_redacted_and_decodable(oversized):
    secret = "private-upstream-key"
    body = json.dumps({"error": {"code": "invalid_parameter", "message": "参数错误 " + secret}},
                      ensure_ascii=False).encode()
    if oversized:
        body += b" " * (256 * 1024)
    transport = SimpleNamespace(status=400, safe_headers={}, chunks=lambda _: iter([body]))
    error = http_error(transport, lambda: None, "call-12345678", secret)
    details, raw = decode_upstream_error({"_review_upstream_error": error.upstream_error}, "call-12345678")
    assert secret.encode() not in raw
    assert "参数错误".encode() in raw
    assert len(raw) <= 128 * 1024
    assert details["error_truncated"] is oversized


def test_codex_finalizes_diagnostics_before_failure_is_visible():
    run = object.__new__(CodexRun)
    run.cancel_requested = SimpleNamespace(is_set=lambda: True)
    run.status = "running"
    run.diagnostic = {}
    run.stderr_bytes = 0
    run.process = None
    order = []
    run._diagnostic = lambda value: value
    run._log_failure = lambda: None
    run._kill = lambda: None
    run._persist_terminal = lambda: None
    run.reporter = SimpleNamespace(close=lambda: None)
    run.done = SimpleNamespace(set=lambda: None)

    def finalize():
        order.append(("diagnostics", run.status))

    run._finalize_raw_diagnostics = finalize
    run.emit_terminal = lambda *_args, **_kwargs: order.append(("terminal", run.status))
    run.run()
    assert order[:2] == [("diagnostics", "running"), ("terminal", "aborted")]


def test_process_failure_keeps_original_error_and_ready_archive(tmp_path, monkeypatch):
    import native_session

    events = []

    class Reporter:
        acked_sequence = 0
        closed = False
        queue = []
        thread = SimpleNamespace(is_alive=lambda: False)

        def __init__(self, *_args, **_kwargs):
            pass

        def emit(self, kind, **details):
            events.append((kind, details))
            return len(events)

        def close(self):
            self.closed = True

    monkeypatch.setattr(native_session, "EventReporter", Reporter)
    executable = tmp_path / "fake-codex"
    executable.write_text(f'#!{sys.executable}\n' + '''
import json
import sys
if '--version' in sys.argv:
    print('codex-cli 0.169.0')
else:
    sys.stdin.read()
    print('模型响应缺少必需字段 private-capability-value', file=sys.stderr, flush=True)
    print(json.dumps({'type': 'error', 'message': '执行失败的原始正文 private-capability-value'}, ensure_ascii=False), flush=True)
    sys.exit(1)
''')
    executable.chmod(0o755)
    workspace = tmp_path / "source"
    workspace.mkdir()
    runtime = tmp_path / "runtime"
    (runtime / "results").mkdir(parents=True)
    (runtime / "sessions").mkdir()
    run = CodexRun({"job_id": "job-diagnostics-123", "session_id": "session-diagnostics-123",
                    "model": "测试模型", "deadline_at": time.time() + 30,
                    "event_url": "http://127.0.0.1:1/events",
                    "event_token": "测试事件凭据", "capability": "private-capability-value",
                    "prompt": "测试审查", "codex_native_tools": True}, workspace, runtime,
                   "http://127.0.0.1:1")
    run.binary = executable
    original_kill = run._kill

    def finish_test_process():
        # 夹具进程在发出错误后自行退出，避开 macOS 对刚退出进程组的信号限制。
        if run.process:
            run.process.wait(timeout=3)
        original_kill()

    run._kill = finish_test_process
    run.run()
    assert run.status == "failed"
    terminal = next(details for kind, details in events if kind == "session.error")
    assert terminal["payload"]["raw_diagnostics"]["available"] is True
    assert terminal["payload"]["raw_diagnostics"]["status"] == "ready"
    archive, _ = run.raw_diagnostics.archive()
    with zipfile.ZipFile(archive) as bundle:
        original = bundle.read("event-payloads.raw").decode()
        stderr = bundle.read("codex-stderr.raw").decode()
    assert "执行失败的原始正文" in original
    assert "模型响应缺少必需字段" in stderr
    assert "private-capability-value" not in original + stderr
