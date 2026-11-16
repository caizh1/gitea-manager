"""执行器专用的只读源码工具，使用标准输入输出 MCP 协议。"""

import fnmatch
import json
import hashlib
from pathlib import Path
import sys


class SourceTools:
    def __init__(self, root, audit):
        self.root = Path(root).resolve()
        self.audit = Path(audit)

    def resolve(self, path):
        relative = Path(path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or relative.parts[0] not in ("before", "after")
        ):
            raise ValueError("只允许读取当前快照的 before 或 after")
        target = self.root / relative
        if any(p.is_symlink() for p in [target, *target.parents] if p != self.root.parent):
            raise ValueError("不能跟随链接")
        target.resolve().relative_to(self.root)
        return target

    def call(self, name, args):
        if name == "changes":
            target = self.root / "change.diff"
            if target.is_symlink() or not target.is_file():
                raise ValueError("固定版本变更内容不可用")
            return {
                "diff": target.read_text(encoding="utf-8"),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        if name == "read":
            path = args["path"]
            target = self.resolve(path)
            if not target.is_file() or target.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("文件不存在或超过单文件读取上限")
            lines = target.read_text(encoding="utf-8").splitlines()
            start = args.get("start", 1)
            count = args.get("count", 100)
            if type(start) is not int or type(count) is not int or start < 1 or not 1 <= count <= 200:
                raise ValueError("读取行范围无效")
            end = min(len(lines), start + count - 1)
            result = {
                "path": path,
                "line_start": start,
                "line_end": end,
                "total_lines": len(lines),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "lines": [{"line": i + 1, "text": lines[i]} for i in range(start - 1, end)],
            }
            if len(json.dumps(result).encode()) > 128 * 1024:
                raise ValueError("本次读取过大，请缩小行范围")
            with self.audit.open("a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
            return result
        if name in ("list", "search"):
            side = args.get("side", "after")
            base = self.resolve(side)
            pattern = args.get("pattern", "*")
            if not isinstance(pattern, str) or len(pattern) > 256:
                raise ValueError("路径匹配条件无效")
            query = args.get("query", "")
            if name == "search" and (not isinstance(query, str) or not 1 <= len(query) <= 256):
                raise ValueError("搜索条件无效")
            results = []
            for p in sorted(base.rglob("*")):
                if (
                    not p.is_file()
                    or p.is_symlink()
                    or not fnmatch.fnmatch(p.relative_to(base).as_posix(), pattern)
                ):
                    continue
                path = p.relative_to(self.root).as_posix()
                if name == "list":
                    results.append({"path": path})
                elif p.stat().st_size <= 4 * 1024 * 1024:
                    try:
                        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                            if query in line:
                                results.append(
                                    {
                                        "path": path,
                                        "line": i,
                                        "text": line[:1000],
                                        "truncated": len(line) > 1000,
                                    }
                                )
                                if len(results) >= 100:
                                    break
                    except UnicodeError:
                        continue
                if len(results) >= 100:
                    return {"matches": results, "limited": True}
            return {"matches": results, "limited": False}
        raise ValueError("未知的只读工具")


def main():
    tools = SourceTools(sys.argv[1], sys.argv[2])
    schemas = [
        {
            "name": "changes",
            "description": "读取固定版本的完整 PR diff 及其摘要",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "read",
            "description": "按行读取固定版本源码，路径以 before/ 或 after/ 开头",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer"},
                    "count": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "list",
            "description": "列出固定版本文件，pattern 使用通配符，最多返回 100 项",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "side": {"type": "string", "enum": ["before", "after"]},
                    "pattern": {"type": "string"},
                },
            },
        },
        {
            "name": "search",
            "description": "按字面字符串搜索固定版本源码，最多返回 100 项",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "side": {"type": "string", "enum": ["before", "after"]},
                    "pattern": {"type": "string"},
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    ]
    for schema in schemas:
        schema["annotations"] = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
    for line in sys.stdin:
        try:
            req = json.loads(line)
            method = req.get("method")
            if "id" not in req:
                continue
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "review", "version": "1"},
                }
            elif method == "tools/list":
                result = {"tools": schemas}
            elif method == "tools/call":
                try:
                    value = tools.call(req["params"]["name"], req["params"].get("arguments", {}))
                    result = {
                        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                        "isError": False,
                    }
                except (ValueError, KeyError, OSError, UnicodeError) as exc:
                    result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            elif method == "ping":
                result = {}
            else:
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": req["id"],
                            "error": {"code": -32601, "message": "方法不存在"},
                        }
                    ),
                    flush=True,
                )
                continue
            print(
                json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}, ensure_ascii=False),
                flush=True,
            )
        except (ValueError, KeyError):
            continue


if __name__ == "__main__":
    main()
