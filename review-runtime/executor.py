"""隔离执行服务：无源码仓库挂载、无上游密钥、每次调用使用独立临时目录。"""
import hmac
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 256000
busy = threading.Lock()
BROKER = os.environ.get('AI_REVIEW_BROKER_URL', 'http://review-worker:5051/v1')
VERSION = '1.18.7'


class ExecutionError(Exception):
    def __init__(self, code, diagnostic=None):
        self.code = code
        self.diagnostic = diagnostic or {}


def decode_result(text):
    if len(text) > 128000:
        raise ExecutionError('output_too_large')
    try:
        value = json.loads(text)
    except ValueError:
        raise ExecutionError('invalid_json') from None
    if not isinstance(value, dict):
        raise ExecutionError('invalid_json')
    return value


def direct(data):
    session = requests.Session(); session.trust_env = False
    try:
        response = session.post(BROKER + '/chat/completions',
            headers={'Authorization': 'Bearer ' + data['capability']},
            json={'model': data['model'], 'messages': [{'role': 'system', 'content': data['system']},
                   {'role': 'user', 'content': data['prompt']}], 'stream': False},
            timeout=(5, 160), allow_redirects=False)
        if response.status_code != 200:
            raise ExecutionError('gateway_unavailable')
        value = response.json()
        choice = value['choices'][0]
        if choice.get('finish_reason') != 'stop' or choice['message'].get('tool_calls'):
            raise ExecutionError('truncated_or_tools')
        return decode_result(choice['message']['content'])
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
                     'DISABLE_SHARE', 'DISABLE_AUTOCOMPACT', 'DISABLE_CHANNEL_DB',
                     'DISABLE_TERMINAL_TITLE', 'EXPERIMENTAL_DISABLE_FILEWATCHER'):
            env['OPENCODE_' + flag] = 'true'
        model = data['model']
        config = {'autoupdate': False, 'share': 'disabled', 'snapshot': False,
                  'enabled_providers': ['review'], 'model': 'review/' + model, 'small_model': 'review/' + model,
                  'permission': {'*': 'deny'}, 'plugin': [], 'mcp': {}, 'instructions': [],
                  'lsp': False, 'formatter': False, 'compaction': {'auto': False, 'prune': False},
                  'agent': {'review': {'mode': 'primary', 'prompt': data['system'], 'permission': {'*': 'deny'}, 'steps': 1},
                            'title': {'disable': True}, 'summary': {'disable': True}, 'compaction': {'disable': True}},
                  'provider': {'review': {'name': '内网审查网关', 'npm': '@ai-sdk/openai-compatible',
                    'options': {'baseURL': BROKER, 'apiKey': data['capability'], 'timeout': 160000},
                    'models': {model: {'name': model, 'limit': {'context': 65536, 'output': 6000}}}}}}
        env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config, ensure_ascii=False)
        process = subprocess.Popen([binary, 'run', '--format', 'json', '--log-level', 'ERROR',
                                    '--agent', 'review', '--model', 'review/' + model],
                                   cwd=work/'project', env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            output, stderr = process.communicate(input=data['prompt'], timeout=175)
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        if process.returncode or len(output) > 1024*1024:
            diagnostic = {'exit_code':process.returncode}
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
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                raise ExecutionError('cli_protocol') from None
            if event.get('type') in ('error', 'tool_use') or event.get('part', {}).get('type') == 'tool':
                raise ExecutionError('cli_event_or_tools')
            if event.get('type') == 'text':
                texts.append(event['part']['text'])
        return decode_result(''.join(texts))


@app.get('/health')
def health():
    return jsonify(ok=True, opencode_version=VERSION)


@app.post('/run')
def run():
    expected = Path(os.environ['AI_REVIEW_EXECUTOR_TOKEN_FILE']).read_text().strip()
    supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')
    if not hmac.compare_digest(expected, supplied):
        return jsonify(error='执行器身份无效'), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {'engine', 'model', 'capability', 'system', 'prompt'}:
        return jsonify(error='执行输入格式错误'), 400
    if any(not isinstance(v, str) or not v for v in data.values()) or len(data['capability']) > 200 or len(data['model']) > 128:
        return jsonify(error='执行输入字段错误'), 400
    if not busy.acquire(blocking=False):
        return jsonify(error='执行器正忙'), 429
    try:
        if data['engine'] not in ('opencode_cli', 'new_api_http'):
            return jsonify(error='未支持的执行引擎'), 400
        result = opencode(data) if data['engine'] == 'opencode_cli' else direct(data)
        return jsonify(result=result)
    except ExecutionError as exc:
        return jsonify(error='模型执行未通过校验', error_code=exc.code, diagnostic=exc.diagnostic), 502
    except (subprocess.TimeoutExpired, requests.Timeout):
        return jsonify(error='模型执行超时', error_code='timeout'), 502
    except (ValueError, KeyError, IndexError, requests.RequestException, subprocess.SubprocessError, OSError):
        # 不返回 CLI 原始输出、请求内容或异常参数，避免日志和 UI 泄漏源码及令牌。
        return jsonify(error='模型执行失败、超时或输出未通过结构校验'), 502
    finally:
        busy.release()
