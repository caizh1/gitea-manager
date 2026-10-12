"""生产反向代理与会话 Cookie 边界：只信任指定直连地址及被覆盖的客户端 IP。"""
import os
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from unittest.mock import patch
from flask import Flask, request, session
from access.runtime import configure_transport, persistent_secret, signing_key


def read_shared_key(path):
    return persistent_secret(path)


class AccessTransportTests(unittest.TestCase):
    def app(self, **environment):
        app = Flask(__name__)
        app.secret_key = '隔离测试专用会话签名密钥-123456789abcdef'
        with patch.dict(os.environ, environment):
            configure_transport(app)
        @app.get('/probe')
        def probe():
            session['test'] = '测试会话'
            return {'secure': request.is_secure, 'address': request.remote_addr}
        return app.test_client()

    def test_untrusted_client_cannot_forge_tls_or_rate_limit_address(self):
        client = self.app(TRUSTED_PROXY_HOSTS='', TRUSTED_PROXY_NETWORKS='')
        response = client.get('/probe', headers={'X-Forwarded-Proto': 'https', 'X-Real-IP': '1.2.3.4'})
        self.assertEqual(response.json, {'secure': False, 'address': '127.0.0.1'})
        self.assertNotIn('; Secure', response.headers['Set-Cookie'])

    def test_trusted_proxy_sets_tls_cookie_and_validated_client_address(self):
        client = self.app(TRUSTED_PROXY_HOSTS='', TRUSTED_PROXY_NETWORKS='127.0.0.1/32')
        response = client.get('/probe', headers={'X-Forwarded-Proto': 'https', 'X-Real-IP': '192.0.2.4'})
        self.assertEqual(response.json, {'secure': True, 'address': '192.0.2.4'})
        for value in ('; Secure', '; HttpOnly', 'SameSite=Lax'):
            self.assertIn(value, response.headers['Set-Cookie'])

    def test_invalid_forwarded_chain_is_not_accepted(self):
        client = self.app(TRUSTED_PROXY_HOSTS='', TRUSTED_PROXY_NETWORKS='127.0.0.1/32')
        response = client.get('/probe', headers={'X-Forwarded-Proto': 'https,http', 'X-Real-IP': '1.2.3.4,5.6.7.8'})
        self.assertEqual(response.json, {'secure': False, 'address': '127.0.0.1'})

    def test_explicit_proxy_service_dns_is_compared_to_direct_peer(self):
        client = self.app(TRUSTED_PROXY_HOSTS='frontend', TRUSTED_PROXY_NETWORKS='')
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 0))]):
            self.assertTrue(client.get('/probe', headers={'X-Forwarded-Proto': 'https'}).json['secure'])
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('192.0.2.1', 0))]):
            self.assertFalse(client.get('/probe', headers={'X-Forwarded-Proto': 'https'}).json['secure'])

    def test_parallel_key_initialization_never_observes_partial_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / '.session-key')
            with ProcessPoolExecutor(max_workers=4) as pool:
                values = list(pool.map(read_shared_key, [path] * 16))
            self.assertEqual(len(set(values)), 1)
            self.assertGreaterEqual(len(values[0]), 32)
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)

    def test_known_public_session_key_is_rejected(self):
        with patch.dict(os.environ, {'SECRET_KEY': 'change-me-in-production'}):
            with self.assertRaises(RuntimeError):
                signing_key('change-me-in-production', 'sqlite:////tmp/unused.db')


if __name__ == '__main__':
    unittest.main()
