"""Codex Responses 网关，沿用任务授权并记录完整终态。"""

import hashlib
import contextlib
import json
import time

from flask import Response, jsonify, request, stream_with_context

from .common import ReviewError, encode, endpoint
from .model_transport import open_model_stream


class ResponseStream:
    def __init__(self, limit, probe=False, native_tools=False):
        self.pending = bytearray()
        self.total = 0
        self.limit = limit
        self.completed = None
        self.failed = False
        self.probe = probe
        self.native_tools = native_tools

    def validate_item(self, item):
        kind = item.get("type", "")
        if kind in ("message", "reasoning", ""):
            return
        if self.native_tools and kind in ("function_call", "custom_tool_call"):
            return
        names = ("read", "list", "search", "changes")
        allowed = kind == "function_call" and (
            item.get("namespace") == "mcp__review"
            and item.get("name") in names
            or item.get("name") in tuple("mcp__review__" + name for name in names)
            or self.probe
            and item.get("name") == "read_probe"
        )
        if not allowed:
            raise ReviewError("模型返回未授权工具调用")

    def feed(self, chunk):
        self.total += len(chunk)
        if self.total > self.limit:
            raise ReviewError("Responses 输出超过限制")
        self.pending.extend(chunk)
        import re

        while True:
            match = re.search(rb"\r?\n\r?\n", self.pending)
            if not match:
                break
            raw = self.pending[: match.start()]
            self.pending = self.pending[match.end() :]
            data = b"\n".join(line[5:].strip() for line in raw.splitlines() if line.startswith(b"data:"))
            if not data:
                continue
            event = json.loads(data)
            self.validate_item(event.get("item") or {})
            if event.get("type") == "response.completed":
                if self.completed:
                    raise ReviewError("Responses 返回重复结束事件")
                self.completed = event["response"]
                for item in self.completed.get("output", []):
                    self.validate_item(item)
            if event.get("type") in ("response.failed", "response.incomplete", "error"):
                self.failed = True

    def finish(self):
        if (
            self.pending.strip()
            or self.failed
            or not self.completed
            or self.completed.get("status") != "completed"
        ):
            raise ReviewError("Responses 未完整结束，未接受审查结论")
        return self.completed


def register(app, store):
    @app.post("/v1/responses")
    def responses():
        from .authorization import authorize
        from services.upgrade_maintenance import active as maintenance

        transport = None
        call_id = None
        try:
            body = request.get_json(silent=True)
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            if not isinstance(body, dict) or body.get("stream") is not True:
                raise ReviewError("仅支持流式 Responses")
            with store.tx() as c:
                cap = c.execute("SELECT * FROM capabilities WHERE token_hash=?", (token_hash,)).fetchone()
                if (
                    not cap
                    or cap["expires"] < time.time()
                    or cap["remaining"] == 0
                    or body.get("model") != cap["model"]
                ):
                    raise ReviewError("任务授权已失效")
                job = dict(store._active(c, cap["job"], cap["owner"]))
                config = store.job_config(cap["job"], c)
                if config.get("engine") != "codex_cli":
                    raise ReviewError("该任务未使用 Codex 引擎")
                snapshot_row = c.execute("SELECT snapshot FROM jobs WHERE id=?", (cap["job"],)).fetchone()
                snapshot = json.loads(snapshot_row[0] or "{}")
                native_tools = snapshot.get("codex_tools") == "native"
                if not native_tools:
                    # 历史任务沿用原 MCP 协议，新任务直接转发 CLI 原生工具。
                    body["tools"] = [
                        tool for tool in body.get("tools", [])
                        if tool.get("type") == "namespace" and tool.get("name") == "mcp__review"
                        or tool.get("type") == "function" and tool.get("name") in (
                            "mcp__review__read", "mcp__review__list", "mcp__review__search", "mcp__review__changes"
                        )
                    ]
                    for tool in body["tools"]:
                        if any(t.get("type") != "function" or t.get("name") not in ("read", "list", "search", "changes")
                               for t in tool.get("tools", [])):
                            raise ReviewError("历史审查请求包含未授权工具")
                output_limit = config.get("model_max_output_tokens", 0)
                if output_limit > 0:
                    body["max_output_tokens"] = output_limit
                else:
                    body.pop("max_output_tokens", None)
                row = c.execute(
                    "SELECT model_calls,session_id,repository FROM jobs WHERE id=?", (cap["job"],)
                ).fetchone()
                if row["session_id"] != request.headers.get("X-Review-Session"):
                    raise ReviewError("模型调用与任务当前会话不匹配")
                # 相同会话与完整请求禁止重放，包括流中断后的 CLI 自动重试。
                serialized = encode(body)
                fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
                if c.execute(
                    "SELECT 1 FROM review_native_calls WHERE job=? AND session_id=? AND request_hash=?",
                    (cap["job"], row["session_id"], fingerprint),
                ).fetchone():
                    raise ReviewError("该模型请求已提交，禁止隐式重放")
                call_id = hashlib.sha256((token_hash + str(row["model_calls"])).encode()).hexdigest()[:32]
                # Codex 按冻结的 Token 窗口和阈值压缩历史，上游校验实际窗口。
                # 请求体仍受执行器与 Flask 的传输大小限制，包括压缩请求。
                c.execute(
                    "INSERT INTO review_native_calls VALUES (?,?,?,?,?,?,?,?)",
                    (
                        call_id,
                        token_hash,
                        cap["job"],
                        row["session_id"],
                        fingerprint,
                        "started",
                        time.time(),
                        time.time(),
                    ),
                )
                c.execute(
                    "UPDATE jobs SET model_calls=model_calls+1,stage='Codex 自主检索与审查' WHERE id=?",
                    (cap["job"],),
                )
                c.execute(
                    "INSERT INTO review_model_inputs VALUES (?,?,?,?)",
                    (cap["job"], row["model_calls"] + 1, serialized, time.time()),
                )
                if cap["remaining"] > 0:
                    c.execute(
                        "UPDATE capabilities SET remaining=remaining-1 WHERE token_hash=?", (token_hash,)
                    )
            repo = store.repo(row["repository"])

            def guard():
                with contextlib.closing(store.connect()) as c:
                    current = c.execute(
                        "SELECT expires FROM capabilities WHERE token_hash=?", (token_hash,)
                    ).fetchone()
                if current is None or current["expires"] < time.time():
                    raise ReviewError("模型调用授权已撤销")
                store.active(cap["job"], cap["owner"])
                if maintenance() or time.time() >= cap["expires"]:
                    raise ReviewError("任务期限或维护门禁已生效")
                authorize(repo, job)

            guard()
            outbound = dict(
                body,
                model=config["model"],
                store=False,
            )
            if body.get("previous_response_id"):
                raise ReviewError("审查请求必须携带完整会话上下文")
            if config.get("thinking_protocol") == "deepseek":
                from .thinking import codex_effort

                outbound["reasoning"] = {"effort": codex_effort(config)}
            transport = open_model_stream(
                endpoint(config["api_base_url"], "AI_REVIEW_MODEL_HOSTS") + "/responses",
                outbound,
                store.unseal(config["api_key"]),
                timeout=min(config["model_request_timeout_seconds"], cap["expires"] - time.time()),
                heartbeat=guard,
            )
            if transport.status != 200:
                raise ReviewError(f"Responses 服务返回 HTTP {transport.status}")
            collector = ResponseStream(config.get("model_response_max_mib", 8) * 1024 * 1024,
                                       native_tools=native_tools)

            @stream_with_context
            def generate():
                state = "failed"
                try:
                    for chunk in transport.chunks(guard):
                        collector.feed(chunk)
                        yield chunk
                    value = collector.finish()
                    guard()
                    state = "completed"
                    usage = value.get("usage") or {}
                    with store.tx() as c:
                        c.execute(
                            "INSERT OR REPLACE INTO review_codex_usage VALUES (?,?,?,?,?)",
                            (
                                call_id,
                                cap["job"],
                                int(usage.get("input_tokens", 0)),
                                int(usage.get("output_tokens", 0)),
                                encode(usage),
                            ),
                        )
                    store.update(
                        cap["job"],
                        cap["owner"],
                        diagnostic={
                            "source": "responses",
                            "call_id": call_id,
                            "status": "completed",
                            "usage": usage,
                        },
                    )
                finally:
                    transport.close()
                    with store.tx() as c:
                        c.execute(
                            "UPDATE review_native_calls SET status=?,updated=? WHERE call_id=?",
                            (state, time.time(), call_id),
                        )

            return Response(generate(), content_type="text/event-stream")
        except (ReviewError, ValueError, KeyError) as exc:
            if transport:
                transport.close()
            if call_id:
                with store.tx() as c:
                    c.execute(
                        "UPDATE review_native_calls SET status='failed',updated=? WHERE call_id=?",
                        (time.time(), call_id),
                    )
            return jsonify(error={"message": str(exc)}), 400


def verify_connection(store, config):
    import requests

    try:
        return _verify_connection(store, config)
    except requests.RequestException as exc:
        raise ReviewError("Responses 连接验证失败 " + type(exc).__name__) from None


def _verify_connection(store, config):
    """检查模型目录、流式响应及工具结果往返。"""
    import requests

    key = store.unseal(config["api_key"])
    base = endpoint(config["api_base_url"], "AI_REVIEW_MODEL_HOSTS")
    tool = {
        "type": "function",
        "name": "read_probe",
        "description": "读取验收文本",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    initial = [{"role": "user", "content": "请调用 read_probe 获取校验值，再返回该值。"}]
    with requests.Session() as client:
        client.trust_env = False
        headers = {"Authorization": "Bearer " + key}
        directory = client.get(base + "/models", headers=headers, timeout=(5, 20), allow_redirects=False)
        directory.raise_for_status()
        listed = config["model"] in [r.get("id") for r in directory.json().get("data", [])]

        def invoke(items, choice):
            body = {
                "model": config["model"],
                "input": items,
                "tools": [tool],
                "tool_choice": choice,
                "stream": True,
                "store": False,
                "reasoning": {"effort": "none"},
            }
            output_limit = config.get("model_max_output_tokens", 0)
            if output_limit > 0:
                body["max_output_tokens"] = output_limit
            collector = ResponseStream(1024 * 1024, probe=True)
            with client.post(
                base + "/responses",
                json=body,
                headers=headers,
                stream=True,
                timeout=(5, 90),
                allow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise ReviewError("Responses 连接验证失败，HTTP " + str(response.status_code))
                for chunk in response.iter_content(4096):
                    collector.feed(chunk)
            return collector.finish()

        first = invoke(initial, {"type": "function", "name": "read_probe"})
        calls = [i for i in first["output"] if i.get("type") == "function_call"]
        if len(calls) != 1 or calls[0].get("name") != "read_probe":
            raise ReviewError("模型未返回预期的工具调用")
        second = invoke(
            initial
            + first["output"]
            + [
                {
                    "type": "function_call_output",
                    "call_id": calls[0]["call_id"],
                    "output": "验收值为 REVIEW_OK",
                }
            ],
            "auto",
        )
        text = "".join(p.get("text", "") for i in second["output"] for p in i.get("content", []))
        if "REVIEW_OK" not in text:
            raise ReviewError("模型未完成工具结果往返")
        return {
            "listed": listed,
            "message": "Responses 流式接口及工具结果往返通过",
            "protocol": "responses",
            "model": config["model"],
        }
