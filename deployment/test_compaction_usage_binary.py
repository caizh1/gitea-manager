"""真实交付 OpenCode＋本地协议替身：用量帧必须触发压缩；不评估模型审查质量。

在执行器镜像中运行本文件；无需外网、凭据或宿主机仓库写权限。
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, '/opt/review')
from native_session import SessionRun, VERSION


class CompactionUsageBinaryTests(unittest.TestCase):
    def test_usage_drives_real_compaction(self):
        self.assertEqual(VERSION, '1.18.7', '此回归固定当前标准交付二进制')
        for tokens, expected_compaction in ((200000, False), (210000, True), (260989, True)):
            with self.subTest(input_tokens=tokens), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                workspace, runtime = root/'workspace', root/'runtime'
                workspace.mkdir(); runtime.mkdir()
                (workspace/'sample.c').write_text('/* 压缩回归固定样本 */\nint sample = 0;\n')
                calls, events, handler_errors = [], [], []

                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *_args):
                        pass

                    def do_POST(self):
                        try:
                            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                            if self.path == '/events':
                                rows = body if isinstance(body, list) else [body]
                                events.extend(rows)
                                payload = json.dumps({'ok':True, 'inserted':len(rows),
                                    'session_id':rows[-1]['session_id'], 'acked_seq':rows[-1]['seq']}).encode()
                                kind = 'application/json'
                            else:
                                calls.append(body)
                                if len(calls) > 4:
                                    raise AssertionError('协议替身调用超出固定步骤')
                                names = [tool['function']['name'] for tool in body.get('tools', [])]
                                if len(calls) == 1:
                                    name, args = 'read', {'filePath':str(workspace/'sample.c'), 'limit':82}
                                    count = tokens
                                elif 'StructuredOutput' in names:
                                    name, args = 'StructuredOutput', {
                                        'coverage':{'reviewed_paths':['sample.c'], 'skipped':[]}, 'findings':[]}
                                    count = 1000
                                else:
                                    name, args, count = None, None, 1000
                                delta = {'content':'已读取 sample.c，只有一个变量声明。协议替身摘要，继续提交审查结果。'}
                                if name:
                                    delta = {'tool_calls':[{'index':0, 'id':'call_'+str(len(calls)),
                                        'type':'function', 'function':{'name':name, 'arguments':json.dumps(args)}}]}

                                def frame(choices, usage=None):
                                    value = {'id':'chatcmpl-fixture', 'object':'chat.completion.chunk',
                                        'created':int(time.time()), 'model':'fixture', 'choices':choices}
                                    if usage is not None:
                                        value['usage'] = usage
                                    return ('data: '+json.dumps(value)+'\n\n').encode()

                                payload = frame([{'index':0, 'delta':dict(role='assistant', **delta), 'finish_reason':None}])
                                payload += frame([{'index':0, 'delta':{}, 'finish_reason':'tool_calls' if name else 'stop'}])
                                payload += frame([], {'prompt_tokens':count, 'completion_tokens':100,
                                    'total_tokens':count+100, 'completion_tokens_details':{'reasoning_tokens':0}})
                                payload += b'data: [DONE]\n\n'
                                kind = 'text/event-stream'
                            self.send_response(200)
                            self.send_header('Content-Type', kind)
                            self.send_header('Content-Length', str(len(payload)))
                            self.end_headers(); self.wfile.write(payload)
                        except Exception as exc:
                            handler_errors.append(type(exc).__name__+': '+str(exc))
                            self.send_error(500)

                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                server.daemon_threads = True
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                url = f'http://127.0.0.1:{server.server_port}'
                previous = os.environ.get('AI_REVIEW_EVENT_URL')
                os.environ['AI_REVIEW_EVENT_URL'] = url+'/events'
                try:
                    run = SessionRun({'job_id':'job_usage_regression', 'session_id':'session_usage_regression',
                        'model':'fixture', 'deadline_at':time.time()+60, 'event_url':url+'/events',
                        'event_token':'fixture', 'capability':'fixture', 'prompt':'读取 sample.c，然后提交审查结果。',
                        'model_context_tokens':262144, 'compaction_threshold_tokens':210000},
                        workspace, runtime, url+'/v1')
                    run.run()
                    self.assertFalse(handler_errors, handler_errors)
                    self.assertEqual(run.status, 'completed', (run.error_code, run.diagnostic))
                    saved = json.loads(run.result_path.read_text())
                    self.assertEqual(saved['coverage']['reviewed_paths'], ['sample.c'])
                    phases = [row['status'] for row in events if row.get('type') == 'session.compaction']
                    self.assertEqual(phases, ['running', 'completed'] if expected_compaction else [])
                    self.assertEqual(len(calls), 3 if expected_compaction else 2)
                    if expected_compaction:
                        self.assertFalse(calls[1].get('tools'), '第二轮必须是压缩，不得继续检索')
                    self.assertIn('StructuredOutput', [t['function']['name'] for t in calls[-1]['tools']])
                    print(json.dumps({'输入Token':tokens, '压缩事件':phases, '模型请求数':len(calls),
                        '结果':'通过', '边界':'真实二进制和协议替身；未验证内网模型审查质量'}, ensure_ascii=False), flush=True)
                finally:
                    if previous is None:
                        os.environ.pop('AI_REVIEW_EVENT_URL', None)
                    else:
                        os.environ['AI_REVIEW_EVENT_URL'] = previous
                    server.shutdown(); server.server_close(); worker.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
