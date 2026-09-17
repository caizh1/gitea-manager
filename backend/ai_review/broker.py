"""仅转发任务授权的模型请求；执行器不接触真实上游凭据。"""
import hashlib
import json
import time
import requests
from flask import Flask, request, jsonify, Response

from .common import ReviewError, endpoint
from .store import Store


def validate_completion(content, streaming):
    """网关统一验证结束语义，不能只信任 CLI 输出了合法 JSON。"""
    try:
        if not streaming:
            value=json.loads(content)
            choices=value['choices']
            if len(choices)!=1 or choices[0].get('finish_reason')!='stop':
                raise ValueError()
            message=choices[0]['message']
            if message.get('tool_calls') or message.get('function_call') or not isinstance(message.get('content'),str):
                raise ValueError()
            return
        ended=False; reason=None
        for block in content.decode('utf-8').replace('\r\n','\n').split('\n\n'):
            data='\n'.join(line[5:].lstrip(' ') for line in block.split('\n') if line.startswith('data:'))
            if not data:continue
            if ended:raise ValueError()
            if data=='[DONE]':ended=True;continue
            value=json.loads(data)
            if value.get('error'):raise ValueError()
            choices=value['choices']
            if not isinstance(choices,list) or len(choices)>1:raise ValueError()
            for choice in choices:
                if choice.get('index')!=0:raise ValueError()
                delta=choice.get('delta',{})
                if delta.get('tool_calls') or delta.get('function_call'):raise ValueError()
                current=choice.get('finish_reason')
                if current is not None:
                    if current!='stop' or reason is not None:raise ValueError()
                    reason=current
        if not ended or reason!='stop':raise ValueError()
    except (ValueError,KeyError,TypeError,AttributeError,IndexError,UnicodeError):
        raise ReviewError('模型响应未正常结束、已截断或尝试调用工具，不能作为完整审查结果') from None


def create_broker(store=None):
    store = store or Store()
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 256000

    @app.post('/v1/chat/completions')
    def completion():
        try:
            token = request.headers.get('Authorization', '').removeprefix('Bearer ')
            body = request.get_json(silent=True)
            if (not isinstance(body, dict) or not isinstance(body.get('messages'), list)
                    or not 1 <= len(body['messages']) <= 12 or body.get('tools') or body.get('functions')):
                raise ReviewError('只允许受限的文本模型调用')
            messages = body['messages']
            if any(not isinstance(m, dict) or m.get('role') not in ('system', 'user', 'assistant')
                   or not isinstance(m.get('content'), str) for m in messages):
                raise ReviewError('模型消息必须是纯文本')
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            with store.tx() as c:
                cap = c.execute('SELECT * FROM capabilities WHERE token_hash=?', (token_hash,)).fetchone()
                if not cap or cap['expires'] < time.time() or cap['remaining'] <= 0 or body.get('model') != cap['model']:
                    raise ReviewError('任务模型授权无效或已过期')
                store._active(c, cap['job'], cap['owner'])
                config = store.config(connection=c)
                used = c.execute('SELECT model_calls FROM jobs WHERE id=?', (cap['job'],)).fetchone()[0]
                if used >= config['max_calls']:
                    raise ReviewError('已达到本任务模型调用上限')
                c.execute('UPDATE capabilities SET remaining=remaining-1 WHERE token_hash=?', (token_hash,))
                c.execute('UPDATE jobs SET model_calls=model_calls+1 WHERE id=?', (cap['job'],))
            target = endpoint(config['api_base_url'], 'AI_REVIEW_MODEL_HOSTS')
            outbound = {'model': cap['model'], 'messages': messages, 'max_tokens': 6000,
                        'temperature': 0.1, 'stream': bool(body.get('stream')),
                        'response_format': {'type': 'json_object'}}
            if cap['model'].lower().startswith('deepseek'):
                outbound['thinking'] = {'type': 'disabled'}
            session = requests.Session(); session.trust_env = False
            try:
                with session.post(target + '/chat/completions', json=outbound,
                                  headers={'Authorization': 'Bearer ' + store.unseal(config['api_key'])},
                                  timeout=(5, 120), allow_redirects=False, stream=True) as response:
                    if response.status_code != 200:
                        store.update(cap['job'], cap['owner'], error=f'模型服务返回 HTTP {response.status_code}')
                        return jsonify(error={'message': f'模型服务返回 HTTP {response.status_code}'}), 502
                    content, size = [], 0
                    started = time.monotonic()
                    for part in response.iter_content(8192):
                        size += len(part)
                        if size > 1024*1024 or time.monotonic() - started > 150:
                            raise ReviewError('模型响应超过大小或时间上限')
                        content.append(part)
                    store.active(cap['job'], cap['owner'])
                    raw=b''.join(content)
                    try:validate_completion(raw,outbound['stream'])
                    except ReviewError as exc:
                        store.update(cap['job'],cap['owner'],error=str(exc))
                        raise
                    return Response(raw, content_type='text/event-stream' if outbound['stream'] else 'application/json')
            finally:
                session.close()
        except ReviewError as exc:
            return jsonify(error={'message': str(exc)}), 403
        except (requests.RequestException, ValueError):
            return jsonify(error={'message': '模型连接失败或超时'}), 502

    @app.get('/health')
    def health():
        return jsonify(ok=True)
    return app
