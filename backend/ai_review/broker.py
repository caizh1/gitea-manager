"""仅转发任务授权的模型请求；执行器不接触真实上游凭据。"""
import hashlib
import base64
import contextlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
import requests
from flask import Flask, request, jsonify, Response, stream_with_context

from .common import ReviewError, endpoint, encode, MODEL_CONNECT_TIMEOUT
from .store import Store
from .performance import span
from .model_transport import fetch_model, open_model_stream, ModelTransportError
from .budgets import model_timeout, check_input, MAX_MODEL_INPUT_BYTES
from .completion import ResponseCollector, CompletionError, interpret, body_limit


NATIVE_ID = re.compile(r'^[A-Za-z0-9_-]{8,160}$')
NATIVE_TOOLS = frozenset({'read', 'grep', 'glob'})
UPSTREAM_ERROR_LIMIT = 128 * 1024
with (Path(__file__).with_name('review_result_schema.json')).open(encoding='utf-8') as schema_file:
    REVIEW_RESULT_SCHEMA = json.load(schema_file)


class NativeSSEEnd:
    """只保留当前小事件，用于区分“可信结束后断流”和“未结束断流”。"""
    MAX_EVENT = 256 * 1024

    def __init__(self):
        self.pending = bytearray()
        self.discarding = False
        self.trusted = False
        self.phase = "waiting"
        self.reasoning_chars = 0
        self.content_chars = 0

    @staticmethod
    def boundary(value):
        matches = [(value.find(mark), len(mark)) for mark in (b'\n\n', b'\r\n\r\n')]
        matches = [(index, size) for index, size in matches if index >= 0]
        return min(matches) if matches else (-1, 0)

    def feed(self, chunk):
        self.pending.extend(chunk)
        while True:
            index, size = self.boundary(self.pending)
            if index < 0:
                if self.discarding or len(self.pending) > self.MAX_EVENT:
                    self.discarding = True
                    del self.pending[:-3]
                return
            frame = bytes(self.pending[:index])
            del self.pending[:index + size]
            if self.discarding or len(frame) > self.MAX_EVENT:
                self.discarding = False
                continue
            data = b'\n'.join(line[5:].lstrip() for line in frame.splitlines()
                              if line.startswith(b'data:'))
            if data == b'[DONE]':
                self.trusted = True
                continue
            if not data:
                continue
            try:
                value = json.loads(data)
                choices = value.get('choices', []) if isinstance(value, dict) else []
                for choice in choices if isinstance(choices, list) else []:
                    delta = choice.get('delta', {}) if isinstance(choice, dict) else {}
                    reasoning = delta.get('reasoning') or delta.get('reasoning_content')
                    if isinstance(reasoning, str) and reasoning:
                        self.reasoning_chars += len(reasoning)
                        self.phase = 'thinking'
                    if isinstance(delta.get('content'), str) and delta['content']:
                        self.content_chars += len(delta['content'])
                        self.phase = 'answering'
                    if delta.get('tool_calls'):
                        self.phase = 'tool_call'

                if isinstance(choices, list) and any(isinstance(choice, dict)
                        and choice.get('finish_reason') is not None for choice in choices):
                    self.trusted = True
            except (ValueError, TypeError, UnicodeError):
                pass


class NativeSSEInspector:
    """在转发前检查完整SSE事件，仅拦截顶层error；普通事件保持原字节。"""
    MAX_EVENT = 256 * 1024

    def __init__(self):
        self.pending = bytearray()
        self.passthrough = False

    def feed(self, chunk):
        self.pending.extend(chunk)
        output, failures = bytearray(), []
        while True:
            match = re.search(br'(?:\r\n\r\n|\n\n|\r\r)', self.pending)
            if not match:
                if self.passthrough or len(self.pending) > self.MAX_EVENT:
                    if len(self.pending) > 3:
                        output.extend(self.pending[:-3])
                        del self.pending[:-3]
                    self.passthrough = True
                break
            frame = bytes(self.pending[:match.end()])
            del self.pending[:match.end()]
            if self.passthrough:
                output.extend(frame)
                self.passthrough = False
                continue
            data = b'\n'.join(line[5:].lstrip() for line in frame.splitlines()
                              if line.startswith(b'data:'))
            try:
                value = json.loads(data) if data and data != b'[DONE]' else None
            except (ValueError, UnicodeError):
                value = None
            if isinstance(value, dict) and isinstance(value.get('error'), (dict, str)):
                failures.append((frame, value['error']))
                self.pending.clear()
                break
            else:
                output.extend(frame)
        return bytes(output), failures

    def finish(self):
        tail = bytes(self.pending)
        self.pending.clear()
        return tail


def upstream_error_identity(value, secret=''):
    """从供应商错误中只提取短标识符；正文仅进入受限raw证据。"""
    if isinstance(value, dict):
        error_type, error_code = value.get('type'), value.get('code')
    else:
        error_type, error_code = 'string_error', None
    valid = lambda item: item if (isinstance(item, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,120}', item)
                                  and (not secret or secret not in item)) else None
    return valid(error_type), valid(error_code)


def native_request_shape(body):
    if not isinstance(body, dict) or not isinstance(body.get('messages'), list) or not body['messages']:
        raise ReviewError('原生模型消息格式无效')
    if type(body.get('stream', False)) is not bool:
        raise ReviewError('原生模型流参数无效')
    for message in body['messages']:
        if not isinstance(message, dict) or message.get('role') not in ('system', 'user', 'assistant', 'tool'):
            raise ReviewError('原生模型消息角色无效')
        content = message.get('content')
        if content is not None and not isinstance(content, (str, list)):
            raise ReviewError('原生模型消息正文无效')
        if isinstance(content, list) and any(not isinstance(part, dict) for part in content):
            raise ReviewError('原生模型消息正文无效')
        if message.get('role') == 'tool' and not isinstance(message.get('tool_call_id'), str):
            raise ReviewError('原生工具回执缺少调用标识')
    tools = body.get('tools') or []
    if not isinstance(tools, list):
        raise ReviewError('原生工具声明无效')
    names = []
    for tool in tools:
        function = tool.get('function') if isinstance(tool, dict) else None
        name = function.get('name') if isinstance(function, dict) else None
        if not isinstance(tool, dict) or tool.get('type') != 'function' or not isinstance(function, dict):
            raise ReviewError('原生工具声明无效')
        if name == 'StructuredOutput':
            if function.get('parameters') != REVIEW_RESULT_SCHEMA:
                raise ReviewError('原生结构化结果 Schema 与固定审查契约不一致')
        elif name not in NATIVE_TOOLS:
            raise ReviewError('原生会话只允许 read、grep、glob')
        names.append(name)
    if len(names) != len(set(names)):
        raise ReviewError('原生工具声明包含重复名称')
    if 'StructuredOutput' in names and body.get('tool_choice') != 'required':
        raise ReviewError('原生结构化结果必须使用 required 工具选择')
    return body


def validate_completion(content, streaming, limit=8*1024*1024, metadata=None):
    collector = ResponseCollector(streaming, limit)
    collector.feed(content)
    envelope, protocol = collector.finish()
    protocol.update(metadata or {})
    return interpret(envelope, protocol)


def create_broker(store=None):
    store = store or Store()
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024

    def native_completion(token, body):
        started = time.monotonic()
        transport = None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        job_id = request.headers.get('X-Review-Job', '')
        session_id = request.headers.get('X-Review-Session', '')
        call_id = request.headers.get('X-Review-Call', '')
        try:
            deadline = float(request.headers.get('X-Review-Deadline', ''))
        except ValueError:
            deadline = 0

        def transport_failure(kind):
            return {
                'Timeout': ('MODEL_TOTAL_TIMEOUT', '模型请求超过本次总时限'),
                'ReadTimeout': ('MODEL_IDLE_TIMEOUT', '模型服务在本次期限内未返回数据'),
                'ConnectTimeout': ('MODEL_CONNECT_TIMEOUT', '模型服务连接超过连接时限'),
                'SSLError': ('MODEL_TLS_ERROR', '模型服务 TLS 连接或证书校验失败'),
                'ConnectionError': ('MODEL_CONNECTION_ERROR', '模型服务连接失败或连接中断'),
                'ResponseTooLarge': ('MODEL_RESPONSE_TOO_LARGE', '模型响应超过大小上限'),
            }.get(kind, ('MODEL_TRANSPORT_ERROR', '模型传输或响应协议异常'))

        def mark_failed(code, message, source='model_transport', **details):
            from .diagnostics import describe, safe_remote
            actual = store.job(job_id, evidence=False)
            if actual['status'] in ('cancelling', 'cancelled'):
                with store.tx() as c:
                    c.execute("UPDATE review_native_calls SET status='cancelled',updated=? WHERE call_id=?", (time.time(), call_id))
                logging.info('模型请求因人工取消停止：任务 %s', job_id)
                return
            metrics = dict(getattr(transport, 'metrics', {}) or {})
            metrics.update(details)
            primary = dict(metrics, status='failed', source=source, error_code=code,
                           call_id=call_id, job=job_id, session_id=session_id,
                           elapsed_seconds=round(time.monotonic() - started, 3))
            safe_primary = safe_remote(primary)
            for key in ('first_data_elapsed_seconds', 'last_data_elapsed_seconds', 'tls_phase',
                        'tls_exception_type', 'tls_errno', 'tls_verify_code'):
                if key in metrics and metrics[key] is None:
                    safe_primary[key] = None
            logging.error('AI 原生模型首因 %s', encode(dict(safe_primary, message=message)))
            try:
                with store.tx() as c:
                    c.execute("UPDATE review_native_calls SET status='failed',updated=? WHERE call_id=?",
                              (time.time(), call_id))
                current = store.job(job_id, evidence=False).get('diagnostic') or {}
                if current.get('call_id') != call_id or current.get('status') == 'failed':
                    return
                store.update(job_id, cap['owner'], error=message,
                             diagnostic=dict(current, **safe_primary, message=message))
            except Exception as recording:
                describe(recording, {'id': job_id}, phase='原生模型失败诊断落库')

        def mark_succeeded(**details):
            store.active(job_id, cap['owner'])
            with store.tx() as c:
                c.execute("UPDATE review_native_calls SET status='succeeded',updated=? WHERE call_id=? AND status!='failed'",
                          (time.time(), call_id))
            current = store.job(job_id, evidence=False).get('diagnostic') or {}
            if current.get('call_id') == call_id and current.get('status') != 'failed':
                metrics = dict(getattr(transport, 'metrics', {}) or {})
                metrics.update(details)
                store.update(job_id, cap['owner'], diagnostic=dict(current, status='succeeded',
                    source='model_transport', elapsed_seconds=round(time.monotonic() - started, 3),
                    **metrics))

        def upstream_envelope(phase, reason, status, raw, error=None):
            original_size = len(raw)
            raw = bytes(raw[:UPSTREAM_ERROR_LIMIT])
            error_type, error_code = upstream_error_identity(error, upstream_key)
            headers = dict(getattr(transport, 'safe_headers', {}) or {})
            metrics = dict(getattr(transport, 'metrics', {}) or {})
            value = {
                'call_id': call_id, 'error_code': 'MODEL_HTTP_ERROR' if reason == 'upstream_http_error'
                    else 'MODEL_UPSTREAM_ERROR', 'source': 'model_upstream', 'phase': phase,
                'reason': reason, 'upstream_http_status': status,
                'error_bytes': metrics.get('error_bytes', len(raw)),
                'captured_bytes': len(raw),
                'error_sha256': metrics.get('error_sha256', hashlib.sha256(raw).hexdigest()),
                'captured_sha256': hashlib.sha256(raw).hexdigest(),
                'error_truncated': bool(metrics.get('error_truncated')) or original_size > len(raw),
                'raw_b64': base64.b64encode(raw).decode(), 'headers': headers,
            }
            for key in ('received_bytes', 'declared_error_bytes', 'headers_elapsed_seconds',
                        'first_data_elapsed_seconds', 'last_data_elapsed_seconds'):
                metric = metrics.get(key)
                if isinstance(metric, (int, float)) and not isinstance(metric, bool) and metric >= 0:
                    value[key] = metric
            if error_type:
                value['upstream_error_type'] = error_type
            if error_code:
                value['upstream_error_code'] = error_code
            return value

        def safe_upstream_details(envelope):
            details = {key: value for key, value in envelope.items()
                       if key not in ('raw_b64', 'headers', 'error_code', 'source')}
            headers = envelope.get('headers') or {}
            request_id = headers.get('x-request-id') or headers.get('request-id')
            trace_id = headers.get('x-trace-id') or headers.get('traceparent') or headers.get('cf-ray')
            details['upstream_request_id'] = (request_id if isinstance(request_id, str)
                and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,180}', request_id) else None)
            details['upstream_trace_id'] = (trace_id if isinstance(trace_id, str)
                and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,180}', trace_id) else None)
            return {key: value for key, value in details.items() if value is not None}
        if (not NATIVE_ID.fullmatch(job_id) or not NATIVE_ID.fullmatch(session_id)
                or not NATIVE_ID.fullmatch(call_id) or deadline <= time.time()):
            return jsonify(error={'message': '原生会话调用身份或期限无效'}), 403
        try:
            native_request_shape(body)
            request_hash = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                separators=(',', ':')).encode()).hexdigest()
            with store.tx() as c:
                cap = c.execute('SELECT * FROM capabilities WHERE token_hash=?', (token_hash,)).fetchone()
                if (not cap or cap['job'] != job_id or cap['expires'] < time.time()
                        or cap['remaining'] == 0 or body.get('model') != cap['model'] or deadline > cap['expires'] + 1):
                    raise ReviewError('任务原生会话授权无效或已过期')
                store._active(c, cap['job'], cap['owner'])
                binding = c.execute('SELECT * FROM review_native_bindings WHERE token_hash=?', (token_hash,)).fetchone()
                if binding:
                    if (not binding['active'] or binding['job'] != job_id or binding['session_id'] != session_id
                            or binding['model'] != cap['model'] or abs(binding['deadline'] - deadline) > .001):
                        raise ReviewError('原生会话授权绑定不匹配')
                else:
                    c.execute('INSERT INTO review_native_bindings VALUES (?,?,?,?,?,1)',
                              (token_hash, job_id, session_id, cap['model'], deadline))
                failed = c.execute("SELECT call_id FROM review_native_calls WHERE token_hash=? AND request_hash=? AND status='failed' LIMIT 1",
                                   (token_hash, request_hash)).fetchone()
                if failed:
                    return jsonify(error={'message': '相同失败模型请求已被阻止重放',
                                          'first_call_id': failed['call_id']}), 409
                c.execute('INSERT INTO review_native_calls VALUES (?,?,?,?,?,?,?,?)',
                          (call_id, token_hash, job_id, session_id, request_hash, 'started', time.time(), time.time()))
                if cap['remaining'] > 0:
                    c.execute('UPDATE capabilities SET remaining=remaining-1 WHERE token_hash=?', (token_hash,))
                config = store.job_config(job_id, connection=c)
                job = c.execute('SELECT model_calls FROM jobs WHERE id=?', (job_id,)).fetchone()
                c.execute('UPDATE jobs SET model_calls=model_calls+1 WHERE id=?', (job_id,))
                c.execute('INSERT INTO review_model_inputs VALUES (?,?,?,?)',
                          (job_id, job['model_calls'] + 1, encode(body['messages']), time.time()))
            remaining = min(config.get('model_request_timeout_seconds', 600),
                            deadline - time.time(), cap['expires'] - time.time())
            if remaining <= 0:
                raise ReviewError('原生会话总期限已到')
            store.update(job_id, cap['owner'], diagnostic={
                'call_id': call_id, 'status': 'waiting', 'source': 'model_gateway',
                'request_started_at': time.time(), 'timeout_seconds': round(remaining, 3),
                'job': job_id, 'session_id': session_id, 'model_call': job['model_calls'] + 1,
                'thinking_protocol': config.get('thinking_protocol', 'legacy'),
                'thinking_enabled': config.get('thinking_enabled', False),
                'thinking_token_budget': config.get('thinking_token_budget', 0),
                'task_remaining_seconds': round(deadline - time.time(), 3)})
            outbound = {key: value for key, value in body.items() if key in (
                'messages', 'tools', 'tool_choice', 'parallel_tool_calls', 'stream', 'stream_options',
                'response_format', 'seed', 'stop', 'top_p', 'frequency_penalty', 'presence_penalty')}
            outbound.update(model=cap['model'], temperature=0.1)
            if config.get('model_max_output_tokens'):
                outbound['max_tokens'] = config['model_max_output_tokens']
            from .thinking import parameters as thinking_parameters
            outbound.update(thinking_parameters(config))

            def still_authorized():
                with contextlib.closing(store.connect()) as c:
                    current_cap = c.execute('SELECT * FROM capabilities WHERE token_hash=?', (token_hash,)).fetchone()
                    current_binding = c.execute('SELECT * FROM review_native_bindings WHERE token_hash=?',
                                                (token_hash,)).fetchone()
                    if (not current_cap or current_cap['expires'] < time.time() or not current_binding
                            or not current_binding['active'] or current_binding['session_id'] != session_id
                            or current_binding['deadline'] < time.time()):
                        raise ReviewError('原生会话授权已撤销或到期')
                    store._active(c, job_id, current_cap['owner'])

            upstream_key = store.unseal(config['api_key'])
            transport = open_model_stream(endpoint(config['api_base_url'], 'AI_REVIEW_MODEL_HOSTS') + '/chat/completions',
                outbound, upstream_key, connect_timeout=MODEL_CONNECT_TIMEOUT,
                timeout=remaining, heartbeat=still_authorized)
            if transport.status != 200:
                status = transport.status
                raw = bytearray()
                secondary = None
                try:
                    for chunk in transport.chunks(still_authorized):
                        raw.extend(chunk[:UPSTREAM_ERROR_LIMIT - len(raw)])
                except ModelTransportError as exc:
                    secondary = getattr(exc, 'kind', type(exc).__name__)
                raw = bytes(raw).replace(upstream_key.encode(), b'[credential-removed]')
                error = None
                try:
                    parsed = json.loads(raw)
                    error = parsed.get('error') if isinstance(parsed, dict) else None
                except (ValueError, UnicodeError):
                    pass
                envelope = upstream_envelope('response_headers', 'upstream_http_error', status, raw, error)
                if secondary:
                    envelope['secondary_exception_type'] = secondary
                details = safe_upstream_details(envelope)
                transport.close()
                message = f'模型服务返回 HTTP {status}，未自动重放'
                mark_failed('MODEL_HTTP_ERROR', message, source='model_upstream', **details)
                return jsonify(_review_upstream_error=envelope), 502

            @stream_with_context
            def generate():
                ending = NativeSSEEnd()
                inspector = NativeSSEInspector()
                json_body = bytearray()
                last_progress = 0.0
                try:
                    with span(store, job_id, 'model', call_id):
                        for chunk in transport.chunks(still_authorized):
                            ending.feed(chunk)
                            if time.monotonic() - last_progress >= 2:
                                last_progress = time.monotonic()
                                current = store.job(job_id, evidence=False).get('diagnostic') or {}
                                if current.get('call_id') == call_id and current.get('status') != 'failed':
                                    store.update(job_id, cap['owner'], diagnostic=dict(current,
                                        **transport.metrics, model_phase=ending.phase,
                                        reasoning_chars=ending.reasoning_chars, content_chars=ending.content_chars,
                                        elapsed_seconds=round(time.monotonic()-started, 3),
                                        task_remaining_seconds=max(0, round(deadline-time.time(), 3))))
                            if transport.content_type == 'text/event-stream':
                                output, failures = inspector.feed(chunk)
                                if output:
                                    yield output
                                if failures:
                                    frame, error = failures[0]
                                    frame = frame.replace(upstream_key.encode(), b'[credential-removed]')
                                    envelope = upstream_envelope('response_stream', 'upstream_sse_error', 200,
                                                                 frame, error)
                                    mark_failed('MODEL_UPSTREAM_ERROR', '模型服务流返回错误，未自动重放',
                                                source='model_upstream', **safe_upstream_details(envelope))
                                    yield ('event: review.upstream.error\ndata: ' + json.dumps(
                                        {'_review_upstream_error': envelope}, ensure_ascii=False,
                                        separators=(',', ':')) + '\n\n').encode()
                                    return
                            else:
                                json_body.extend(chunk)
                                if len(json_body) > body_limit(config):
                                    raise ModelTransportError('ResponseTooLarge', transport.metrics)
                    if transport.content_type != 'text/event-stream':
                        raw = bytes(json_body).replace(upstream_key.encode(), b'[credential-removed]')
                        try:
                            parsed = json.loads(raw)
                        except (ValueError, UnicodeError):
                            parsed = None
                        error = parsed.get('error') if isinstance(parsed, dict) else None
                        if isinstance(error, (dict, str)):
                            envelope = upstream_envelope('response_stream', 'upstream_json_error', 200,
                                                         raw, error)
                            mark_failed('MODEL_UPSTREAM_ERROR', '模型服务响应返回错误，未自动重放',
                                        source='model_upstream', **safe_upstream_details(envelope))
                            yield json.dumps({'_review_upstream_error': envelope}, ensure_ascii=False,
                                             separators=(',', ':')).encode()
                            return
                        yield raw
                    else:
                        tail = inspector.finish()
                        if tail:
                            yield tail
                    mark_succeeded()
                except GeneratorExit:
                    # 客户端已断开时不再写错误帧。
                    raise
                except BaseException as exc:
                    kind = getattr(exc, 'kind', type(exc).__name__)
                    code, reason = transport_failure(kind)
                    message = reason + f'（{kind}），未自动重放'
                    if isinstance(exc, ModelTransportError) and ending.trusted:
                        # 传输层随后报错不得推翻已收到的明确结束；
                        # length 等完整性仍由上层 _review_completion 判定为 partial。
                        mark_succeeded(transport_after_trusted_end=kind)
                    else:
                        mark_failed(code, message, exception_type=kind)
                    if isinstance(exc, ModelTransportError) and not ending.trusted:
                        marker = {'_review_transport_error': {
                            'call_id': call_id, 'error_code': code, 'exception_type': kind}}
                        yield ('event: review.transport.error\ndata: ' + json.dumps(
                            marker, ensure_ascii=False, separators=(',', ':')) + '\n\n').encode()
                    return
                finally:
                    transport.close()

            return Response(generate(), content_type=transport.content_type or
                            ('text/event-stream' if outbound.get('stream') else 'application/json'))
        except ReviewError as exc:
            mark_failed('MODEL_GATEWAY_REJECTED', str(exc), source='model_gateway',
                        exception_type=type(exc).__name__)
            return jsonify(error={'message': str(exc), 'call_id': call_id}), 400
        except (requests.RequestException, ValueError) as exc:
            kind = getattr(exc, 'kind', type(exc).__name__)
            code, reason = transport_failure(kind)
            message = reason + f'（{kind}），未自动重放'
            details = dict(getattr(exc, 'details', {}) or {})
            details['exception_type'] = kind
            mark_failed(code, message, **details)
            return jsonify(error={'message': message, 'call_id': call_id}), 502

    @app.post('/v1/chat/completions')
    def completion():
        job_id = None
        call_id = None
        transfer = {}
        limit_seconds = None
        started_request = time.monotonic()
        def diagnostic(event, **fields):
            logging.info('AI 模型网关 %s', json.dumps(dict(time_utc=datetime.now(timezone.utc).isoformat(),
                event=event, job=job_id, elapsed=round(time.monotonic()-started_request,3), **fields),ensure_ascii=False))
        def failure(code, message, **details):
            if job_id is None:
                return
            try:
                current = store.job(job_id, evidence=False).get('diagnostic') or {}
                # 后续授权拦截不能覆盖同次调用的首个失败，也不能串到下一单元。
                if current.get('call_id') != call_id or current.get('status') == 'failed':
                    return
                record = dict(current, **transfer)
                record.update(details)
                record.update(status='failed', error_code=code, message=message,
                              source=('input_budget' if code == 'MODEL_INPUT_BUDGET' else 'model_gateway' if code == 'MODEL_GATEWAY_REJECTED' else 'model_service' if code == 'MODEL_HTTP_ERROR' else 'model_protocol' if code == 'MODEL_INVALID_COMPLETION' else 'model_transport'),
                              elapsed_seconds=round(time.monotonic()-started_request,3))
                store.update(job_id, cap['owner'], error=message, diagnostic=record)
            except ReviewError:
                pass

        try:
            token = request.headers.get('Authorization', '').removeprefix('Bearer ')
            body = request.get_json(silent=True)
            if request.headers.get('X-Review-Session'):
                return native_completion(token, body)
            if (not isinstance(body, dict) or not isinstance(body.get('messages'), list)
                    or not 1 <= len(body['messages']) <= 12 or body.get('tools') or body.get('functions')):
                raise ReviewError('只允许受限的文本模型调用')
            messages = body['messages']
            if any(not isinstance(m, dict) or m.get('role') not in ('system', 'user', 'assistant')
                   or not isinstance(m.get('content'), str) for m in messages):
                raise ReviewError('模型消息必须是纯文本')
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            call_id = token_hash[:24]
            with store.tx() as c:
                cap = c.execute('SELECT * FROM capabilities WHERE token_hash=?', (token_hash,)).fetchone()
                job_id = cap['job'] if cap else None
                if not cap or cap['expires'] < time.time() or cap['remaining'] <= 0 or body.get('model') != cap['model']:
                    diagnostic('拒绝重复或无效授权', exists=bool(cap), consumed=bool(cap and cap['remaining']<=0))
                    raise ReviewError('任务模型授权无效或已过期')
                store._active(c, cap['job'], cap['owner'])
                config = store.job_config(cap['job'], connection=c)
                job = c.execute('SELECT model_calls,plan_units,call_limit FROM jobs WHERE id=?', (cap['job'],)).fetchone()
                limit = job['call_limit'] or (job['plan_units']*2 if job['plan_units'] else config['max_calls'])
                if limit > 0 and job['model_calls'] >= limit:
                    raise ReviewError('已达到本任务模型调用上限')
                c.execute('UPDATE capabilities SET remaining=remaining-1 WHERE token_hash=?', (token_hash,))
                c.execute('UPDATE jobs SET model_calls=model_calls+1 WHERE id=?', (cap['job'],))
                # 最终发送的消息包含 CLI 附加内容，必须与 Worker 原始输入分别保存核对。
                c.execute('INSERT INTO review_model_inputs VALUES (?,?,?,?)',
                          (cap['job'],job['model_calls']+1,encode(messages),time.time()))
            limit_seconds = model_timeout(config)
            current = store.job(job_id, evidence=False).get('diagnostic') or {}
            if current.get('call_id') != call_id:
                current = {'call_id':call_id, 'request_started_at':time.time(), 'job':job_id}
            store.update(job_id, cap['owner'], diagnostic=dict(current, status='waiting',
                timeout_seconds=limit_seconds, model_call=job['model_calls']+1))
            try:
                transfer.update(input_bytes=check_input(messages),input_limit_bytes=MAX_MODEL_INPUT_BYTES)
            except ReviewError as exc:
                transfer.update(input_bytes=exc.input_bytes,input_limit_bytes=MAX_MODEL_INPUT_BYTES,rejection_branch='input_budget')
                failure(exc.error_code,str(exc),source_detail='input_budget')
                return jsonify(error={'message':str(exc)}),413
            target = endpoint(config['api_base_url'], 'AI_REVIEW_MODEL_HOSTS')
            diagnostic('开始唯一上游请求', call=job['model_calls']+1, call_id=call_id, timeout_seconds=limit_seconds)
            outbound = {'model': cap['model'], 'messages': messages,
                        'temperature': 0.1, 'stream': bool(body.get('stream')),
                        'response_format': {'type': 'json_object'}}
            if config.get('model_max_output_tokens'):
                outbound['max_tokens'] = config['model_max_output_tokens']
            from .thinking import parameters as thinking_parameters
            outbound.update(thinking_parameters(config))
            with span(store,job_id,'model',call_id):
                status, raw = fetch_model(target + '/chat/completions', outbound,
                                          store.unseal(config['api_key']), connect_timeout=MODEL_CONNECT_TIMEOUT,
                                          timeout=limit_seconds, diagnostics=transfer, response_limit=body_limit(config))
            diagnostic('上游传输已结束', http_status=status, **transfer)
            if status != 200:
                failure('MODEL_HTTP_ERROR', f'模型服务返回 HTTP {status}，未自动重放', upstream_http_status=status)
                return jsonify(error={'message': f'模型服务返回 HTTP {status}'}), 502
            store.active(cap['job'], cap['owner'])
            try:
                value, protocol = validate_completion(raw, False if transfer.get('normalized_response') else outbound['stream'], body_limit(config), transfer)
                transfer.update(protocol, configured_output_tokens=config.get('model_max_output_tokens', 0), body_limit_bytes=body_limit(config))
            except CompletionError as exc:
                transfer.update(exc.metadata)
                # 原始回执只在加密证据中保存，不进入诊断或模型网关日志。
                with store.tx() as c:
                    store._active(c, job_id, cap['owner'])
                    c.execute('CREATE TABLE IF NOT EXISTS review_model_failures (job TEXT, call_id TEXT, receipt TEXT, PRIMARY KEY(job,call_id))')
                    c.execute('INSERT OR IGNORE INTO review_model_failures VALUES (?,?,?)', (job_id,call_id,store.seal_evidence(raw.decode('utf-8', errors='replace'),body_limit(config)*8)))
                message = '模型响应没有可用的完整对象：'+exc.code+'；已记录本单元，继续审查其余代码'
                failure('MODEL_INVALID_COMPLETION', message)
                diagnostic('响应校验未通过', call_id=call_id, **transfer)
                return jsonify(error={'message':message}), 502
            diagnostic('上游响应完整并已校验', bytes=len(raw))
            current = store.job(job_id, evidence=False).get('diagnostic') or {}
            if current.get('call_id') == call_id:
                store.update(job_id, cap['owner'], diagnostic=dict(current, status='succeeded', source='model_transport',
                    elapsed_seconds=round(time.monotonic()-started_request,3), **transfer))
            content = json.dumps(value, ensure_ascii=False)
            if outbound['stream']:
                chunks = [{'choices':[{'index':0,'delta':{'content':content},'finish_reason':None}]},
                          {'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]}]
                body = ''.join('data: '+json.dumps(chunk, ensure_ascii=False)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n'
                return Response(body, content_type='text/event-stream')
            return jsonify(choices=[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':content}}])
        except ReviewError as exc:
            diagnostic('请求停止', exception_type=type(exc).__name__)
            if job_id is not None and 'cap' in locals() and cap:
                failure('MODEL_GATEWAY_REJECTED', str(exc), source_detail='gateway')
            return jsonify(error={'message': str(exc)}), 403
        except (requests.RequestException, ValueError) as exc:
            kind = getattr(exc, 'kind', type(exc).__name__)
            diagnostic('上游连接或协议异常', exception_type=kind, **transfer)
            codes = {'Timeout': ('MODEL_TOTAL_TIMEOUT', f'模型请求超过本次 {limit_seconds} 秒总时限'),
                     'ReadTimeout': ('MODEL_IDLE_TIMEOUT', f'模型服务在本次 {limit_seconds} 秒期限内未返回数据'),
                     'ConnectTimeout': ('MODEL_CONNECT_TIMEOUT', '模型服务连接超过 15 秒'),
                     'SSLError': ('MODEL_TLS_ERROR', '模型服务 TLS 连接或证书校验失败'),
                     'ConnectionError': ('MODEL_CONNECTION_ERROR', '模型服务连接失败或连接中断'),
                     'ResponseTooLarge': ('MODEL_RESPONSE_TOO_LARGE', '模型响应超过大小上限')}
            code, message = codes.get(kind, ('MODEL_TRANSPORT_ERROR', '模型传输或响应协议异常'))
            failure(code, message+'（'+kind+'），未自动重放', exception_type=kind)
            return jsonify(error={'message': '模型连接失败或超时'}), 502

    @app.get('/health')
    def health():
        return jsonify(ok=True)

    @app.route('/internal/raw-diagnostics/<job_id>/<session_id>/archive',methods=['GET','HEAD'])
    def raw_diagnostics_archive(job_id, session_id):
        from .common import secret_file
        from .native_review import EXECUTOR_URL
        from .raw_diagnostics import MAX_ARCHIVE_BYTES, verify_proxy
        timestamp = request.headers.get('X-Raw-Diagnostics-Time', '')
        signature = request.headers.get('X-Raw-Diagnostics-Signature', '')
        action='head' if request.method=='HEAD' else 'archive'
        if not verify_proxy(action, job_id, session_id, timestamp, signature):
            return jsonify(error='原始诊断代理鉴权失败'), 401
        try:
            job = store.job(job_id, evidence=False)
        except ReviewError:
            return jsonify(error='审查任务不存在'), 404
        if job.get('session_id') != session_id:
            return jsonify(error='审查任务与原生会话不匹配'), 403
        if job.get('status') not in ('succeeded','failed','superseded','publish_unknown'):
            return jsonify(error='原始诊断仅在任务终态后可下载'), 409
        client = requests.Session(); client.trust_env = False
        try:
            upstream = client.get(EXECUTOR_URL + f'/sessions/{session_id}/diagnostics/archive',
                params={'job_id':job_id}, headers={'Authorization':'Bearer '+secret_file('AI_REVIEW_EXECUTOR_TOKEN')},
                stream=True, timeout=(5,30), allow_redirects=False)
        except requests.RequestException:
            client.close()
            return jsonify(error='原始诊断存储不可用'), 503
        if upstream.status_code != 200:
            status = upstream.status_code
            try:
                body=upstream.raw.read(65537,decode_content=True)
                value = json.loads(body) if len(body)<=65536 else None
                message = value.get('error') if isinstance(value,dict) else None
            except (ValueError,UnicodeError,AttributeError,TypeError,requests.RequestException,OSError):
                message = None
            finally:
                upstream.close();client.close()
            return jsonify(error=message or '原始诊断不可用'), status
        if (upstream.headers.get('Content-Type','').split(';',1)[0] != 'application/zip'
                or upstream.headers.get('X-Diagnostics-Job-ID') != job_id
                or upstream.headers.get('X-Diagnostics-Session-ID') != session_id):
            upstream.close();client.close()
            return jsonify(error='原始诊断归档身份或格式无效'), 502
        try: length = int(upstream.headers.get('Content-Length',''))
        except ValueError: length = -1
        if not 0 <= length <= MAX_ARCHIVE_BYTES:
            upstream.close();client.close()
            return jsonify(error='原始诊断归档大小无效'), 502

        def archive_chunks():
            sent=0
            try:
                for chunk in upstream.iter_content(64*1024):
                    sent += len(chunk)
                    if sent > MAX_ARCHIVE_BYTES:
                        raise RuntimeError('原始诊断归档超过代理上限')
                    yield chunk
            finally:
                upstream.close();client.close()
        headers={key:value for key,value in upstream.headers.items()
                 if key.lower().startswith('x-diagnostics-')}
        headers.update({'Content-Length':str(length),'Content-Type':'application/zip',
                        'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        if request.method=='HEAD':
            upstream.close();client.close()
            result=Response('',headers=headers)
            result.headers['Content-Length']=str(length)
            return result
        result=Response(archive_chunks(),headers=headers)
        result.call_on_close(lambda:(upstream.close(),client.close()))
        return result
    return app
