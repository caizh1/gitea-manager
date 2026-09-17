"""对发布状态机进行故障注入：所有位置仍通过真实补丁校验器。"""
import copy
import json
import unittest
from unittest.mock import patch, Mock

import test_ai_review as core
from ai_review.common import PublishUnknown, Stale
from ai_review.publish import Publisher, marker
from ai_review.publish import verify_hunk


class MemoryGitea:
    prefix = '/repos/test/code'

    def __init__(self, snapshot):
        self.snapshot = snapshot; self.reviews = []; self.code = {}; self.timeline = []; self.summaries = []
        self.calls = []; self.next_id = 1; self.fail_after = ''; self.corrupt = False; self.guard = None

    def pr(self, _):
        return dict(state='open', head={'sha':self.snapshot['head']}, base={'sha':self.snapshot['base']}, merge_base=self.snapshot['merge_base'])

    def version(self):return '1.26.4+review.1'

    def pages(self, path):
        if path.endswith('/timeline'): rows = self.timeline
        elif '/reviews/' in path and path.endswith('/comments'): rows = self.code[int(path.split('/')[-2])]
        elif path.endswith('/reviews'): rows = self.reviews
        elif path.endswith('/comments'): rows = self.summaries
        else: raise AssertionError(path)
        return copy.deepcopy(rows)

    def request(self, method, path, data=None):
        self.calls.append((method,path,copy.deepcopy(data)))
        if method=='GET' and '/reviews/' in path:
            row=copy.deepcopy(next(r for r in self.reviews if r['id']==int(path.split('/')[-1])))
            event=next((r for r in self.timeline if r['review_id']==row['id']),None)
            row['html_url']='http://gitea/test/code/pulls/1#issuecomment-'+str(event['id']) if event else ''
            return row
        if method=='GET' and '/issues/comments/' in path:
            return None  # 真实 1.26.4 对非普通评论返回 204。
        if method=='DELETE':
            number=int(path.split('/')[-1])
            if '/reviews/' in path:
                self.reviews=[r for r in self.reviews if r['id']!=number]
                self.timeline=[t for t in self.timeline if t['review_id']!=number]
                self.code.pop(number)
            else: self.timeline=[t for t in self.timeline if t['id']!=number]
            return
        if path.endswith('/reviews'):
            row=next((r for r in self.reviews if r['state']=='PENDING'),None)
            if row is None:
                row={'id':self.next_id,'user':{'id':9}};self.next_id+=1
                self.reviews.append(row);self.code[row['id']]=[]
            row.update(state=data['event'],commit_id=data['commit_id'],body=data['body'])
            for item in data['comments']:
                self.code[row['id']].append({'id':100+len(self.code[row['id']]),'user':{'id':9},'body':item['body'],
                    'path':item['path'],'position':item['new_position'],'original_position':item['old_position'],
                    'diff_hunk':'@@ -1 +1 @@\n-int f(void) { return 0; }\n+int f(void) { return '+('8' if self.corrupt else '1')+'; }'})
            self.timeline.append({'id':len(self.calls),'review_id':row['id'],'type':'review','user':{'id':9},'body':data['body']})
            if self.fail_after==data['event']:
                self.fail_after='';raise PublishUnknown('模拟服务已经写入但响应丢失')
            return copy.deepcopy(row)
        if '/issues/' in path and method in ('POST','PATCH'):
            if self.fail_after=='summary': self.fail_after='';raise PublishUnknown('模拟总体评论请求暂时不可确认')
            row={'id':500,'user':{'id':9},'body':data['body']};self.summaries[:]=[row];return copy.deepcopy(row)
        raise AssertionError((method,path))


class PublicationTests(unittest.TestCase):
    def setUp(self):
        core.StoreTests.setUp(self)
        self.store.enqueue(self.repo,1,{'head':'a'*40,'base':'b'*40})
        self.job=self.store.claim()
        _,anchors,self.snapshot=core.example()
        self.snapshot.update(head='a'*40,base='b'*40,merge_base='b'*40)
        self.snapshot['coverage'].update(files_total=1,files_reviewed=1)
        anchor=next(a for a in anchors if a['side']=='new')
        row={'anchor':anchor,'severity':'P2','title':'合成缺陷','trigger':'测试触发','explanation':'测试原因',
             'impact':'测试影响','recommendation':'测试建议','path':'code.c','scope':'f','fingerprint':'测试问题'}
        self.result={'findings':[row],'coverage':self.snapshot['coverage']}
        self.store.update(self.job['id'],self.job['owner'],snapshot=self.snapshot,result=self.result)
        self.client=MemoryGitea(self.snapshot)
        self.pub=Publisher(self.store,self.client,self.job,self.store.repo(self.repo),self.snapshot,self.result)

    def test_one_formal_request_contains_explicit_sha_and_inline(self):
        self.pub.publish()
        self.assertEqual(len(self.client.timeline), 1)
        self.assertEqual(len(self.client.summaries), 0)
        writes = [d for m, p, d in self.client.calls if m == 'POST' and p.endswith('/reviews')]
        self.assertEqual([d['event'] for d in writes], ['COMMENT'])
        self.assertEqual(writes[0]['commit_id'], 'a'*40)
        self.assertEqual(len(writes[0]['comments']), 1)
        self.assertEqual(writes[0]['comments'][0]['path'], 'code.c')
        self.assertTrue(self.store.job(self.job['id'])['review_fragment'].startswith('issuecomment-'))

    def test_gitea_short_display_hunk_retains_full_header_count(self):
        _, anchors, snap = core.example('', 'int f(void) {\n    return 1;\n}\n')
        a = next(a for a in anchors if a['side']=='new' and a['line']==2)
        comment={'path':'code.c','position':2,'original_position':0,
                 'diff_hunk':'@@ -0,0 +1,3 @@\n+int f(void) {\n+    return 1;'}
        verify_hunk(comment,a,snap)
        snap['files'][0]['new']=snap['files'][0]['new'].replace('\n','\r\n')
        verify_hunk(comment,a,snap)
        comment['diff_hunk'] += '\n+wrong();'
        with self.assertRaises(core.ReviewError):verify_hunk(comment,a,snap)

    def test_stock_gitea_publishes_inline_without_pending(self):
        self.client.version = lambda: '1.26.4'
        self.pub.publish()
        writes = [d for m, p, d in self.client.calls if m == 'POST']
        self.assertEqual([d['event'] for d in writes], ['COMMENT'])
        self.assertEqual(len(writes[0]['comments']), 1)
        self.assertIsNotNone(self.result['findings'][0]['anchor'])

    def test_corrupted_publication_is_retained_for_inspection(self):
        self.client.corrupt = True
        with self.assertRaises(core.ReviewError):
            self.pub.publish()
        self.assertEqual(len(self.client.reviews), 1)
        self.assertEqual(self.client.reviews[0]['state'], 'COMMENT')
        self.assertIsNotNone(self.result['findings'][0]['anchor'])
        self.assertEqual(self.store.job(self.job['id'])['status'], 'publishing')
        self.assertFalse(any(m == 'DELETE' for m, _, _ in self.client.calls))

    def test_final_response_loss_recovery_never_resubmits(self):
        self.client.fail_after = 'COMMENT'
        with self.assertRaises(PublishUnknown):
            self.pub.publish()
        self.pub.publish(recovery=True)
        self.assertEqual(sum(m == 'POST' for m, _, _ in self.client.calls), 1)
        self.assertEqual(len(self.client.timeline), 1)

    def test_changed_summary_is_not_confirmed_or_overwritten(self):
        self.pub.publish()
        self.client.reviews[0]['body'] += '\n意外混入的总结内容'
        before = len(self.client.calls)
        with self.assertRaises(core.ReviewError):
            self.pub.publish(recovery=True)
        self.assertFalse(any(m != 'GET' for m, _, _ in self.client.calls[before:]))
        self.assertIn('意外混入', self.client.reviews[0]['body'])

    def test_owned_partial_or_legacy_draft_is_not_resumed(self):
        self.client.reviews = [{'id': 90, 'user': {'id': 9}, 'state': 'PENDING',
                                'body': marker(self.job), 'commit_id': 'a'*40}]
        self.client.code[90] = []
        for recovery in (False, True):
            with self.assertRaises(PublishUnknown):
                self.pub.publish(recovery=recovery)
        self.assertFalse(any(m in ('DELETE', 'POST') for m, _, _ in self.client.calls))

    def test_unknown_absent_result_is_not_replayed(self):
        with self.assertRaises(PublishUnknown):
            self.pub.publish(recovery=True)
        self.assertFalse(any(m == 'POST' for m, _, _ in self.client.calls))

    def test_summary_outage_cannot_fail_formal_publication(self):
        self.client.fail_after = 'summary'
        self.pub.publish()
        self.pub.publish(recovery=True)
        self.assertEqual(len(self.client.reviews), 1)
        self.assertEqual(len(self.client.summaries), 0)

    def test_unowned_draft_is_never_deleted_or_submitted(self):
        self.client.reviews = [{'id': 90, 'user': {'id': 9}, 'state': 'PENDING', 'body': '其他任务的人类讨论'}]
        self.client.code[90] = []
        with self.assertRaises(PublishUnknown):
            self.pub.publish()
        self.assertFalse(any(m in ('DELETE', 'POST') for m, _, _ in self.client.calls))

    def test_push_before_publication_prevents_write(self):
        self.client.snapshot = dict(self.snapshot, head='c'*40)
        with self.assertRaises(Stale):
            self.pub.publish()
        self.assertTrue(self.pub.cancelled)
        self.assertFalse(any(m == 'POST' for m, _, _ in self.client.calls))

    def test_invalid_local_anchor_degrades_before_write(self):
        self.result['findings'][0]['anchor'] = dict(self.result['findings'][0]['anchor'], line=99)
        self.pub.publish()
        self.assertEqual(self.client.code[self.client.reviews[0]['id']], [])
        self.assertIn('本地锚点', self.client.reviews[0]['body'])

    def test_empty_findings_only_publishes_summary(self):
        self.result['findings'] = []
        self.pub.publish()
        self.assertEqual(len(self.client.reviews), 1)
        self.assertEqual(self.client.code[self.client.reviews[0]['id']], [])

    def test_recovery_only_reads_complete_formal_result(self):
        from ai_review.worker import reconcile_unknown
        self.pub.publish()
        self.store.finish(self.job['id'], self.job['owner'], 'publish_unknown')
        with self.store.tx() as c:
            c.execute('UPDATE jobs SET available=0 WHERE id=?', (self.job['id'],))
        self.client.close = lambda: None
        before = len(self.client.calls)
        with patch('ai_review.worker.client_for', return_value=self.client), patch('ai_review.worker.refresh_pr'):
            reconcile_unknown(self.store)
        self.assertEqual(self.store.job(self.job['id'])['status'], 'succeeded')
        self.assertFalse(any(m != 'GET' for m, _, _ in self.client.calls[before:]))

    def test_recovery_retains_pending_and_mismatched_formal_results(self):
        from ai_review.worker import reconcile_unknown
        self.pub.publish()
        for state in ('PENDING', 'COMMENT'):
            self.client.reviews[0]['state'] = state
            self.client.code[1][0]['position'] = 99
            with self.store.tx() as c:
                c.execute("UPDATE jobs SET status='publish_unknown',available=0 WHERE id=?", (self.job['id'],))
            self.client.close = lambda: None
            before = len(self.client.calls)
            with patch('ai_review.worker.client_for', return_value=self.client):
                reconcile_unknown(self.store)
            self.assertEqual(self.store.job(self.job['id'])['status'], 'publish_unknown')
            self.assertTrue(self.store.job(self.job['id'])['error'])
            self.assertFalse(any(m != 'GET' for m, _, _ in self.client.calls[before:]))

    def test_confirm_unpublished_requires_no_owned_review_or_bot_draft(self):
        from ai_review.worker import confirm_unpublished
        self.store.finish(self.job['id'], self.job['owner'], 'publish_unknown')
        self.client.close = lambda: None
        self.client.repository = lambda: {'id': 7}
        for row in (
            {'id': 7, 'state': 'COMMENT', 'body': marker(self.job), 'user': {'id': 9}},
            {'id': 8, 'state': 'PENDING', 'body': '其他草稿', 'user': {'id': 9}},
        ):
            self.client.reviews = [row]; self.client.code[row['id']] = []
            with patch('ai_review.worker.client_for', return_value=self.client), self.assertRaises(core.ReviewError):
                confirm_unpublished(self.store, self.job['id'])
            self.assertEqual(self.store.job(self.job['id'])['status'], 'publish_unknown')
        self.client.reviews = []
        with patch('ai_review.worker.client_for', return_value=self.client):
            confirm_unpublished(self.store, self.job['id'])
        self.assertEqual(self.store.job(self.job['id'])['status'], 'failed')
        self.assertFalse(any(m != 'GET' for m, _, _ in self.client.calls))

    def test_cpp_methods_include_class_and_namespace(self):
        from ai_review.snapshot import functions
        code='namespace N { struct A { int f() { return 1; } }; struct B { int f() { return 2; } }; }'
        scopes, error=functions(code,'sample.cpp')
        self.assertFalse(error)
        self.assertEqual([s['name'] for s in scopes],['N::A::f()','N::B::f()'])

    def test_poisoned_task_cannot_retry_forever(self):
        job=self.job
        for _ in range(2):
            with self.store.tx() as c:c.execute('UPDATE jobs SET lease=0 WHERE id=?',(job['id'],))
            job=self.store.claim()
        self.assertEqual(job['attempts'],3)
        with self.store.tx() as c:c.execute('UPDATE jobs SET lease=0 WHERE id=?',(job['id'],))
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.job(job['id'])['status'],'failed')

    def test_history_reference_survives_more_than_ten_reruns(self):
        for _ in range(12):
            self.result['findings'][0]['existing_review_id']=73
            with self.store.tx() as c:
                c.execute("UPDATE jobs SET status='succeeded',review_id=99,result=? WHERE id=?",(core.encode(self.result),self.job['id']))
            self.store.enqueue(self.repo,1,{'head':'a'*40},manual=True)
            self.job=self.store.claim()
        current=copy.deepcopy(self.result);current['findings'][0].pop('existing_review_id')
        publisher=Publisher(self.store,self.client,self.job,self.store.repo(self.repo),self.snapshot,current)
        publisher.prior_findings()
        self.assertEqual(current['findings'][0]['existing_review_id'],73)

    def test_comment_contract_cannot_be_inline_code_anchor(self):
        _, _, snap=core.example('', '/* ARM contract */\nint f(void) {\n    return 1;\n}\n')
        items=core.work_items(snap)
        self.assertFalse(any(a['line']==1 for item in items for a in item['anchors']))

    def test_filtered_review_page_does_not_hide_later_reviews(self):
        from ai_review.gitea import Gitea
        client=Gitea.__new__(Gitea);client.page_limit=50;client.response_total=None
        def response(method,path,params):
            client.response_total=60
            return [{'id':i} for i in (range(1,50) if params['page']==1 else range(51,61))]
        client.request=response
        rows=client.pages('/repos/test/code/pulls/1/reviews')
        self.assertEqual(rows[-1]['id'],60)

    def test_many_ordinary_comments_are_read_once(self):
        from ai_review.gitea import Gitea
        client=Gitea.__new__(Gitea);client.response_total=80
        client.request=Mock(return_value=[{'id':i} for i in range(80)])
        self.assertEqual(len(client.pages('/repos/test/code/issues/1/comments')),80)
        client.request.assert_called_once()

    def test_manual_review_acknowledges_actual_version_without_new_job(self):
        from ai_review.worker import refresh_pr
        self.store.finish(self.job['id'],self.job['owner'],'succeeded')
        with patch('ai_review.worker.client_for',return_value=Mock(pr=self.client.pr)):
            refresh_pr(self.store,self.store.repo(self.repo),self.job)
        self.assertIsNone(self.store.claim())

    def test_valid_json_is_not_enough_when_model_stream_is_truncated(self):
        from ai_review.broker import validate_completion
        for reason in ('length','tool_calls',None):
            chunks=[{'choices':[{'index':0,'delta':{'content':'{"findings":[]}'},'finish_reason':None}]},
                    {'choices':[{'index':0,'delta':{},'finish_reason':reason}]}]
            raw=(''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n').encode()
            with self.assertRaises(core.ReviewError):validate_completion(raw,True)
        raw=raw.replace(b'"finish_reason": null}]}\n\ndata: [DONE]',b'"finish_reason": "stop"}]}\n\ndata: [DONE]')
        validate_completion(raw,True)
        with self.assertRaises(core.ReviewError):validate_completion(raw.replace(b'data: [DONE]\n\n',b''),True)


if __name__=='__main__':unittest.main()
