"""只用于隔离验收的协议替身，不是 AI 模型，不用于质量准确率统计。"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

control = {'mode': 'normal', 'delay': 0, 'calls': 0}
lock = threading.Lock()


def generate(messages, mode):
    prompt = '\n'.join(m.get('content', '') for m in messages if m.get('role') == 'user')
    if prompt.startswith('连接验证：'): return {'ok': True}
    data = json.loads(prompt.split('审查输入：\n')[-1])
    if 'candidates' in data:
        if mode == 'refutation_incomplete': return {'checks': []}
        return {'checks': [{'candidate_id': r['candidate_id'], 'verdict': 'confirmed',
                            'location_verified': mode != 'unlocated', 'reason': '协议替身：按固定验收输入返回，不代表模型判断。',
                            'finding': {k:v for k,v in r.items() if k != 'candidate_id'}}
                           for r in data['candidates']]}
    anchors = [a for a in data.get('anchors', []) if 'REVIEW_TEST_TARGET' in a['quote']]
    if not anchors:
        return {'findings': []}
    a = anchors[0]
    root = next((o for o in data.get('operations',[]) if a['id'] in o['anchor_ids']), None)
    proof = [{'source_id': data[v]['source_id'], 'quote': data[v]['code']} for v in ('before','after') if data.get(v,{}).get('code')]
    result = {'findings': [{'operation_id': root['id'] if root else '', 'before_behavior': '协议替身的修改前状态',
        'after_behavior': '协议替身的修改后状态', 'trace': ['读取固定合成输入', '返回预定意见'], 'support': proof, 'anchor_id': '不存在的锚点' if mode == 'wrong_anchor' else a['id'],
        'quote': '不匹配的引用' if mode == 'wrong_quote' else a['quote'], 'severity': 'P2',
        'title': '固定位置协议验收意见', 'trigger': '仅在合成测试中使用标记行',
        'explanation': '此替身用于检验位置映射和发布流程，不能用于衡量 AI 缺陷识别能力。',
        'impact': '测试意见应出现在预期行，或按注入条件降级。', 'recommendation': '核对测试报告中的预期位置与实际位置。'}]}
    if mode == 'natural_only':
        for key in ('support','trace','before_behavior','after_behavior'):
            result['findings'][0].pop(key)
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, value, content_type='application/json'):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

    def authorized(self):
        import hmac
        return hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + os.environ['FIXTURE_TOKEN'])

    def do_GET(self):
        if not self.authorized(): return self.reply(401, {'错误': '测试凭据无效'})
        if self.path == '/v1/models': return self.reply(200, {'data': [{'id': 'review-fixture'}]})
        if self.path == '/test/control': return self.reply(200, control)
        self.reply(404, {})

    def do_POST(self):
        if not self.authorized(): return self.reply(401, {'错误': '测试凭据无效'})
        data = json.loads(self.rfile.read(min(int(self.headers.get('Content-Length', '0')), 256000)))
        if self.path == '/test/control':
            with lock:
                control.update(mode=data.get('mode', 'normal'), delay=min(float(data.get('delay', 0)), 180), calls=0)
            return self.reply(200, {'说明': '仅修改隔离协议替身'})
        if self.path != '/v1/chat/completions': return self.reply(404, {})
        with lock:
            mode, delay = control['mode'], control['delay']; control['calls'] += 1
        time.sleep(delay)
        if mode in ('429', '500'): return self.reply(int(mode), {'error': {'message': '本机故障注入'}})
        try: value = generate(data['messages'], mode)
        except (ValueError, KeyError): return self.reply(400, {'error': {'message': '协议替身输入不符合固定结构'}})
        content = '{非法结构' if mode == 'invalid_json' else json.dumps(value, ensure_ascii=False)
        message = {'role': 'assistant', 'content': content}
        if mode == 'tools': message['tool_calls'] = [{'id': '仅测试', 'type': 'function', 'function': {'name': 'bash', 'arguments': '{}'}}]
        reason = 'length' if mode == 'truncated' else 'tool_calls' if mode == 'tools' else 'stop'
        if data.get('stream'):
            base = {'id': '本机协议替身', 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': 'review-fixture'}
            chunks = [dict(base, choices=[{'index': 0, 'delta': message, 'finish_reason': None}]),
                      dict(base, choices=[{'index': 0, 'delta': {}, 'finish_reason': reason}])]
            raw = ''.join('data: '+json.dumps(c, ensure_ascii=False)+'\n\n' for c in chunks)+'data: [DONE]\n\n'
            return self.reply(200, raw.encode(), 'text/event-stream')
        self.reply(200, {'id': '本机协议替身', 'model': 'review-fixture', 'choices': [{'index': 0, 'message': message, 'finish_reason': reason}]})


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 5053), Handler).serve_forever()
