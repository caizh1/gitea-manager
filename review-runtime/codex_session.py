"""Linux Codex 原生 review 执行器，复用会话回执与停止机制。"""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import selectors
import subprocess
import sys
import threading

import requests

from native_session import SessionRun, NativeError, atomic_json

VERSION = "0.169.0"


class Gateway:
    def __init__(self, run):
        self.run = run
        self.final = None
        self.completed_calls = 0
        self.failure = None
        self.lock = threading.Lock()
        self.idle = threading.Event()
        self.idle.set()
        self.token = secrets.token_urlsafe(32)
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                if (
                    self.path != "/v1/responses"
                    or self.headers.get("Authorization") != "Bearer " + gateway.token
                ):
                    self.send_error(403)
                    return
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 16 * 1024 * 1024:
                    self.send_error(413)
                    return
                gateway.idle.clear()
                data = self.rfile.read(length)
                from backend_responses import StreamCollector

                collector = StreamCollector(run.response_limit)
                client = requests.Session()
                client.trust_env = False
                try:
                    body = json.loads(data)
                    with client.post(
                        run.broker + "/responses",
                        json=body,
                        stream=True,
                        headers={
                            "Authorization": "Bearer " + run.capability,
                            "X-Review-Session": run.session_id,
                        },
                        timeout=(5, max(1, run.remaining())),
                        allow_redirects=False,
                    ) as response:
                        self.send_response(response.status_code)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        if response.status_code != 200:
                            gateway.failure = "模型网关拒绝请求，HTTP " + str(response.status_code)
                            try:
                                reason = response.json().get("error", {}).get("message", "")
                                run.diagnostic = {"source": "model_gateway", "reason": str(reason)[:500]}
                            except ValueError:
                                pass
                            return
                        for chunk in response.iter_content(4096):
                            collector.feed(chunk)
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        final = collector.finish()
                        with gateway.lock:
                            gateway.final = final
                            gateway.completed_calls += 1
                        run.emit(
                            "model.completed",
                            summary="模型调用完成",
                            payload={"usage": final.get("usage", {})},
                        )
                except Exception as exc:
                    gateway.failure = "Responses 协议或传输失败 " + type(exc).__name__
                finally:
                    self.close_connection = True
                    client.close()
                    gateway.idle.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class CodexRun(SessionRun):
    def __init__(self, data, workspace, runtime_root, broker):
        super().__init__(data, workspace, runtime_root, broker)
        self.binary = Path(os.environ.get("AI_REVIEW_CODEX", "/opt/review/codex"))
        self.phase = data.get("codex_phase", "primary")
        self.tool_errors = []
        self.turn_completed = False
        self.final_review = None

    def authorize(self, data):
        with self.lock:
            if self.status in ("completed", "failed", "aborted", "unknown"):
                raise NativeError("session_terminal")
            if (
                data.get("job_id") != self.job_id
                or data.get("model") != self.model
                or data.get("deadline_at") != self.deadline
                or not isinstance(data.get("capability"), str)
                or not data["capability"]
            ):
                raise NativeError("session_authorization_mismatch")
            self.capability = data["capability"]
        return self.public()

    def abort(self, code="user_cancelled"):
        with self.lock:
            if self.done.is_set():
                return
            self.cancel_requested.set()
            self.error_code = code
        self._kill()

    def _config(self):
        model = json.loads(Path(__file__).with_name("codex_model.json").read_text())
        model.update(
            slug=self.model,
            display_name=self.model,
            context_window=self.model_context_tokens,
            max_context_window=self.model_context_tokens,
            auto_compact_token_limit=self.model_context_tokens * 4,
        )
        model["truncation_policy"] = {"mode": "tokens", "limit": self.model_context_tokens}
        catalog = self.root / "models.json"
        catalog.write_text(json.dumps({"models": [model]}, ensure_ascii=False))
        settings = {
            "model_catalog_json": str(catalog),
            "model": self.model,
            "review_model": self.model,
            "model_provider": "manager",
            "approval_policy": "never",
            "sandbox_mode": "read-only",
            "web_search": "disabled",
            "project_doc_max_bytes": 0,
            "model_context_window": self.model_context_tokens,
            "model_auto_compact_token_limit": self.model_context_tokens * 4,
            "model_reasoning_effort": "high" if self.thinking_enabled else "none",
            "features.shell_tool": False,
            "features.apply_patch_freeform": False,
            "features.multi_agent": False,
            "features.enable_request_compression": False,
            "features.responses_websockets": False,
            "features.memories": False,
            "model_providers.manager.name": "Manager",
            "model_providers.manager.base_url": self.gateway.url,
            "model_providers.manager.env_key": "REVIEW_CODEX_TOKEN",
            "model_providers.manager.wire_api": "responses",
            "model_providers.manager.request_max_retries": 0,
            "model_providers.manager.stream_max_retries": 0,
            "mcp_servers.review.command": sys.executable,
            "mcp_servers.review.args": [
                str(Path(__file__).with_name("review_tools.py")),
                str(self.project),
                str(self.root / "reads.jsonl"),
                str(max(1, self.retrieval_budget)),
            ],
            "mcp_servers.review.required": True,
            "mcp_servers.review.default_tools_approval_mode": "approve",
            "mcp_servers.review.enabled_tools": ["read", "list", "search", "changes"],
            "mcp_servers.review.tools.read.output_token_limit": 32768,
            "mcp_servers.review.tools.list.output_token_limit": 32768,
            "mcp_servers.review.tools.search.output_token_limit": 32768,
            "mcp_servers.review.tools.changes.output_token_limit": 32768,
        }
        args = []
        for key, value in settings.items():
            args += ["-c", key + "=" + json.dumps(value, ensure_ascii=False)]
        return args

    def _consume(self, event):
        kind = event.get("type")
        if kind == "thread.started":
            self.native_session_id = event.get("thread_id")
        elif kind in ("error", "turn.failed"):
            raise NativeError("codex_failed")
        elif kind == "item.completed":
            item = event.get("item", {})
            if item.get("type") in ("command_execution", "file_change", "web_search"):
                raise NativeError("codex_unexpected_tool")
            if item.get("type") == "mcp_tool_call":
                if item.get("status") != "completed":
                    self.tool_errors.append(item.get("tool", "unknown"))
                self.emit(
                    "tool.completed",
                    summary="源码检索完成",
                    payload={
                        "tool": item.get("tool", ""),
                        "status": item.get("status"),
                        "call_id": item.get("id"),
                        "path": item.get("arguments", {}).get("path"),
                    },
                )
            elif item.get("type") == "agent_message":
                self.final_review = item.get("text", "")
        elif kind == "turn.completed":
            self.turn_completed = True
            self.emit("model.usage", summary="原生审查回合完成", payload={"usage": event.get("usage", {})})

    def _result(self):
        if not self.gateway.idle.wait(timeout=min(10, self.remaining())):
            raise NativeError("codex_response_incomplete")
        if self.gateway.failure or not self.gateway.final or not self.final_review or not self.turn_completed:
            raise NativeError("codex_response_incomplete")
        final = self.gateway.final
        texts = [
            part.get("text", "")
            for item in final.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ]
        final_text = "".join(texts)
        self.raw_diagnostics.result_text(final_text)
        encoded = final_text.strip()
        if encoded.startswith("```json\n") and encoded.endswith("\n```"):
            encoded = encoded[8:-4].strip()
        try:
            result = json.loads(encoded)
        except ValueError:
            raise NativeError("codex_review_json_invalid") from None
        if self.phase.startswith("history-") and isinstance(result, dict) and "issue_id" in result:
            result = {"findings": [], "overall_explanation": json.dumps(result, ensure_ascii=False)}
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("findings"), list)
            or not isinstance(result.get("overall_explanation"), str)
        ):
            raise NativeError("codex_review_shape_invalid")
        result["_budget_exhausted"] = (self.root / "reads.exhausted").exists()
        result["_tool_errors"] = self.tool_errors
        result["_review_completion"] = {"complete": True, "finish_reason": "stop"}
        result["_codex_reads"] = []
        audit = self.root / "reads.jsonl"
        if audit.exists():
            result["_codex_reads"] = [json.loads(line) for line in audit.read_text().splitlines()]
        return result

    def run(self):
        try:
            if self.cancel_requested.is_set():
                raise NativeError("user_cancelled")
            self.status = "starting"
            self.emit("session.starting", summary="启动 Codex 原生 review")
            self._prepare_workspace()
            (self.root / "config").mkdir(exist_ok=True)
            version = subprocess.check_output([self.binary, "--version"], timeout=30, text=True).strip()
            if version != "codex-cli " + VERSION:
                raise NativeError("codex_version_mismatch")
            self.gateway = Gateway(self)
            args = [
                str(self.binary),
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--json",
                *self._config(),
                "review",
                "-",
            ]
            env = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(self.root),
                "CODEX_HOME": str(self.root / "config"),
                "LANG": "C.UTF-8",
                "REVIEW_CODEX_TOKEN": self.gateway.token,
            }
            self.process = subprocess.Popen(
                args,
                cwd=self.root,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )

            def submit():
                try:
                    self.process.stdin.write(self.prompt.encode())
                    self.process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass

            writer = threading.Thread(target=submit, daemon=True)
            writer.start()
            self.prompt_submitted = True
            self.status = "running"
            self.emit("session.running", summary="Codex 自主检索与审查")
            pending = bytearray()
            output_bytes = 0
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    if self.cancel_requested.is_set() or self.remaining() <= 0:
                        raise NativeError("deadline_exceeded" if self.remaining() <= 0 else "user_cancelled")
                    for key, _ in selector.select(0.2):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output_bytes += len(chunk)
                        if output_bytes > self.response_limit:
                            raise NativeError("codex_output_limit")
                        if key.data == "stderr":
                            continue
                        pending.extend(chunk)
                        if len(pending) > self.response_limit:
                            raise NativeError("codex_output_limit")
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            if line.strip():
                                self._consume(json.loads(line))
            if self.process.wait(timeout=5) != 0 or pending.strip():
                raise NativeError("codex_exit_failed")
            result = self._result()
            self.result_sha256 = hashlib.sha256(
                json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            atomic_json(self.result_path, result)
            self.result = result
            self.emit_terminal(
                "session.completed",
                status="completed",
                summary="Codex 原生审查完成",
                payload={
                    "result_sha256": self.result_sha256,
                    "review_complete": True,
                    "finish_reason": "stop",
                    "finding_count": len(result["findings"]),
                },
            )
            self.status = "completed"
        except Exception as exc:
            self.error_code = exc.code if isinstance(exc, NativeError) else "codex_runtime_failed"
            self.status = "aborted" if self.cancel_requested.is_set() else "failed"
            self.diagnostic = self.diagnostic or {
                "source": "codex",
                "reason": self.error_code,
                "exception_type": type(exc).__name__,
            }
            self.emit_terminal(
                "session.error",
                status=self.status,
                summary="Codex 审查未完成",
                payload={"error_code": self.error_code, "diagnostic": self.diagnostic},
            )
        finally:
            self._kill()
            for stream in (
                getattr(self.process, "stdin", None),
                getattr(self.process, "stdout", None),
                getattr(self.process, "stderr", None),
            ):
                if stream and not stream.closed:
                    stream.close()
            self.reporter.close()
            self._finalize_raw_diagnostics()
            try:
                self._persist_terminal()
            finally:
                self.done.set()
