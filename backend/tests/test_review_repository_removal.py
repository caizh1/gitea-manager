"""接入移除的状态、历史、并发与远端身份边界回归。"""
import json
import os
import concurrent.futures
import copy
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from flask import Flask
from ai_review.common import ReviewError, PublishUnknown
from ai_review.repository_removal import remove_repository
from ai_review.store import Store
from routes.ai_review_routes import ai_review_bp
from tests.access_helpers import init_test_auth, login_platform
from tests.test_ai_review_webhook_binding import GiteaFixture


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        key = root / 'master.key'
        key.write_bytes(Fernet.generate_key())
        env = patch.dict(os.environ, {'AI_REVIEW_MASTER_KEY_FILE': str(key), 'AI_REVIEW_GITEA_HOSTS': '127.0.0.1'})
        env.start(); self.addCleanup(env.stop)
        self.state = Store(root / 'review.db')
        self.state.save_config({'revision': 1, 'enabled': True, 'debounce_seconds': 0, 'model': '测试模型'})
        self.data = dict(enabled=True, repo_name='team/project', server_name='隔离测试', gitea_url='http://127.0.0.1:3000',
                         bot_token=self.state.seal('隔离测试凭据'), webhook_secret=self.state.seal('隔离测试签名'),
                         bot_id=9, bot_name='review-bot', hook_status='已安装并回读验证')
        self.repo = self.state.save_repo(1, 7, self.data)
        self.callback = f"http://127.0.0.1:5000/api/ai-review/webhooks/{self.repo['id']}"
        self.state.repo_status(self.repo['id'], hook_id=11, webhook_url=self.callback)
        self.target = SimpleNamespace(gitea_url=self.data['gitea_url'], api_token='测试管理凭据')
        self.network = patch('ai_review.repository_removal.Gitea')
        self.client_class = self.network.start(); self.addCleanup(self.network.stop)
        self.client = self.client_class.return_value
        self.hook = {'id': 11, 'config': {'url': self.callback}}
        self.client.request.side_effect = [self.hook, None, None]

    def remove(self, revision=None):
        return remove_repository(self.state, self.repo['id'], self.repo['revision'] if revision is None else revision, self.target)

    def test_removal_retains_jobs_clears_secrets_and_allows_fresh_identity(self):
        job = self.state.enqueue(self.repo['id'], 1, {'head': 'a', 'base': 'b'})
        removed = self.remove()
        self.assertTrue(removed['removal_complete'])
        self.assertFalse(removed['enabled'])
        self.assertFalse(removed['bot_token_configured'])
        self.assertEqual(self.state.job(job)['status'], 'superseded')
        self.assertIsNone(self.state.enqueue(self.repo['id'], 2, {'head': 'a', 'base': 'b'}))
        current = self.state.save_repo(1, 7, self.data)
        self.assertNotEqual(current['id'], self.repo['id'])
        self.assertEqual(self.state.job(job)['repository'], self.repo['id'])
        self.assertEqual(self.state.repo(self.repo['id'])['repo_id'], 7)
        self.assertEqual([c.args[0] for c in self.client.request.call_args_list], ['GET', 'DELETE', 'GET'])

    def test_retry_after_completion_does_not_touch_remote(self):
        self.remove(); self.client.reset_mock()
        self.assertTrue(self.remove()['removal_complete'])
        self.client.request.assert_not_called()

    def test_running_and_uncertain_publications_block_before_any_change(self):
        job = self.state.enqueue(self.repo['id'], 1, {'head': 'a', 'base': 'b'})
        for status in ('running', 'publishing', 'recovering', 'publish_unknown', 'cancelling'):
            with self.subTest(状态=status):
                with self.state.tx() as connection:
                    connection.execute('UPDATE jobs SET status=? WHERE id=?', (status, job))
                with self.assertRaisesRegex(ReviewError, '审查工作区'):
                    self.remove()
                self.assertTrue(self.state.repo(self.repo['id'])['enabled'])
                self.client.request.assert_not_called()

    def test_pending_webhook_binding_blocks_before_archival_or_remote_delete(self):
        for phase in ('planned', 'creating', 'verifying', 'cleanup'):
            with self.subTest(阶段=phase):
                self.state.repo_status(self.repo['id'], webhook_binding={'phase': phase})
                with self.assertRaisesRegex(ReviewError, '通知创建或清理尚待核对'):
                    self.remove()
                current = self.state.repo(self.repo['id'])
                self.assertTrue(current['enabled'])
                self.assertFalse(current.get('access_archived'))
                self.client_class.assert_not_called()

    def failed_empty_onboarding(self, fail_create=None):
        from routes.ai_review_routes import ensure_webhook
        self.state.repo_status(self.repo['id'], hook_id=None, webhook_url=None, hook_status='未安装')
        client = GiteaFixture()
        client.prefix = '/repos/team/project'
        client.hooks[3]['config']['url'] = 'http://unrelated.invalid/hook'
        client.fail_create = fail_create
        client.repository = lambda: {'id': 7, 'full_name': 'team/project', 'empty': True, 'default_branch': ''}
        client.close = lambda: None
        with patch.dict(os.environ, AI_REVIEW_WEBHOOK_HOSTS='127.0.0.1'), \
                patch('routes.ai_review_routes.Gitea', return_value=client), \
                patch('routes.ai_review_routes.server', return_value=self.target):
            with self.assertRaisesRegex(ReviewError, '空仓库|创建响应未知'):
                ensure_webhook(self.state, self.repo['id'], 'http://127.0.0.1:5000')
        self.client_class.return_value = client
        return client

    def test_empty_repository_can_remove_failed_onboarding_without_a_commit(self):
        client = self.failed_empty_onboarding()
        self.assertEqual(self.state.repo(self.repo['id'])['webhook_binding']['phase'], 'verifying')
        old = copy.deepcopy(client.hooks[3])
        removed = self.remove()
        self.assertTrue(removed['removal_complete'])
        self.assertFalse(removed['enabled'])
        self.assertFalse(removed['webhook_secret_configured'])
        self.assertEqual(client.hooks, {3: old})
        self.assertEqual([call[0] for call in client.calls].count('POST'), 1)
        self.assertEqual([call[0] for call in client.calls].count('DELETE'), 1)
        self.remove()
        self.assertEqual([call[0] for call in client.calls].count('DELETE'), 1)

    def test_lost_create_receipt_can_be_found_by_its_saved_unique_name(self):
        client = self.failed_empty_onboarding('after')
        self.assertEqual(self.state.repo(self.repo['id'])['webhook_binding']['phase'], 'creating')
        self.assertTrue(self.remove()['removal_complete'])
        self.assertEqual(set(client.hooks), {3})
        self.assertEqual([call[0] for call in client.calls].count('POST'), 1)

    def test_unknown_create_without_receipt_remains_pending_without_new_writes(self):
        client = self.failed_empty_onboarding('before')
        with self.assertRaisesRegex(ReviewError, '待核对'):
            self.remove()
        self.assertTrue(self.state.repo(self.repo['id'])['removal_pending'])
        self.assertEqual([call[0] for call in client.calls].count('POST'), 1)
        self.assertFalse(any(call[0] == 'DELETE' for call in client.calls))

    def test_unverified_hook_replaced_by_another_configuration_is_not_deleted(self):
        client = self.failed_empty_onboarding()
        own = next(hook for ident, hook in client.hooks.items() if ident != 3)
        own['config']['url'] = 'http://127.0.0.1:5000/api/ai-review/webhooks/999'
        with self.assertRaises(ReviewError):
            self.remove()
        self.assertEqual(len(client.hooks), 2)
        self.assertFalse(any(call[0] == 'DELETE' for call in client.calls))

    def test_unverified_hook_name_type_origin_and_secret_must_all_match(self):
        client = self.failed_empty_onboarding()
        identifier = next(ident for ident in client.hooks if ident != 3)
        original = copy.deepcopy(client.hooks[identifier])
        changes = [dict(name='其他通知'), dict(type='slack'),
                   dict(config=dict(original['config'], url='http://other.invalid/api/ai-review/webhooks/1'))]
        for change in changes:
            with self.subTest(修改字段=list(change)):
                client.hooks[identifier] = dict(copy.deepcopy(original), **change)
                with self.assertRaises(ReviewError):
                    self.remove()
                self.assertFalse(any(call[0] == 'DELETE' for call in client.calls))
        client.hooks[identifier] = original
        self.state.repo_status(self.repo['id'], webhook_secret=self.state.seal('不同的测试密钥'))
        with self.assertRaises(ReviewError):
            self.remove()
        self.assertFalse(any(call[0] == 'DELETE' for call in client.calls))

    def test_interruption_after_deleting_recovered_receipt_can_resume(self):
        client = self.failed_empty_onboarding('after')
        original = client.request
        def interrupted(method, *args, **kwargs):
            value = original(method, *args, **kwargs)
            if method == 'DELETE':
                raise KeyboardInterrupt('模拟删除后进程中断')
            return value
        client.request = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.remove()
        binding = self.state.repo(self.repo['id'])['webhook_binding']
        self.assertEqual(binding['phase'], 'verifying')
        self.assertIsInstance(binding['id'], int)
        client.request = original
        self.assertTrue(self.remove()['removal_complete'])
        self.assertEqual([call[0] for call in client.calls].count('POST'), 1)
        self.assertEqual([call[0] for call in client.calls].count('DELETE'), 1)

    def test_failed_replacement_preserves_preexisting_hook(self):
        from ai_review.webhook_binding import ensure_binding
        client = GiteaFixture()
        client.hooks[3]['config']['url'] = self.callback
        client.repository = lambda: {'id': 7}
        client.close = lambda: None
        before = copy.deepcopy(client.hooks[3])
        with self.assertRaisesRegex(ReviewError, '实际签名投递验证'):
            ensure_binding(self.state, self.state.repo(self.repo['id']), client, self.callback,
                           ['pull_request'], lambda _: False)
        self.client_class.return_value = client
        self.assertTrue(self.remove()['removal_complete'])
        self.assertEqual(client.hooks, {3: before})

    def test_missing_confirmed_create_receipt_is_safe_to_remove(self):
        client = self.failed_empty_onboarding()
        client.hooks = {3: client.hooks[3]}
        self.assertTrue(self.remove()['removal_complete'])
        self.assertFalse(any(call[0] == 'DELETE' for call in client.calls))

    def test_duplicate_unconfirmed_receipt_is_not_guessed(self):
        client = self.failed_empty_onboarding('after')
        own = next(hook for ident, hook in client.hooks.items() if ident != 3)
        client.hooks[99] = dict(copy.deepcopy(own), id=99)
        with self.assertRaises(ReviewError):
            self.remove()
        self.assertFalse(any(call[0] == 'DELETE' for call in client.calls))

    def test_unverified_delete_response_loss_is_reconciled_without_redeletion(self):
        client = self.failed_empty_onboarding()
        client.fail_delete = True
        self.assertTrue(self.remove()['removal_complete'])
        self.assertEqual([call[0] for call in client.calls].count('DELETE'), 1)

    def test_removal_waits_for_installer_and_rechecks_the_saved_state(self):
        entered = threading.Event()
        self.client.repository.side_effect = lambda: (entered.set(), {'id': 7})[1]
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with self.state.lock('webhook-install-' + str(self.repo['id']), blocking=True):
                future = pool.submit(self.remove)
                self.assertFalse(entered.wait(.1), '安装锁释放前不能开始移除通知')
            self.assertTrue(future.result(timeout=5)['removal_complete'])

    def test_concurrent_removals_delete_the_hook_only_once(self):
        barrier = threading.Barrier(2)
        def remove():
            barrier.wait(timeout=5)
            return self.remove()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: remove(), range(2)))
        self.assertTrue(all(result['removal_complete'] for result in results))
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list].count('DELETE'), 1)

    def test_verified_webhook_binding_does_not_block_removal(self):
        self.state.repo_status(self.repo['id'], webhook_binding={'phase': 'ready', 'id': 11})
        self.assertTrue(self.remove()['removal_complete'])
        self.assertNotIn('webhook_binding', self.state.repo(self.repo['id'], public=True))

    def test_old_worker_lease_still_blocks_after_superseded(self):
        job = self.state.enqueue(self.repo['id'], 1, {'head': 'a', 'base': 'b'})
        with self.state.tx() as connection:
            connection.execute("UPDATE jobs SET status='superseded',owner='worker',lease=? WHERE id=?", (time.time()+60, job))
        with self.assertRaises(ReviewError): self.remove()
        self.assertTrue(self.state.repo(self.repo['id'])['enabled'])

    def test_stale_revision_and_archived_ownership_never_write_remote(self):
        with self.assertRaises(ReviewError): self.remove(revision=99)
        self.state.archive_repo(self.repo['id'])
        with self.assertRaisesRegex(ReviewError, '归属已失效'): self.remove()
        self.client_class.assert_not_called()

    def test_replaced_hook_is_never_deleted_and_pending_reconnect_is_blocked(self):
        self.client.request.side_effect = [{'id': 11, 'config': {'url': 'http://elsewhere.invalid/hook'}}]
        with self.assertRaisesRegex(ReviewError, '未删除其他通知'): self.remove()
        row = self.state.repo(self.repo['id'])
        self.assertTrue(row['removal_pending']); self.assertFalse(row['enabled'])
        self.assertEqual([c.args[0] for c in self.client.request.call_args_list], ['GET'])
        with self.assertRaisesRegex(ReviewError, '尚未清理'): self.state.save_repo(1, 7, self.data)

    def test_remote_timeout_leaves_retryable_state(self):
        self.client.request.side_effect = ReviewError('连接超时')
        with self.assertRaises(ReviewError): self.remove()
        self.assertTrue(self.state.repo(self.repo['id'])['removal_pending'])
        self.client.request.side_effect = [self.hook, None, None]
        self.assertTrue(self.remove()['removal_complete'])

    def test_unknown_delete_response_is_verified_without_repeating_delete(self):
        self.client.request.side_effect = [self.hook, PublishUnknown('删除回执丢失'), None]
        self.assertTrue(self.remove()['removal_complete'])
        self.assertEqual([c.args[0] for c in self.client.request.call_args_list], ['GET', 'DELETE', 'GET'])

    def test_missing_hook_is_idempotent_and_never_deletes_other_hook(self):
        self.client.request.side_effect = [None]
        self.assertTrue(self.remove()['removal_complete'])
        self.assertEqual([c.args[0] for c in self.client.request.call_args_list], ['GET'])

    def test_instance_change_and_identity_mismatch_keep_pending_state(self):
        self.target.gitea_url = 'http://127.0.0.1:3001'
        with self.assertRaisesRegex(ReviewError, '其他实例'): self.remove()
        self.client_class.assert_not_called()
        self.target.gitea_url = self.data['gitea_url']
        self.client.repository.side_effect = ReviewError('仓库身份发生变化')
        with self.assertRaisesRegex(ReviewError, '身份发生变化'): self.remove()
        self.client.request.assert_not_called()

    def test_unsigned_late_event_does_not_write_after_removal(self):
        self.remove()
        app = Flask(__name__); app.config['AI_REVIEW_DB'] = str(self.state.path)
        app.register_blueprint(ai_review_bp, url_prefix='/api/ai-review')
        with self.state.connect() as connection:
            before = connection.execute('SELECT count(*) FROM deliveries').fetchone()[0]
        response = app.test_client().post(f"/api/ai-review/webhooks/{self.repo['id']}", json={'repository': {'id': 7}})
        self.assertEqual(response.status_code, 202)
        with self.state.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM deliveries').fetchone()[0], before)

    def test_registered_delete_requires_login_and_csrf(self):
        from access.control import install, endpoints, PLATFORM
        app = Flask(__name__); app.secret_key = '隔离测试会话'
        app.config.update(AI_REVIEW_DB=str(self.state.path), TESTING=True)
        init_test_auth(app)
        app.register_blueprint(ai_review_bp, url_prefix='/api/ai-review'); install(app)
        self.assertIn('ai_review.remove_repository', endpoints(PLATFORM))
        client = app.test_client()
        response = client.delete(f"/api/ai-review/repositories/{self.repo['id']}", json={'revision': 1})
        self.assertIn(response.status_code, (401, 403))
        csrf = login_platform(client)
        self.assertEqual(client.delete(f"/api/ai-review/repositories/{self.repo['id']}", json={'revision': 1}).status_code, 403)
        with patch('routes.ai_review_routes.server', return_value=self.target):
            response = client.delete(f"/api/ai-review/repositories/{self.repo['id']}", json={'revision': 1}, headers={'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json['removal_complete'])


if __name__ == '__main__': unittest.main()
