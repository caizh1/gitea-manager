"""资源保护与分页发布中断测试；不使用真实凭据。"""
import copy
import unittest
from unittest.mock import MagicMock,patch
from ai_review.gitea import Gitea
from ai_review.common import PublishUnknown
from ai_review.publish import Publisher
import test_ai_review_publication as baseline

class ResourcesTests(unittest.TestCase):
    def test_oversized_diff_file_does_not_skip_following_file(self):
        client=Gitea.__new__(Gitea);client.guard=None;client.url='http://gitea.invalid';client.prefix='/repos/test/code';client.session=MagicMock()
        response=client.session.get.return_value.__enter__.return_value;response.status_code=200
        def chunks():
            yield b'diff --git a/large.c b/large.c\n--- a/large.c\n+++ b/large.c\n@@ -1 +1 @@\n-'
            for _ in range(65*1024):yield b'x'*1024
            yield b'\n+y\ndiff --git a/ok.c b/ok.c\n--- a/ok.c\n+++ b/ok.c\n@@ -1 +1 @@\n-a\n+b\n'
        response.iter_content.side_effect=lambda _:chunks()
        values=list(client.diff_patches(1))
        self.assertEqual(len(values),2);self.assertEqual(values[0].new_path,'large.c')
        self.assertIn('67108864',values[0].unsupported);self.assertEqual(values[1].new_path,'ok.c');self.assertTrue(values[1].hunks)

class PageRecoveryTests(unittest.TestCase):
    setUp=baseline.PublicationTests.setUp

    def test_lost_first_page_response_is_reconciled_then_unsent_pages_continue(self):
        self.result['findings']=[dict(copy.deepcopy(self.result['findings'][0]),fingerprint=str(i),title=str(i)) for i in range(4)]
        self.result['publication_limit']=2
        self.client.fail_after='COMMENT'
        pub=Publisher(self.store,self.client,self.job,self.store.repo(self.repo),self.snapshot,self.result)
        with self.assertRaises(PublishUnknown):pub.publish()
        self.assertEqual(len(self.client.timeline),1)
        saved=self.store.job(self.job['id'])['result']
        Publisher(self.store,self.client,self.job,self.store.repo(self.repo),self.snapshot,saved).publish(recovery=True)
        self.assertEqual(len(self.client.timeline),2)
        self.assertEqual(sum(call[0]=='POST' for call in self.client.calls),2)

    def test_uncertain_page_without_receipt_is_never_replayed(self):
        self.result['findings']=[dict(copy.deepcopy(self.result['findings'][0]),fingerprint=str(i),title=str(i)) for i in range(4)]
        self.result['publication_limit']=2
        original=self.client.request
        def lost(method,*args,**kwargs):
            if method=='POST':raise PublishUnknown('发送结果未知')
            return original(method,*args,**kwargs)
        with patch.object(self.client,'request',side_effect=lost):
            with self.assertRaises(PublishUnknown):Publisher(self.store,self.client,self.job,self.store.repo(self.repo),self.snapshot,self.result).publish()
        saved=self.store.job(self.job['id'])['result']
        with self.assertRaises(PublishUnknown):Publisher(self.store,self.client,self.job,self.store.repo(self.repo),self.snapshot,saved).publish(recovery=True)
        self.assertEqual(len(self.client.timeline),0)
