"""通过真实本机 HTTP 验证审查网关的检索、收尾和原始回执传递。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'review-runtime'))
from native_gateway import NativeGateway


class ReviewParameterTests(unittest.TestCase):
    def test_research_auto_and_final_named_tool_keep_history_and_schema(self):
        received, failures = [], []
        schema = json.loads((ROOT / 'backend/ai_review/review_result_schema.json').read_text())
        result = {'coverage': {'reviewed_paths': [], 'skipped': [
            {'path': 'sample.c', 'reason': '协议验收未进行真实代码判断'}]}, 'findings': []}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body = json.dumps({'choices': [{'message': {'role': 'assistant', 'tool_calls': [{
                    'id': 'submit-result', 'type': 'function', 'function': {
                        'name': 'StructuredOutput', 'arguments': json.dumps(result, ensure_ascii=False)}}]},
                    'finish_reason': 'tool_calls'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        broker = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=broker.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(broker.server_close)
        self.addCleanup(broker.shutdown)
        with tempfile.TemporaryDirectory() as folder:
            gateway = NativeGateway('http://127.0.0.1:' + str(broker.server_address[1]),
                'test-capability', 'job-test', 'session-test', 'qwen3.8-27b-fp8', time.time()+300,
                folder, result_schema=schema, retrieval_budget=200, failure=failures.append)
            gateway.start()
            try:
                messages = [
                    {'role': 'system', 'content': '只读审查协议验收'},
                    {'role': 'user', 'content': '审查固定源码'},
                    {'role': 'assistant', 'content': None, 'tool_calls': [{
                        'id': 'empty-build-search', 'type': 'function', 'function': {
                            'name': 'glob', 'arguments': '{"pattern":"after/**/build.sh"}'}}]},
                    {'role': 'tool', 'tool_call_id': 'empty-build-search', 'content': 'No files found'},
                ]
                structured = {'type': 'function', 'function': {
                    'name': 'StructuredOutput', 'parameters': schema}}
                body = {'model': gateway.model, 'messages': messages, 'tool_choice': 'required',
                    'tools': [{'type': 'function', 'function': {
                        'name': 'glob', 'parameters': {'type': 'object'}}}, structured]}
                client = requests.Session()
                client.trust_env = False
                with client:
                    for finishing in (False, True):
                        gateway.retrieval_used = 200 if finishing else 0
                        response = client.post(gateway.url + '/chat/completions', json=body,
                            headers={'Authorization': 'Bearer ' + gateway.local_token}, timeout=10)
                        self.assertEqual(response.status_code, 200, (response.text, failures))
                        self.assertEqual(response.json()['choices'][0]['message']['tool_calls'][0]
                                         ['function']['name'], 'StructuredOutput')
                self.assertEqual(received[0]['tool_choice'], 'auto')
                self.assertEqual(received[0]['messages'], messages)
                self.assertEqual(received[0]['tools'], body['tools'])
                self.assertEqual(received[1]['tool_choice'], {
                    'type': 'function', 'function': {'name': 'StructuredOutput'}})
                self.assertEqual(received[1]['tools'], [structured])
                self.assertEqual(received[1]['messages'][1:], messages[1:])
                self.assertIn('立即通过 StructuredOutput', received[1]['messages'][0]['content'])
                self.assertFalse(gateway.failure_signalled)
            finally:
                gateway.close()


if __name__ == '__main__':
    unittest.main()
