"""接入移除的状态、历史、并发与远端身份边界回归。"""
import json
import os
from pathlib import Path
import tempfile
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
