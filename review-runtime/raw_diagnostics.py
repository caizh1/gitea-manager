"""受限保存 OpenCode 原始协议证据；从采集源头排除认证信息。"""
from datetime import datetime, timedelta, timezone
from collections import deque
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading
import time
import zipfile


TASK_LIMIT = 256 * 1024 * 1024
FAILURE_RESERVE = 4 * 1024 * 1024
FAILURE_TAIL = 1024 * 1024
GLOBAL_LIMIT = 2 * 1024 * 1024 * 1024
RETENTION_DAYS = 7
FREE_FLOOR = 64 * 1024 * 1024
_GLOBAL_LOCK = threading.Lock()
_ARCHIVE_LOCK = threading.Lock()
_GLOBAL_USAGE = {}
MAX_SSE_BUFFER = 16 * 1024 * 1024
PROCESS_LOG_PREFIX = 4 * 1024 * 1024
PROCESS_LOG_TAIL = 1024 * 1024
CALL_ID = re.compile(r'^[A-Za-z0-9_-]{8,160}$')


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(',', ':'))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def cleanup_expired(runtime_root):
    """仅删除带有本模块清单且已到期的固定会话目录及同名归档。"""
    base = Path(runtime_root) / 'diagnostics'
    now = datetime.now(timezone.utc).timestamp()
    if not base.exists():
        return
    candidates = {path.parent for pattern in ('manifest.json', 'capture.json')
                  for path in base.glob('*/' + pattern)}
    for root in candidates:
        if root.is_symlink():
            continue
        try:
            metadata = root / 'manifest.json'
            if not metadata.exists():
                metadata = root / 'capture.json'
            value = json.loads(metadata.read_text(encoding='utf-8'))
            session_id = value.get('session_id')
            if (not isinstance(session_id, str) or root.name != session_id
                    or float(value['retention_expires_at']) > now):
                continue
            shutil.rmtree(root)
            archive = base / 'archives' / (session_id + '.zip')
            if not archive.is_symlink():
                archive.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError):
            continue
    _GLOBAL_USAGE.pop(str(Path(runtime_root).resolve()), None)


class SSEBytes:
    """只把 CR、LF、CRLF 当作 SSE 行结束符，绝不按 Unicode 分行。"""
    def __init__(self):
        self.buffer = bytearray()
        self.data = []
        self.offset = 0
        self.scan = 0
        self.event_size = 0

    def _line(self, line):
        if not line:
            if not self.data:
                return None
            payload = b'\n'.join(self.data)
            self.data.clear()
            self.event_size = 0
            return payload
        if line.startswith(b':'):
            return None
        field, separator, value = line.partition(b':')
        if field == b'data':
            if separator and value.startswith(b' '):
                value = value[1:]
            self.data.append(value)
            self.event_size += len(value) + (1 if len(self.data) > 1 else 0)
            if self.event_size > MAX_SSE_BUFFER:
                raise ValueError('sse_event_too_large')
        return None

    def feed(self, chunk):
        self.buffer.extend(chunk)
        events, frames = [], []
        start = 0
        index = self.scan
        while index < len(self.buffer):
            value = self.buffer[index]
            if value not in (10, 13):
                index += 1
                continue
            if value == 13 and index + 1 == len(self.buffer):
                break
            end = index + 1
            ending = 'LF' if value == 10 else 'CR'
            if value == 13 and self.buffer[index + 1:index + 2] == b'\n':
                end += 1
                ending = 'CRLF'
            line = bytes(self.buffer[start:index])
            frames.append({'offset': self.offset + start, 'line_bytes': len(line),
                           'ending': ending, 'time_utc': utc_now()})
            payload = self._line(line)
            if payload is not None:
                events.append(payload)
            start = end
            index = end
        if start:
            del self.buffer[:start]
            self.offset += start
            index -= start
        self.scan = index
        if len(self.buffer) > MAX_SSE_BUFFER:
            raise ValueError('sse_event_too_large')
        return events, frames

    def finish(self):
        """SSE 事件必须由空行提交；EOF 剩余内容一律报告缺口。"""
        incomplete = bool(self.buffer or self.data)
        detail = {'offset': self.offset, 'pending_line_bytes': len(self.buffer),
                  'pending_data_lines': len(self.data), 'time_utc': utc_now()}
        self.buffer.clear()
        self.data.clear()
        return incomplete, detail


class RawDiagnostics:
    """原始正文留在执行器持久卷；写入失败只降低诊断完整性。"""
    def __init__(self, runtime_root, job_id, session_id):
        if not job_id.replace('_', '').replace('-', '').isalnum():
            raise ValueError('invalid_job_id')
        if not session_id.replace('_', '').replace('-', '').isalnum():
            raise ValueError('invalid_session_id')
        self.runtime_root = Path(runtime_root)
        with _GLOBAL_LOCK:
            cleanup_expired(self.runtime_root)
        self.root = self.runtime_root / 'diagnostics' / session_id
        self.root.mkdir(parents=True, exist_ok=False, mode=0o700)
        os.chmod(self.root, 0o700)
        self.job_id, self.session_id = job_id, session_id
        self.native_session_id = None
        self.created_at = utc_now()
        self.expires_at = (datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)).timestamp()
        self.bytes = 0
        self.original_bytes = 0
        self.truncated = False
        self.error = None
        self.finalized = False
        self.manifest_ready = False
        self.archive_sha256 = None
        self.lock = threading.Lock()
        self.files = set()
        self.calls = set()
        self.event_tail = deque()
        self.event_tail_bytes = 0
        self.failure_active = False
        self.failure_post_remaining = FAILURE_TAIL
        self.failure_reserve_remaining = FAILURE_RESERVE
        self.response_index = 0
        self.process_log_observed = 0
        self.process_log_saved = 0
        self.process_log_truncated = False
        self.process_log_tail = bytearray()
        self.model_responses = {}
        self.capture_lock = threading.Lock()
        self.last_space_check = float('-inf')
        self.cached_free = 0
        _atomic_json(self.root / 'capture.json', {'version': 1, 'status': 'capturing',
            'job_id': self.job_id, 'session_id': self.session_id,
            'created_at': self.created_at, 'retention_expires_at': self.expires_at,
            'retention_expires_utc': datetime.fromtimestamp(
                self.expires_at, timezone.utc).isoformat()})
        self._initialize_usage()
        self._record('causality.jsonl', {'kind': 'capture_started', 'time_utc': self.created_at})

    @property
    def manifest_path(self):
        return self.root / 'manifest.json'

    def _initialize_usage(self):
        key = str(self.runtime_root.resolve())
        with _GLOBAL_LOCK:
            if key not in _GLOBAL_USAGE:
                base = self.runtime_root / 'diagnostics'
                total = 0
                for path in base.rglob('*') if base.exists() else ():
                    try:
                        if path.is_file() and not path.is_symlink():
                            total += path.stat().st_size
                    except OSError:
                        continue
                _GLOBAL_USAGE[key] = total

    def _global_bytes(self):
        return _GLOBAL_USAGE.get(str(self.runtime_root.resolve()), 0)

    def _append(self, name, data, observed=True):
        if isinstance(data, str):
            data = data.encode('utf-8')
        if observed:
            self.original_bytes += len(data)
        if not data or self.error:
            return False
        try:
            with _GLOBAL_LOCK:
                key = str(self.runtime_root.resolve())
                now = time.monotonic()
                if now - self.last_space_check >= 1:
                    self.cached_free = shutil.disk_usage(self.runtime_root).free
                    self.last_space_check = now
                if (self.bytes + len(data) > TASK_LIMIT - FAILURE_RESERVE
                        or self._global_bytes() + len(data) > GLOBAL_LIMIT
                        or self.cached_free - len(data) < FREE_FLOOR):
                    self.truncated = True
                    self.error = 'capture_limit_reached'
                    logging.error('原始诊断采集已降级：任务=%s，会话=%s，原因=%s',
                                  self.job_id, self.session_id, self.error)
                    return False
                path = self.root / name
                with path.open('ab') as handle:
                    handle.write(data)
                    handle.flush()
                os.chmod(path, 0o600)
                self.files.add(name)
                self.bytes += len(data)
                _GLOBAL_USAGE[key] = self._global_bytes() + len(data)
                self.cached_free -= len(data)
            return True
        except OSError as exc:
            self.truncated = True
            self.error = 'capture_storage_error_' + type(exc).__name__
            logging.error('原始诊断采集已降级：任务=%s，会话=%s，原因=%s',
                          self.job_id, self.session_id, self.error)
            return False

    def _append_reserve(self, name, data):
        """限额后仍保留首个失败附近的小窗口，不恢复普通采集。"""
        data = bytes(data[:self.failure_reserve_remaining])
        if not data:
            return False
        try:
            with _GLOBAL_LOCK:
                key = str(self.runtime_root.resolve())
                if (self.bytes + len(data) > TASK_LIMIT
                        or self._global_bytes() + len(data) > GLOBAL_LIMIT
                        or shutil.disk_usage(self.runtime_root).free - len(data) < FREE_FLOOR):
                    return False
                path = self.root / name
                with path.open('ab') as handle:
                    handle.write(data)
                os.chmod(path, 0o600)
                self.files.add(name)
                self.bytes += len(data)
                self.failure_reserve_remaining -= len(data)
                _GLOBAL_USAGE[key] = self._global_bytes() + len(data)
            return True
        except OSError:
            return False

    def _reserve_edges(self, name, raw):
        remaining = self.failure_reserve_remaining
        if not remaining:
            return
        if len(raw) <= remaining:
            self._append_reserve(name + '-partial.raw', raw)
            return
        first = remaining // 2
        self._append_reserve(name + '-prefix.raw', raw[:first])
        self._append_reserve(name + '-suffix.raw', raw[-(remaining - first):])

    def _record(self, name, value):
        return self._append(name, json.dumps(value, ensure_ascii=False,
                                             separators=(',', ':')) + '\n', observed=False)

    def set_native_session(self, value):
        self.native_session_id = value
        self._record('causality.jsonl', {'kind': 'native_session_bound',
            'time_utc': utc_now(), 'native_session_id': value})

    def add_call(self, value):
        if isinstance(value, str) and value:
            self.calls.add(value)

    def event_chunk(self, chunk, connection, index, offset):
        self.event_tail.append(bytes(chunk))
        self.event_tail_bytes += len(chunk)
        while self.event_tail and self.event_tail_bytes > FAILURE_TAIL:
            removed = self.event_tail.popleft()
            self.event_tail_bytes -= len(removed)
        if self.failure_active and self.failure_post_remaining > 0:
            post = chunk[:self.failure_post_remaining]
            self._append_reserve('failure-window.raw', post)
            self.failure_post_remaining -= len(post)
        digest = hashlib.sha256(chunk).hexdigest()
        saved = self._append('events.raw', chunk)
        self._record('event-chunks.jsonl', {'connection': connection, 'index': index,
            'offset': offset, 'bytes': len(chunk), 'sha256': digest, 'saved': saved,
            'time_utc': utc_now()})

    def frames(self, values):
        for value in values:
            self._record('event-frames.jsonl', value)

    def event_payload(self, payload, outcome, diagnostic=None):
        if outcome == 'error' and not self.failure_active:
            self.failure_active = True
            self._append_reserve('failure-window.raw', b''.join(self.event_tail))
            self._append_reserve('failure-payload.raw', payload)
        path = self.root / 'event-payloads.raw'
        try:
            offset = path.stat().st_size if path.exists() else 0
        except OSError:
            offset = 0
        saved = self._append('event-payloads.raw', payload, observed=False)
        self._record('event-payloads.jsonl', {'offset': offset, 'bytes': len(payload),
            'sha256': hashlib.sha256(payload).hexdigest(), 'saved': saved, 'outcome': outcome,
            'diagnostic': diagnostic or {}, 'time_utc': utc_now()})

    def response(self, method, path, status, content_type, raw):
        self.response_index += 1
        store = self.root / 'native-responses.raw'
        try:
            offset = store.stat().st_size if store.exists() else 0
        except OSError:
            offset = 0
        saved = self._append('native-responses.raw', raw)
        if not saved and self.failure_reserve_remaining:
            self._reserve_edges(f'native-response-{self.response_index}', raw)
        self._record('native-responses.jsonl', {'method': method, 'path': path,
            'status': status, 'content_type': content_type, 'offset': offset, 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(), 'saved': saved, 'time_utc': utc_now()})

    def result_text(self, text):
        raw = text.encode('utf-8')
        saved = self._append('model-result.raw', raw)
        if not saved and self.failure_reserve_remaining:
            self._reserve_edges('model-result', raw)
        self._record('causality.jsonl', {'kind': 'model_result', 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(), 'saved': saved, 'time_utc': utc_now()})

    def model_upstream_error(self, raw, metadata):
        """保存 Broker 传来的上游错误原文；调用方已排除认证头与凭据。"""
        raw = bytes(raw)
        store = self.root / 'model-upstream-errors.raw'
        try:
            offset = store.stat().st_size if store.exists() else 0
        except OSError:
            offset = 0
        saved = self._append('model-upstream-errors.raw', raw)
        if not saved and self.failure_reserve_remaining:
            self._reserve_edges('model-upstream-error', raw)
        safe = {key: metadata.get(key) for key in (
            'call_id', 'source', 'phase', 'reason', 'error_code',
            'upstream_http_status', 'upstream_error_type', 'upstream_error_code',
            'secondary_exception_type', 'error_bytes', 'error_sha256', 'captured_sha256',
            'captured_bytes', 'received_bytes', 'declared_error_bytes',
            'headers_elapsed_seconds', 'first_data_elapsed_seconds',
            'last_data_elapsed_seconds', 'error_truncated', 'headers')
            if metadata.get(key) is not None}
        safe.update(offset=offset, captured_bytes=len(raw),
                    captured_sha256=hashlib.sha256(raw).hexdigest(), saved=saved,
                    time_utc=utc_now())
        self._record('model-upstream-errors.jsonl', safe)
        if isinstance(metadata.get('call_id'), str) and metadata['call_id']:
            self.calls.add(metadata['call_id'])
        return saved

    def model_response_start(self, metadata):
        call_id = metadata.get('call_id')
        if not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id):
            return
        safe = {key: metadata.get(key) for key in (
            'call_id', 'model', 'stream', 'tool_choice', 'tool_names', 'message_count',
            'request_bytes', 'broker_http_status', 'content_type') if metadata.get(key) is not None}
        with self.capture_lock:
            self.calls.add(call_id)
            name = 'model-response-' + call_id + '.raw'
            self.model_responses[call_id] = {'name': name,
                'observed': 0, 'saved': 0, 'sha256': hashlib.sha256(),
                'tail': bytearray(), 'truncated': False, 'metadata': safe}
            self._record('model-responses.jsonl', dict(safe, kind='start',
                                                       file=name,
                                                       time_utc=utc_now()))

    def model_response_chunk(self, call_id, raw):
        raw = bytes(raw)
        with self.capture_lock:
            state = self.model_responses.get(call_id)
            if state is None:
                return
            state['observed'] += len(raw)
            state['sha256'].update(raw)
            state['tail'].extend(raw)
            if len(state['tail']) > FAILURE_TAIL:
                del state['tail'][:-FAILURE_TAIL]
            if self._append(state['name'], raw):
                state['saved'] += len(raw)
            else:
                state['truncated'] = True

    def model_response_end(self, call_id, outcome, error_type=None):
        with self.capture_lock:
            state = self.model_responses.pop(call_id, None)
            if state is None:
                return
            tail_saved = 0
            if state['truncated'] and state['tail']:
                before = self.failure_reserve_remaining
                tail_name = 'model-response-' + call_id + '-tail.raw'
                self._append_reserve(tail_name, state['tail'])
                tail_saved = before - self.failure_reserve_remaining
            value = dict(state['metadata'], kind='end', file=state['name'], outcome=outcome,
                         observed_bytes=state['observed'], saved_bytes=state['saved'],
                         sha256=state['sha256'].hexdigest(), truncated=state['truncated'],
                         tail_saved_bytes=tail_saved, time_utc=utc_now())
            if error_type:
                value['error_type'] = error_type
            encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n'
            if not self._append('model-responses.jsonl', encoded, observed=False):
                self._append_reserve('model-responses-metadata.jsonl', encoded.encode())

    def process_log(self, raw):
        """有界保存已由调用方精确剔除本地认证值的 OpenCode ERROR 日志。"""
        raw = bytes(raw)
        self.process_log_observed += len(raw)
        remaining = max(0, PROCESS_LOG_PREFIX - self.process_log_saved)
        prefix = raw[:remaining]
        if prefix:
            if self._append('opencode-stderr.raw', prefix):
                self.process_log_saved += len(prefix)
            else:
                self.process_log_truncated = True
        overflow = raw[len(prefix):]
        if overflow:
            self.process_log_truncated = True
            self.process_log_tail.extend(overflow)
            if len(self.process_log_tail) > PROCESS_LOG_TAIL:
                del self.process_log_tail[:-PROCESS_LOG_TAIL]

    def finish_process_log(self):
        tail_saved = False
        if self.process_log_tail:
            tail_saved = self._append('opencode-stderr-tail.raw', self.process_log_tail,
                                      observed=False)
        self._record('causality.jsonl', {'kind': 'opencode_stderr_complete',
            'observed_bytes': self.process_log_observed,
            'prefix_saved_bytes': self.process_log_saved,
            'tail_saved_bytes': len(self.process_log_tail) if tail_saved else 0,
            'truncated': self.process_log_truncated or not tail_saved and bool(self.process_log_tail),
            'time_utc': utc_now()})
        self.process_log_tail.clear()

    def cause(self, kind, values=None):
        self._record('causality.jsonl', dict(values or {}, kind=kind, time_utc=utc_now()))

    def status(self):
        if datetime.now(timezone.utc).timestamp() >= self.expires_at:
            return {'status': 'expired', 'available': False, 'truncated': self.truncated,
                    'bytes': self.bytes, 'original_bytes': self.original_bytes,
                    'retention_expires_at': self.expires_at, 'error': 'diagnostics_expired',
                    'sha256': self.archive_sha256}
        archive = self.runtime_root / 'diagnostics' / 'archives' / (self.session_id + '.zip')
        if archive.exists():
            state = 'partial' if self.truncated else 'available'
        elif self.finalized:
            state = 'partial' if self.truncated else 'ready'
        else:
            state = 'capturing'
        if self.finalized and not self.manifest_ready:
            state = 'unavailable'
        return {'status': state, 'available': self.finalized and self.manifest_ready,
                'truncated': self.truncated,
                'bytes': self.bytes, 'original_bytes': self.original_bytes,
                'retention_expires_at': self.expires_at, 'error': self.error,
                'sha256': self.archive_sha256}

    def finalize(self):
        with self.lock:
            if self.finalized:
                return self.status()
            self.finalized = True
            try:
                manifest = {'version': 1, 'description': 'OpenCode 原始协议诊断证据',
                    'job_id': self.job_id, 'session_id': self.session_id,
                    'native_session_id': self.native_session_id, 'call_ids': sorted(self.calls),
                    'created_at': self.created_at, 'finalized_at': utc_now(),
                    'retention_expires_at': self.expires_at,
                    'retention_expires_utc': datetime.fromtimestamp(
                        self.expires_at, timezone.utc).isoformat(), 'captured_bytes': self.bytes,
                    'original_bytes': self.original_bytes, 'truncated': self.truncated,
                    'error': self.error, 'files': {}}
                for name in sorted(self.files):
                    path = self.root / name
                    if path.exists() and not path.is_symlink():
                        manifest['files'][name] = {'bytes': path.stat().st_size,
                            'sha256': file_sha256(path)}
                _atomic_json(self.manifest_path, manifest)
                self.manifest_ready = True
            except OSError as exc:
                self.error = 'manifest_storage_' + type(exc).__name__
                self.truncated = True
            return self.status()

    def archive(self):
        if not self.finalized or not self.manifest_ready:
            raise RuntimeError('diagnostics_not_ready')
        if datetime.now(timezone.utc).timestamp() >= self.expires_at:
            raise RuntimeError('diagnostics_expired')
        archives = self.runtime_root / 'diagnostics' / 'archives'
        archives.mkdir(parents=True, exist_ok=True, mode=0o700)
        if archives.is_symlink() or archives.resolve().parent != (self.runtime_root / 'diagnostics').resolve():
            raise OSError('diagnostics_archive_path')
        os.chmod(archives, 0o700)
        target = archives / (self.session_id + '.zip')
        if target.is_symlink():
            raise OSError('diagnostics_archive_symlink')
        with _ARCHIVE_LOCK:
            if not target.exists():
                sources = [path for path in sorted(self.root.iterdir())
                           if path.is_file() and not path.is_symlink()]
                estimated = sum(path.stat().st_size for path in sources) + 1024 * 1024
                key = str(self.runtime_root.resolve())
                with _GLOBAL_LOCK:
                    if (self._global_bytes() + estimated > GLOBAL_LIMIT
                            or shutil.disk_usage(self.runtime_root).free - estimated < FREE_FLOOR):
                        raise OSError('diagnostics_archive_space')
                    _GLOBAL_USAGE[key] = self._global_bytes() + estimated
                temporary = target.with_suffix('.zip.partial')
                try:
                    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED) as output:
                        for path in sources:
                            output.write(path, path.name)
                    os.chmod(temporary, 0o600)
                    size = temporary.stat().st_size
                    os.replace(temporary, target)
                    with _GLOBAL_LOCK:
                        _GLOBAL_USAGE[key] = self._global_bytes() - estimated + size
                except Exception:
                    temporary.unlink(missing_ok=True)
                    with _GLOBAL_LOCK:
                        _GLOBAL_USAGE[key] = max(0, self._global_bytes() - estimated)
                    raise
        self.archive_sha256 = file_sha256(target)
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding='utf-8'))
            manifest['archive_sha256'] = self.archive_sha256
            manifest['archive_bytes'] = target.stat().st_size
            _atomic_json(self.manifest_path, manifest)
        except (OSError, ValueError):
            logging.error('原始诊断归档摘要持久化失败：任务=%s，会话=%s',
                          self.job_id, self.session_id)
        return target, self.status()

    @classmethod
    def load(cls, runtime_root, session_id):
        if not session_id.replace('_', '').replace('-', '').isalnum():
            return None
        base = (Path(runtime_root) / 'diagnostics').resolve()
        root = base / session_id
        if root.is_symlink() or root.parent.resolve() != base:
            return None
        manifest = root / 'manifest.json'
        if not manifest.exists():
            return None
        value = json.loads(manifest.read_text(encoding='utf-8'))
        row = cls.__new__(cls)
        row.runtime_root, row.root = Path(runtime_root), root
        row.job_id, row.session_id = value['job_id'], value['session_id']
        row.native_session_id = value.get('native_session_id')
        row.created_at, row.expires_at = value['created_at'], float(value['retention_expires_at'])
        row.bytes, row.original_bytes = value['captured_bytes'], value['original_bytes']
        row.truncated, row.error, row.finalized = value['truncated'], value.get('error'), True
        row.manifest_ready = True
        row.archive_sha256 = value.get('archive_sha256')
        row.lock, row.files, row.calls = threading.Lock(), set(value.get('files', {})), set(value.get('call_ids', []))
        row.last_space_check, row.cached_free = float('-inf'), 0
        row.event_tail, row.event_tail_bytes = deque(), 0
        row.failure_active, row.failure_post_remaining = False, FAILURE_TAIL
        row.failure_reserve_remaining = FAILURE_RESERVE
        row.response_index = 0
        row.process_log_observed = row.process_log_saved = 0
        row.process_log_truncated = False
        row.process_log_tail = bytearray()
        row.model_responses = {}
        row.capture_lock = threading.Lock()
        row._initialize_usage()
        return row


class DisabledRawDiagnostics:
    """诊断存储不可用时的安全降级；审查主流程继续。"""
    def __init__(self, error='capture_initialization_failed'):
        self.error = error

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None

    def status(self):
        return {'status': 'unavailable', 'available': False, 'truncated': True,
                'bytes': 0, 'original_bytes': 0, 'retention_expires_at': None,
                'error': self.error, 'sha256': None}
