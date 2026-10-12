"""自动接入的权限、持久主机配置、重复提交与敏感回执验证。"""
import concurrent.futures
import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from flask import Flask, g
from tests.access_helpers import init_test_auth as init_auth, login_platform
from models import db, GiteaServer
from routes.ai_review_routes import ai_review_bp
from ai_review.common import ReviewError, endpoint
from ai_review.host_policy import ensure, approve, read
from ai_review.store import Store
from ai_review.setup_service import initialize, submit, public_job, ensure_bot, resource, verify_delivery


class SetupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        key = self.root / '主密钥'; key.write_bytes(Fernet.generate_key())
        self.env = patch.dict(os.environ, {'AI_REVIEW_DB': str(self.root/'review.db'), 'AI_REVIEW_MASTER_KEY_FILE': str(key),
            'AI_REVIEW_HOST_POLICY_FILE': str(self.root/'hosts.json'), 'AI_REVIEW_GITEA_HOSTS': 'legacy.internal',
            'AI_REVIEW_MODEL_HOSTS': 'old-model.internal', 'AI_REVIEW_WEBHOOK_HOSTS': '', 'AI_REVIEW_SETUP_DISABLED': '1'})
        self.env.start(); self.addCleanup(self.env.stop); ensure()
        self.app = Flask(__name__); self.app.secret_key = '仅测试会话密钥'
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + str(self.root/'manager.db'), AI_REVIEW_DB=str(self.root/'review.db'), TESTING=True)
        db.init_app(self.app); init_auth(self.app); self.app.register_blueprint(ai_review_bp, url_prefix='/api/ai-review')
        self.context = self.app.app_context(); self.context.push(); self.addCleanup(self.context.pop)
        db.create_all(); self.addCleanup(db.session.remove)
        target = GiteaServer(name='已有内网实例', host='gitea.internal', ssh_user='root', gitea_url='http://gitea.internal:3000',
                            gitea_port=3000, api_token='仅测试管理凭据')
        db.session.add(target); db.session.commit(); self.target = target
        self.store = Store(self.root/'review.db'); initialize(self.store)
        self.client = self.app.test_client()
        self.payload = {'server_id':target.id, 'config_revision':self.store.config()['revision'], 'api_base_url': 'http://model.internal/v1', 'api_key': '仅测试模型凭据',
                        'repositories': [{'server_id':target.id, 'repo_id':12, 'name':'团队/仓库'}]}

    def login(self):
        g.pop('_login_user', None)
        login_platform(self.client)
        return {'X-Review-CSRF': self.client.get('/api/ai-review/csrf').json['token']}

    def test_auth_and_csrf_reject_before_configuration(self):
        self.assertEqual(self.client.post('/api/ai-review/setup', json=self.payload).status_code, 401)
        self.login()
        self.assertEqual(self.client.post('/api/ai-review/setup', json=self.payload).status_code, 403)
        self.assertNotIn('model.internal', read()['AI_REVIEW_MODEL_HOSTS'])

    def test_cross_origin_rejected(self):
        headers = self.login(); headers['Origin'] = 'https://其他站点.invalid'
        self.assertEqual(self.client.post('/api/ai-review/setup', json=self.payload, headers=headers).status_code, 403)

    def test_one_submission_persists_no_public_key(self):
        headers = self.login()
        response = self.client.post('/api/ai-review/setup', json=self.payload, headers=headers)
        self.assertEqual(response.status_code, 202)
        self.assertNotIn('仅测试模型凭据', response.get_data(as_text=True))
        self.assertEqual(self.client.post('/api/ai-review/setup', json=self.payload, headers=headers).status_code, 400)
        self.assertIn('model.internal', read()['AI_REVIEW_MODEL_HOSTS'])
        self.assertIn('gitea.internal', read()['AI_REVIEW_GITEA_HOSTS'])
        self.assertNotIn('仅测试模型凭据'.encode(), (self.root/'review.db').read_bytes())
        self.assertEqual(public_job(self.store)['status'], 'pending')

    def test_untrusted_input_cannot_select_nonexistent_server(self):
        payload = dict(self.payload, repositories=[{'server_id':9999, 'repo_id':12, 'name':'团队/仓库'}])
        with self.assertRaises(ReviewError): submit(self.store, payload, 'http://manager.internal')

    def test_unsafe_model_addresses_do_not_expand_policy(self):
        for url in ('file:///etc/passwd', 'http://key@model.internal', 'http://model.internal?q=1', 'http://model.internal/#secret', 'http://bad\\host'):
            with self.subTest(地址=url), self.assertRaises(ReviewError): submit(self.store, dict(self.payload, api_base_url=url), 'http://manager.internal')
        self.assertNotIn('model.internal', read()['AI_REVIEW_MODEL_HOSTS'])

    def test_concurrent_host_updates_are_visible_without_restart(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: approve({'AI_REVIEW_MODEL_HOSTS':[f'https://model{i}.internal/v1']}), range(12)))
        self.assertEqual(len(read()['AI_REVIEW_MODEL_HOSTS']), 13)
        self.assertEqual(endpoint('https://model11.internal/v1', 'AI_REVIEW_MODEL_HOSTS'), 'https://model11.internal/v1')
        self.assertIn('legacy.internal', read()['AI_REVIEW_GITEA_HOSTS'])
        self.assertEqual((self.root/'hosts.json').stat().st_mode & 0o777, 0o600)

    def test_broken_host_file_fails_closed(self):
        (self.root/'hosts.json').write_text('{损坏')
        with self.assertRaises(ReviewError): endpoint('http://legacy.internal', 'AI_REVIEW_GITEA_HOSTS')

    def test_probe_requires_unexpired_random_nonce(self):
        token = 'a'*48
        with self.store.tx() as c: c.execute('INSERT INTO setup_probes VALUES (?,?,0)', (token, __import__('time').time()+30))
        self.assertEqual(self.client.get('/api/ai-review/setup-probe/'+token).text, hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(self.client.get('/api/ai-review/setup-probe/'+'b'*48).status_code, 404)
        with self.store.tx() as c: c.execute('UPDATE setup_probes SET expires=0')
        self.assertEqual(self.client.get('/api/ai-review/setup-probe/'+token).status_code, 404)

    def test_probe_does_not_disclose_repository_or_configuration(self):
        response = self.client.get('/api/ai-review/setup-probe/'+'b'*48)
        self.assertNotIn('已有内网实例', response.text)
        self.assertEqual(self.client.get('/api/ai-review/setup').status_code, 401)

    def test_signed_test_hook_proves_delivery_without_review(self):
        repo = self.store.save_repo(self.target.id,12,{'repo_name':'团队/仓库','enabled':False,'webhook_secret':self.store.seal('测试签名秘密')})
        raw = json.dumps({'repository':{'id':12,'full_name':'团队/仓库'}}, ensure_ascii=False).encode()
        headers = {'X-Gitea-Event':'push','X-Gitea-Signature':hmac.new('测试签名秘密'.encode(),raw,hashlib.sha256).hexdigest()}
        response = self.client.post('/api/ai-review/webhooks/'+str(repo['id']),data=raw,headers=headers)
        self.assertEqual(response.status_code,202)
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM setup_hook_receipts').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],0)

    def test_invalid_signature_cannot_fake_delivery(self):
        repo = self.store.save_repo(self.target.id,12,{'repo_name':'团队/仓库','enabled':False,'webhook_secret':self.store.seal('测试签名秘密')})
        response = self.client.post('/api/ai-review/webhooks/'+str(repo['id']),json={'repository':{'id':12,'full_name':'团队/仓库'}},headers={'X-Gitea-Signature':'a'*64})
        self.assertEqual(response.status_code,403)
        with contextlib.closing(self.store.connect()) as c: self.assertEqual(c.execute('SELECT count(*) FROM setup_hook_receipts').fetchone()[0],0)

    def test_bot_identity_and_token_are_reused(self):
        user = {}; commands = []
        class Admin:
            def request(_, method, path, **kwargs): return user.copy() or None
        class Installer:
            custom='/data/gitea'; config='/data/gitea/conf/app.ini'
            def exec(_, args):
                commands.append(args)
                if 'create' in args: user.update(id=81, login=args[args.index('--username')+1], full_name=args[args.index('--fullname')+1], is_admin=False)
                return 'a'*40 if 'generate-access-token' in args else ''
        first = ensure_bot(self.store,self.target,Installer(),Admin())
        second = ensure_bot(self.store,self.target,Installer(),Admin())
        self.assertEqual(first['id'],second['id']); self.assertEqual(first['token'],second['token'])
        self.assertEqual(sum('generate-access-token' in c for c in commands),1)
        self.assertFalse(any('a'*40 in c for c in commands))
        self.assertEqual(sum('create' in c for c in commands),1)

    def test_preexisting_unowned_account_is_not_adopted(self):
        class Admin:
            def request(_, *args, **kwargs): return {'id':81,'full_name':'别人的账号','is_admin':False}
        class Installer:
            custom='/data/gitea'; config='/data/gitea/conf/app.ini'
            def exec(_, args): raise AssertionError('不应操作已有账号')
        with self.assertRaises(ReviewError): ensure_bot(self.store,self.target,Installer(),Admin())

    def test_real_test_event_subscription_is_restored(self):
        state = {'events': ['pull_request'], 'sent': False}
        store = self.store
        class Admin:
            def request(_, method, path, data=None, **kwargs):
                if method == 'GET': return {'events': list(state['events'])}
                if method == 'PATCH': state['events'] = data['events']; return
                if method == 'POST':
                    if 'push' not in state['events']: return
                    state['sent'] = True
                    with store.tx() as c: c.execute('INSERT INTO setup_hook_receipts VALUES (1,?)', (__import__('time').time(),))
        verify_delivery(self.store, Admin(), '/repos/团队/仓库', 1, 5, 'main')
        self.assertTrue(state['sent']); self.assertEqual(state['events'], ['pull_request'])

    def test_failed_delivery_still_restores_event_subscription(self):
        state = {'events': ['pull_request']}
        class Admin:
            def request(_, method, path, data=None, **kwargs):
                if method == 'GET': return {'events': list(state['events'])}
                if method == 'PATCH': state['events'] = data['events']; return
                raise ReviewError('模拟测试投递明确失败')
        with self.assertRaises(ReviewError): verify_delivery(self.store, Admin(), '/repos/团队/仓库', 1, 5, 'main')
        self.assertEqual(state['events'], ['pull_request'])


if __name__ == '__main__': unittest.main()


class ConnectionFailureTests(unittest.TestCase):
    setUp = SetupTests.setUp
    def test_connection_failure_stops_following_repositories_and_exposes_safe_details(self):
        from ai_review.setup_service import execute
        from ai_review.setup_transport import AccessError
        payload = dict(self.payload)
        payload['repositories'] = [dict(payload['repositories'][0]), dict(payload['repositories'][0], repo_id=13, name='团队/第二仓库')]
        with self.app.app_context():
            created = submit(self.store, payload, 'http://manager.internal')
            with self.store.connect() as c: job = c.execute('SELECT * FROM setup_jobs WHERE id=?', (created['id'],)).fetchone()
            with patch('ai_review.setup_service.verify_model'), patch('ai_review.assets.install.Installer', side_effect=AccessError('SSH_RESET', 'SSH 协商', 'SSH 连接中断')) as installer:
                execute(self.store, job)
            result = public_job(self.store, created['id'])
        self.assertEqual(installer.call_count, 1)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual([r['error_code'] for r in result['detail']['results']], ['SSH_RESET', 'SETUP_STOPPED'])
        self.assertEqual(result['detail']['results'][0]['step'], '准备容器连接')
        self.assertNotIn('仅测试模型凭据', json.dumps(result, ensure_ascii=False))

    def test_callback_does_not_hide_ssh_failure(self):
        from ai_review.setup_service import callback_probe
        from ai_review.setup_transport import AccessError
        from unittest.mock import Mock
        approve({'AI_REVIEW_WEBHOOK_HOSTS': ['http://manager.internal']})
        with self.assertRaises(AccessError) as caught:
            callback_probe(self.store, Mock(exec=Mock(side_effect=AccessError('SSH_RESET', '执行远程命令', '断线', unknown=True))), 'http://manager.internal')
        self.assertEqual(caught.exception.code, 'SSH_RESET')
