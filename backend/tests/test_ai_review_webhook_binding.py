"""勾子密钥更换、未知回执和旧资源保护；服务替身故意忽略 PATCH secret。"""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from ai_review.common import AccessRevoked, PublishUnknown, ReviewError, WriteRejected
from ai_review.store import Store
from ai_review.webhook_binding import ensure_binding


URL = 'http://manager.invalid/api/ai-review/webhooks/1'
EVENTS = ['pull_request', 'pull_request_sync']


class GiteaFixture:
    prefix = '/repos/test/code'

    def __init__(self):
        self.hooks = {3: {'id': 3, 'type': 'gitea', 'name': '', 'active': True, 'events': EVENTS,
                          'config': {'url': URL, 'content_type': 'json'}, 'secret': '旧测试密钥'}}
        self.calls = []
        self.fail_create = None
        self.fail_delete = False
        self.bad_readback = False
        self.change_old = False

    def public(self, value):
        value = copy.deepcopy(value); value.pop('secret', None)
        if self.bad_readback and value['id'] != 3:
            value['active'] = False
        return value

    def pages(self, _path):
        return [self.public(h) for h in self.hooks.values()]

    def request(self, method, path, data=None, missing=False):
        self.calls.append((method, path, copy.deepcopy(data)))
        if method == 'POST':
            if self.fail_create == 'rejected':
                raise WriteRejected('创建被明确拒绝')
            if self.fail_create == 'unauthorized':
                raise AccessRevoked('创建权限已失效')
            if self.fail_create == 'before':
                raise PublishUnknown('创建回执未知')
            ident = max(self.hooks, default=3) + 1
            value = copy.deepcopy(data); secret = value['config'].pop('secret')
            self.hooks[ident] = dict(value, id=ident, secret=secret)
            if self.change_old:
                self.hooks[3]['name'] = '人工更新'
            if self.fail_create == 'after':
                self.fail_create = None
                raise PublishUnknown('创建回执未知')
            return self.public(self.hooks[ident])
        ident = int(path.rsplit('/', 1)[1])
        if method == 'GET':
            if ident not in self.hooks and missing:
                return None
            return self.public(self.hooks[ident])
        if method == 'PATCH':
            value = copy.deepcopy(data); value.get('config', {}).pop('secret', None)
            self.hooks[ident].update(value)
            return self.public(self.hooks[ident])
        if method == 'DELETE':
            self.hooks.pop(ident, None)
            if self.fail_delete:
                self.fail_delete = False
                raise PublishUnknown('删除回执未知')
            return None
        raise AssertionError(method)


class WebhookBindingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name); key = root/'master.key'; key.write_bytes(Fernet.generate_key())
        env = patch.dict(os.environ, AI_REVIEW_MASTER_KEY_FILE=str(key)); env.start(); self.addCleanup(env.stop)
        self.store = Store(root/'review.db')
        self.store.save_repo(1, 12, {'gitea_url': 'http://gitea.invalid', 'repo_name': 'test/code',
                                   'webhook_secret': self.store.seal('新测试密钥')})
        self.client = GiteaFixture()

    def ensure(self):
        return ensure_binding(self.store, self.store.repo(1), self.client, URL, EVENTS)

    def posts(self):
        return sum(method == 'POST' for method, *_ in self.client.calls)

    def test_replaces_stale_secret_then_reuses_confirmed_hook(self):
        self.client.hooks[9] = dict(copy.deepcopy(self.client.hooks[3]), id=9,
                                    config={'url': 'http://unrelated.invalid', 'content_type': 'json'})
        hook = self.ensure()
        self.assertNotIn(3, self.client.hooks)
        self.assertIn(9, self.client.hooks)
        self.assertEqual(self.client.hooks[hook['id']]['secret'], '新测试密钥')
        self.assertLess(next(i for i, c in enumerate(self.client.calls) if c[0] == 'POST'),
                        next(i for i, c in enumerate(self.client.calls) if c[0] == 'DELETE'))
        self.assertEqual(self.ensure()['id'], hook['id'])
        self.assertEqual(self.posts(), 1)
        self.assertNotIn('webhook_binding', self.store.repo(1, public=True))
        self.assertTrue(all('secret' not in (data or {}).get('config', {})
                            for method, _, data in self.client.calls if method == 'PATCH'))

    def test_lost_create_receipt_is_recovered_by_saved_name(self):
        self.client.fail_create = 'after'
        with self.assertRaises(ReviewError): self.ensure()
        self.assertIn(3, self.client.hooks)
        self.ensure()
        self.assertEqual(self.posts(), 1)
        self.assertNotIn(3, self.client.hooks)

    def test_unseen_unknown_create_is_not_replayed(self):
        self.client.fail_create = 'before'
        with self.assertRaises(ReviewError): self.ensure()
        with self.assertRaisesRegex(ReviewError, '结果未知'): self.ensure()
        self.assertEqual(self.posts(), 1)
        self.assertIn(3, self.client.hooks)

    def test_explicit_rejection_can_be_retried_after_correction(self):
        for reason in ('rejected', 'unauthorized'):
            with self.subTest(原因=reason):
                self.client.fail_create = reason
                with self.assertRaises(ReviewError): self.ensure()
                self.assertEqual(self.store.repo(1)['webhook_binding']['phase'], 'planned')
        self.client.fail_create = None
        self.ensure()
        self.assertNotIn(3, self.client.hooks)

    def test_client_updates_cannot_replace_private_creation_receipt(self):
        self.ensure()
        before = self.store.repo(1)
        self.store.save_repo(1, 12, {'revision': before['revision'], 'webhook_binding': {'phase': 'ready'}}, 1)
        self.assertEqual(self.store.repo(1)['webhook_binding'], before['webhook_binding'])

    def test_failed_new_readback_preserves_old_hook(self):
        self.client.bad_readback = True
        with self.assertRaisesRegex(ReviewError, '回读验证失败'): self.ensure()
        self.assertIn(3, self.client.hooks)
        self.client.bad_readback = False
        self.ensure()
        self.assertEqual(self.posts(), 1)

    def test_lost_delete_receipt_does_not_create_another_hook(self):
        self.client.fail_delete = True
        with self.assertRaises(PublishUnknown): self.ensure()
        hook = self.ensure()
        self.assertEqual(self.posts(), 1)
        self.assertEqual(list(self.client.hooks), [hook['id']])

    def test_changed_old_hook_is_not_deleted(self):
        self.client.change_old = True
        with self.assertRaisesRegex(ReviewError, '其他操作修改'): self.ensure()
        with self.assertRaisesRegex(ReviewError, '其他操作修改'): self.ensure()
        self.assertIn(3, self.client.hooks)
        self.assertEqual(self.posts(), 1)

    def test_unknown_duplicate_and_other_hook_type_are_preserved(self):
        self.client.hooks[4] = dict(copy.deepcopy(self.client.hooks[3]), id=4)
        with self.assertRaisesRegex(ReviewError, '重复 Webhook'): self.ensure()
        self.assertEqual(self.posts(), 0)
        self.client.hooks.pop(4)
        self.client.hooks[3]['type'] = 'slack'
        with self.assertRaisesRegex(ReviewError, '其他类型'): self.ensure()
        self.assertIn(3, self.client.hooks)

    def test_new_secret_and_deleted_managed_hook_can_be_installed(self):
        first = self.ensure()
        self.store.repo_status(1, webhook_secret=self.store.seal('轮换后的测试密钥'))
        second = self.ensure()
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(self.client.hooks[second['id']]['secret'], '轮换后的测试密钥')
        self.client.hooks.clear()
        third = self.ensure()
        self.assertEqual(self.client.hooks[third['id']]['secret'], '轮换后的测试密钥')
        self.assertEqual(self.posts(), 3)


if __name__ == '__main__':
    unittest.main()
