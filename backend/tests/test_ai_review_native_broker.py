"""原生模型网关真实 HTTP 失败链诊断测试。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta
import base64
import json
import os
from pathlib import Path
import ssl
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from ai_review.broker import (create_broker, NativeSSEEnd, NativeSSEInspector, native_request_shape,
                              upstream_error_identity, native_sampling_parameters,
                              REVIEW_RESULT_SCHEMA)
from ai_review.common import ReviewError
from ai_review.diagnostics import message_for, safe_remote
from ai_review.model_transport import ModelTransportError, safe_response_headers
from ai_review.native_review import _capability
from ai_review.store import Store


class _Upstream:
    def __init__(self, mode):
        self.mode = mode
        self.bodies = []
        self.temp = tempfile.TemporaryDirectory() if mode == 'tls' else None
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if mode == 'capture':
                    owner.bodies.append(json.loads(raw))
                    body = (b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                            b'data: [DONE]\n\n')
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if mode == 'sse_error':
                    body = (b'data: {"error":{"type":"upstream_error","code":"overloaded",'
                            b'"message":"Internal server error upstream-test-key"}}\n\n')
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('X-Request-ID', 'req-test-123')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers(); self.wfile.write(body); return
                if mode == 'sse_error_sequence':
                    body = (b'data: {"choices":[{"delta":{"content":"before"},"finish_reason":null}]}\n\n'
                            b'data: {"error":{"type":"upstream_error","code":"failed"}}\n\n'
                            b'data: {"choices":[{"delta":{"content":"after"},"finish_reason":"stop"}]}\n\n')
                    self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(body))); self.end_headers()
                    self.wfile.write(body); return
                if mode == 'json_error':
                    body = b'{"error":"Internal server error upstream-test-key"}'
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers(); self.wfile.write(body); return
                if mode in ('http403', 'http500', 'http500_partial'):
                    body = (b'{"error":{"type":"permission_error","code":"denied",'
                            b'"message":"upstream-test-key"}}')
                    self.send_response(403 if mode == 'http403' else 500)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('X-Trace-ID', 'trace-test-456')
                    self.send_header('Content-Length', str(len(body) + (20 if mode == 'http500_partial' else 0)))
                    self.end_headers(); self.wfile.write(body)
                    if mode == 'http500_partial':
                        self.wfile.flush(); self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if mode == 'timeout':
                    time.sleep(5)
                    return
                self.send_response(403)
                self.send_header('Content-Length', '0')
                self.end_headers()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        if mode == 'tls':
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
            certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.utcnow() - timedelta(days=1))
                .not_valid_after(datetime.utcnow() + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
                .sign(key, hashes.SHA256()))
            root = Path(self.temp.name)
            cert_path, key_path = root / 'cert.pem', root / 'key.pem'
            cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_path, key_path)
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        scheme = 'https' if self.mode == 'tls' else 'http'
        return f'{scheme}://127.0.0.1:{self.server.server_address[1]}/v1'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.temp:
            self.temp.cleanup()


class NativeBrokerFailureTests(unittest.TestCase):
    def test_structured_output_only_accepts_frozen_schema(self):
        base = {'messages': [{'role': 'user', 'content': '仅测试结构化结果'}],
                'stream': True, 'tool_choice': 'required'}
        structured = {'type': 'function', 'function': {
            'name': 'StructuredOutput', 'description': '提交最终结果',
            'parameters': json.loads(json.dumps(REVIEW_RESULT_SCHEMA))}}
        read = {'type': 'function', 'function': {'name': 'read', 'parameters': {'type': 'object'}}}
        valid = dict(base, tools=[read, structured])
        self.assertIs(native_request_shape(valid), valid)
        for choice in (None, 'auto', 'required',
                       {'type': 'function', 'function': {'name': 'StructuredOutput'}}):
            with self.subTest(choice=choice):
                value = dict(valid, tool_choice=choice)
                self.assertIs(native_request_shape(value), value)

        changed = json.loads(json.dumps(structured))
        changed['function']['parameters']['additionalProperties'] = True
        invalid = [
            dict(base, tools=[changed]),
            dict(base, tools=[structured, structured]),
            dict(base, tools=[{'type': 'function', 'function': {'name': 'shell', 'parameters': {}}}]),
            dict(base, tools=[[]]),
            dict(base, tools=[structured], tool_choice='none'),
            dict(base, tools=[structured], tool_choice={'type': 'function', 'function': {'name': 'glob'}}),
            dict(base, tools=[structured], tool_choice={'type': 'invalid', 'function': {'name': 'StructuredOutput'}}),
            dict(base, tools=[structured], tool_choice=[]),
        ]
        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaises(ReviewError):
                    native_request_shape(body)

    def test_structured_output_diagnostics_keep_safe_location_and_specific_reason(self):
        for reason, expected in (
                ('missing_structured_output', '未通过结构化结果工具'),
                ('structured_output_error', '不符合固定格式'),
                ('structured_result_not_object', '不是对象'),
                ('structured_result_too_large', '超过大小限制')):
            with self.subTest(reason=reason):
                diagnostic = safe_remote({'error_code': 'invalid_json', 'source': 'native_result',
                    'phase': 'structured_result', 'reason': reason,
                    'extraction_path': 'session.message.response.info.structured',
                    'native_error_type': 'StructuredOutputError', 'message': '模型原始正文'})
                self.assertEqual(diagnostic['reason'], reason)
                self.assertEqual(diagnostic['extraction_path'],
                                 'session.message.response.info.structured')
                self.assertNotIn('message', diagnostic)
                self.assertIn(expected, message_for('invalid_json', reason))
        self.assertEqual(message_for('native_message_error', 'native_message_error'),
                         '原生模型消息执行失败，审查未完成')
        native_error = safe_remote({'error_code': 'native_message_error',
            'source': 'native_result', 'phase': 'message_completion',
            'reason': 'native_message_error',
            'extraction_path': 'session.message.response.info.error',
            'native_error_type': 'APIError'})
        self.assertEqual(native_error['extraction_path'], 'session.message.response.info.error')

    def test_sse_end_tracker_is_bounded_and_recovers_at_next_event(self):
        tracker = NativeSSEEnd()
        block = b'data: ' + b'x' * 8192
        for _ in range((tracker.MAX_EVENT // len(block)) + 2):
            tracker.feed(block)
        self.assertLessEqual(len(tracker.pending), 3)
        tracker.feed(b'\n\n')
        tracker.feed(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
        self.assertTrue(tracker.trusted)

    def test_sse_inspector_preserves_line_endings_and_oversized_event_boundaries(self):
        for separator in (b'\n\n', b'\r\n\r\n', b'\r\r'):
            inspector = NativeSSEInspector()
            frame = b'data: {"choices":[]}' + separator
            output, failures = inspector.feed(frame)
            self.assertEqual(output, frame)
            self.assertEqual(failures, [])
        inspector = NativeSSEInspector(); inspector.MAX_EVENT = 16
        first = b'data: ' + b'x' * 30
        output1, failures1 = inspector.feed(first)
        output2, failures2 = inspector.feed(b'\r\n\r\ndata: {"choices":[]}\n\n')
        self.assertEqual(output1 + output2, first + b'\r\n\r\ndata: {"choices":[]}\n\n')
        self.assertEqual(failures1 + failures2, [])

    def test_safe_upstream_identifiers_and_headers_never_echo_actual_key(self):
        secret = 'upstream-test-key'
        headers = safe_response_headers({'X-Request-ID': secret, 'X-Trace-ID': 'trace-ok',
            'Authorization': 'Bearer ' + secret, 'Set-Cookie': secret}, secret)
        self.assertEqual(headers, {'x-trace-id': 'trace-ok'})
        self.assertEqual(upstream_error_identity({'type': secret, 'code': 'safe-code'}, secret),
                         (None, 'safe-code'))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        key = root / '主密钥'
        key.write_bytes(Fernet.generate_key())
        environment = patch.dict(os.environ, {'AI_REVIEW_MASTER_KEY_FILE': str(key)})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = Store(root / 'review.db')

    def request(self, mode, timeout, repository_id=99, output_tokens=0, thinking=None,
                model='测试模型', request_values=None):
        upstream = _Upstream(mode)
        self.addCleanup(upstream.close)
        current = self.store.config()
        self.store.save_config({'revision': current['revision'], 'enabled': True,
            'debounce_seconds': 0, 'model': model, 'engine': 'opencode_native',
            'api_base_url': upstream.url, 'api_key': self.store.seal('upstream-test-key'),
            'model_request_timeout_seconds': timeout,
            'model_max_output_tokens': output_tokens, **(thinking or {})})
        repository = self.store.save_repo(1, repository_id, {'enabled': True,
            'bot_token': self.store.seal('机器人凭据'), 'webhook_secret': self.store.seal('签名秘密'),
            'repo_name': f'test/native-broker-{repository_id}', 'bot_id': 9})['id']
        job_id = self.store.enqueue(repository, 1, {'head': 'a' * 40, 'base': 'b' * 40})
        job = self.store.claim()
        # 验证旧单会话协议；分批协议另有成本与恢复测试。
        with self.store.tx() as c: c.execute("UPDATE jobs SET batch_protocol=0 WHERE id=?", (job["id"],))
        deadline = time.time() + timeout + 5
        capability = _capability(self.store, job, model, deadline, -1)
        body = {'model': model, 'stream': True,
                'messages': [{'role': 'user', 'content': '仅测试失败链'}], 'tools': []}
        body.update(request_values or {})
        headers = {'Authorization': 'Bearer ' + capability, 'X-Review-Job': job_id,
                   'X-Review-Session': f'session_test_{repository_id:04d}',
                   'X-Review-Call': f'call_test_{repository_id:06d}', 'X-Review-Deadline': str(deadline)}
        with patch('ai_review.broker.endpoint', return_value=upstream.url):
            response = create_broker(self.store).test_client().post('/v1/chat/completions',
                                                                     json=body, headers=headers)
            response.get_data()
        return response, self.store.job(job_id, evidence=False)['diagnostic'], upstream

    def test_actual_http_qwen_sampling_and_tool_choice_match_deployment_contract(self):
        structured = {'type': 'function', 'function': {
            'name': 'StructuredOutput', 'parameters': REVIEW_RESULT_SCHEMA}}
        glob = {'type': 'function', 'function': {'name': 'glob', 'parameters': {'type': 'object'}}}
        messages = [
            {'role': 'user', 'content': '审查固定源码'},
            {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'empty-build-search', 'type': 'function',
                'function': {'name': 'glob', 'arguments': '{"pattern":"after/**/build.sh"}'}}]},
            {'role': 'tool', 'tool_call_id': 'empty-build-search', 'content': 'No files found'},
        ]
        for index, choice in enumerate(('auto', {'type': 'function', 'function': {'name': 'StructuredOutput'}})):
            with self.subTest(choice=choice):
                response, _, upstream = self.request('capture', 5, repository_id=950+index,
                    model='qwen3.8-27b-fp8', thinking={'thinking_protocol': 'qwen_vllm', 'thinking_enabled': False},
                    request_values={'temperature': 0.55, 'top_p': 1, 'tool_choice': choice,
                                    'tools': [glob, structured] if choice == 'auto' else [structured],
                                    'messages': messages})
                self.assertEqual(response.status_code, 200)
                sent = upstream.bodies[0]
                self.assertEqual(sent['temperature'], 0.7)
                self.assertEqual(sent['top_p'], 0.8)
                self.assertEqual(sent['top_k'], 20)
                self.assertEqual(sent['presence_penalty'], 1.5)
                self.assertEqual(sent['chat_template_kwargs'], {'enable_thinking': False})
                self.assertNotIn('reasoning_effort', sent)
                self.assertEqual(sent['tool_choice'], choice)
                self.assertEqual(sent['messages'], messages)
                self.assertEqual(sent['tools'][-1], structured)

    def test_sampling_preserves_other_models_and_qwen_thinking_profile(self):
        values = {'temperature': 0.55, 'top_p': 1, 'presence_penalty': 0.2}
        self.assertEqual(native_sampling_parameters(values, {'model': '其他模型'}), values)
        self.assertEqual(native_sampling_parameters({}, {'model': '其他模型'}), {})
        profile = native_sampling_parameters(values, {'model': 'Qwen/Qwen3.8-27B-FP8',
            'thinking_protocol': 'qwen_vllm', 'thinking_enabled': True})
        self.assertEqual(profile['temperature'], 1.0)
        self.assertEqual(profile['top_p'], 0.95)
        self.assertEqual(profile['presence_penalty'], 0.0)

    def test_actual_http_403_keeps_status_and_header_metrics(self):
        response, diagnostic, _upstream = self.request('http403', 5)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(diagnostic['error_code'], 'MODEL_HTTP_ERROR')
        self.assertEqual(diagnostic['upstream_http_status'], 403)
        self.assertIn('headers_elapsed_seconds', diagnostic)
        self.assertEqual(diagnostic['transfer_phase'], 'completed')
        self.assertEqual(diagnostic['source'], 'model_upstream')
        self.assertEqual(diagnostic['reason'], 'upstream_http_error')
        self.assertEqual(diagnostic['upstream_error_type'], 'permission_error')
        self.assertEqual(diagnostic['upstream_error_code'], 'denied')
        self.assertEqual(diagnostic['upstream_trace_id'], 'trace-test-456')
        self.assertNotIn('upstream-test-key', json.dumps(diagnostic))
        envelope = response.get_json()['_review_upstream_error']
        self.assertEqual(envelope['captured_bytes'], len(base64.b64decode(envelope['raw_b64'])))
        self.assertNotIn(b'upstream-test-key', base64.b64decode(envelope['raw_b64']))
        self.assertIn(b'[credential-removed]', base64.b64decode(envelope['raw_b64']))

    def test_http_200_sse_error_is_consumed_and_persisted_as_first_cause(self):
        response, diagnostic, _upstream = self.request('sse_error', 5, repository_id=301)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'Internal server error', response.data)
        self.assertIn(b'_review_upstream_error', response.data)
        self.assertEqual(diagnostic['error_code'], 'MODEL_UPSTREAM_ERROR')
        self.assertEqual(diagnostic['source'], 'model_upstream')
        self.assertEqual(diagnostic['phase'], 'response_stream')
        self.assertEqual(diagnostic['reason'], 'upstream_sse_error')
        self.assertEqual(diagnostic['upstream_http_status'], 200)
        self.assertEqual(diagnostic['upstream_request_id'], 'req-test-123')
        self.assertNotIn('upstream-test-key', json.dumps(diagnostic))

    def test_same_chunk_stops_at_first_sse_error_and_drops_later_success(self):
        response, diagnostic, _upstream = self.request('sse_error_sequence', 5, repository_id=306)
        self.assertIn(b'before', response.data)
        self.assertNotIn(b'after', response.data)
        self.assertNotIn(b'finish_reason":"stop', response.data)
        self.assertIn(b'_review_upstream_error', response.data)
        self.assertEqual(diagnostic['reason'], 'upstream_sse_error')
        self.assertEqual(diagnostic['status'], 'failed')

    def test_http_200_json_top_level_error_is_not_success(self):
        response, diagnostic, _upstream = self.request('json_error', 5, repository_id=302)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'_review_upstream_error', response.data)
        self.assertNotIn(b'Internal server error', response.data)
        self.assertEqual(diagnostic['reason'], 'upstream_json_error')
        self.assertEqual(diagnostic['upstream_error_type'], 'string_error')

    def test_large_normal_json_keeps_existing_response_limit(self):
        raw = json.dumps({'choices': [{'message': {'role': 'assistant',
            'content': 'x' * (192 * 1024)}, 'finish_reason': 'stop'}]}).encode()
        class Stream:
            status = 200
            content_type = 'application/json'
            safe_headers = {}
            metrics = {'transfer_phase': 'completed', 'received_bytes': len(raw)}
            def chunks(self, _heartbeat): yield raw
            def close(self): return
        with patch('ai_review.broker.open_model_stream', return_value=Stream()):
            response, diagnostic, _upstream = self.request('http403', 5, repository_id=305)
        self.assertEqual(response.data, raw)
        self.assertEqual(diagnostic['status'], 'succeeded')

    def test_http_500_partial_body_keeps_http_primary_and_transport_secondary(self):
        response, diagnostic, _upstream = self.request('http500_partial', 5, repository_id=303)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(diagnostic['error_code'], 'MODEL_HTTP_ERROR')
        self.assertEqual(diagnostic['upstream_http_status'], 500)
        self.assertEqual(diagnostic['reason'], 'upstream_http_error')
        self.assertIn('secondary_exception_type', diagnostic)
        self.assertGreater(diagnostic['error_bytes'], 0)
        self.assertNotIn('upstream-test-key', json.dumps(diagnostic))

    def test_persistence_failure_still_logs_safe_first_cause(self):
        original = self.store.update
        def fail_diagnostic(job, owner, **fields):
            if (fields.get('diagnostic') or {}).get('status') == 'failed':
                raise OSError('upstream-test-key must never be logged')
            return original(job, owner, **fields)
        with patch.object(self.store, 'update', side_effect=fail_diagnostic), \
                self.assertLogs(level='ERROR') as captured:
            response, _diagnostic, _upstream = self.request('sse_error', 5, repository_id=304)
        self.assertEqual(response.status_code, 200)
        logs = '\n'.join(captured.output)
        self.assertIn('MODEL_UPSTREAM_ERROR', logs)
        self.assertIn('原生模型失败诊断落库', logs)
        self.assertNotIn('upstream-test-key', logs)

    def test_actual_total_timeout_keeps_primary_transport_kind(self):
        response, diagnostic, _upstream = self.request('timeout', 1)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(diagnostic['error_code'], 'MODEL_TOTAL_TIMEOUT')
        self.assertEqual(diagnostic['exception_type'], 'Timeout')
        self.assertEqual(diagnostic['source'], 'model_transport')

    def test_actual_tls_failure_keeps_safe_transport_stage_and_host(self):
        response, diagnostic, _upstream = self.request('tls', 5)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(diagnostic['error_code'], 'MODEL_TLS_ERROR')
        self.assertEqual(diagnostic['source'], 'model_transport')
        self.assertEqual(diagnostic['exception_type'], 'SSLError')
        self.assertEqual(diagnostic['transfer_phase'], 'connecting')
        self.assertEqual(diagnostic['tls_phase'], 'connecting')
        self.assertEqual(diagnostic['target_host'], '127.0.0.1')
        self.assertEqual(diagnostic['received_bytes'], 0)
        self.assertIsNone(diagnostic['first_data_elapsed_seconds'])
        self.assertIsNone(diagnostic['last_data_elapsed_seconds'])
        self.assertIn('tls_exception_type', diagnostic)
        self.assertIn('tls_errno', diagnostic)
        self.assertIn('tls_verify_code', diagnostic)

    def test_tls_failure_after_http_200_keeps_first_transport_diagnostic(self):
        class Stream:
            status = 200
            content_type = 'text/event-stream'
            metrics = {'transfer_phase': 'headers_received', 'received_bytes': 0,
                       'first_data_elapsed_seconds': None, 'last_data_elapsed_seconds': None,
                       'target_host': 'model.example', 'tls_phase': None,
                       'tls_exception_type': None, 'tls_errno': None, 'tls_verify_code': None}

            def chunks(self, _heartbeat):
                chunk = b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
                self.metrics.update(transfer_phase='receiving', received_bytes=len(chunk),
                    first_data_elapsed_seconds=.125, last_data_elapsed_seconds=.125,
                    tls_phase='receiving', tls_exception_type='SSLError', tls_errno=71,
                    tls_verify_code=20)
                yield chunk
                raise ModelTransportError('SSLError', self.metrics)

            def close(self):
                return

        with patch('ai_review.broker.open_model_stream', return_value=Stream()):
            response, diagnostic, _upstream = self.request('http403', 5)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'partial', response.data)
        self.assertIn(b'_review_transport_error', response.data)
        self.assertEqual(diagnostic['error_code'], 'MODEL_TLS_ERROR')
        self.assertEqual(diagnostic['transfer_phase'], 'receiving')
        self.assertGreater(diagnostic['received_bytes'], 0)
        self.assertLess(diagnostic['received_bytes'], len(response.data))
        self.assertEqual(diagnostic['first_data_elapsed_seconds'], .125)
        self.assertEqual(diagnostic['last_data_elapsed_seconds'], .125)
        self.assertEqual(diagnostic['tls_phase'], 'receiving')
        self.assertEqual(diagnostic['tls_errno'], 71)
        self.assertEqual(diagnostic['tls_verify_code'], 20)
        self.assertEqual(diagnostic['target_host'], 'model.example')

    def test_transport_error_after_trusted_finish_does_not_discard_response(self):
        stop = b'data: {"choices":[{"delta":{"content":"result"},"finish_reason":"stop"}]}\n\n'
        done = b'data: [DONE]\n\n'
        length = b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}\n\n'
        for index, (label, chunks) in enumerate((('stop_done', [stop, done]),
                ('stop_without_done', [stop]), ('length_remains_partial', [length]))):
            with self.subTest(label=label):
                class Stream:
                    status = 200
                    content_type = 'text/event-stream'

                    def __init__(self, rows):
                        self.rows = rows
                        self.metrics = {'transfer_phase': 'receiving',
                            'received_bytes': sum(map(len, rows)),
                            'first_data_elapsed_seconds': .1, 'last_data_elapsed_seconds': .2,
                            'target_host': 'model.example', 'tls_phase': 'receiving',
                            'tls_exception_type': 'SSLError', 'tls_errno': 71,
                            'tls_verify_code': 20}

                    def chunks(self, _heartbeat):
                        yield from self.rows
                        raise ModelTransportError('SSLError', self.metrics)

                    def close(self):
                        return

                with patch('ai_review.broker.open_model_stream', return_value=Stream(chunks)):
                    response, diagnostic, _upstream = self.request('http403', 5, repository_id=100 + index)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(b'_review_transport_error', response.data)
                self.assertEqual(response.data, b''.join(chunks))
                self.assertEqual(diagnostic['status'], 'succeeded')
                self.assertEqual(diagnostic['transport_after_trusted_end'], 'SSLError')
                if label == 'length_remains_partial':
                    self.assertIn(b'"finish_reason":"length"', response.data)

    def test_native_upstream_output_limit_keeps_zero_absent_and_positive_exact(self):
        for index, output_tokens in enumerate((0, 65536)):
            with self.subTest(output_tokens=output_tokens):
                response, diagnostic, upstream = self.request(
                    'capture', 5, repository_id=200 + index, output_tokens=output_tokens)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(diagnostic['status'], 'succeeded')
                self.assertEqual(len(upstream.bodies), 1)
                if output_tokens:
                    self.assertEqual(upstream.bodies[0]['max_tokens'], output_tokens)
                else:
                    self.assertNotIn('max_tokens', upstream.bodies[0])

    def test_thinking_parameters_reach_actual_upstream(self):
        for enabled in (False, True):
            response, diagnostic, upstream = self.request('capture', 5, repository_id=700+int(enabled),
                thinking={'thinking_protocol':'qwen_vllm','thinking_enabled':enabled,'thinking_token_budget':2048})
            self.assertEqual(response.status_code,200)
            self.assertEqual(upstream.bodies[0]['chat_template_kwargs'],{'enable_thinking':enabled})
            self.assertEqual(upstream.bodies[0].get('thinking_token_budget'),2048 if enabled else None)
            self.assertEqual(diagnostic['thinking_enabled'],enabled)
