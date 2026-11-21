"""PR-Agent复用原生会话授权、事件确认、诊断和取消协议。"""
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import threading
import time

from native_gateway import NativeGateway
from native_session import NativeError, SessionRun, atomic_json, drain_redacted
from runtime_diagnostics import safe_exception

VERSION = '0.49.0'


class PRAgentRun(SessionRun):
    def __init__(self, data, workspace, runtime_root, broker):
        super().__init__(data, workspace, runtime_root, broker)
        self.input = data['pr_agent_input']
        self.reasoning_effort = data.get('reasoning_effort', 'none')
        self.max_findings = data.get('max_findings', 20)
        self.git_workspace = True
        self.final_review = None
        self.source_head = data['source_head']

    def _consume(self, line):
        event = json.loads(line)
        if event.get('type') == 'usage':
            self.emit('model.usage', summary='PR-Agent模型调用完成',
                      payload={'usage': event['usage'], 'call_id': self.gateway.last_call_id})
        elif event.get('type') == 'result' and self.final_review is None:
            self.final_review = event['result']
        else:
            details = event.get('details')
            if not isinstance(details, dict):
                details = {}
            counters = {key: value for key, value in details.items()
                        if key in ('expected_files', 'actual_files', 'chunks', 'failed_chunks',
                                   'remaining_files', 'partial_files') and type(value) is int}
            raise NativeError(event.get('code', 'pr_agent_protocol_invalid'),
                              {'exception_type': event.get('exception_type', ''), **counters})

    def _stderr(self):
        try:
            drain_redacted(self.process.stderr, [self.capability, self.gateway.local_token],
                           lambda chunk: self.raw_diagnostics.process_log(chunk, engine='pr_agent'))
        finally:
            self.raw_diagnostics.finish_process_log(engine='pr_agent')

    def run(self):
        try:
            if self.cancel_requested.is_set():
                raise NativeError(self.error_code or 'user_cancelled')
            self.status = 'starting'
            self.emit('session.starting', summary='启动 PR-Agent原生审查')
            self._prepare_workspace()
            git_objects = self.project.parents[1] / 'objects.git'
            env = dict(PATH=os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'), LANG='C.UTF-8',
                       HOME=str(self.root), GIT_CONFIG_NOSYSTEM='1',
                       GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_COUNT='2',
                       GIT_CONFIG_KEY_0='safe.directory', GIT_CONFIG_VALUE_0=str(self.project),
                       GIT_CONFIG_KEY_1='safe.directory', GIT_CONFIG_VALUE_1=str(git_objects),
                       GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0',
                       PR_AGENT_API_KEY='')
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.project, env=env,
                                           timeout=min(30, self.remaining()), text=True).strip()
            if head != self.source_head:
                raise NativeError('workspace_version_changed')
            self.gateway = NativeGateway(self.broker, self.capability, self.job_id, self.session_id,
                                         self.model, self.deadline, self.project, self.emit,
                                         response_limit=self.response_limit, failure=self._model_request_failed,
                                         capture=self._capture_gateway, diff_only=True)
            self.gateway.start()
            env['PR_AGENT_API_KEY'] = self.gateway.local_token
            runtime = Path('/opt/pr-agent-runtime')
            python = os.environ.get('AI_REVIEW_PR_AGENT_PYTHON', str(runtime / 'bin/python3.12'))
            env['LD_LIBRARY_PATH'] = str(runtime / 'lib')
            env['TIKTOKEN_CACHE_DIR'] = os.environ.get('AI_REVIEW_PR_AGENT_ASSETS', '/opt/pr-agent-assets')
            payload = {'input': self.input, 'model': self.model, 'gateway': self.gateway.url,
                       'job_id': self.job_id,
                       'timeout': max(1, int(self.remaining())), 'context_tokens': self.model_context_tokens,
                       'output_tokens': self.upstream_output_tokens, 'max_findings': self.max_findings,
                       'reasoning_effort': self.reasoning_effort}
            input_path = self.root / 'input.json'
            atomic_json(input_path, payload)
            self.process = subprocess.Popen([python, str(Path(__file__).with_name('pr_agent_runner.py')), str(input_path)],
                                            cwd=self.project, env=env, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, start_new_session=True)
            self.process_log_thread = threading.Thread(target=self._stderr, daemon=True)
            self.process_log_thread.start()
            self.prompt_submitted = True
            self.status = 'running'
            self.emit('session.running', summary='PR-Agent审查固定PR变更')
            pending = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() - self.last_heartbeat >= 5:
                        self.last_heartbeat = time.monotonic()
                        self.emit('heartbeat', status='running', summary='PR-Agent会话运行中', payload={})
                    if self.cancel_requested.is_set() or self.remaining() <= 0:
                        raise NativeError('user_cancelled' if self.cancel_requested.is_set() else 'deadline_exceeded')
                    if self.model_failure.is_set():
                        raise NativeError('model_request_failed', self.model_failure_details)
                    for key, _ in selector.select(.2):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        pending.extend(chunk)
                        if len(pending) > self.response_limit:
                            raise NativeError('pr_agent_output_limit')
                        while b'\n' in pending:
                            line, _, pending = pending.partition(b'\n')
                            if line.strip():
                                self.raw_diagnostics.event_payload(bytes(line), 'accepted')
                                self._consume(line)
            code = self.process.wait(timeout=5)
            if code or pending.strip() or self.final_review is None:
                raise NativeError('pr_agent_response_incomplete', {'exit_code': code})
            self.result = self.final_review
            self.result_sha256 = hashlib.sha256(json.dumps(self.result, ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            atomic_json(self.result_path, self.result)
            self._finalize_raw_diagnostics()
            self.emit_terminal('session.completed', status='completed', summary='PR-Agent审查完成',
                               payload={'result_sha256': self.result_sha256, 'review_complete': True,
                                        'finish_reason': 'stop', 'finding_count': len(self.result['findings'])})
            self.status = 'completed'
        except Exception as error:
            if self.cancel_requested.is_set():
                self.error_code = self.error_code or 'user_cancelled'
            else:
                self.error_code = error.code if isinstance(error, NativeError) else 'pr_agent_execution_failed'
            self.diagnostic = self._diagnostic(getattr(error, 'diagnostic', None) or safe_exception(
                error, source='pr_agent', phase='review', reason=self.error_code))
            self._finalize_raw_diagnostics()
            self.status = 'aborted' if self.cancel_requested.is_set() else 'failed'
            self.emit_terminal('session.error', status=self.status, summary='PR-Agent审查未完成',
                               payload={'error_code': self.error_code, 'diagnostic': self.diagnostic})
        finally:
            self._kill()
            for stream in (getattr(self.process, 'stdout', None), getattr(self.process, 'stderr', None)):
                if stream:
                    stream.close()
            self.reporter.close()
            self._finalize_raw_diagnostics()
            try:
                self._persist_terminal()
            finally:
                self.done.set()
