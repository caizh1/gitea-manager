"""OpenCode 本机模型网关：绑定会话、生成调用标识并阻止失败请求重放。"""
import base64
import binascii
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import logging
import math
import secrets
import socket
import threading
import time
from pathlib import Path
import re

import requests
from urllib3.exceptions import HTTPError as Urllib3Error
from runtime_diagnostics import json_failure, safe_exception


MAX_REQUEST_BYTES = 64 * 1024 * 1024


def leading_system_messages(messages):
    """把系统指令合并至唯一首条消息，保留对话与工具回执的原顺序。"""
    systems = [m for m in messages if m.get('role') == 'system']
    if not systems:
        return list(messages)
    contents = [m.get('content') for m in systems]
    if all(isinstance(c, str) for c in contents):
        content = '\n\n'.join(contents)
    else:
        content = []
        for value in contents:
            if isinstance(value, list):
                content.extend(value)
            elif isinstance(value, str):
                content.append({'type': 'text', 'text': value})
            else:
                raise ValueError('系统消息正文无效')
    return [dict(systems[0], content=content)] + [m for m in messages if m.get('role') != 'system']


MAX_UPSTREAM_ERROR_CAPTURE = 128 * 1024
MAX_UPSTREAM_ERROR_ENVELOPE = 256 * 1024
UPSTREAM_HEADER_NAMES = frozenset({
    'content-type', 'date', 'x-request-id', 'request-id', 'x-trace-id', 'traceparent', 'cf-ray',
})


class GatewayLimitError(ValueError):
    def __init__(self, received, limit):
        self.received, self.limit = received, limit
        super().__init__('response_too_large')


class GatewayProtocolError(ValueError):
    def __init__(self, reason, received=0):
        self.reason, self.received = reason, received
        super().__init__(reason)


class GatewayUpstreamError(ValueError):
    def __init__(self, details):
        self.details = details
        super().__init__('broker_transport_error')


def decode_upstream_error(value, expected_call_id):
    """校验 Broker 私有错误封套；raw_b64 只交给受限证据存储。"""
    payload = value.get('_review_upstream_error') if isinstance(value, dict) else None
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise GatewayProtocolError('invalid_broker_error_frame')
    required = {
        'call_id': str, 'error_code': str, 'source': str, 'phase': str, 'reason': str,
        'upstream_http_status': int, 'error_bytes': int, 'error_sha256': str,
        'captured_bytes': int, 'captured_sha256': str,
        'error_truncated': bool, 'raw_b64': str,
    }
    if any(type(payload.get(key)) is not kind for key, kind in required.items()):
        raise GatewayProtocolError('invalid_broker_error_frame')
    if (payload['call_id'] != expected_call_id or payload['source'] != 'model_upstream'
            or payload['phase'] not in ('response_headers', 'response_stream')
            or payload['reason'] not in ('upstream_http_error', 'upstream_sse_error',
                                         'upstream_json_error')
            or not 100 <= payload['upstream_http_status'] <= 599
            or payload['error_bytes'] < 0
            or not re.fullmatch(r'[0-9a-f]{64}', payload['error_sha256'])
            or not re.fullmatch(r'[0-9a-f]{64}', payload['captured_sha256'])):
        raise GatewayProtocolError('invalid_broker_error_frame')
    # 在解码前限制文本，避免畸形私有帧用少量业务额度触发大额 base64 分配。
    if len(payload['raw_b64']) > ((MAX_UPSTREAM_ERROR_CAPTURE + 2) // 3 * 4):
        raise GatewayProtocolError('invalid_broker_error_frame')
    try:
        raw = base64.b64decode(payload['raw_b64'], validate=True)
    except (ValueError, TypeError, binascii.Error):
        raise GatewayProtocolError('invalid_broker_error_frame') from None
    captured_sha256 = hashlib.sha256(raw).hexdigest()
    if (len(raw) > MAX_UPSTREAM_ERROR_CAPTURE or payload['captured_bytes'] != len(raw)
            or payload['error_bytes'] != len(raw)
            or payload['captured_sha256'] != captured_sha256
            or payload['error_sha256'] != captured_sha256):
        raise GatewayProtocolError('invalid_broker_error_frame')
    headers = payload.get('headers') or {}
    if (not isinstance(headers, dict) or any(key not in UPSTREAM_HEADER_NAMES
            or not isinstance(item, str) or len(item) > 512
            for key, item in headers.items())
            or sum(len(key) + len(item) for key, item in headers.items()) > 4096):
        raise GatewayProtocolError('invalid_broker_error_frame')
    safe = {key: payload[key] for key in (
        'call_id', 'error_code', 'source', 'phase', 'reason', 'upstream_http_status',
        'error_bytes', 'captured_bytes', 'error_sha256', 'captured_sha256', 'error_truncated')}
    for key in ('upstream_error_type', 'upstream_error_code', 'secondary_exception_type'):
        item = payload.get(key)
        if item is not None:
            if not isinstance(item, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,128}', item):
                raise GatewayProtocolError('invalid_broker_error_frame')
            safe[key] = item
    for key in ('received_bytes', 'declared_error_bytes'):
        item = payload.get(key)
        if item is not None:
            if type(item) is not int or item < 0:
                raise GatewayProtocolError('invalid_broker_error_frame')
            safe[key] = item
    for key in ('headers_elapsed_seconds', 'first_data_elapsed_seconds',
                'last_data_elapsed_seconds'):
        item = payload.get(key)
        if item is not None:
            if (not isinstance(item, (int, float)) or isinstance(item, bool)
                    or item < 0 or not math.isfinite(item)):
                raise GatewayProtocolError('invalid_broker_error_frame')
            safe[key] = item
    safe['headers'] = dict(headers)
    safe['captured_bytes'] = len(raw)
    safe['captured_sha256'] = captured_sha256
    return safe, raw


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True


INSTRUCTION_SUFFIX = re.compile(
    r'\n\n<system-reminder>\nInstructions from: .*?\n.*?</system-reminder>\s*$', re.DOTALL)


def sanitize_repository_instructions(value):
    """删除 read 工具自动附加的仓库指令；文件自身正文保持原样。"""
    result = json.loads(json.dumps(value, ensure_ascii=False))
    for message in result.get('messages', []):
        if not isinstance(message, dict) or message.get('role') != 'tool':
            continue
        content = message.get('content')
        if isinstance(content, str):
            message['content'] = INSTRUCTION_SUFFIX.sub('', content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get('text'), str):
                    part['text'] = INSTRUCTION_SUFFIX.sub('', part['text'])
    return result


def request_fingerprint(value):
    """完整规范化请求指纹；仅相同会话内失败且完全相同的请求会被拦截。"""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def response_text_bytes(value):
    """统计模型实际正文；不把 usage、心跳和协议元数据计入正文额度。"""
    total = 0
    if not isinstance(value, dict):
        return total
    for key in ('content', 'reasoning_content', 'reasoning', 'text', 'refusal'):
        content = value.get(key)
        if isinstance(content, str):
            total += len(content.encode())
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += response_text_bytes(part)
    return total


class NativeGateway:
    def __init__(self, broker, capability, job_id, session_id, model, deadline, workspace, event=None,
                 response_limit=8 * 1024 * 1024, failure=None, result_schema=None, capture=None, retrieval_budget=0,
                 diff_only=False):
        self.broker = broker.rstrip('/')
        self.capability = capability
        self.job_id = job_id
        self.session_id = session_id
        self.model = model
        self.deadline = float(deadline)
        self.workspace = Path(workspace).resolve()
        self.event = event or (lambda *_args, **_kwargs: None)
        self.failure = failure or (lambda _details: None)
        self.capture = capture or (lambda _kind, _raw, _details: None)
        self.failure_signalled = False
        self.last_call_id = None
        self.response_limit = int(response_limit)
        self.result_schema = result_schema if isinstance(result_schema, dict) else {}
        self.local_token = secrets.token_urlsafe(32)
        self.failed = {}
        self.inflight = {}
        self.connections = set()
        self.sessions = set()
        self.responses = set()
        self.upstream_sockets = set()
        self.retrieval_budget = retrieval_budget
        self.diff_only = diff_only
        self.retrieval_used = 0
        self.active = True
        self.lock = threading.Lock()
        self.server = GatewayServer(('127.0.0.1', 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, name='原生模型网关', daemon=True)

    def _allowed_tool(self, tool):
        if not isinstance(tool, dict):
            return False
        function = tool.get('function')
        if tool.get('type') != 'function' or not isinstance(function, dict):
            return False
        name = function.get('name')
        if name in ('read', 'grep', 'glob'):
            return True
        if name != 'StructuredOutput': return False
        parameters = function.get('parameters')
        if parameters == self.result_schema: return True
        if os.environ.get('OPENCODE_EXPECTED_VERSION') == '1.1.51':
            # 旧版 Zod 只增加草案标识和安全整数上限，其余结构仍逐字段严格比较。
            expected = json.loads(json.dumps(self.result_schema))
            expected['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
            expected['properties']['findings']['items']['properties']['line']['maximum'] = 9007199254740991
            return parameters == expected
        return False

    def _tool_calls(self, content_type, raw):
        rows = []
        if content_type.split(';', 1)[0].strip().lower() == 'text/event-stream':
            partial = {}
            for block in re.split(br'\r?\n\r?\n', raw):
                data = b'\n'.join(line[5:].lstrip() for line in block.splitlines() if line.startswith(b'data:'))
                if not data or data == b'[DONE]':
                    continue
                try:
                    value = json.loads(data)
                except ValueError:
                    continue
                choices = value.get('choices', []) if isinstance(value, dict) else []
                if not isinstance(choices, list):
                    raise ValueError('response_choices')
                for choice in choices:
                    if not isinstance(choice, dict):
                        raise ValueError('response_choice')
                    delta = choice.get('delta') or {}
                    if not isinstance(delta, dict) or not isinstance(delta.get('tool_calls') or [], list):
                        raise ValueError('tool_calls')
                    for position, tool in enumerate(delta.get('tool_calls') or []):
                        if not isinstance(tool, dict):
                            raise ValueError('tool_call')
                        index = tool.get('index', position)
                        if not isinstance(index, (str, int)):
                            raise ValueError('tool_index')
                        target = partial.setdefault(index, {'name': '', 'arguments': ''})
                        function = tool.get('function') or {}
                        if not isinstance(function, dict):
                            raise ValueError('tool_function')
                        name, arguments = function.get('name') or '', function.get('arguments') or ''
                        if not isinstance(name, str) or not isinstance(arguments, str):
                            raise ValueError('tool_function')
                        target['name'] += name
                        target['arguments'] += arguments
            rows = list(partial.values())
        else:
            value = json.loads(raw)
            choices = value.get('choices') if isinstance(value, dict) else None
            if choices is not None and not isinstance(choices, list):
                raise ValueError('response_choices')
            for choice in choices or []:
                if not isinstance(choice, dict):
                    raise ValueError('response_choice')
                message = choice.get('message') or {}
                if not isinstance(message, dict) or not isinstance(message.get('tool_calls') or [], list):
                    raise ValueError('tool_calls')
                for tool in message.get('tool_calls') or []:
                    if not isinstance(tool, dict) or not isinstance(tool.get('function') or {}, dict):
                        raise ValueError('tool_call')
                    function = tool.get('function') or {}
                    if not isinstance(function.get('name') or '', str) or not isinstance(function.get('arguments') or '', str):
                        raise ValueError('tool_function')
                    rows.append(function)
        return rows

    def validate_response(self, content_type, raw, allow_retrieval=True):
        value = json.loads(raw)
        effective = 0
        choices = value.get('choices', []) if isinstance(value, dict) else []
        if not isinstance(choices, list):
            raise ValueError('response_choices')
        for choice in choices:
            if not isinstance(choice, dict):
                raise ValueError('response_choice')
            message = choice.get('message') or {}
            if not isinstance(message, dict):
                raise ValueError('response_message')
            effective += response_text_bytes(message) + response_text_bytes(choice)
        calls = self._tool_calls(content_type, raw)
        if self.diff_only and calls:
            raise ValueError('tools_disabled_for_diff_review')
        for call in calls:
            effective += len((call.get('name') or '').encode()) + len((call.get('arguments') or '').encode())
        if effective > self.response_limit:
            raise GatewayLimitError(effective, self.response_limit)
        for call in calls:
            name = call.get('name')
            if name not in ('read', 'grep', 'glob', 'StructuredOutput'):
                raise ValueError('unsupported_tool')
            args = json.loads(call.get('arguments') or '{}')
            if not isinstance(args, dict):
                raise ValueError('tool_arguments')
            if name == 'StructuredOutput':
                if not isinstance(args.get('coverage'), dict) or not isinstance(args.get('findings'), list):
                    raise ValueError('structured_output_arguments')
                continue
            field = 'filePath' if name == 'read' else 'path'
            target = args.get(field)
            if target is None and name in ('grep', 'glob'):
                continue
            if not isinstance(target, str) or not target:
                raise ValueError('tool_path')
            path = Path(target)
            path = path.resolve() if path.is_absolute() else (self.workspace / path).resolve()
            if path != self.workspace and self.workspace not in path.parents:
                raise ValueError('tool_path_escape')

        requested = sum(call.get('name') in ('read', 'grep', 'glob') for call in calls)
        if requested and not allow_retrieval:
            raise ValueError('context_retrieval_closed_for_finalization')
        if self.retrieval_budget and requested:
            with self.lock:
                if self.retrieval_used + requested > self.retrieval_budget:
                    raise ValueError('context_retrieval_budget_exceeded')
                self.retrieval_used += requested

    def stream_filter(self, expected_call_id=None, allow_retrieval=True):
        gateway = self

        class Filter:
            def __init__(self):
                self.pending = bytearray()
                self.held = bytearray()
                self.calls = {}
                self.tool_seen = False
                self.structured_bytes = 0
                self.upstream_error = None

            def account(self, size):
                self.structured_bytes += size
                if self.structured_bytes > gateway.response_limit:
                    raise GatewayLimitError(self.structured_bytes, gateway.response_limit)

            def feed(self, chunk):
                self.pending.extend(chunk)
                output = bytearray()
                while True:
                    match = re.search(br'\r?\n\r?\n', self.pending)
                    if not match:
                        break
                    frame = bytes(self.pending[:match.end()])
                    del self.pending[:match.end()]
                    data = b'\n'.join(line[5:].lstrip() for line in frame.splitlines()
                                      if line.startswith(b'data:'))
                    tool_frame = False
                    meaningful = data == b'[DONE]'
                    if data and data != b'[DONE]':
                        try:
                            value = json.loads(data)
                        except ValueError:
                            raise GatewayProtocolError('invalid_sse_frame', len(frame)) from None
                        if not isinstance(value, dict):
                            raise GatewayProtocolError('invalid_sse_frame', len(frame))
                        # usage 常以 choices=[] 独立结束帧返回；原生压缩依赖其 Token 计数，
                        # 不能当作心跳丢弃，也不计入模型正文额度。
                        if isinstance(value.get('usage'), dict):
                            meaningful = True
                        failure = value.get('_review_transport_error')
                        if failure is not None:
                            if (not isinstance(failure, dict)
                                    or not isinstance(failure.get('call_id'), str)
                                    or failure.get('call_id') != expected_call_id
                                    or not isinstance(failure.get('error_code'), str)
                                    or not isinstance(failure.get('exception_type'), str)):
                                raise GatewayProtocolError('invalid_broker_error_frame', len(frame))
                            self.upstream_error = dict(failure)
                            continue
                        upstream = decode_upstream_error(value, expected_call_id)
                        if upstream is not None:
                            details, original = upstream
                            self.upstream_error = details
                            gateway.report_upstream_error(expected_call_id, original, details)
                            continue
                        choices = value.get('choices', [])
                        if not isinstance(choices, list):
                            raise GatewayProtocolError('invalid_sse_frame', len(frame))
                        for choice in choices:
                            if not isinstance(choice, dict):
                                raise GatewayProtocolError('invalid_sse_frame', len(frame))
                            meaningful = True
                            delta = choice.get('delta') or {}
                            if not isinstance(delta, dict) or not isinstance(delta.get('tool_calls') or [], list):
                                raise GatewayProtocolError('invalid_sse_frame', len(frame))
                            text_bytes = response_text_bytes(delta) + response_text_bytes(choice)
                            if text_bytes:
                                self.account(text_bytes)
                            for position, tool in enumerate(delta.get('tool_calls') or []):
                                if not isinstance(tool, dict):
                                    raise GatewayProtocolError('invalid_sse_frame', len(frame))
                                index = tool.get('index', position)
                                if not isinstance(index, (str, int)):
                                    raise GatewayProtocolError('invalid_sse_frame', len(frame))
                                target = self.calls.setdefault(index, {'name': '', 'arguments': ''})
                                function = tool.get('function') or {}
                                if not isinstance(function, dict):
                                    raise GatewayProtocolError('invalid_sse_frame', len(frame))
                                name, arguments = function.get('name') or '', function.get('arguments') or ''
                                if not isinstance(name, str) or not isinstance(arguments, str):
                                    raise GatewayProtocolError('invalid_sse_frame', len(frame))
                                self.account(len(name.encode()) + len(arguments.encode()))
                                target['name'] += name
                                target['arguments'] += arguments
                                tool_frame = True
                            if choice.get('finish_reason') == 'tool_calls':
                                tool_frame = True
                    if tool_frame:
                        self.tool_seen = True
                    if self.tool_seen:
                        # 工具开始后必须等待完整参数验证；纯心跳没有正文语义，可直接丢弃，
                        # 避免长时间心跳累计占用任务正文额度或 held 内存。
                        if meaningful:
                            self.held.extend(frame)
                        if len(self.held) > gateway.response_limit + 65536:
                            raise GatewayLimitError(len(self.held), gateway.response_limit)
                    else:
                        output.extend(frame)
                # 完整心跳帧不计正文；只有无法形成完整帧的累计字节受未完成帧上限约束。
                private_prefix = (self.pending.startswith(b'event: review.upstream.error')
                                  or self.pending.startswith(b'data: {"_review_upstream_error"'))
                pending_limit = (MAX_UPSTREAM_ERROR_ENVELOPE if private_prefix
                                 else gateway.response_limit)
                if len(self.pending) > pending_limit:
                    raise GatewayLimitError(len(self.pending), pending_limit)
                return bytes(output)

            def finish(self):
                if self.pending:
                    received = len(self.pending)
                    self.pending.clear()
                    raise GatewayProtocolError('incomplete_sse_frame', received)
                if self.upstream_error:
                    raise GatewayUpstreamError(self.upstream_error)
                if self.tool_seen:
                    gateway.validate_response('application/json', json.dumps({
                        'choices': [{'message': {'tool_calls': [{'function': call} for call in self.calls.values()]}}]
                    }).encode(), allow_retrieval=allow_retrieval)
                    return bytes(self.held)
                return bytes(self.held)

        return Filter()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_address[1]}/v1'

    def start(self):
        self.thread.start()

    def authorize(self, capability, deadline):
        with self.lock:
            if not self.active or abs(self.deadline - float(deadline)) > .001:
                raise ValueError('会话期限不匹配')
            self.capability = capability

    def notify_failure(self, call_id, source, **details):
        """只把同会话首个确定失败交给会话线程；回调不得在 handler 内做关闭等待。"""
        with self.lock:
            if self.failure_signalled:
                return
            self.failure_signalled = True
        diagnostic = dict(details)
        diagnostic.setdefault('diagnostic_version', 1)
        diagnostic.update(job_id=self.job_id, session_id=self.session_id,
                          call_id=call_id, source=source)
        self.failure(diagnostic)

    def capture_evidence(self, kind, raw=b'', details=None):
        try:
            self.capture(kind, raw, dict(details or {}))
            return True
        except Exception as exc:
            logging.error('模型协议证据采集失败：job=%s session=%s call=%s kind=%s exception=%s',
                          self.job_id, self.session_id, (details or {}).get('call_id'),
                          kind, type(exc).__name__)
            return False

    def report_upstream_error(self, call_id, raw, details):
        """原文只写受限证据；事件、普通日志和终态只记录安全元数据。"""
        self.capture_evidence('model_upstream_error', raw, details)
        public = {key: value for key, value in details.items() if key != 'headers'}
        self.event('model.error', status='failed', summary='模型上游返回错误', payload=public)
        logging.error('模型上游错误：job=%s session=%s call=%s status=%s phase=%s bytes=%s sha256=%s',
                      self.job_id, self.session_id, call_id,
                      details.get('upstream_http_status'), details.get('phase'),
                      details.get('error_bytes'), details.get('error_sha256'))
        self.notify_failure(call_id, 'model_upstream',
                            **{key: value for key, value in public.items()
                               if key not in ('call_id', 'source')})

    def close(self):
        with self.lock:
            self.active = False
            responses = list(self.responses)
            sessions = list(self.sessions)
            connections = list(self.connections)
            upstream_sockets = list(self.upstream_sockets)
        if self.thread.is_alive():
            self.server.shutdown()
        # raw.read1 可能持有 urllib3 的 BufferedReader 锁；先关闭底层 socket，
        # 让静默上游的阻塞读取返回，再调用 response/session.close 做常规回收。
        for upstream in upstream_sockets:
            try:
                upstream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                upstream.close()
            except OSError:
                pass
        for response in responses:
            try:
                response.close()
            except (OSError, requests.RequestException):
                pass
        for session in sessions:
            try:
                session.close()
            except (OSError, requests.RequestException):
                pass
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        self.server.server_close()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    @staticmethod
    def response_socket(response):
        candidates = [
            getattr(getattr(response, 'raw', None), '_connection', None),
            getattr(getattr(getattr(getattr(response, 'raw', None), '_fp', None), 'fp', None), 'raw', None),
        ]
        for candidate in candidates:
            target = getattr(candidate, 'sock', None) or getattr(candidate, '_sock', None)
            if target is not None:
                return target
        return None

    def _handler(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def setup(self):
                super().setup()
                self.response_started = False
                with gateway.lock:
                    gateway.connections.add(self.connection)

            def finish(self):
                try:
                    super().finish()
                finally:
                    with gateway.lock:
                        gateway.connections.discard(self.connection)

            def log_message(self, *_args):
                return

            def reply(self, status, message, call_id=None):
                if self.response_started:
                    self.close_connection = True
                    return
                body = json.dumps({'error': {'message': message, 'call_id': call_id}}, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.response_started = True
                self.wfile.write(body)
                self.close_connection = True

            def do_POST(self):
                if self.path.split('?', 1)[0] != '/v1/chat/completions':
                    self.reply(404, '未支持的模型路径')
                    return
                supplied = self.headers.get('Authorization', '').removeprefix('Bearer ')
                if not secrets.compare_digest(supplied, gateway.local_token):
                    self.reply(401, '本机会话身份无效')
                    return
                try:
                    length = int(self.headers.get('Content-Length', ''))
                except ValueError:
                    length = -1
                if length < 2 or length > MAX_REQUEST_BYTES:
                    self.reply(413, '模型请求大小无效')
                    return
                try:
                    body = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError):
                    self.reply(400, '模型请求不是有效 JSON')
                    return
                if not isinstance(body, dict) or body.get('model') not in (gateway.model, 'review/' + gateway.model):
                    self.reply(400, '模型与会话授权不匹配')
                    return
                body['model'] = gateway.model
                body = sanitize_repository_instructions(body)
                for tool in body.get('tools', []):
                    function = tool.get('function') if isinstance(tool, dict) else None
                    if isinstance(function, dict) and function.get('name') == 'StructuredOutput':
                        if not gateway._allowed_tool(tool):
                            self.reply(400, '结构化结果工具声明无效')
                            return
                body['tools'] = [tool for tool in body.get('tools', []) if gateway._allowed_tool(tool)]
                if os.environ.get('OPENCODE_EXPECTED_VERSION') == '1.1.51':
                    calls = {call.get('id') for message in body['messages'] if message.get('role') == 'assistant'
                             for call in message.get('tool_calls', []) if call.get('function', {}).get('name') == 'StructuredOutput'}
                    receipts = [m for m in body['messages'] if m.get('role') == 'tool' and m.get('tool_call_id') in calls
                                and m.get('content') == '管理器已接收结构化结果。']
                    if receipts:
                        # 工具已经提交结果，由本地确定性结束会话，不再要求模型生成第二份结果。
                        value = {'id':'manager-legacy-complete','object':'chat.completion.chunk' if body.get('stream') else 'chat.completion',
                                 'created':int(time.time()),'model':gateway.model,'choices':[{'index':0,'finish_reason':'stop',
                                 'delta' if body.get('stream') else 'message':{'role':'assistant','content':'审查结果已提交。'}}]}
                        raw = json.dumps(value,ensure_ascii=False).encode()
                        if body.get('stream'): raw = b'data: '+raw+b'\n\ndata: [DONE]\n\n'
                        self.send_response(200)
                        self.send_header('Content-Type','text/event-stream' if body.get('stream') else 'application/json')
                        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                        return
                    for tool in body['tools']:
                        if tool['function']['name'] == 'StructuredOutput':
                            tool['function']['parameters'] = gateway.result_schema
                            body['tool_choice'] = 'required'
                finishing = False
                if gateway.retrieval_budget and not gateway.diff_only:
                    left = max(0, gateway.retrieval_budget - gateway.retrieval_used)
                    finishing = left == 0 or gateway.deadline - time.time() <= 60
                    if finishing:
                        body['tools'] = [tool for tool in body['tools'] if tool['function']['name'] == 'StructuredOutput']
                        instruction = '检索额度或收尾时间已到。禁止继续检索，立即通过 StructuredOutput 提交已有结论；未完成范围如实列入 skipped，不得宣称全部审查完成。'
                        body['messages'] = list(body['messages']) + [{'role': 'system', 'content': instruction}]
                if any(tool['function']['name'] == 'StructuredOutput' for tool in body['tools']):
                    # 与普通 ChipMate 对话一样允许模型自主选择工具；收尾只指定结果工具。
                    body['tool_choice'] = ({'type': 'function', 'function': {'name': 'StructuredOutput'}}
                                           if finishing else 'auto')
                if gateway.diff_only:
                    # 双层限制：原生权限全部拒绝；发往模型的请求也不暴露任何工具。
                    for key in ('tools', 'tool_choice', 'parallel_tool_calls', 'response_format'):
                        body.pop(key, None)
                # 严格聊天模板要求系统消息位于开头；保持工具调用和回执的相对顺序。
                body['messages'] = leading_system_messages(body['messages'])
                fingerprint = request_fingerprint(body)
                call_id = secrets.token_urlsafe(18)
                with gateway.lock:
                    if not gateway.active or time.time() >= gateway.deadline:
                        self.reply(410, '会话授权已撤销或到期', call_id)
                        return
                    previous = gateway.failed.get(fingerprint)
                    if previous:
                        gateway.event('model.error', status='failed',
                                      summary='已阻止失败模型请求的自动重放',
                                      payload={'call_id': call_id, 'first_call_id': previous,
                                               'reason': 'duplicate_failed_request'})
                        self.reply(409, '相同失败请求已被阻止重放', call_id)
                        return
                    previous = gateway.inflight.get(fingerprint)
                    if previous:
                        self.reply(409, '相同模型请求正在执行，已阻止并发重放', call_id)
                        return
                    gateway.inflight[fingerprint] = call_id
                    gateway.last_call_id = call_id
                gateway.event('model.running', status='running', summary='模型调用中',
                              payload={'call_id': call_id})
                headers = {
                    'Authorization': 'Bearer ' + gateway.capability,
                    'X-Review-Job': gateway.job_id,
                    'X-Review-Session': gateway.session_id,
                    'X-Review-Call': call_id,
                    'X-Review-Deadline': str(gateway.deadline),
                }
                remaining = max(0.1, gateway.deadline - time.time())
                failed = True
                session = None
                transferred_bytes = 0
                upstream_details = None
                stream_filter = None
                response_capture_started = False
                capture_error_type = None
                try:
                    session = requests.Session()
                    session.trust_env = False
                    with gateway.lock:
                        if not gateway.active:
                            raise OSError('gateway_closed')
                        gateway.sessions.add(session)
                    with session.post(gateway.broker + '/chat/completions', json=body, headers=headers,
                                      timeout=(5, remaining), allow_redirects=False, stream=True) as response:
                        with gateway.lock:
                            if not gateway.active:
                                response.close()
                                raise OSError('gateway_closed')
                            gateway.responses.add(response)
                            upstream_socket = gateway.response_socket(response)
                            if upstream_socket is not None:
                                gateway.upstream_sockets.add(upstream_socket)
                        content_type = response.headers.get('Content-Type', 'application/json')
                        tool_names = [tool.get('function', {}).get('name') for tool in body.get('tools', [])
                                      if isinstance(tool, dict) and isinstance(tool.get('function'), dict)]
                        gateway.capture_evidence('model_response_start', details={
                            'call_id': call_id, 'model': body.get('model'),
                            'stream': body.get('stream', False), 'tool_choice': body.get('tool_choice'),
                            'tool_names': tool_names, 'message_count': len(body.get('messages') or []),
                            'request_bytes': length, 'broker_http_status': response.status_code,
                            'content_type': content_type})
                        response_capture_started = True
                        self.send_response(response.status_code)
                        self.send_header('Content-Type', content_type)
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        self.response_started = True
                        streaming = content_type.split(';', 1)[0].strip().lower() == 'text/event-stream'
                        stream_filter = gateway.stream_filter(call_id, allow_retrieval=not finishing) if streaming and response.status_code < 400 else None
                        raw = bytearray()
                        while True:
                            chunk = response.raw.read1(8192, decode_content=True)
                            if not chunk:
                                break
                            transferred_bytes += len(chunk)
                            gateway.capture_evidence('model_response_chunk', chunk,
                                                     {'call_id': call_id})
                            if stream_filter:
                                chunk = stream_filter.feed(chunk)
                            elif not stream_filter:
                                raw.extend(chunk)
                                private_envelope = raw.lstrip().startswith(b'{"_review_upstream_error"')
                                raw_limit = (MAX_UPSTREAM_ERROR_ENVELOPE if private_envelope
                                             else gateway.response_limit + 65536)
                                if len(raw) > raw_limit:
                                    raise GatewayLimitError(len(raw), gateway.response_limit)
                                continue
                            if chunk:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                        if stream_filter:
                            tail = stream_filter.finish()
                            if tail:
                                self.wfile.write(tail)
                                self.wfile.flush()
                        elif raw:
                            try:
                                envelope = json.loads(raw)
                            except (ValueError, UnicodeError):
                                envelope = None
                            upstream = decode_upstream_error(envelope, call_id)
                            if upstream is not None:
                                upstream_details, original = upstream
                                gateway.report_upstream_error(call_id, original, upstream_details)
                                raw = bytearray(json.dumps({'error': {
                                    'message': '模型上游返回错误，未自动重放',
                                    'call_id': call_id}}, ensure_ascii=False,
                                    separators=(',', ':')).encode())
                            elif response.status_code < 400:
                                gateway.validate_response(content_type, bytes(raw), allow_retrieval=not finishing)
                            self.wfile.write(raw)
                            self.wfile.flush()
                        self.close_connection = True
                        failed = response.status_code >= 400 or upstream_details is not None
                    if failed:
                        with gateway.lock:
                            gateway.failed.setdefault(fingerprint, call_id)
                        if upstream_details is None:
                            gateway.event('model.error', status='failed', summary='模型调用失败',
                                          payload={'call_id': call_id, 'http_status': response.status_code})
                            gateway.notify_failure(call_id, 'model_http', reason='upstream_http_error',
                                                   http_status=response.status_code)
                    else:
                        gateway.event('model.completed', status='completed', summary='模型调用完成',
                                      payload={'call_id': call_id})
                except GatewayLimitError as exc:
                    capture_error_type = type(exc).__name__
                    gateway.event('model.error', status='failed', summary='模型响应超过任务额度',
                                  payload={'call_id': call_id, 'reason': 'response_too_large',
                                           'received_bytes': exc.received, 'limit_bytes': exc.limit})
                    self.reply(502, '模型响应超过任务额度且不会重放', call_id)
                    gateway.notify_failure(call_id, 'local_limit', reason='response_too_large',
                                           received_bytes=exc.received, limit_bytes=exc.limit)
                except GatewayUpstreamError as exc:
                    capture_error_type = type(exc).__name__
                    if exc.details.get('source') != 'model_upstream':
                        diagnostic = {'phase': 'broker_stream', 'reason': 'broker_transport_error',
                                      'received_bytes': transferred_bytes,
                                      'error_code': exc.details['error_code'],
                                      'exception_type': exc.details['exception_type']}
                        gateway.event('model.error', status='failed', summary='模型传输在响应开始后失败',
                                      payload=dict(diagnostic, call_id=call_id))
                        gateway.notify_failure(call_id, 'model_transport', **diagnostic)
                    self.reply(502, '模型调用失败且不会自动重放', call_id)
                except GatewayProtocolError as exc:
                    capture_error_type = type(exc).__name__
                    diagnostic = {'phase': 'broker_stream', 'reason': exc.reason,
                                  'received_bytes': exc.received or transferred_bytes}
                    gateway.event('model.error', status='failed', summary='模型流协议不完整',
                                  payload=dict(diagnostic, call_id=call_id))
                    self.reply(502, '模型流协议不完整且不会重放', call_id)
                    gateway.notify_failure(call_id, 'model_protocol', **diagnostic)
                except ValueError as exc:
                    capture_error_type = type(exc).__name__
                    allowed = {'response_choices', 'response_choice', 'response_message', 'tool_calls',
                               'tool_call', 'tool_index', 'tool_function', 'unsupported_tool',
                               'tool_arguments', 'tool_path', 'tool_path_escape',
                               'structured_output_arguments'}
                    controlled = str(exc) if str(exc) in allowed else None
                    if isinstance(exc, json.JSONDecodeError):
                        diagnostic = json_failure(
                            exc, source='model_protocol', phase='response_decode',
                            extraction_path='broker.response', received_bytes=transferred_bytes,
                            response_chars=len(exc.doc))
                        source, summary, message = ('model_protocol', '模型响应 JSON 格式无效',
                                                    '模型响应格式无效且不会重放')
                    else:
                        source = ('invalid_tool' if controlled in {
                            'unsupported_tool', 'tool_arguments', 'tool_path', 'tool_path_escape',
                            'structured_output_arguments'}
                                  else 'model_protocol')
                        diagnostic = {'diagnostic_version': 1, 'phase': 'response_validation',
                                      'reason': controlled or 'response_schema_invalid',
                                      'extraction_path': 'broker.response',
                                      'received_bytes': transferred_bytes}
                        summary = ('模型工具请求越界或格式无效' if source == 'invalid_tool'
                                   else '模型响应结构无效')
                        message = summary + '且不会重放'
                    gateway.event('model.error', status='failed', summary=summary,
                                  payload=dict(diagnostic, call_id=call_id))
                    self.reply(400, message, call_id)
                    gateway.notify_failure(call_id, source,
                                           **{key: value for key, value in diagnostic.items()
                                              if key != 'source'})
                except (requests.RequestException, Urllib3Error, OSError, AttributeError) as exc:
                    capture_error_type = type(exc).__name__
                    if not (stream_filter and stream_filter.upstream_error
                            and stream_filter.upstream_error.get('source') == 'model_upstream'):
                        diagnostic = safe_exception(exc, source='model_transport',
                                                    phase='broker_stream', reason='transport')
                        diagnostic['received_bytes'] = transferred_bytes
                        gateway.event('model.error', status='failed', summary='模型连接失败',
                                      payload=dict(diagnostic, call_id=call_id))
                        gateway.notify_failure(call_id, 'model_transport',
                                               **{key: value for key, value in diagnostic.items()
                                                  if key != 'source'})
                    self.reply(502, '模型连接失败且不会自动重放', call_id)
                finally:
                    if response_capture_started:
                        gateway.capture_evidence('model_response_end', details={
                            'call_id': call_id, 'outcome': 'failed' if failed else 'completed',
                            'error_type': capture_error_type})
                    with gateway.lock:
                        gateway.inflight.pop(fingerprint, None)
                        if failed:
                            gateway.failed.setdefault(fingerprint, call_id)
                        if session is not None:
                            gateway.sessions.discard(session)
                        try:
                            gateway.responses.discard(response)
                            gateway.upstream_sockets.discard(upstream_socket)
                        except UnboundLocalError:
                            pass
                    try:
                        session.close()
                    except AttributeError:
                        pass

        return Handler
