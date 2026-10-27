"""真实应用入口的签名回归，覆盖全局前置检查、缓存正文与大小边界。"""
import contextlib
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from flask import Flask, request

from ai_review.store import Store
from models import db


class WebhookIngressTests(unittest.TestCase):
    limit = 1024 * 1024
    secret = '仅供隔离回归使用的签名密钥'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='webhook-ingress-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        key = self.root / 'master.key'
        key.write_bytes(Fernet.generate_key())
        environment = patch.dict(os.environ, {
            'DISABLE_BACKGROUND_TASKS': '1', 'AI_REVIEW_SETUP_DISABLED': '1',
            'AI_REVIEW_DB': str(self.root / 'review.db'),
            'AI_REVIEW_MASTER_KEY_FILE': str(key),
            'AI_REVIEW_HOST_POLICY_FILE': '', 'GM_UPGRADE_MAINTENANCE': '0',
        })
        environment.start()
        self.addCleanup(environment.stop)
        import app as application
        with patch.object(application, 'DATABASE_URL', 'sqlite:///' + str(self.root / 'manager.db')), \
                patch.object(application, 'SECRET_KEY', '隔离测试会话密钥' * 4), \
                patch.object(application, 'INIT_PASSWORD', '隔离测试管理员密码'):
            self.app = application.create_app()
        self.app.config['TESTING'] = True
        self.addCleanup(self.close_database)
        self.store = Store()
        self.repo = self.store.save_repo(1, 12, {
            'repo_name': '团队/仓库', 'enabled': False,
            'webhook_secret': self.store.seal(self.secret),
        })
        self.url = '/api/ai-review/webhooks/' + str(self.repo['id'])
        self.raw = ('{\r\n  "repository": {"full_name": "团队/仓库", "id": 12}\r\n}\n').encode()
        self.client = self.app.test_client()

    def close_database(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def send(self, raw=None, signed=None, **kwargs):
        raw = self.raw if raw is None else raw
        signed = raw if signed is None else signed
        signature = hmac.new(self.secret.encode(), signed, hashlib.sha256).hexdigest()
        return self.client.post(self.url, data=raw, content_type='application/json',
            headers={'X-Gitea-Event': 'push', 'X-Gitea-Signature': signature}, **kwargs)

    def assert_receipts(self, count):
        with contextlib.closing(self.store.connect()) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM setup_hook_receipts').fetchone()[0], count)
            self.assertEqual(connection.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_signed_push_survives_all_real_application_guards(self):
        response = self.send()
        self.assertEqual(response.status_code, 202, response.json)
        self.assertEqual(response.json, {'ignored': True})
        self.assert_receipts(1)

    def test_global_instance_guard_does_not_parse_webhook_json(self):
        with patch('flask.Request.get_json', side_effect=AssertionError('全局互斥检查不能读取 Webhook 正文')):
            response = self.send()
        self.assertEqual(response.status_code, 202, response.json)
        self.assert_receipts(1)

    def test_cached_original_bytes_survive_earlier_json_parsing(self):
        @self.app.before_request
        def earlier_json_parser():
            request.get_json(silent=True)
        response = self.send()
        self.assertEqual(response.status_code, 202, response.json)
        self.assert_receipts(1)

    def test_changed_whitespace_is_not_canonicalized_before_verification(self):
        response = self.send(raw=self.raw + b' ', signed=self.raw)
        self.assertEqual(response.status_code, 403, response.json)
        self.assert_receipts(0)

    def test_missing_malformed_and_wrong_signatures_never_record_delivery(self):
        for signature in ('', 'invalid', 'a' * 63, 'A' * 64, '0' * 64):
            with self.subTest(签名格式=signature[:8]):
                response = self.client.post(self.url, data=self.raw, content_type='application/json',
                    headers={'X-Gitea-Event': 'push', 'X-Gitea-Signature': signature})
                self.assertEqual(response.status_code, 403, response.json)
        self.assert_receipts(0)

    def test_exact_size_limit_accepts_original_signed_bytes(self):
        response = self.send(raw=self.raw + b' ' * (self.limit - len(self.raw)))
        self.assertEqual(response.status_code, 202, response.json)
        self.assert_receipts(1)

    def test_oversized_known_length_is_rejected(self):
        response = self.send(raw=self.raw + b' ' * (self.limit + 1 - len(self.raw)))
        self.assertEqual(response.status_code, 413)
        self.assert_receipts(0)

    def test_oversized_stream_without_content_length_is_bounded(self):
        raw = self.raw + b' ' * (self.limit + 65536 - len(self.raw))
        stream = io.BytesIO(raw)
        response = self.send(raw=b'', environ_overrides={
            'CONTENT_LENGTH': '', 'wsgi.input_terminated': True, 'wsgi.input': stream})
        self.assertEqual(response.status_code, 413)
        self.assertLessEqual(stream.tell(), self.limit + 1)
        self.assert_receipts(0)

    def test_signed_stream_without_content_length_is_accepted(self):
        response = self.send(signed=self.raw, raw=b'', environ_overrides={
            'CONTENT_LENGTH': '', 'wsgi.input_terminated': True, 'wsgi.input': io.BytesIO(self.raw)})
        self.assertEqual(response.status_code, 202, response.json)
        self.assert_receipts(1)


class InstanceGuardScopeTests(unittest.TestCase):
    def test_protected_write_routes_still_acquire_the_selected_instance_lock(self):
        from svn_migration.guards import install
        app = Flask(__name__)
        app.add_url_rule('/backup', 'backups.create_backup', lambda: ('', 204), methods=['POST'])
        app.add_url_rule('/restore', 'restore.execute_restore', lambda: ('', 204), methods=['POST'])
        app.add_url_rule('/server/<int:sid>', 'servers.update_server', lambda sid: ('', 204), methods=['PUT'])
        app.add_url_rule('/delete/<int:sid>', 'servers.delete_server', lambda sid: ('', 204), methods=['DELETE'])
        app.add_url_rule('/recover/<int:task_id>', 'restore.recover_restore', lambda task_id: ('', 204), methods=['POST'])
        install(app)
        acquired = []
        @contextlib.contextmanager
        def lock(ident):
            acquired.append(ident)
            yield
        lookup = SimpleNamespace(session=SimpleNamespace(get=lambda model, ident: SimpleNamespace(target_server_id=11)))
        with patch('svn_migration.guards.target_guard', side_effect=lock), patch('svn_migration.guards.db', lookup):
            client = app.test_client()
            self.assertEqual(client.post('/backup', json={'source_server_id': 3}).status_code, 204)
            self.assertEqual(client.post('/restore', json={'target_server_id': 5}).status_code, 204)
            self.assertEqual(client.put('/server/7', json={'name': '测试服务器'}).status_code, 204)
            self.assertEqual(client.delete('/delete/9').status_code, 204)
            self.assertEqual(client.post('/recover/13').status_code, 204)
        self.assertEqual(acquired, [3, 5, 7, 9, 11])

    def test_migration_conflict_still_rejects_protected_write(self):
        from svn_migration.common import MigrationError
        from svn_migration.guards import install
        app = Flask(__name__)
        app.add_url_rule('/backup', 'backups.create_backup', lambda: ('', 204), methods=['POST'])
        install(app)
        with patch('svn_migration.guards.target_guard', side_effect=MigrationError('测试实例正在迁移', 'busy', 409)):
            response = app.test_client().post('/backup', json={'source_server_id': 3})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['code'], 'busy')


if __name__ == '__main__':
    unittest.main()
