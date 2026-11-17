"""Linux Codex 原生 review 执行器，复用会话回执与停止机制。"""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import selectors
import subprocess
import sys
import time
import threading

import requests

from native_session import SessionRun, NativeError, atomic_json, drain_redacted
from runtime_diagnostics import safe_exception, safe_token

VERSION = "0.169.0"


def decode_review(text):
    """接受完整原生结果及外围说明，拒绝截断和多个冲突结果。"""
    encoded = text.strip()
    if encoded.startswith("```json\n") and encoded.endswith("\n```"):
        encoded = encoded[8:-4].strip()
    try:
        return json.loads(encoded)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    results = []
    for index, char in enumerate(encoded):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(encoded, index)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("findings"), list) and isinstance(value.get("overall_explanation"), str):
            results.append(value)
    if len(results) != 1:
        raise NativeError("codex_review_json_invalid")
    return results[0]


def is_compaction(body, headers):
    """读取固定 Codex 版本在 Responses 请求中声明的操作类型。"""
    metadata = body.get("client_metadata")
    raw = metadata.get("x-codex-turn-metadata") if isinstance(metadata, dict) else None
    try:
        value = json.loads(raw or headers.get("x-codex-turn-metadata", "{}"))
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and value.get("request_kind") == "compaction"


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

                collector = StreamCollector(run.response_limit, native_tools=run.native_tools)
                client = requests.Session()
                client.trust_env = False
                try:
                    body = json.loads(data)
                    compacting = is_compaction(body, self.headers)
                    if compacting:
                        run.emit("session.compaction", status="running", summary="Codex 正在压缩上下文")
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
                            gateway.failure = "codex_gateway_failed"
                            diagnostic = {"source": "model_gateway", "phase": "response_headers",
                                          "reason": "broker_http_error", "http_status": response.status_code}
                            try:
                                value = response.json()
                                if isinstance(value, dict) and isinstance(value.get("diagnostic"), dict):
                                    diagnostic.update(value["diagnostic"])
                            except (ValueError, requests.RequestException):
                                pass
                            run.diagnostic = diagnostic
                            return
                        call_id = response.headers.get("X-Review-Call-ID") or secrets.token_hex(16)
                        run.raw_diagnostics.model_response_start({"call_id": call_id, "stream": True,
                                                                  "broker_http_status": response.status_code})
                        for chunk in response.iter_content(4096):
                            run.raw_diagnostics.model_response_chunk(call_id, chunk)
                            collector.feed(chunk)
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        final = collector.finish()
                        with gateway.lock:
                            if not compacting:
                                gateway.final = final
                            gateway.completed_calls += 1
                        if compacting:
                            run.emit("session.compaction", status="completed", summary="Codex 上下文压缩完成")
                        run.emit(
                            "model.completed",
                            summary="模型调用完成",
                            payload={"usage": final.get("usage", {})},
                        )
                except Exception as exc:
                    gateway.failure = "codex_gateway_failed"
                    run.diagnostic = run.diagnostic or getattr(exc, "diagnostic", None) or safe_exception(
                        exc, source="model_transport", phase="response_stream", reason="response_stream_failed")
                finally:
                    if 'call_id' in locals():
                        run.raw_diagnostics.model_response_end(call_id, "failed" if gateway.failure else "completed")
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
        self.native_tools = data.get("codex_native_tools", False)
        self.native_commands = []
        self.command_started = {}
        self.stderr_bytes = 0
        self.git_workspace = data.get('workspace_layout') in ('git-worktree-v1', 'git-worktree-v2')
        self.git_objects = self.project.parents[1] / 'objects.git' if self.project.parent.name == 'worktrees' else self.project.parent / 'objects.git'
        self.source_head = data.get('source_head')
        self.reasoning_effort = data.get('reasoning_effort', 'high' if self.thinking_enabled else 'none')

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
        output_reserve = self.upstream_output_tokens or 8192
        compact_at = self.compaction_threshold_tokens or min(
            self.model_context_tokens * 4 // 5, self.model_context_tokens - output_reserve
        )
        model = json.loads(Path(__file__).with_name("codex_model.json").read_text())
        model.update(
            slug=self.model,
            display_name=self.model,
            context_window=self.model_context_tokens,
            max_context_window=self.model_context_tokens,
            auto_compact_token_limit=compact_at,
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
            "model_auto_compact_token_limit": compact_at,
            "model_reasoning_effort": self.reasoning_effort,
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
            ],
            "mcp_servers.review.required": True,
            "mcp_servers.review.default_tools_approval_mode": "approve",
            "mcp_servers.review.enabled_tools": ["read", "list", "search", "changes"],
            "mcp_servers.review.tools.read.output_token_limit": 32768,
            "mcp_servers.review.tools.list.output_token_limit": 32768,
            "mcp_servers.review.tools.search.output_token_limit": 32768,
            "mcp_servers.review.tools.changes.output_token_limit": 32768,
        }
        if self.native_tools:
            settings = {key: value for key, value in settings.items()
                        if not key.startswith("mcp_servers.review.")}
            settings.update({
                # 命令运行在现有执行器容器中，源码卷沿用只读挂载。
                "sandbox_mode": "danger-full-access",
                "features.shell_tool": True,
                "features.shell_snapshot": False,
                "allow_login_shell": False,
            })
        args = []
        for key, value in settings.items():
            args += ["-c", key + "=" + json.dumps(value, ensure_ascii=False)]
        return args

    def _consume(self, event):
        kind = event.get("type")
        if kind == "thread.started":
            self.native_session_id = event.get("thread_id")
        elif kind in ("error", "turn.failed"):
            error = event.get("error") or {}
            error_type = (error.get("code") or error.get("type")) if isinstance(error, dict) else None
            error_type = error_type or event.get("code")
            self.diagnostic = self.diagnostic or {
                "source": "codex", "phase": "cli_turn", "event_type": kind,
                "reason": "codex_failed", "native_error_type": safe_token(error_type),
            }
            raise NativeError("codex_failed")
        elif kind == "item.started" and event.get("item", {}).get("type") == "command_execution":
            self.command_started[event["item"]["id"]] = time.monotonic()
        elif kind == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "command_execution" and self.native_tools:
                command = {key: item.get(key) for key in
                           ("id", "command", "aggregated_output", "exit_code", "status")}
                started = self.command_started.pop(item.get("id"), None)
                if started is not None:
                    command["duration_seconds"] = round(time.monotonic() - started, 3)
                self.native_commands.append(command)
                self.emit("tool.completed", summary="Codex 原生命令完成",
                          payload={"tool": "exec_command", "call_id": item.get("id"),
                                   "command": item.get("command"),
                                   "exit_code": item.get("exit_code"), "status": item.get("status")})
            elif item.get("type") in ("command_execution", "file_change", "web_search"):
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
            raise NativeError("codex_response_incomplete", {
                "source": "codex", "phase": "result_collection", "reason": "codex_response_incomplete",
                "completed_calls": self.gateway.completed_calls, "turn_completed": self.turn_completed,
                "final_response_received": bool(self.gateway.final), "final_text_received": bool(self.final_review),
            })
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
        result = decode_review(final_text)
        if self.phase.startswith("history-") and isinstance(result, dict) and "issue_id" in result:
            result = {"findings": [], "overall_explanation": json.dumps(result, ensure_ascii=False)}
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("findings"), list)
            or not isinstance(result.get("overall_explanation"), str)
        ):
            raise NativeError("codex_review_shape_invalid")
        result["_tool_errors"] = self.tool_errors
        result["_review_completion"] = {"complete": True, "finish_reason": "stop"}
        result["_codex_reads"] = []
        if self.native_tools:
            result["_codex_commands"] = self.native_commands
        audit = self.root / "reads.jsonl"
        if audit.exists():
            result["_codex_reads"] = [json.loads(line) for line in audit.read_text().splitlines()]
        return result

    def _drain_codex_stderr(self):
        """保留受限原始错误日志，精确移除本次执行器的认证值。"""
        capture_failed = False

        def consume(raw):
            nonlocal capture_failed
            self.stderr_bytes += len(raw)
            try:
                self.raw_diagnostics.process_log(raw, engine="codex")
            except Exception as exc:
                if not capture_failed:
                    logging.error("Codex 错误日志保存失败，异常=%s", type(exc).__name__)
                    capture_failed = True

        try:
            drain_redacted(self.process.stderr, (self.capability, self.gateway.token), consume)
        except (OSError, ValueError) as exc:
            logging.error("Codex 错误日志采集失败，异常=%s", type(exc).__name__)
        finally:
            try:
                self.raw_diagnostics.finish_process_log(engine="codex")
            except Exception as exc:
                logging.error("Codex 错误日志收尾失败，异常=%s", type(exc).__name__)

    def run(self):
        try:
            if self.cancel_requested.is_set():
                raise NativeError("user_cancelled")
            self.status = "starting"
            self.emit("session.starting", summary="启动 Codex 原生 review")
            if self.git_workspace:
                head = subprocess.check_output(["git", "-c", "safe.directory=" + str(self.project), "-c", "safe.directory=" + str(self.git_objects), "rev-parse", "HEAD"], cwd=self.project, text=True, timeout=30).strip()
                if head != self.source_head:
                    raise NativeError("workspace_version_changed")
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
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(self.project),
                "GIT_CONFIG_KEY_1": "safe.directory",
                "GIT_CONFIG_VALUE_1": str(self.git_objects),
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            }
            self.process = subprocess.Popen(
                args,
                cwd=self.project if self.native_tools else self.root,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            self.process_log_thread = threading.Thread(target=self._drain_codex_stderr, daemon=True)
            self.process_log_thread.start()

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
                            raise NativeError("codex_output_limit", {"source": "codex", "phase": "cli_output",
                                              "reason": "codex_output_limit", "received_bytes": output_bytes,
                                              "limit_bytes": self.response_limit})
                        pending.extend(chunk)
                        if len(pending) > self.response_limit:
                            raise NativeError("codex_output_limit")
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            if line.strip():
                                safe_line = bytes(line)
                                for secret in (self.capability, self.gateway.token):
                                    if secret:
                                        safe_line = safe_line.replace(secret.encode(), "[已脱敏]".encode())
                                try:
                                    event = json.loads(line)
                                    if not isinstance(event, dict):
                                        raise ValueError("事件必须为对象")
                                except (ValueError, UnicodeError):
                                    self.raw_diagnostics.event_payload(safe_line, "error")
                                    raise NativeError("codex_event_json_invalid", {
                                        "source": "codex", "phase": "cli_output", "reason": "json_syntax",
                                        "received_bytes": len(line)}) from None
                                self.raw_diagnostics.event_payload(
                                    safe_line, "error" if event.get("type") in ("error", "turn.failed") else "accepted")
                                self._consume(event)
            exit_code = self.process.wait(timeout=5)
            if exit_code != 0 or pending.strip():
                raise NativeError("codex_exit_failed", {"source": "codex", "phase": "process_exit",
                                  "reason": "codex_exit_failed", "exit_code": exit_code,
                                  "pending_bytes": len(pending)})
            result = self._result()
            self.result_sha256 = hashlib.sha256(
                json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            atomic_json(self.result_path, result)
            self.result = result
            self._finalize_raw_diagnostics()
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
            terminal_status = "aborted" if self.cancel_requested.is_set() else "failed"
            details = getattr(exc, "diagnostic", None) or safe_exception(
                exc, source="codex", phase="cli_execution", reason=self.error_code)
            self.diagnostic = self._diagnostic(self.diagnostic or details)
            self.diagnostic.setdefault("stderr_bytes", self.stderr_bytes)
            self._log_failure()
            # 失败状态一旦可见，Worker 即会结束轮询，因此先固定诊断包。
            self._finalize_raw_diagnostics()
            self.status = terminal_status
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
