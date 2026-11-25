"""验收通道配置、旧勾子切换、回退和队列隔离。"""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from ai_review import pr_agent_channel
from ai_review.common import ReviewError
from ai_review.host_policy import ensure
from ai_review.repository_removal import remove_repository
from ai_review.store import Store
from tests.test_ai_review_webhook_binding import GiteaFixture


class ChannelFixture(GiteaFixture):
    def request(self, method, path, data=None, missing=False):
        # Gitea 回读会把 PR 总类展开成更多子事件。
        if method == 'PATCH' and 'pull_request' in (data or {}).get('events', []):
            data = dict(data, events=list(dict.fromkeys(data['events'] + ['pull_request_assign'])))
        return super().request(method, path, data, missing)

    def repository(self):
        return {'default_branch': 'main'}

    def close(self):
        pass


class ChannelTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        key = self.root / 'master.key'
        key.write_bytes(Fernet.generate_key())
        env = patch.dict('os.environ', {
            'AI_REVIEW_MASTER_KEY_FILE': str(key),
            'AI_REVIEW_WEBHOOK_HOSTS': 'manager.invalid',
            'AI_REVIEW_HOST_POLICY_FILE': str(self.root / 'hosts.json'),
            'PR_AGENT_CHANNEL_DIR': str(self.root / 'channels'),
        })
        env.start()
        self.addCleanup(env.stop)
        ensure()
        self.store = Store(self.root / 'review.db')
        self.store.save_config(dict(self.store.config(), enabled=True, api_base_url='https://model.invalid',
                                    api_key=self.store.seal('验收模型密钥')))
        self.repo = self.store.save_repo(1, 1, {
            'repo_name': 'test/code', 'enabled': True, 'gitea_url': 'http://gitea.invalid',
            'bot_token': self.store.seal('验收机器人'), 'webhook_secret': self.store.seal('旧测试密钥'),
            'hook_id': 3, 'webhook_url': 'http://manager.invalid/api/ai-review/webhooks/1',
        })
        self.target = SimpleNamespace(gitea_url='http://gitea.invalid', api_token='验收管理凭据')
        self.fixture = ChannelFixture()
        self.fixture.hooks[3]['events'] = self.fixture.hooks[3]['events'] + ['pull_request_assign']
        self.channel_name = self.root / 'channels' / '1.json'

    def install(self):
        return pr_agent_channel.install(self.store, self.repo['id'], 'http://manager.invalid', self.target)

    def test_migrate_existing_hook_and_reuse_native_configuration(self):
        with patch.object(pr_agent_channel, 'Gitea', return_value=self.fixture), patch.object(pr_agent_channel, '_probe'):
            first = self.install()
            second = self.install()
        self.assertEqual(first['native_pr_agent'], 'ready')
        self.assertEqual(second['hook_id'], 3)
        self.assertEqual(list(self.fixture.hooks), [3])
        self.assertEqual(self.fixture.hooks[3]['secret'], '旧测试密钥')
        self.assertTrue(set(pr_agent_channel.EVENTS).issubset(self.fixture.hooks[3]['events']))
        self.assertNotIn('push', self.fixture.hooks[3]['events'])
        channel = json.loads(self.channel_name.read_text())
        self.assertTrue(channel['enabled'])
        self.assertTrue(channel['settings']['pr_reviewer.enable_large_pr_chunking'])
        self.assertTrue(channel['settings']['pr_reviewer.inline_key_issues'])
        self.assertIsNone(self.store.enqueue(1, 1, {'head': 'a' * 40}))
        self.assertIsNone(self.store.claim())
        self.store.save_repo(1, 1, {'enabled': False, 'revision': self.store.repo(1)['revision']}, 1)
        self.assertFalse(json.loads(self.channel_name.read_text())['enabled'])

    def test_failed_probe_restores_existing_hook(self):
        with patch.object(pr_agent_channel, 'Gitea', return_value=self.fixture), patch.object(
            pr_agent_channel, '_probe', side_effect=ReviewError('签名投递失败')
        ):
            with self.assertRaisesRegex(ReviewError, '签名投递失败'):
                self.install()
        self.assertEqual(self.fixture.hooks[3]['config']['url'],
                         'http://manager.invalid/api/ai-review/webhooks/1')
        self.assertIsNone(self.store.repo(1).get('native_pr_agent'))
        self.assertFalse(self.channel_name.exists())

    def test_switch_to_another_engine_reuses_hook_and_allows_manager_queue(self):
        with patch.object(pr_agent_channel, 'Gitea', return_value=self.fixture), patch.object(pr_agent_channel, '_probe'):
            self.install()
        self.store.save_config({'revision': self.store.config()['revision'], 'engine': 'codex_cli'})
        channel = json.loads(self.channel_name.read_text())
        self.assertTrue(channel['legacy'])
        self.assertTrue(channel['enabled'])
        self.assertEqual(self.store.repo(1)['hook_id'], 3)
        with patch('ai_review.runtime_status.available', return_value=True):
            self.assertIsNotNone(self.store.enqueue(1, 1, {'head': 'a' * 40, 'base': 'b' * 40}))
        self.store.save_config({'revision': self.store.config()['revision'], 'engine': 'pr_agent'})
        self.assertFalse(json.loads(self.channel_name.read_text())['legacy'])

    def test_removal_revokes_native_credentials_before_webhook_cleanup(self):
        with patch.object(pr_agent_channel, 'Gitea', return_value=self.fixture), patch.object(pr_agent_channel, '_probe'):
            self.install()

        def check_revoked(*args):
            self.assertFalse(self.channel_name.exists())

        with patch('ai_review.repository_removal.cleanup_webhook', side_effect=check_revoked):
            removed = remove_repository(self.store, 1, self.store.repo(1)['revision'], self.target)
        self.assertTrue(removed['removal_complete'])
        self.assertFalse(self.channel_name.exists())
