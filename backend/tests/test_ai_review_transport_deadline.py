"""真实慢响应检验总时限、单次发送、连接关闭与子进程回收。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from ai_review.model_transport import fetch_model, ModelTransportError, MAX_RESPONSE


class TransportDeadlineTests(unittest.TestCase):
    def server(self, mode):
        received = []
        closed = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                received.append(self.path)
                self.rfile.read(int(self.headers['Content-Length']))
                try:
                    if mode == 'headers':
                        self.wfile.write(b'HTTP/1.1 200 ')
                    elif mode in ('redirect', 'error'):
                        self.send_response(302 if mode == 'redirect' else 500)
                        self.send_header('Location', '/unexpected')
                        self.end_headers()
                        return
                    else:
                        self.send_response(200)
                        body=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':('x'*(MAX_RESPONSE+8192) if mode=='large' else '{\"findings\":[]}')}}]}).encode()
                        length = len(body) if mode in ('large','normal') else 100000
                        self.send_header('Content-Length', str(length))
                        self.end_headers()
                    if mode == 'large':
                        self.wfile.write(body)
                        return
                    if mode == 'normal':
                        self.wfile.write(body)
                        return
                    until = time.monotonic() + 4
                    while time.monotonic() < until:
                        self.wfile.write(b'x' if mode == 'headers' else b': heartbeat\n\n')
                        time.sleep(.02)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    closed.set()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}/completion', received, closed

    def test_heartbeat_and_trickled_headers_cannot_outlive_total_deadline(self):
        for mode in ('body', 'headers'):
            with self.subTest(阶段=mode):
                url, received, closed = self.server(mode)
                processes = []
                original = subprocess.Popen
                def record(*args, **kwargs):
                    process = original(*args, **kwargs)
                    processes.append(process)
                    return process
                started = time.monotonic()
                with patch('ai_review.model_transport.subprocess.Popen', side_effect=record):
                    with self.assertRaises(ModelTransportError) as caught:
                        fetch_model(url, {'messages': []}, 'test-private-key', timeout=.7)
                self.assertEqual(caught.exception.kind, 'Timeout')
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(received, ['/completion'])
                self.assertTrue(closed.wait(1), '上游连接必须随超时进程关闭')
                self.assertEqual(len(processes), 1)
                self.assertIsNotNone(processes[0].poll())
                self.assertTrue(processes[0].stdin.closed and processes[0].stdout.closed)
                self.assertNotIn('test-private-key', str(processes[0].args))

    def test_status_is_returned_without_redirect_or_retry(self):
        for mode, expected in [('redirect', 302), ('error', 500)]:
            with self.subTest(响应=mode):
                url, received, _ = self.server(mode)
                self.assertEqual(fetch_model(url, {}, 'test-key', timeout=3), (expected, b''))
                self.assertEqual(received, ['/completion'])

    def test_success_and_failed_process_launch(self):
        url, received, _ = self.server('normal')
        status,raw=fetch_model(url, {}, 'test-key', timeout=3)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(raw)['choices'][0]['message']['content'],'{"findings":[]}')
        self.assertEqual(received, ['/completion'])
        with patch('ai_review.model_transport.subprocess.Popen', side_effect=OSError('敏感内部原因')):
            with self.assertRaises(ModelTransportError) as caught:
                fetch_model(url, {}, 'test-key', timeout=3)
        self.assertEqual(str(caught.exception), 'TransportFailure')
        self.assertEqual(received, ['/completion'])

    def test_orphaned_transfer_still_has_its_own_deadline(self):
        # 直接启动传输子进程，不提供父进程的超时兜底，模拟父进程已经退出。
        from ai_review import model_transport
        url, received, closed = self.server('body')
        process = subprocess.Popen([model_transport.sys.executable, '-I', model_transport.__file__],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, errors = process.communicate(json.dumps({'target': url, 'outbound': {}, 'key': 'test-key',
                'connect_timeout': 1, 'timeout': .7, 'ca_bundle': None}).encode(), timeout=3)
            self.assertEqual(json.loads(output)['error'], 'Timeout')
            self.assertGreater(json.loads(output)['metrics']['received_bytes'], 0)
            self.assertEqual(errors, b'')
            self.assertEqual(received, ['/completion'])
            self.assertTrue(closed.wait(1))
        finally:
            if process.poll() is None:process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):stream.close()

    def test_response_limit_is_enforced_before_returning_body(self):
        url, received, _ = self.server('large')
        with self.assertRaises(ModelTransportError) as caught:
            fetch_model(url, {}, 'test-key', timeout=3)
        self.assertEqual(caught.exception.kind, 'ResponseTooLarge')
        self.assertEqual(received, ['/completion'])


if __name__ == '__main__':
    unittest.main()
