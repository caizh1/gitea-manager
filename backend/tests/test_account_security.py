"""本人改密的真实 SQLite、CSRF、会话失效与双请求竞争回归。"""
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from werkzeug.security import check_password_hash
from access.authentication import initialize_access
from access.control import install
from access.models import AccessAudit, AccessSession, ManagerAccount
from models import db, Setting
from tests import test_access_auth as fixture


class AccountSecurityTests(unittest.TestCase):
    old_password = '安全测试-password-123'
    new_password = '新的安全-password-456'

    def setUp(self):
        self.fixture = fixture.AccessAuthenticationTest()
        self.fixture.setUp()
        self.app = self.fixture.app
        install(self.app)
        self.client = self.app.test_client()

    def tearDown(self):
        self.fixture.tearDown()
        self.fixture.doCleanups()

    def post(self, client, path, data, **kwargs):
        headers = {'X-CSRF-Token': client.get('/api/session', base_url='https://localhost').json['csrf_token']}
        headers.update(kwargs.pop('headers', {}))
        return client.post('/api' + path, json=data, headers=headers, base_url='https://localhost', **kwargs)

    def login(self, client=None, password=None):
        client = client or self.client
        response = self.post(client, '/login', {'password': password or self.old_password})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def change(self, client=None, **values):
        return self.post(client or self.client, '/access/password',
                         {'current_password': self.old_password, 'new_password': self.new_password, **values})

    def password_hash(self):
        with self.app.app_context():
            return ManagerAccount.query.filter_by(source_id=None).one().password_hash

    def test_local_source_is_explicit_even_after_initial_password_change(self):
        payload = self.login()
        self.assertEqual(payload['user']['auth_source'], 'local')
        self.assertFalse(payload['user']['must_change_password'])
        self.assertEqual(self.change().status_code, 200)

    def test_success_rotates_csrf_and_revokes_every_previous_cookie(self):
        old_session = self.login()
        second = self.app.test_client()
        self.login(second)
        copied = self.client.get_cookie('session').value
        response = self.change()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertNotEqual(response.json['csrf_token'], old_session['csrf_token'])
        self.assertTrue(response.json['authenticated'])
        self.assertTrue(self.client.get('/api/session', base_url='https://localhost').json['authenticated'])
        attacker = self.app.test_client()
        attacker.set_cookie('session', copied)
        for client in (second, attacker):
            self.assertFalse(client.get('/api/session', base_url='https://localhost').json['authenticated'])
        with self.app.app_context():
            self.assertEqual(AccessSession.query.filter_by(revoked_at=None).count(), 1)
            self.assertEqual(ManagerAccount.query.one().auth_version, 2)

    def test_old_password_fails_and_new_password_survives_reinitialization(self):
        self.login()
        self.assertEqual(self.change().status_code, 200)
        with self.app.app_context():
            initialize_access()
            self.assertEqual(db.session.get(Setting, 'admin_password').value, ManagerAccount.query.one().password_hash)
        another = self.app.test_client()
        self.assertEqual(self.post(another, '/login', {'password': self.old_password}).status_code, 401)
        self.login(another, self.new_password)

    def test_wrong_current_password_leaves_password_and_session_unchanged(self):
        self.login()
        before = self.password_hash()
        self.assertEqual(self.change(current_password='错误密码').status_code, 403)
        self.assertEqual(self.password_hash(), before)
        self.assertTrue(self.client.get('/api/session', base_url='https://localhost').json['authenticated'])

    def test_invalid_password_types_lengths_and_reuse_never_change_hash(self):
        self.login()
        before = self.password_hash()
        for value in ('', 'short', self.old_password, '长' * 4097, None, [], True, 123):
            with self.subTest(输入类型=type(value).__name__, 长度=len(value) if isinstance(value, str) else None):
                self.assertEqual(self.change(new_password=value).status_code, 400)
                self.assertEqual(self.password_hash(), before)
        self.assertEqual(self.change(current_password=[]).status_code, 403)

    def test_password_can_include_unicode_and_significant_spaces(self):
        self.login()
        password = '  新的安全密码-🔐-123  '
        self.assertEqual(self.change(new_password=password).status_code, 200)
        self.assertTrue(check_password_hash(self.password_hash(), password))
        self.assertFalse(check_password_hash(self.password_hash(), password.strip()))

    def test_anonymous_and_missing_csrf_cannot_change_password(self):
        before = self.password_hash()
        self.assertEqual(self.change().status_code, 401)
        self.login()
        response = self.client.post('/api/access/password', json={
            'current_password': self.old_password, 'new_password': self.new_password}, base_url='https://localhost')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.password_hash(), before)

    def test_cross_origin_and_previous_csrf_cannot_change_password(self):
        payload = self.login()
        before = self.password_hash()
        response = self.post(self.client, '/access/password', {}, headers={'Origin': 'https://external.invalid'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.password_hash(), before)
        self.assertEqual(self.change().status_code, 200)
        response = self.post(self.client, '/access/password', {}, headers={'X-CSRF-Token': payload['csrf_token']})
        self.assertEqual(response.status_code, 403)

    def test_initial_gate_can_change_password_without_host_ip(self):
        with self.app.app_context():
            ManagerAccount.query.one().must_change_password = True
            db.session.commit()
        self.assertTrue(self.login()['needs_config'])
        self.assertEqual(self.client.get('/api/access/accounts', base_url='https://localhost').status_code, 403)
        response = self.change()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(response.json['user']['must_change_password'])
        self.assertEqual(self.client.get('/api/access/accounts', base_url='https://localhost').status_code, 200)

    def test_directory_account_including_platform_admin_cannot_change_local_password(self):
        self.fixture.ldap_source()
        self.client = self.fixture.client
        self.client.get('/api/session', base_url='https://localhost')
        identity = {'subject': 'entryUUID:test', 'username': 'admin', 'display_name': '目录管理员',
                    'email': 'directory@example.test', 'dn': 'uid=admin,dc=test'}
        with patch('access.authentication.ldap_directory.authenticate', return_value=identity):
            response = self.post(self.client, '/login', {'source': 'ldap', 'username': 'admin', 'password': '目录测试'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['user']['auth_source'], 'ldap')
        before = self.password_hash()
        for admin in (False, True):
            with self.app.app_context():
                ManagerAccount.query.filter(ManagerAccount.source_id.isnot(None)).one().is_platform_admin = admin
                db.session.commit()
            response = self.change()
            self.assertEqual(response.status_code, 403, response.json)
            self.assertIn('公司目录', response.json['error'])
            self.assertEqual(self.password_hash(), before)

    def test_audit_and_response_never_contain_password_or_hash(self):
        self.login()
        response = self.change()
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            audit = AccessAudit.query.filter_by(action='修改本地应急管理员密码').one()
            text = response.get_data(as_text=True) + json.dumps([audit.actor_name, audit.detail_json, audit.target])
            for secret in (self.old_password, self.new_password, self.password_hash()):
                self.assertNotIn(secret, text)

    def test_two_concurrent_changes_only_one_commits(self):
        clients = [self.client, self.app.test_client()]
        tokens = [self.login(client)['csrf_token'] for client in clients]
        barrier = threading.Barrier(2)
        def synchronized_check(encoded, raw):
            result = check_password_hash(encoded, raw)
            barrier.wait(timeout=15)
            return result
        def request(index):
            return clients[index].post('/api/access/password', json={
                'current_password': self.old_password, 'new_password': self.new_password + str(index)},
                headers={'X-CSRF-Token': tokens[index]}, base_url='https://localhost').status_code
        with patch('access.admin.check_password_hash', side_effect=synchronized_check):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(request, range(2)))
        self.assertEqual(sorted(outcomes), [200, 409])
        winner = outcomes.index(200)
        self.assertTrue(check_password_hash(self.password_hash(), self.new_password + str(winner)))
        with self.app.app_context():
            self.assertEqual(ManagerAccount.query.one().auth_version, 2)
            self.assertEqual(AccessSession.query.filter_by(revoked_at=None).count(), 1)
            self.assertEqual(AccessAudit.query.filter_by(action='修改本地应急管理员密码').count(), 1)


if __name__ == '__main__':
    unittest.main()
