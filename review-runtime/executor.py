"""隔离执行服务：无源码仓库挂载、无上游密钥、每次调用使用独立临时目录。"""
import hmac
import json
import logging
import os
from pathlib import Path
import signal
import selectors
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify, send_file
from native_session import sessions as native_sessions, NativeError, VERSION as NATIVE_VERSION
from runtime_diagnostics import json_failure, safe_exception, safe_token

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024
busy = threading.Lock()
diagnostics_download = threading.BoundedSemaphore(1)
BROKER = os.environ.get('AI_REVIEW_BROKER_URL', 'http://review-worker:5051/v1')
VERSION = '1.18.7'


class ExecutionError(Exception):
    def __init__(self, code, diagnostic=None):
        self.code = code
        self.diagnostic = diagnostic or {}


def oom_count():
    """只读取本容器的 OOM 计数，不把 SIGKILL 无条件归因为内存不足。"""
    try:
        values = dict(line.split() for line in Path('/sys/fs/cgroup/memory.events').read_text().splitlines())
        return int(values['oom_kill'])
    except (OSError, ValueError, KeyError):
        return None


def communicate_bounded(process, prompt, timeout=625, stdout_limit=8*1024*1024, consume=None, stdout_reader=None):
    """持续限制两路输出；所有退出路径都终止本次独立进程组并回收。"""
    payload = memoryview(prompt.encode('utf-8'))
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for name, stream in [('stdin', process.stdin), ('stdout', process.stdout), ('stderr', process.stderr)]:
                if stream is None:continue
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_WRITE if name == 'stdin' else selectors.EVENT_READ, name)
            def drain_file():
                if stdout_reader is None:return
                if shutil.disk_usage(tempfile.gettempdir()).free<64*1024*1024:
                    raise ExecutionError('output_storage_full',{'reserved_bytes':64*1024*1024})
                while True:
                    chunk=stdout_reader.read(8192)
                    if not chunk:break
                    consume(chunk)
            while selector.get_map():
                drain_file()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired('隔离审查进程', timeout)
                for key, _ in selector.select(min(remaining, .2)):
                    stream, name = key.fileobj, key.data
                    if name == 'stdin':
                        try:
                            size = os.write(stream.fileno(), payload[:8192]) if payload else 0
                            payload = payload[size:]
                        except BrokenPipeError:
                            payload = memoryview(b'')
                        if not payload:
                            selector.unregister(stream); stream.close()
                    else:
                        chunk = os.read(stream.fileno(), 8192)
                        if not chunk:
                            selector.unregister(stream); stream.close(); continue
                        if name == 'stdout' and consume is not None:
                            consume(chunk)
                        else:
                            buffers[name].extend(chunk)
                            if name == 'stderr':
                                del buffers[name][:-65536]
                            elif len(buffers[name]) > stdout_limit:
                                raise ExecutionError('output_too_large', {'limit_bytes':stdout_limit})
            process.wait(timeout=max(.01, deadline-time.monotonic()))
            drain_file()
        return tuple(buffers[name].decode('utf-8', errors='replace') for name in ('stdout', 'stderr'))
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:stream.close()


class CLICollector:
    """仅保留文本和错误事件；进度、心跳事件逐行消耗。"""
    def __init__(self, limit):
        self.limit=limit; self.pending=bytearray(); self.events=[]; self.size=0

    def feed(self, chunk):
        self.pending.extend(chunk)
        while True:
            index=self.pending.find(b'\n')
            if index<0:break
            line=bytes(self.pending[:index]);del self.pending[:index+1]
            self.consume(line)
        if len(self.pending)>self.limit*8:raise ExecutionError('output_too_large',{'limit_bytes':self.limit})

    def consume(self,line):
        if not line.strip():return
        try:event=json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            if isinstance(exc, json.JSONDecodeError):
                diagnostic=json_failure(exc,source='cli_protocol',phase='cli_event_decode',
                    extraction_path='cli.stdout.event',received_bytes=len(line),reason='cli_event_json')
            else:
                diagnostic=safe_exception(exc,source='cli_protocol',phase='cli_event_decode',reason='invalid_encoding')
                diagnostic.update(extraction_path='cli.stdout.event',received_bytes=len(line))
            raise ExecutionError('cli_protocol',diagnostic) from None
        if not isinstance(event,dict):
            raise ExecutionError('cli_protocol',{'diagnostic_version':1,'source':'cli_protocol',
                'phase':'cli_event_decode','reason':'cli_event_schema',
                'extraction_path':'cli.stdout.event','received_bytes':len(line),
                'result_type':type(event).__name__})
        if event.get('type') in ('text','error','tool_use') or event.get('part',{}).get('type')=='tool':
            self.size+=len(str(event.get('part',{}).get('text','')).encode())+128
            if self.size>self.limit:raise ExecutionError('output_too_large',{'limit_bytes':self.limit})
            self.events.append(json.dumps(event,ensure_ascii=False))

    def finish(self):
        if self.pending:self.consume(bytes(self.pending));self.pending.clear()
        return '\n'.join(self.events)


def decode_result(text, limit=8*1024*1024, diagnostic_context=None):
    context=dict(diagnostic_context or {})
    encoded=text.encode()
    if len(encoded) > limit:
        raise ExecutionError('output_too_large',dict(context,diagnostic_version=1,
            source='cli_result',phase='result_decode',reason='output_too_large',
            extraction_path='cli.events[type=text].part.text',received_bytes=len(encoded),
            response_chars=len(text),limit_bytes=limit))
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        reason='output_truncated' if context.get('finish_reason')=='length' else None
        diagnostic=json_failure(exc,source='cli_result',phase='result_decode',
            extraction_path='cli.events[type=text].part.text',received_bytes=len(encoded),
            response_chars=len(text),reason=reason)
        diagnostic.update(context)
        raise ExecutionError('invalid_json',diagnostic) from None
    if not isinstance(value, dict):
        raise ExecutionError('invalid_json',dict(context,diagnostic_version=1,
            source='cli_result',phase='result_decode',reason='result_not_object',
            extraction_path='cli.events[type=text].part.text',received_bytes=len(encoded),
            response_chars=len(text),result_type=type(value).__name__))
    return value


def direct(data):
    session = requests.Session(); session.trust_env = False
    try:
        response = session.post(BROKER + '/chat/completions',
            headers={'Authorization': 'Bearer ' + data['capability']},
            json={'model': data['model'], 'messages': [{'role': 'system', 'content': data['system']},
                   {'role': 'user', 'content': data['prompt']}], 'stream': False},
            timeout=(5, data['timeout_seconds']+10), allow_redirects=False)
        if response.status_code != 200:
            raise ExecutionError('gateway_unavailable')
        value = response.json()
        choice = value['choices'][0]
        return decode_result(choice['message']['content'], data.get('response_limit_bytes',8*1024*1024),
                             {'finish_reason': choice.get('finish_reason') or 'missing',
                              'text_part_count': 1})
    finally:
        session.close()


def opencode(data):
    binary = '/opt/review/opencode'
    with tempfile.TemporaryDirectory(prefix='review-', dir='/tmp') as folder:
        work = Path(folder)
        for name in ('config', 'cache', 'data', 'state', 'managed', 'project', 'tmp'):
            (work / name).mkdir()
        models = work / '模型目录.json'; models.write_text('{}')
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'CI': 'true', 'NO_COLOR': '1',
               'TMPDIR': str(work/'tmp'), 'TMP': str(work/'tmp'), 'TEMP': str(work/'tmp'),
               'XDG_CONFIG_HOME': str(work/'config'), 'XDG_CACHE_HOME': str(work/'cache'),
               'XDG_DATA_HOME': str(work/'data'), 'XDG_STATE_HOME': str(work/'state'),
               'OPENCODE_TEST_MANAGED_CONFIG_DIR': str(work/'managed'),
               'OPENCODE_MODELS_PATH': str(models)}
        for flag in ('DISABLE_AUTOUPDATE', 'DISABLE_MODELS_FETCH', 'DISABLE_DEFAULT_PLUGINS',
                     'DISABLE_PROJECT_CONFIG', 'DISABLE_EXTERNAL_SKILLS', 'DISABLE_CLAUDE_CODE',
                     'DISABLE_CLAUDE_CODE_PROMPT', 'DISABLE_CLAUDE_CODE_SKILLS', 'DISABLE_LSP_DOWNLOAD',
                     'DISABLE_SHARE', 'DISABLE_CHANNEL_DB',
                     'DISABLE_TERMINAL_TITLE', 'EXPERIMENTAL_DISABLE_FILEWATCHER'):
            env['OPENCODE_' + flag] = 'true'
        model = data['model']
        config = {'autoupdate': False, 'share': 'disabled', 'snapshot': False,
                  'enabled_providers': ['review'], 'model': 'review/' + model, 'small_model': 'review/' + model,
                  'permission': {'*': 'deny'}, 'plugin': [], 'mcp': {}, 'instructions': [],
                  'lsp': False, 'formatter': False, 'compaction': {'auto': True, 'prune': True},
                  'agent': {'review': {'mode': 'primary', 'prompt': data['system'], 'permission': {'*': 'deny'}},
                            'title': {'disable': True}, 'summary': {'disable': True}, 'compaction': {'disable': True}},
                  'provider': {'review': {'name': '内网审查网关', 'npm': '@ai-sdk/openai-compatible',
                    'options': {'baseURL': BROKER, 'apiKey': data['capability'], 'timeout': (data['timeout_seconds']+10)*1000},
                    'models': {model: {'name': model, 'limit': {'context': 65536, 'output': data.get('model_max_output_tokens') or 32768}}}}}}
        env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config, ensure_ascii=False)
        before_oom = oom_count()
        collector = CLICollector(data.get('response_limit_bytes',8*1024*1024))
        # Bun 的大 JSON stdout 写入管道可在退出时被截断；普通文件输出避免该丢失路径。
        with (work/'events.jsonl').open('wb') as output_file, (work/'events.jsonl').open('rb') as reader:
            process = subprocess.Popen([binary, 'run', '--pure', '--format', 'json', '--log-level', 'ERROR',
                                        '--agent', 'review', '--model', 'review/' + model],
                                       cwd=work/'project', env=env, stdin=subprocess.PIPE, stdout=output_file,
                                       stderr=subprocess.PIPE, start_new_session=True)
            _, stderr = communicate_bounded(process, data['prompt'], timeout=data['timeout_seconds']+25,
                                             stdout_limit=collector.limit, consume=collector.feed, stdout_reader=reader)
        output = collector.finish()
        after_oom = oom_count()
        if process.returncode == -signal.SIGKILL and before_oom is not None and after_oom is not None and after_oom > before_oom:
            raise ExecutionError('cli_oom', {'exit_code': process.returncode, 'category': '内存不足'})
        if process.returncode:
            diagnostic = {'diagnostic_version':1,'source':'native_process','phase':'process_exit',
                          'reason':'process_exit','exit_code':process.returncode,
                          'received_bytes':collector.size}
            for label, phrase in [('磁盘空间不足','No space left'),('内存不足','out of memory'),
                                  ('权限拒绝','Permission denied'),('数据库错误','SQLITE'),('进程创建失败','spawn')]:
                if phrase.lower() in stderr.lower():diagnostic['category']=label
            for line in output.splitlines():
                try:
                    event=json.loads(line);error=event.get('error',{})
                    if event.get('type')=='error' and isinstance(error,dict):
                        if error.get('name') in ('APIError','UnknownError','ProviderAuthError','MessageOutputLengthError'):
                            diagnostic['error_type']=error['name']
                        if type(error.get('data',{}).get('statusCode')) is int:
                            diagnostic['http_status']=error['data']['statusCode']
                except (ValueError,AttributeError):pass
            raise ExecutionError('cli_exit' if process.returncode else 'output_too_large',diagnostic)
        texts = []
        finish_reasons = []
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ExecutionError('cli_protocol',json_failure(exc,source='cli_protocol',
                    phase='cli_event_decode',extraction_path='cli.collected_event',
                    received_bytes=len(line.encode()),reason='cli_event_json')) from None
            if not isinstance(event,dict):
                raise ExecutionError('cli_protocol',{'diagnostic_version':1,'source':'cli_protocol',
                    'phase':'cli_event_decode','reason':'cli_event_schema',
                    'extraction_path':'cli.collected_event','received_bytes':len(line.encode()),
                    'result_type':type(event).__name__})
            if event.get('type') in ('error', 'tool_use') or event.get('part', {}).get('type') == 'tool':
                name = event.get('error', {}).get('name') if isinstance(event.get('error'), dict) else None
                detail = {'event_type': 'tool' if event.get('type') != 'error' else 'error'}
                if name in ('APIError', 'UnknownError', 'ProviderAuthError', 'MessageOutputLengthError'):
                    detail['error_type'] = name
                detail.update(diagnostic_version=1,source='cli_event',phase='cli_event_validate',
                              reason='cli_error_event' if event.get('type')=='error' else 'cli_tool_event',
                              extraction_path='cli.collected_event')
                raise ExecutionError('cli_event_or_tools', detail)
            if event.get('type') == 'text':
                part=event.get('part')
                if not isinstance(part,dict) or not isinstance(part.get('text'),str):
                    raise ExecutionError('cli_protocol',{'diagnostic_version':1,'source':'cli_protocol',
                        'phase':'cli_event_validate','reason':'cli_text_schema',
                        'extraction_path':'cli.event.part.text'})
                texts.append(part['text'])
                if isinstance(event.get('finish_reason'),str):finish_reasons.append(event['finish_reason'])
        return decode_result(''.join(texts), data.get('response_limit_bytes',8*1024*1024),
                             {'finish_reason':finish_reasons[-1] if finish_reasons else 'missing',
                              'text_part_count':len(texts)})


runtime_status = {'ready': False, 'reason': '执行器启动自检中'}

def initialize_runtime():
    global runtime_status
    from runtime_probe import probe
    try:
        runtime_status = probe()
    except Exception as exc:
        runtime_status = {'ready': False, 'reason': str(exc)}
    logging.warning('执行器启动检测：%s', json.dumps(runtime_status, ensure_ascii=False))

threading.Thread(target=initialize_runtime, daemon=True, name='运行时自检').start()

@app.get('/health')
def health():
    return jsonify(ok=runtime_status.get('ready', False), **runtime_status), (200 if runtime_status.get('ready') else 503)


def executor_authorized():
    expected = Path(os.environ['AI_REVIEW_EXECUTOR_TOKEN_FILE']).read_text().strip()
    supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')
    return hmac.compare_digest(expected, supplied)


@app.post('/execute')
def execute_native():
    if not runtime_status.get('ready'):
        return jsonify(error='AI 执行器不可用'), 503
    if not executor_authorized():
        return jsonify(error='执行器身份无效'), 401
    try:
        row, _created = native_sessions.execute(request.get_json(silent=True))
        row.done.wait(timeout=row.remaining() + 2 if hasattr(row, 'remaining') else 2)
        return jsonify(row.public())
    except NativeError as exc:
        return jsonify(error='原生会话请求无效', error_code=exc.code, diagnostic=exc.diagnostic), 400


@app.get('/sessions/<session_id>')
def get_native(session_id):
    if not executor_authorized():
        return jsonify(error='执行器身份无效'), 401
    row = native_sessions.get(session_id)
    if row is None:
        return jsonify(session_id=session_id, status='unknown', error_code='session_not_found'), 404
    return jsonify(row.public())


@app.post('/sessions/<session_id>/abort')
def abort_native(session_id):
    if not executor_authorized():
        return jsonify(error='执行器身份无效'), 401
    row = native_sessions.get(session_id)
    if row is None:
        return jsonify(session_id=session_id, status='unknown', error_code='session_not_found'), 404
    row.abort('authorization_revoked')
    return jsonify(row.public())


@app.post('/sessions/<session_id>/authorize')
def authorize_native(session_id):
    if not executor_authorized():
        return jsonify(error='执行器身份无效'), 401
    try:
        return jsonify(native_sessions.authorize(session_id, request.get_json(silent=True)))
    except NativeError as exc:
        status = 404 if exc.code == 'session_not_found' else 409
        return jsonify(session_id=session_id, status='unknown', error_code=exc.code), status


@app.post('/sessions/<session_id>/ack')
def acknowledge_native(session_id):
    if not executor_authorized():
        return jsonify(error='执行器身份无效'), 401
    try:
        return jsonify(native_sessions.acknowledge(session_id, request.get_json(silent=True)))
    except NativeError as exc:
        status = 404 if exc.code == 'session_not_found' else 409
        return jsonify(session_id=session_id, status='unknown', error_code=exc.code), status


@app.get('/sessions/<session_id>/diagnostics/archive')
def download_native_diagnostics(session_id):
    if not executor_authorized():
        return jsonify(available=False, error='executor_unauthorized'), 401
    job_id = request.args.get('job_id', '')
    if not diagnostics_download.acquire(blocking=False):
        return jsonify(available=False, error='diagnostics_download_busy'), 429
    try:
        (path, metadata), bound_job = native_sessions.diagnostics_archive(session_id, job_id)
    except NativeError as exc:
        status = {'diagnostics_identity_invalid': 400, 'session_not_found': 404,
                  'diagnostics_unavailable': 404, 'diagnostics_not_ready': 409,
                  'diagnostics_expired': 410}.get(exc.code, 503)
        diagnostics_download.release()
        return jsonify(available=False, truncated=False, bytes=0,
                       retention_expires_at=None, error=exc.code), status
    except Exception:
        diagnostics_download.release()
        logging.exception('原始诊断归档入口失败：会话=%s', session_id)
        return jsonify(available=False, error='diagnostics_archive_failed'), 503
    try:
        response = send_file(path, mimetype='application/zip', as_attachment=True,
                             download_name=f'ai-review-raw-diagnostics-{session_id}.zip',
                             conditional=True)
    except Exception:
        diagnostics_download.release()
        raise
    headers = {
        'X-Diagnostics-Available': 'true',
        'X-Diagnostics-Truncated': str(bool(metadata['truncated'])).lower(),
        'X-Diagnostics-Captured-Bytes': str(metadata['bytes']),
        'X-Diagnostics-Original-Bytes': str(metadata['original_bytes']),
        'X-Diagnostics-SHA256': metadata['sha256'] or '',
        'X-Diagnostics-Retention-Expires-At': str(metadata['retention_expires_at'] or ''),
        'X-Diagnostics-Job-ID': bound_job,
        'X-Diagnostics-Session-ID': session_id,
    }
    response.headers.update(headers)
    # 让 WSGI ClosingIterator 确定执行 call_on_close；否则 file_wrapper 可能绕过回调。
    response.direct_passthrough = False
    response.call_on_close(diagnostics_download.release)
    return response


@app.post('/run')
def run():
    if not executor_authorized():
        return jsonify(error='执行器身份无效'), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) - {'engine', 'model', 'capability', 'system', 'prompt', 'timeout_seconds', 'response_limit_bytes', 'model_max_output_tokens'} or not {'engine', 'model', 'capability', 'system', 'prompt'} <= set(data):
        return jsonify(error='执行输入格式错误'), 400
    if any(not isinstance(data[k], str) or not data[k] for k in ('engine','model','capability','system','prompt')) or len(data['capability']) > 200 or len(data['model']) > 128:
        return jsonify(error='执行输入字段错误'), 400
    data.setdefault('timeout_seconds', 600)
    if type(data['timeout_seconds']) is not int or not 20 <= data['timeout_seconds'] <= 3600:
        return jsonify(error='模型请求期限无效'), 400
    data.setdefault('response_limit_bytes',8*1024*1024)
    data.setdefault('model_max_output_tokens',0)
    if type(data['response_limit_bytes']) is not int or not 1024*1024 <= data['response_limit_bytes'] <= 64*1024*1024 or type(data['model_max_output_tokens']) is not int or not 0 <= data['model_max_output_tokens'] <= 1048576:
        return jsonify(error='模型输出额度无效'),400
    if not busy.acquire(blocking=False):
        return jsonify(error='执行器正忙'), 429
    started=time.monotonic()
    def timing():return {'executor_seconds':round(time.monotonic()-started,6)}
    def contextual(values):
        allowed={'diagnostic_version','time_utc','source','phase','reason','extraction_path',
                 'job','job_id','session_id','native_session_id','call_id','http_status',
                 'received_bytes','response_chars','text_part_count','json_offset','json_line',
                 'json_column','parser_reason','parser_message','finish_reason','native_error_type',
                 'result_type','exit_code','limit_bytes','response_limit_bytes','exception_type',
                 'errno','category','event_type','error_type','stack_location','reserved_bytes'}
        result={key:value for key,value in (values or {}).items() if key in allowed}
        result.setdefault('diagnostic_version',1)
        result.setdefault('time_utc',datetime.now(timezone.utc).isoformat())
        for header,key in (('X-Review-Job','job'),('X-Review-Session','session_id'),
                           ('X-Review-Call','call_id')):
            value=request.headers.get(header)
            if value:result.setdefault(key,safe_token(value))
        return result
    try:
        if data['engine'] not in ('opencode_cli', 'new_api_http'):
            return jsonify(error='未支持的执行引擎'), 400
        result = opencode(data) if data['engine'] == 'opencode_cli' else direct(data)
        return jsonify(result=result, timing=timing())
    except ExecutionError as exc:
        safe_diagnostic=contextual(exc.diagnostic)
        logging.error('模型执行失败：代码=%s，诊断=%s',exc.code,json.dumps(safe_diagnostic,ensure_ascii=False))
        return jsonify(error='模型执行未通过校验', error_code=exc.code, diagnostic=safe_diagnostic, timing=timing()), 502
    except (subprocess.TimeoutExpired, requests.Timeout) as exc:
        diagnostic=contextual(safe_exception(exc,source='executor_timeout',phase='process_wait',reason='timeout'))
        logging.error('模型执行超时：诊断=%s',json.dumps(diagnostic,ensure_ascii=False))
        return jsonify(error='模型执行超时', error_code='timeout', diagnostic=diagnostic, timing=timing()), 502
    except (ValueError, KeyError, IndexError, requests.RequestException, subprocess.SubprocessError, OSError) as exc:
        diagnostic=contextual(safe_exception(exc,source='executor_runtime',phase='execute',reason='runtime_exception'))
        if type(getattr(exc,'errno',None)) is int:diagnostic['errno']=exc.errno
        logging.error('模型执行失败：诊断=%s',json.dumps(diagnostic,ensure_ascii=False))
        return jsonify(error='模型执行失败、超时或输出未通过结构校验',
                       error_code='runtime_'+type(exc).__name__.lower(),diagnostic=diagnostic,timing=timing()), 502
    finally:
        busy.release()
