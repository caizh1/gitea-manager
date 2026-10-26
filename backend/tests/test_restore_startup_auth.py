"""恢复隔离探针对真实HTTP登录要求的源、目标身份回归。"""
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from services.restore_isolation import IsolatedGitea
from services.commit_service import RestoreValidationError


class StartupAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        seen = self.requests

        class ProtectedAPI(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get('Authorization')))
                permitted = self.headers.get('Authorization') == 'token source-snapshot-token'
                self.send_response(200 if permitted else 403)
                self.end_headers()
                self.wfile.write(json.dumps({'version': '1.26.4'} if permitted else {'message': '必须登录'}).encode())

            def log_message(self, *args):
                pass

        self.http = HTTPServer(('127.0.0.1', 0), ProtectedAPI)
        self.thread = threading.Thread(target=self.http.serve_forever)
        self.thread.start()
        port = self.http.server_port

        class Container:
            def start(self):
                pass

            def exec_run(self, args, user=None):
                self_args = [value.removeprefix('--header=') for value in args if value.startswith('--header=')]
                headers = dict(value.split(': ', 1) for value in self_args)
                path = urlsplit(args[-1]).path
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                try:
                    conn.request('GET', path, headers=headers)
                    response = conn.getresponse()
                    body = response.read()
                    return (0 if response.status == 200 else 1), body
                finally:
                    conn.close()

        self.container = Container()
        network = SimpleNamespace(name='隔离测试网络', connect=lambda *args, **kwargs: None)
        self.creation = {}

        def create(*args, **kwargs):
            self.creation.update(kwargs)
            return self.container

        self.client = SimpleNamespace(
            networks=SimpleNamespace(list=lambda: [], create=lambda *args, **kwargs: network),
            containers=SimpleNamespace(create=create))
        self.original = SimpleNamespace(id='原测试容器', attrs={
            'Image': '固定测试镜像', 'Config': {'Env': ['GITEA__service__REQUIRE_SIGNIN_VIEW=true']}})
        self.target = SimpleNamespace(api_token='obsolete-target-token')
        self.read_patch = patch('services.restore_isolation.read_file', return_value=(b'[service]\nREQUIRE_SIGNIN_VIEW=true\n', {}))
        self.write_patch = patch('services.restore_isolation.put_file')
        self.read_patch.start()
        self.write_patch.start()

    def tearDown(self):
        self.read_patch.stop()
        self.write_patch.stop()
        self.http.shutdown()
        self.thread.join(timeout=3)
        self.http.server_close()

    def test_source_snapshot_identity_starts_without_replacing_target_identity(self):
        isolated = IsolatedGitea(self.client, self.original, object(), self.target, '本轮操作',
                                validation_token='source-snapshot-token')
        with patch('services.restore_isolation.time', SimpleNamespace(monotonic=iter([0, 121]).__next__, sleep=lambda _: None)):
            isolated.start()
        self.assertEqual(self.requests, [('/api/v1/version', 'token source-snapshot-token')])
        self.assertEqual(self.target.api_token, 'obsolete-target-token')
        self.assertEqual(self.creation['environment']['GITEA__service__REQUIRE_SIGNIN_VIEW'], 'true')

    def test_invalid_snapshot_identity_keeps_startup_closed(self):
        isolated = IsolatedGitea(self.client, self.original, object(), self.target, '本轮操作',
                                validation_token='invalid-snapshot-token')
        with patch('services.restore_isolation.time', SimpleNamespace(monotonic=iter([0, 121]).__next__, sleep=lambda _: None)):
            with self.assertRaisesRegex(RuntimeError, '未开放业务端口') as failure:
                isolated.start()
        self.assertEqual(self.target.api_token, 'obsolete-target-token')
        self.assertNotIn('invalid-snapshot-token', str(failure.exception))
        self.assertEqual(len(self.requests), 1)

    def test_legacy_identity_and_post_sync_reader_still_follow_server(self):
        self.target.api_token = 'source-snapshot-token'
        isolated = IsolatedGitea(self.client, self.original, object(), self.target, '本轮操作')
        with patch('services.restore_isolation.time', SimpleNamespace(monotonic=iter([0, 121]).__next__, sleep=lambda _: None)):
            isolated.start()
        self.target.api_token = 'replaced-token'
        from services.commit_service import RestoreValidationError
        with self.assertRaises(RestoreValidationError):
            isolated.api_get_json('/api/v1/user')
        self.assertEqual(self.requests[-1], ('/api/v1/user', 'token replaced-token'))


if __name__ == '__main__':
    unittest.main()
