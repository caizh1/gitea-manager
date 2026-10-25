"""真实 OpenCode 连续分批与整任务取消；上游为本地协议替身。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, '/opt/review')
from native_session import NativeSessions, NativeError


def main():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder); workspace = root/'workspaces'/'batch_job_test'; workspace.mkdir(parents=True)
        os.environ['AI_REVIEW_NATIVE_ROOT'] = str(root/'runtime')
        os.environ['AI_REVIEW_WORKSPACE_ROOT'] = str(workspace.parent)
        calls, events = [], []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == '/events':
                    records = data if isinstance(data, list) else [data]; events.extend(records)
                    payload = json.dumps({'ok': True, 'inserted': len(records), 'session_id': records[-1]['session_id'], 'acked_seq': records[-1]['seq']}).encode()
                    content_type = 'application/json'
                else:
                    calls.append(data)
                    assert not data.get('tools'), '分批模型没有工具'
                    report = json.dumps({'summary': '协议验证完成', 'findings': [], 'limitations': []}, ensure_ascii=False)
                    payload = ('data: '+json.dumps({'choices': [{'index': 0, 'delta': {'role':'assistant','content':report}, 'finish_reason': None}]})+'\n\n').encode()
                    payload += b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                    content_type = 'text/event-stream'
                self.send_response(200); self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = 'http://127.0.0.1:'+str(server.server_port)
        os.environ['AI_REVIEW_BROKER_URL'] = url+'/v1'
        os.environ['AI_REVIEW_EVENT_URL'] = url+'/events'
        registry = NativeSessions()
        try:
            for i in range(2):
                data = {'job_id': 'batch_job_test', 'session_id': 'batch_session_test_'+str(i),
                        'workspace': str(workspace), 'model': 'fixture', 'prompt': '只审查本片段，输出指定 JSON。',
                        'system': '只根据证据判断，无工具。', 'review_scope': 'diff', 'batch_protocol': 1,
                        'deadline_at': time.time()+60, 'timeout': 60, 'event_url':url+'/events',
                        'event_token':'fixture', 'capability':'fixture', 'model_context_tokens':262144}
                row, created = registry.execute(data)
                assert created and row.done.wait(60), '真实执行器未在期限内完成'
                value = row.public()
                assert value['status'] == 'completed', value
                assert json.loads(value['result']['report_markdown'])['findings'] == []
                registry.acknowledge(data['session_id'], {'result_sha256':value['result_sha256'], 'final_event_seq':value['final_event_seq']})
                seqs = [e['seq'] for e in events if e['session_id'] == data['session_id']]
                assert seqs and seqs[0] == 1 and seqs == list(range(1, max(seqs)+1))
            assert len(calls) == 2, '不得隐式重发模型请求'
            registry.abort(data['session_id'], data['job_id'])
            data.update(session_id='batch_session_test_cancelled', deadline_at=time.time()+60)
            try:
                registry.execute(data)
            except NativeError as exc:
                assert exc.code == 'authorization_revoked'
            else:
                raise AssertionError('整任务取消后不得启动新批次')
            print(json.dumps({'结果':'通过','真实执行器连续批数':2,'实际模型请求':len(calls),
                              '取消后新批次':'拒绝','边界':'协议替身，不代表内网模型效果'},ensure_ascii=False))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__':
    main()
