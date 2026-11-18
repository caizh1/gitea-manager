"""通过真实本地 HTTP 网关控制收尾顺序，回归最终审查结果的收集竞态。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import http.client
from itertools import permutations
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "review-runtime"))
from codex_session import CodexRun, Gateway, NativeError


REVIEW = {
    "findings": [{
        "priority": 1, "title": "[P1] 复制循环越过有效范围",
        "body": "长度等于缓冲区容量时，索引 len 导致越界读写。",
        "code_location": {"absolute_file_path": "after/code.c", "line_range": {"start": 1, "end": 1}},
    }],
    "overall_explanation": "已核对循环边界和缓冲区容量。",
}


def message_output(value):
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
    return [{"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": text}]}]


class ControlledResponses:
    """结束事件立即转发，HTTP 流结束由测试显式放行。"""

    def __init__(self, run):
        self.run = run
        self.calls = []
        self.clients = []
        self.texts = []

    def model_response_start(self, *_):
        pass

    def model_response_chunk(self, *_):
        pass

    def model_response_end(self, *_):
        pass

    def result_text(self, text):
        self.texts.append(text)

    def post(self, _, json, **kwargs):
        call = self.calls[json["test_call"]]

        def chunks(_):
            yield call["frame"]
            assert call["release"].wait(20), "测试未释放响应流"
            if call["failure"]:
                raise OSError("模拟上游流中断")

        return nullcontext(SimpleNamespace(
            status_code=200, headers={"X-Review-Call-ID": str(json["test_call"])},
            iter_content=chunks))

    def close(self):
        pass

    def prepare(self, output, failure=False):
        index = len(self.calls)
        response = {"type": "response.completed",
                    "response": {"status": "completed", "output": output}}
        frame = ("data: " + json.dumps(response) + "\n\n").encode()
        self.calls.append({"frame": frame, "release": threading.Event(), "failure": failure})
        return index

    def start(self, output, compacting=False, failure=False):
        index = self.prepare(output, failure)
        body = {"test_call": index}
        if compacting:
            body["client_metadata"] = {"x-codex-turn-metadata": json.dumps({"request_kind": "compaction"})}
        client = http.client.HTTPConnection("127.0.0.1", self.run.gateway.server.server_port, timeout=3)
        self.clients.append(client)
        client.request("POST", "/v1/responses", json.dumps(body),
                       {"Authorization": "Bearer " + self.run.gateway.token})
        result = client.getresponse()
        assert result.status == 200
        assert result.readline() == self.calls[index]["frame"].splitlines(keepends=True)[0]
        assert result.readline() == b"\n"
        return index

    def release(self, index):
        gateway = self.run.gateway
        with gateway.condition:
            before = gateway.active_calls
            self.calls[index]["release"].set()
            assert gateway.condition.wait_for(lambda: gateway.active_calls < before, timeout=3)


@pytest.fixture
def collection(tmp_path, monkeypatch):
    run = object.__new__(CodexRun)
    run.root = tmp_path
    run.broker = "http://broker.invalid"
    run.capability = "测试授权"
    run.session_id = "测试会话"
    run.response_limit = 32768
    run.remaining = lambda: 30
    run.cancel_requested = threading.Event()
    run.native_tools = True
    run.native_commands = []
    run.tool_errors = []
    run.phase = "primary"
    run.final_review = None
    run.turn_completed = False
    run.diagnostic = {}
    run.emit = lambda *args, **kwargs: None
    transport = ControlledResponses(run)
    run.raw_diagnostics = transport
    monkeypatch.setattr("codex_session.requests.Session", lambda: transport)
    run.gateway = Gateway(run)
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        yield run, transport, executor
    finally:
        for call in transport.calls:
            call["release"].set()
        executor.shutdown(wait=True)
        with run.gateway.condition:
            assert run.gateway.condition.wait_for(lambda: run.gateway.active_calls == 0, timeout=3)
        for client in transport.clients:
            client.close()
        run.gateway.close()


def complete_cli(run, text="CLI 已收到完整结果"):
    run._consume({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
    run._consume({"type": "turn.completed"})


@pytest.mark.parametrize("order", list(permutations(range(3))))
@pytest.mark.parametrize("phase", ["primary", "refutation-0", "history-0"])
def test_out_of_order_responses_deliver_latest_review(collection, order, phase):
    run, transport, executor = collection
    run.phase = phase
    review = {"issue_id": "历史问题", "status": "resolved"} if phase == "history-0" else REVIEW
    transport.start([{"type": "function_call", "name": "exec_command"}])
    transport.start(message_output(review))
    transport.start(message_output("上下文压缩摘要"), compacting=True)
    complete_cli(run)
    result = executor.submit(run._result)
    for index in order[:-1]:
        transport.release(index)
        assert not result.done(), "全部响应收尾前不能发布审查结果"
    transport.release(order[-1])
    value = result.result(timeout=3)
    assert value["_review_completion"]["complete"] is True
    if phase == "history-0":
        assert json.loads(value["overall_explanation"]) == review
    else:
        assert value["findings"] == REVIEW["findings"]
    assert json.loads(transport.texts[0]) == review


def test_collection_can_wait_beyond_previous_ten_second_limit(collection):
    run, transport, executor = collection
    transport.start(message_output(REVIEW))
    complete_cli(run)
    result = executor.submit(run._result)
    # 模拟 HTTP 在结束事件之后继续收尾，仍在任务自身期限内。
    threading.Event().wait(10.2)
    assert not result.done()
    transport.release(0)
    assert result.result(timeout=3)["findings"] == REVIEW["findings"]


@pytest.mark.parametrize("latest", [[], message_output("  ")])
def test_empty_latest_response_never_reuses_an_earlier_review(collection, latest):
    run, transport, _ = collection
    transport.start(message_output(REVIEW))
    transport.start(latest)
    complete_cli(run)
    transport.release(1)
    transport.release(0)
    with pytest.raises(NativeError) as failure:
        run._result()
    assert failure.value.code == "codex_response_incomplete"
    assert failure.value.diagnostic["reason"] == "empty_final_text"


@pytest.mark.parametrize("broken_call", [0, 1])
def test_interrupted_stream_cannot_publish_a_completed_review(collection, broken_call):
    run, transport, _ = collection
    transport.start([{"type": "function_call", "name": "exec_command"}], failure=broken_call == 0)
    transport.start(message_output(REVIEW), failure=broken_call == 1)
    complete_cli(run)
    transport.release(1)
    transport.release(0)
    with pytest.raises(NativeError) as failure:
        run._result()
    assert failure.value.code == "codex_response_incomplete"


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_collection_respects_cancellation_and_task_deadline(collection, stop):
    run, transport, executor = collection
    transport.start(message_output(REVIEW))
    complete_cli(run)
    result = executor.submit(run._result)
    if stop == "cancel":
        run.cancel_requested.set()
    else:
        run.remaining = lambda: 0
    with pytest.raises(NativeError) as failure:
        result.result(timeout=3)
    assert failure.value.code == ("user_cancelled" if stop == "cancel" else "deadline_exceeded")


@pytest.mark.parametrize("text,code", [
    ("{", "codex_review_json_invalid"),
    (json.dumps(REVIEW) + json.dumps(REVIEW), "codex_review_json_invalid"),
    ('{"findings": "无"}', "codex_review_shape_invalid"),
])
def test_complete_but_invalid_review_is_rejected(collection, text, code):
    run, transport, _ = collection
    transport.start(message_output(text))
    complete_cli(run)
    transport.release(0)
    with pytest.raises(NativeError) as failure:
        run._result()
    assert failure.value.code == code


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_cli_exit_before_http_cleanup_persists_completed_session(tmp_path, monkeypatch, order):
    """替身 CLI 通过真实子进程先退出，执行器仍须保存成功结果和终态。"""
    import subprocess

    turn_seen = threading.Event()
    events = []

    def emit(kind, **kwargs):
        events.append((kind, kwargs))
        if kind == "model.usage":
            turn_seen.set()
        return len(events)

    reporter = SimpleNamespace(
        emit=emit, close=lambda: None, closed=True, queue=[], acked_sequence=0,
        thread=SimpleNamespace(is_alive=lambda: False))
    monkeypatch.setattr("native_session.EventReporter", lambda *args, **kwargs: reporter)
    run = CodexRun({
        "job_id": "test-result-job", "session_id": "test-result-session",
        "model": "测试模型", "deadline_at": time.time() + 30,
        "event_url": "http://events.invalid", "event_token": "测试事件授权",
        "capability": "测试模型授权", "prompt": "审查测试代码", "codex_native_tools": True,
    }, tmp_path / "project", tmp_path / "runtime", "http://broker.invalid")
    run.project.mkdir()
    (run.project / "after").mkdir()
    code = "for (size_t i = 0; i <= len; i++) dst[i] = src[i];\n"
    (run.project / "after" / "code.c").write_text(code)
    run._prepare_workspace = lambda: run.root.mkdir(parents=True, exist_ok=True)
    transport = ControlledResponses(run)
    transport.prepare([{"type": "function_call", "name": "exec_command"}])
    transport.prepare(message_output(REVIEW))
    monkeypatch.setattr("codex_session.requests.Session", lambda: transport)
    monkeypatch.setattr("codex_session.subprocess.check_output", lambda *args, **kwargs: "codex-cli 0.169.0")
    cli = '''
import http.client, json, os, sys
sys.stdin.read()
clients = []
for index in range(2):
    client = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=3)
    clients.append(client)
    client.request("POST", "/v1/responses", json.dumps({"test_call": index}),
                   {"Authorization": "Bearer " + os.environ["REVIEW_CODEX_TOKEN"]})
    response = client.getresponse()
    event = json.loads(response.readline()[6:])
    response.readline()
text = event["response"]["output"][0]["content"][0]["text"]
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
print(json.dumps({"type": "turn.completed"}))
'''
    original_popen = subprocess.Popen
    monkeypatch.setattr("codex_session.subprocess.Popen", lambda args, **kwargs: original_popen(
        [sys.executable, "-c", cli, str(run.gateway.server.server_port)], **kwargs))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run.run)
        try:
            assert turn_seen.wait(5)
            assert run.process.wait(timeout=3) == 0
            transport.release(order[0])
            assert not future.done(), "终态必须等待所有响应完成收尾"
            transport.release(order[1])
            future.result(timeout=5)
        finally:
            for call in transport.calls:
                call["release"].set()
    assert run.status == "completed", run.diagnostic
    assert run.error_code is None
    saved_result = json.loads(run.result_path.read_text())
    assert saved_result["findings"] == REVIEW["findings"]
    from ai_review.codex_review import normalize
    snapshot = {
        "id": "固定源码", "codex_tools": "native",
        "diff": "diff --git a/code.c b/code.c\n--- a/code.c\n+++ b/code.c\n@@ -1 +1 @@\n-"
                + code.replace("i <= len", "i < len") + "+" + code,
    }
    assert normalize(saved_result, snapshot, run.project)[0]["severity"] == "P1"
    terminal = json.loads(run.terminal_path.read_text())
    assert terminal["status"] == "completed"
    assert terminal["result_available"] is True
    assert terminal["result_sha256"]
    assert [kind for kind, _ in events][-1] == "session.completed"
