"""执行器 Responses 流完整性解析，与 Manager 使用同一验证规则。"""

import json


class ReviewError(ValueError):
    pass


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


StreamCollector = ResponseStream
