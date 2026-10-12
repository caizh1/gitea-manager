"""认证身份与撤权回归；所有目录通信由独立真实目录验收补充。"""
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch
from ldap3.core.exceptions import LDAPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask
from werkzeug.security import generate_password_hash
from models import db, Setting, GiteaServer
from auth import init_auth
from routes.auth_routes import auth_bp
from routes.access_routes import access_bp
from access.authentication import initialize_access, directory_config
from access.errors import AccessError
from access.ldap_directory import DEFAULTS, format_template, validate_config, _identity, _account_active
from access.models import (ManagerAccount, DirectorySource, AccessSession, OrganizationBinding, OrganizationGrant)
from access.secrets import decrypt, encrypt


class AccessAuthenticationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config.update(SECRET_KEY='仅供隔离测试使用的随机会话密钥-abcdef123456', TESTING=True,
            SQLALCHEMY_DATABASE_URI='sqlite:///' + self.directory.name + '/manager.db',
            SQLALCHEMY_TRACK_MODIFICATIONS=False, ACCESS_KEY_FILE=self.directory.name + '/access.key')
        db.init_app(self.app)
        init_auth(self.app)
        self.app.register_blueprint(auth_bp, url_prefix='/api')
        self.app.register_blueprint(access_bp, url_prefix='/api/access')
        with self.app.app_context():
            db.create_all()
            db.session.add(Setting(key='admin_password', value=generate_password_hash('安全测试-password-123')))
            db.session.commit()
            initialize_access()
        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
        self.directory.cleanup()

    def login(self, client=None):
        response = (client or self.client).post('/api/login', json={'password': '安全测试-password-123'}, base_url='https://localhost')
        self.assertEqual(response.status_code, 200, response.json)
        return response

    def ldap_source(self, simple=False):
        with self.app.app_context():
            config = dict(DEFAULTS) | {'host': 'directory.test', 'user_base': 'dc=test', 'filter': '(uid=%s)',
                                      'type': 'simple' if simple else 'binddn', 'user_dn': 'uid=%s,dc=test'}
            source = DirectorySource(name='测试目录', enabled=True, config_json=json.dumps(config), version=1)
            db.session.add(source)
            db.session.commit()
            return source.id

    def ldap_login(self, subject='entryUUID:11111111-1111-1111-1111-111111111111', username='alice'):
        identity = {'subject': subject, 'username': username, 'display_name': '测试用户', 'email': 'alice@example.test', 'dn': 'uid=alice,dc=test'}
        with patch('access.authentication.ldap_directory.authenticate', return_value=identity):
            response = self.client.post('/api/login', json={'source': 'ldap', 'username': username, 'password': '测试密码'}, base_url='https://localhost')
        self.assertEqual(response.status_code, 200, response.json)
        return response

    def test_legacy_forged_user_id_does_not_authenticate(self):
        with self.client.session_transaction() as session:
            session['_user_id'] = '1'
            session['_fresh'] = True
        self.assertFalse(self.client.get('/api/session').json['authenticated'])
        self.assertEqual(self.client.get('/api/access/accounts').status_code, 401)

    def test_session_random_token_and_server_side_revocation(self):
        self.login()
        payload = self.client.get('/api/session', base_url='https://localhost').json
        self.assertTrue(payload['authenticated'])
        self.assertTrue(payload['user']['is_platform_admin'])
        with self.client.session_transaction(base_url='https://localhost') as session:
            token = session['_user_id']
            self.assertEqual(len(token), 43)
        with self.app.app_context():
            record = AccessSession.query.one()
            self.assertNotEqual(record.token_hash, token)
            record.revoked_at = time.time()
            db.session.commit()
        self.assertFalse(self.client.get('/api/session', base_url='https://localhost').json['authenticated'])

    def test_logout_invalidates_copied_cookie(self):
        self.login()
        copied = self.client.get_cookie('session').value
        self.client.post('/api/logout', base_url='https://localhost')
        attacker = self.app.test_client()
        attacker.set_cookie('session', copied)
        self.assertFalse(attacker.get('/api/session', base_url='https://localhost').json['authenticated'])

    def test_idle_expiration_and_auth_version(self):
        self.login()
        with self.app.app_context():
            record = AccessSession.query.one()
            record.last_seen_at = time.time() - 1801
            db.session.commit()
        self.assertFalse(self.client.get('/api/session', base_url='https://localhost').json['authenticated'])
        self.login()
        with self.app.app_context():
            ManagerAccount.query.one().auth_version += 1
            db.session.commit()
        self.assertFalse(self.client.get('/api/session', base_url='https://localhost').json['authenticated'])

    def test_directory_user_starts_unprivileged_and_cannot_manage_access(self):
        self.ldap_source()
        response = self.ldap_login()
        self.assertFalse(response.json['user']['is_platform_admin'])
        self.assertEqual(response.json['organizations'], [])
        self.assertEqual(self.client.get('/api/access/accounts', base_url='https://localhost').status_code, 403)
        self.assertEqual(self.client.put('/api/access/directory', json={}, base_url='https://localhost').status_code, 403)

    def test_ldap_requires_https_and_does_not_fallback(self):
        self.ldap_source()
        response = self.client.post('/api/login', json={'source': 'ldap', 'username': 'admin', 'password': '安全测试-password-123'})
        self.assertEqual(response.status_code, 403)
        with patch('access.authentication.ldap_directory.authenticate', side_effect=AccessError('绑定密码可能含敏感信息', 503)):
            response = self.client.post('/api/login', json={'source': 'ldap', 'username': 'admin', 'password': '安全测试-password-123'}, base_url='https://localhost')
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('敏感', response.json['error'])

    def test_directory_disable_revokes_existing_session(self):
        self.ldap_source()
        self.ldap_login()
        with self.app.app_context():
            DirectorySource.query.one().enabled = False
            db.session.commit()
        self.assertFalse(self.client.get('/api/session', base_url='https://localhost').json['authenticated'])

    def test_existing_directory_partial_update_always_requires_https(self):
        self.login()
        self.ldap_source()
        for change in ({'filter': '(mail=%s)'}, {'security_protocol': 'unencrypted'}, {'enabled': False}):
            with self.subTest(change=change):
                response = self.client.put('/api/access/directory', json=change, base_url='http://localhost')
                self.assertEqual(response.status_code, 403)
        with self.app.app_context():
            source = DirectorySource.query.one()
            self.assertTrue(source.enabled)
            self.assertEqual(json.loads(source.config_json)['filter'], '(uid=%s)')
            self.assertEqual(source.version, 1)

    def test_simple_auth_requires_password_again_after_five_minutes(self):
        self.ldap_source(simple=True)
        self.ldap_login()
        with self.app.app_context():
            AccessSession.query.one().directory_checked_at = time.time() - 301
            db.session.commit()
        with patch('access.authentication.ldap_directory.revalidate') as query:
            self.assertFalse(self.client.get('/api/session', base_url='https://localhost').json['authenticated'])
        query.assert_not_called()

    def test_bind_source_revalidates_and_fails_closed(self):
        self.ldap_source()
        self.ldap_login()
        with self.app.app_context():
            AccessSession.query.one().directory_checked_at = time.time() - 301
            db.session.commit()
        with patch('access.authentication.ldap_directory.revalidate', side_effect=AccessError('目录离线', 503)):
            self.assertFalse(self.client.get('/api/session', base_url='https://localhost').json['authenticated'])

    def test_same_login_name_new_stable_identity_does_not_inherit_role(self):
        self.ldap_source()
        first = self.ldap_login().json['user']['id']
        with self.app.app_context():
            db.session.get(ManagerAccount, first).is_platform_admin = True
            db.session.commit()
        second = self.ldap_login(subject='entryUUID:22222222-2222-2222-2222-222222222222').json['user']
        self.assertNotEqual(second['id'], first)
        self.assertFalse(second['is_platform_admin'])

    def test_password_change_rotates_session_preserves_emergency_account(self):
        self.login()
        response = self.client.post('/api/access/password', json={'current_password': '安全测试-password-123', 'new_password': '新密码-password-456'}, base_url='https://localhost')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(response.json['user']['must_change_password'])
        identifier = response.json['user']['id']
        self.assertEqual(self.client.patch(f'/api/access/accounts/{identifier}', json={'enabled': False}, base_url='https://localhost').status_code, 409)
        with self.app.app_context():
            self.assertEqual(AccessSession.query.filter_by(revoked_at=None).count(), 1)

    def test_password_encryption_survives_session_secret_rotation_and_rejects_missing_key(self):
        with self.app.app_context():
            ciphertext = encrypt('目录查询密码')
            self.assertNotIn('目录查询密码', ciphertext)
            self.app.config['SECRET_KEY'] = '不同会话密钥'
            self.assertEqual(decrypt(ciphertext), '目录查询密码')
            os.unlink(self.app.config['ACCESS_KEY_FILE'])
            with self.assertRaises(AccessError):
                decrypt(ciphertext)
            self.assertFalse(os.path.exists(self.app.config['ACCESS_KEY_FILE']))

    def test_directory_password_not_returned_or_written_plaintext(self):
        self.login()
        payload = dict(DEFAULTS) | {'name': '测试目录', 'host': 'directory.test', 'user_base': 'dc=test',
            'filter': '(uid=%s)', 'bind_dn': 'cn=service,dc=test', 'bind_password': '独立查询密码', 'enabled': True}
        response = self.client.put('/api/access/directory', json=payload, base_url='https://localhost')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertNotIn('bind_password', response.json)
        self.assertTrue(response.json['bind_password_set'])
        with self.app.app_context():
            row = DirectorySource.query.one()
            self.assertNotIn('独立查询密码', row.config_json)
            self.assertNotIn('独立查询密码', row.password_ciphertext)
            self.assertEqual(directory_config(row)['bind_password'], '独立查询密码')

    def test_directory_diagnostics_report_actual_failed_stage_without_raw_secret(self):
        self.login()
        secret = '禁止回显的目录原始错误和密码'
        config = dict(DEFAULTS) | {'host':'directory.test','user_base':'dc=test','filter':'(uid=%s)',
            'bind_dn':'cn=reader,dc=test','bind_password':secret,'attribute_username':'uid'}
        entry = {'dn':'uid=alice,dc=test','attributes':{'entryUUID':['11111111-1111-1111-1111-111111111111'],'uid':['alice']}}
        for phase in ('connect','bind','search','authenticate','attributes','groups'):
            with self.subTest(phase=phase), ExitStack() as patches:
                conn = MagicMock(); conn.rebind.return_value = True
                patches.enter_context(patch('access.ldap_directory.ldap3.Connection',return_value=conn))
                query = patches.enter_context(patch('access.ldap_directory._search',return_value=[entry]))
                if phase=='connect':conn.open.side_effect=LDAPException(secret)
                elif phase=='bind':conn.rebind.side_effect=LDAPException(secret)
                elif phase=='search':query.side_effect=LDAPException(secret)
                elif phase=='authenticate':conn.rebind.side_effect=[True,LDAPException(secret)]
                elif phase=='attributes':patches.enter_context(patch('access.ldap_directory._identity',side_effect=AccessError('稳定身份不可用',401)))
                else:patches.enter_context(patch('access.ldap_directory._group_allowed',return_value=False))
                response=self.client.post('/api/access/directory/test-login',json={
                    'configuration':config,'username':'alice','password':secret},base_url='https://localhost')
                self.assertEqual(response.status_code,422,response.json)
                self.assertFalse(response.json['ok'])
                self.assertEqual(response.json['steps'][-1]['stage'],phase)
                self.assertFalse(response.json['steps'][-1]['ok'])
                self.assertTrue(all(item['ok'] for item in response.json['steps'][:-1]))
                self.assertNotIn(secret,json.dumps(response.json,ensure_ascii=False))
                self.assertTrue(self.client.get('/api/session',base_url='https://localhost').json['authenticated'])
        for phase in ('connect','bind'):
            with self.subTest(connection_phase=phase):
                conn=MagicMock()
                getattr(conn,'open' if phase=='connect' else 'rebind').side_effect=LDAPException(secret)
                with patch('access.ldap_directory.ldap3.Connection',return_value=conn):
                    response=self.client.post('/api/access/directory/test-connection',
                        json={'configuration':config},base_url='https://localhost')
                self.assertEqual(response.status_code,422)
                self.assertEqual(response.json['steps'][-1]['stage'],phase)
                self.assertNotIn(secret,json.dumps(response.json,ensure_ascii=False))

    def test_login_throttle_shared_across_clients(self):
        for index in range(10):
            response = self.app.test_client().post('/api/login', json={'password': '错误密码'})
            self.assertEqual(response.status_code, 401, index)
        response = self.app.test_client().post('/api/login', json={'password': '安全测试-password-123'})
        self.assertEqual(response.status_code, 429)


class DirectorySemanticsTest(unittest.TestCase):
    def test_go_repeated_filter_arguments_and_invalid_format(self):
        self.assertEqual(format_template('(|(uid=%s)(mail=%[1]s))', 'alice'), '(|(uid=alice)(mail=alice))')
        self.assertEqual(format_template('(|(uid=%[1]s)(mail=%[1]s))', 'alice'), '(|(uid=alice)(mail=alice))')
        for invalid in ('(uid=%d)', '(|(uid=%s)(mail=%s))', '(uid=%[2]s)'):
            with self.assertRaises(AccessError):
                format_template(invalid, 'alice')

    def test_stable_id_required_and_ad_byte_order(self):
        config = dict(DEFAULTS)
        with self.assertRaises(AccessError):
            _identity({'dn': 'cn=alice', 'attributes': {}}, config, 'alice')
        record = {'dn': 'cn=alice', 'raw_attributes': {'objectGUID': [bytes.fromhex('33221100554477668899aabbccddeeff')]}}
        self.assertEqual(_identity(record, config, 'alice')['subject'], 'objectGUID:00112233-4455-6677-8899-aabbccddeeff')

    def test_ad_revalidation_rejects_disabled_locked_expired_and_unknown_state(self):
        identity = {'subject': 'objectGUID:00112233-4455-6677-8899-aabbccddeeff'}
        entry = {'attributes': {'userAccountControl': ['512'], 'accountExpires': ['0']}}
        self.assertTrue(_account_active(entry, identity))
        for attributes in ({'userAccountControl': ['514']},
                           {'userAccountControl': ['512'], 'msDS-User-Account-Control-Computed': ['16']},
                           {'userAccountControl': ['512'], 'msDS-User-Account-Control-Computed': ['8388608']},
                           {'userAccountControl': ['512'], 'accountExpires': ['1']}):
            self.assertFalse(_account_active({'attributes': attributes}, identity))
        with self.assertRaises(AccessError):
            _account_active({'attributes': {}}, identity)

    def test_rejects_unimplemented_privilege_mapping_and_invalid_config(self):
        baseline = dict(DEFAULTS) | {'host': 'directory.test', 'user_base': 'dc=test', 'filter': '(uid=%s)'}
        for update in ({'admin_filter': '(uid=*)'}, {'skip_verify': 'false'}, {'port': 0}, {'security_protocol': 'fallback'}):
            with self.assertRaises(AccessError):
                validate_config(baseline | update)
        self.assertEqual(validate_config(baseline)['security_protocol'], 'ldaps')


if __name__ == '__main__':
    unittest.main()
