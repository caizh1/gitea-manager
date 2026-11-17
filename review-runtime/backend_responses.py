"""执行器 Responses 流完整性解析，与 Manager 使用同一验证规则。"""

import json


class ReviewError(ValueError):
    pass


def protocol_error(message, reason, **details):
    error = ReviewError(message)
    error.error_code = "CODEX_RESPONSE_PROTOCOL"
    error.diagnostic = dict(source="codex_protocol", reason=reason, **details)
    return error


class ResponseStream:
    def __init__(self, limit, probe=False, native_tools=False):
        self.pending = bytearray()
        self.total = 0
        self.limit = limit
        self.completed = None
        self.failed = False
        self.failure_details = {}
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
            raise protocol_error("模型返回未授权工具调用", "unauthorized_tool", event_type=kind)

    def feed(self, chunk):
        self.total += len(chunk)
        if self.total > self.limit:
            raise protocol_error("Responses 输出超过限制", "output_too_large",
                                 received_bytes=self.total, limit_bytes=self.limit)
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
            try:
                event = json.loads(data)
            except (ValueError, UnicodeError) as exc:
                raise protocol_error("Responses 事件格式无效", "json_syntax",
                                     received_bytes=self.total,
                                     json_line=getattr(exc, "lineno", None),
                                     json_column=getattr(exc, "colno", None)) from None
            if not isinstance(event, dict) or not isinstance(event.get("item") or {}, dict):
                raise protocol_error("Responses 事件结构无效", "event_shape")
            self.validate_item(event.get("item") or {})
            if event.get("type") == "response.completed":
                if self.completed:
                    raise protocol_error("Responses 返回重复结束事件", "duplicate_terminal")
                self.completed = event.get("response")
                if not isinstance(self.completed, dict) or not isinstance(self.completed.get("output", []), list):
                    raise protocol_error("Responses 结束事件结构无效", "event_shape")
                for item in self.completed.get("output", []):
                    if not isinstance(item, dict):
                        raise protocol_error("Responses 输出结构无效", "event_shape")
                    self.validate_item(item)
            if event.get("type") in ("response.failed", "response.incomplete", "error"):
                self.failed = True
                response = event.get("response") or {}
                response = response if isinstance(response, dict) else {}
                failure = response.get("error") or event.get("error") or {}
                failure = failure if isinstance(failure, dict) else {}
                incomplete = response.get("incomplete_details") or {}
                self.failure_details = {
                    "event_type": event.get("type"),
                    "upstream_error_code": failure.get("code") or event.get("code"),
                    "upstream_error_type": failure.get("type"),
                    "finish_reason": incomplete.get("reason") if isinstance(incomplete, dict) else None,
                }

    def finish(self):
        if (
            self.pending.strip()
            or self.failed
            or not self.completed
            or self.completed.get("status") != "completed"
        ):
            reason = "responses_failed" if self.failed else "missing_terminal"
            raise protocol_error("Responses 未完整结束，未接受审查结论", reason,
                                 received_bytes=self.total, **self.failure_details)
        return self.completed



StreamCollector = ResponseStream
