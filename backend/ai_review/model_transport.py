"""一次性模型传输：总时限、无数据时限及可安全记录的传输指标。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import selectors
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import ReadTimeoutError

ERRORS = {'SSLError', 'ConnectTimeout', 'ReadTimeout', 'ConnectionError', 'Timeout',
          'ResponseTooLarge', 'RequestException', 'TransportFailure'}
# 隔离启动仍允许从同目录导入纯协议模块。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from completion import ResponseCollector, CompletionError
MAX_RESPONSE = 8 * 1024 * 1024
AUTHORIZATION_INTERVAL = .4
ERROR_BODY_LIMIT = 128 * 1024
SAFE_RESPONSE_HEADERS = frozenset({
    'content-type', 'date', 'x-request-id', 'request-id', 'x-trace-id', 'traceparent', 'cf-ray',
})


def safe_response_headers(headers, secret=''):
    """只保留定位用响应头；认证、Cookie 与任意供应商正文永不跨进程。"""
    result, total = {}, 0
    for name, value in headers.items():
        key = str(name).lower()
        if key not in SAFE_RESPONSE_HEADERS or not isinstance(value, str):
            continue
        value = ''.join(ch for ch in value if 32 <= ord(ch) < 127)[:512]
        if secret and secret in value:
            continue
        if not value or total + len(key) + len(value) > 2048:
            continue
        result[key] = value
        total += len(key) + len(value)
    return result


class ProcessModelStream:
    """子进程保持真实 SSE，父进程可在总期限、撤权或断连时可靠终止进程组。"""
    def __init__(self, target, outbound, key, connect_timeout, timeout, ca_bundle=None, heartbeat=None):
        self.started = time.monotonic()
        self.deadline = self.started + timeout
        self.last_authorization_check = self.started - .5
        self.heartbeat = heartbeat
        self.buffer = bytearray()
        payload = json.dumps({'target': target, 'outbound': outbound, 'key': key,
            'connect_timeout': connect_timeout, 'timeout': timeout, 'idle_timeout': timeout,
            'ca_bundle': ca_bundle}).encode()
        try:
            self.process = subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()), '--stream'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError:
            raise ModelTransportError('TransportFailure') from None

        def write():
            try:
                self.process.stdin.write(payload)
                self.process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        self.writer = threading.Thread(target=write, name='模型流输入', daemon=True)
        self.writer.start()
        try:
            tag, value = self._frame(heartbeat)
            if tag == b'E':
                error = json.loads(value)
                self.metrics = error.get('metrics', {})
                raise ModelTransportError(error.get('error', 'TransportFailure'), self.metrics)
            if tag != b'H':
                raise ModelTransportError('TransportFailure')
            header = json.loads(value)
            self.status = header['status']
            self.content_type = header['content_type']
            self.metrics = header['metrics']
            self.safe_headers = header.get('safe_headers', {})
        except BaseException:
            self.close()
            raise

    def _check(self, heartbeat=None):
        now = time.monotonic()
        if now >= self.deadline:
            raise ModelTransportError('Timeout')
        callback = heartbeat or getattr(self, 'heartbeat', None)
        if callback and now - self.last_authorization_check >= AUTHORIZATION_INTERVAL:
            callback()
            self.last_authorization_check = time.monotonic()

    def _fill(self, needed, heartbeat=None):
        self._check(heartbeat)
        while len(self.buffer) < needed:
            self._check(heartbeat)
            remaining = self.deadline - time.monotonic()
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                ready = selector.select(min(remaining, AUTHORIZATION_INTERVAL))
            if not ready:
                continue
            chunk = os.read(self.process.stdout.fileno(), max(8192, needed - len(self.buffer)))
            if not chunk:
                raise ModelTransportError('TransportFailure')
            self.buffer.extend(chunk)

    def _frame(self, heartbeat=None):
        self._fill(5, heartbeat)
        tag, size = bytes(self.buffer[:1]), struct.unpack('!I', self.buffer[1:5])[0]
        self._fill(5 + size, heartbeat)
        value = bytes(self.buffer[5:5 + size])
        del self.buffer[:5 + size]
        # 大帧可能已完整缓存在父进程；复制和切片完成后仍重新核对期限与撤权。
        self._check(heartbeat)
        return tag, value

    def chunks(self, heartbeat=None):
        while True:
            tag, value = self._frame(heartbeat)
            if tag == b'D':
                elapsed = round(time.monotonic() - self.started, 3)
                self.metrics['received_bytes'] = self.metrics.get('received_bytes', 0) + len(value)
                if self.metrics.get('first_data_elapsed_seconds') is None:
                    self.metrics['first_data_elapsed_seconds'] = elapsed
                self.metrics.update(last_data_elapsed_seconds=elapsed, transfer_phase='receiving')
                yield value
                continue
            if tag == b'X':
                self.metrics.update(json.loads(value))
                return
            if tag == b'E':
                error = json.loads(value)
                self.metrics.update(error.get('metrics', {}))
                raise ModelTransportError(error.get('error', 'TransportFailure'), self.metrics)
            raise ModelTransportError('TransportFailure')

    def close(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            if stream and not stream.closed:
                stream.close()
        writer = getattr(self, 'writer', None)
        if writer and writer.is_alive():
            writer.join(timeout=1)


def open_model_stream(target, outbound, key, connect_timeout=15, timeout=600, ca_bundle=None,
                      heartbeat=None):
    return ProcessModelStream(target, outbound, key, connect_timeout, timeout, ca_bundle, heartbeat)


class ModelTransportError(requests.RequestException):
    def __init__(self, kind, details=None):
        self.kind = kind if kind in ERRORS else 'TransportFailure'
        self.details = dict(details or {})
        super().__init__(self.kind)


def fetch_model(target, outbound, key, connect_timeout=15, timeout=600, ca_bundle=None,
                diagnostics=None, idle_timeout=None, response_limit=MAX_RESPONSE, raw_mode=False):
    # 源码和密钥只经匿名管道传递，不进入进程参数、环境变量或日志。
    payload = json.dumps({'target': target, 'outbound': outbound, 'key': key,
                          'connect_timeout': connect_timeout, 'timeout': timeout,
                          'idle_timeout': idle_timeout or timeout, 'response_limit': response_limit,
                          'ca_bundle': ca_bundle, 'raw_mode': bool(raw_mode)}).encode()
    try:
        process = subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve())],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
    except OSError:
        raise ModelTransportError('TransportFailure') from None
    try:
        try:
            # 子进程在总期限终止传输；额外一秒仅供其返回安全诊断和回收。
            output, _ = process.communicate(payload, timeout=timeout + 1)
        except subprocess.TimeoutExpired:
            raise ModelTransportError('Timeout') from None
        if process.returncode or len(output) > response_limit * 12:
            raise ModelTransportError('TransportFailure')
        try:
            value = json.loads(output)
            if not isinstance(value, dict):
                raise ValueError()
            if diagnostics is not None:
                diagnostics.update(value.get('metrics', {}))
            if value.get('error'):
                raise ModelTransportError(value['error'])
            return value['status'], base64.b64decode(value['body'], validate=True)
        except (ValueError, KeyError, TypeError):
            raise ModelTransportError('TransportFailure') from None
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        process.stdin.close()
        process.stdout.close()


def exception_kind(exc):
    # HTTP 库可能包装无数据超时；只查看异常类型，不读取可能含凭据的异常正文。
    pending, seen = [exc], set()
    while pending and len(seen) < 20:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (requests.ReadTimeout, ReadTimeoutError)):
            return 'ReadTimeout'
        pending.extend(value for value in (*getattr(current, 'args', ()),
                       current.__cause__, current.__context__) if isinstance(value, BaseException))
    return getattr(exc, 'kind', type(exc).__name__)


def safe_exception_metrics(exc, kind, metrics):
    """只提取异常类型和数字 TLS 字段，绝不保存异常正文、请求或凭据。"""
    result = dict(metrics)
    result.setdefault('first_data_elapsed_seconds', None)
    result.setdefault('last_data_elapsed_seconds', None)
    if result.get('tls_phase') is None:
        result['tls_phase'] = result.get('transfer_phase')
    result.setdefault('tls_exception_type', None)
    result.setdefault('tls_errno', None)
    result.setdefault('tls_verify_code', None)
    if kind != 'SSLError':
        return result
    pending, seen = [exc], set()
    while pending and len(seen) < 20:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result['tls_exception_type'] = type(current).__name__
        errno = getattr(current, 'errno', None)
        verify_code = getattr(current, 'verify_code', None)
        if isinstance(errno, int):
            result['tls_errno'] = errno
        if isinstance(verify_code, int):
            result['tls_verify_code'] = verify_code
        pending.extend(value for value in (*getattr(current, 'args', ()),
                       current.__cause__, current.__context__) if isinstance(value, BaseException))
    return result


def stream_frame(tag, value=b''):
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    sys.stdout.buffer.write(tag + struct.pack('!I', len(value)) + value)
    sys.stdout.buffer.flush()


def main_stream():
    deadline_expired = False
    started = time.monotonic()
    data, error_body, error_sent = {}, bytearray(), False
    metrics = {'transfer_phase': 'connecting', 'received_bytes': 0, 'normalized_response': False,
               'first_data_elapsed_seconds': None, 'last_data_elapsed_seconds': None,
               'tls_phase': None, 'tls_exception_type': None, 'tls_errno': None,
               'tls_verify_code': None, 'target_host': None}

    def expired(*_):
        nonlocal deadline_expired
        deadline_expired = True
        raise ModelTransportError('Timeout')

    try:
        data = json.loads(sys.stdin.buffer.read())
        metrics['target_host'] = urlsplit(data['target']).hostname
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, data['timeout'])
        with requests.Session() as session:
            session.trust_env = False
            with session.post(data['target'], json=data['outbound'],
                headers={'Authorization': 'Bearer ' + data['key']},
                timeout=(data['connect_timeout'], data.get('idle_timeout') or data['timeout']),
                verify=data['ca_bundle'] or True, allow_redirects=False, stream=True) as response:
                content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                metrics.update(transfer_phase='headers_received', upstream_http_status=response.status_code,
                    headers_elapsed_seconds=round(time.monotonic() - started, 3), content_type=content_type)
                safe_headers = safe_response_headers(response.headers, data.get('key', ''))
                stream_frame(b'H', {'status': response.status_code, 'content_type': content_type,
                                     'metrics': metrics, 'safe_headers': safe_headers})
                while True:
                    part = response.raw.read1(8192, decode_content=True)
                    if not part:
                        break
                    elapsed = round(time.monotonic() - started, 3)
                    if metrics['first_data_elapsed_seconds'] is None:
                        metrics['first_data_elapsed_seconds'] = elapsed
                    metrics.update(transfer_phase='receiving', last_data_elapsed_seconds=elapsed,
                                   received_bytes=metrics['received_bytes'] + len(part))
                    if response.status_code == 200:
                        stream_frame(b'D', part)
                    else:
                        error_body.extend(part[:ERROR_BODY_LIMIT + 1 - len(error_body)])
                        if len(error_body) > ERROR_BODY_LIMIT:
                            break
                if response.status_code != 200:
                    truncated = len(error_body) > ERROR_BODY_LIMIT
                    captured = bytes(error_body[:ERROR_BODY_LIMIT])
                    secret = data['key'].encode()
                    if secret:
                        scrubbed = captured.replace(secret, b'[credential-removed]')
                        truncated = truncated or len(scrubbed) > ERROR_BODY_LIMIT
                        captured = scrubbed[:ERROR_BODY_LIMIT]
                    metrics.update(error_bytes=len(captured),
                                   error_sha256=hashlib.sha256(captured).hexdigest(),
                                   error_truncated=truncated)
                    declared = response.headers.get('Content-Length', '')
                    if declared.isdigit() and int(declared) >= 0:
                        metrics['declared_error_bytes'] = int(declared)
                        metrics['error_truncated'] = truncated or int(declared) > len(error_body)
                    stream_frame(b'D', captured)
                    error_sent = True
                metrics['transfer_phase'] = 'completed'
                stream_frame(b'X', metrics)
    except Exception as exc:
        kind = 'Timeout' if deadline_expired else exception_kind(exc)
        metrics = safe_exception_metrics(exc, kind, metrics)
        if metrics.get('upstream_http_status') not in (None, 200) and error_body and not error_sent:
            captured = bytes(error_body[:ERROR_BODY_LIMIT])
            secret = data.get('key', '').encode()
            if secret:
                captured = captured.replace(secret, b'[credential-removed]')[:ERROR_BODY_LIMIT]
            metrics.update(error_bytes=len(captured), error_sha256=hashlib.sha256(captured).hexdigest(),
                           error_truncated=True)
            stream_frame(b'D', captured)
        stream_frame(b'E', {'error': kind if kind in ERRORS else 'TransportFailure', 'metrics': metrics})
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def main():
    deadline_expired = False
    started = time.monotonic()
    metrics = {'transfer_phase': 'connecting', 'received_bytes': 0,
               'first_data_elapsed_seconds': None, 'last_data_elapsed_seconds': None,
               'tls_phase': None, 'tls_exception_type': None, 'tls_errno': None,
               'tls_verify_code': None, 'target_host': None}

    def expired(*_):
        nonlocal deadline_expired
        deadline_expired = True
        raise ModelTransportError('Timeout')

    try:
        data = json.loads(sys.stdin.buffer.read())
        metrics['target_host'] = urlsplit(data['target']).hostname
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, data['timeout'])
        with requests.Session() as session:
            session.trust_env = False
            with session.post(data['target'], json=data['outbound'],
                              headers={'Authorization': 'Bearer ' + data['key']},
                              timeout=(data['connect_timeout'], data.get('idle_timeout') or data['timeout']),
                              verify=data['ca_bundle'] or True, allow_redirects=False, stream=True) as response:
                metrics.update(transfer_phase='headers_received', upstream_http_status=response.status_code,
                               headers_elapsed_seconds=round(time.monotonic()-started, 3))
                content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                metrics['content_type'] = content_type
                streaming = content_type == 'text/event-stream' if content_type in ('application/json', 'text/event-stream') else bool(data['outbound'].get('stream'))
                collector = None if data.get('raw_mode') else ResponseCollector(streaming, data.get('response_limit', MAX_RESPONSE))
                received = 0
                content = b''
                if response.status_code == 200:
                    # read1 不等待填满整个缓冲区，首段数据时间不受 SSE 小分片大小影响。
                    while True:
                        part = response.raw.read1(8192, decode_content=True)
                        if not part:
                            break
                        elapsed = round(time.monotonic()-started, 3)
                        if metrics['first_data_elapsed_seconds'] is None:
                            metrics['first_data_elapsed_seconds'] = elapsed
                        metrics.update(transfer_phase='receiving', last_data_elapsed_seconds=elapsed)
                        received += len(part)
                        metrics['received_bytes'] = received
                        if data.get('raw_mode'):
                            content += part
                            if len(content) > data.get('response_limit', MAX_RESPONSE) * 8:
                                raise CompletionError('response_envelope_limit')
                        else:
                            collector.feed(part)
                    if data.get('raw_mode'):
                        metrics.update(normalized_response=False, transfer_phase='completed')
                    else:
                        envelope, protocol = collector.finish()
                        metrics.update(protocol, normalized_response=True, transfer_phase='completed')
                        content = json.dumps(envelope, ensure_ascii=False).encode()
                result = {'status': response.status_code, 'body': base64.b64encode(content).decode()}
    except Exception as exc:
        if isinstance(exc, CompletionError):
            metrics.update(exc.metadata)
        kind = 'Timeout' if deadline_expired else 'ResponseTooLarge' if isinstance(exc, CompletionError) and 'limit' in exc.code else exception_kind(exc)
        metrics = safe_exception_metrics(exc, kind, metrics)
        result = {'error': kind if kind in ERRORS else 'TransportFailure'}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    # 仅父进程消费此回执；指标不包含远端错误正文、源码或密钥。
    sys.stdout.write(json.dumps(dict(result, metrics=metrics)))


if __name__ == '__main__':
    main_stream() if '--stream' in sys.argv else main()
