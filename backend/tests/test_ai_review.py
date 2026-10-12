"""定位、版本竞争、签名、权限与发布证据的确定性验收。"""
import concurrent.futures
import difflib
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch as mock_patch

from cryptography.fernet import Fernet
from flask import Flask
from tests.access_helpers import init_test_auth as init_auth, login_platform
from ai_review.common import ReviewError, Stale, encode
from ai_review.diff import parse_diff, validate_patch, locate
from ai_review.publish import verify_hunk
from ai_review.snapshot import functions, work_items
from ai_review.store import Store
from ai_review.engine import candidates, confirm, capability
from ai_review.broker import create_broker
from routes.ai_review_routes import ai_review_bp


def make_diff(old, new, old_path='code.c', new_path='code.c', context=3):
    header = f'diff --git a/{old_path} b/{new_path}\n'
    return (header+''.join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                    fromfile='a/'+old_path, tofile='b/'+new_path, n=context))).encode()


def example(old='int f(void) { return 0; }\n', new='int f(void) { return 1; }\n'):
    patch = parse_diff(make_diff(old, new))[0]
    anchors = validate_patch(patch, old.encode(), new.encode(), '版本甲')
    snapshot = {'id': '版本甲', 'files': [{'path': 'code.c', 'old': old, 'new': new, 'anchors': [a['id'] for a in anchors]}],
                'anchors': {a['id']: a for a in anchors}, 'coverage': {'skipped': [], 'complete': True}}
    return patch, anchors, snapshot


class DiffTests(unittest.TestCase):
    def test_fifty_fixed_locations(self):
        for n in range(1, 51):
            with self.subTest(样本=n):
                rows = [f'int f{i}(void) {{ return 0; }}\n' for i in range(70)]
                old = ''.join(rows)
                target = n + 5
                rows[target-1] = f'int f{target-1}(void) {{ return 1; }}\n'
                new = ''.join(rows)
                _, anchors, snapshot = example(old, new)
                changed = [a for a in anchors if a['side'] == 'new']
                self.assertEqual([a['line'] for a in changed], [target])
                self.assertEqual(changed[0]['quote'], rows[target-1][:-1])
                anchor, reason = locate({'anchor_id': changed[0]['id'], 'quote': changed[0]['quote'], 'location_verified': True}, snapshot['anchors'], '版本甲')
                self.assertEqual(anchor['line'], target); self.assertEqual(reason, '')

    def test_multiple_hunks_and_repeated_code(self):
        old = 'int a;\n' + 'same();\n'*30 + 'int b;\n'
        new = 'int c;\n' + 'same();\n'*30 + 'int d;\n'
        patch, anchors, _ = example(old, new)
        self.assertEqual(len(patch.hunks), 2)
        self.assertEqual([(a['side'], a['line']) for a in anchors], [('old', 1), ('new', 1), ('old', 32), ('new', 32)])

    def test_insertion_shifts_later_hunks(self):
        old = 'a\n'+'same\n'*20+'z\n'
        new = 'a\ninserted\n'+'same\n'*20+'changed\n'
        _, anchors, _ = example(old, new)
        self.assertIn(('new', 23), [(a['side'], a['line']) for a in anchors])

    def test_wrong_blob_cannot_accept_even_matching_problem_line(self):
        p, _, _ = example('a\nx\nb\n', 'a\ny\nb\n')
        for old, new in [(b'a\nx\nelse\n', b'a\ny\nb\n'), (b'a\nx\nb\n', b'a\ny\nelse\n')]:
            with self.assertRaises(ReviewError): validate_patch(p, old, new, '甲')

    def test_truncated_and_overlapping_hunks(self):
        for raw in [b'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n-a\n+b\n',
                    b'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n@@ -1 +1 @@\n-a\n+c\n']:
            with self.assertRaises(ReviewError):
                p = parse_diff(raw)[0]; validate_patch(p, b'a\n', b'b\n', '甲')

    def test_added_deleted_and_no_newline(self):
        for old, new, headers, hunk in [
            (b'', b'int x;', '--- /dev/null\n+++ b/x.c\n', '@@ -0,0 +1 @@\n+int x;\n\\ No newline at end of file\n'),
            (b'int x;', b'', '--- a/x.c\n+++ /dev/null\n', '@@ -1 +0,0 @@\n-int x;\n\\ No newline at end of file\n')]:
            p = parse_diff(('diff --git a/x.c b/x.c\n'+headers+hunk).encode())[0]
            anchors = validate_patch(p, old, new, '甲')
            self.assertEqual(len(anchors), 1); self.assertEqual(anchors[0]['line'], 1)

    def test_crlf_and_control_character_preserved(self):
        old, new = 'int a;\r\n/*\v*/\r\n', 'int b;\r\n/*\v*/\r\n'
        raw = b'diff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n@@ -1,2 +1,2 @@\n-int a;\r\n+int b;\r\n /*\v*/\r\n'
        anchors = validate_patch(parse_diff(raw)[0], old.encode(), new.encode(), '甲')
        self.assertEqual(anchors[-1]['quote'], 'int b;')

    def test_rename_and_quoted_utf8_path(self):
        raw = b'diff --git a/a.c b/b.c\nsimilarity index 90%\nrename from a.c\nrename to b.c\n--- a/a.c\n+++ "b/\\344\\270\\255 space.c"\n@@ -1 +1 @@\n-old\n+new\n'
        p = parse_diff(raw)[0]
        a = validate_patch(p, b'old\n', b'new\n', '甲')[-1]
        self.assertEqual(a['path'], '中 space.c'); self.assertEqual(a['old_path'], 'a.c')

    def test_malformed_or_external_path(self):
        for value in ('../x', '/x', 'x/../y', 'x\tq'):
            raw = f'diff --git a/x b/x\n--- a/{value}\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n'.encode()
            with self.assertRaises(ReviewError): parse_diff(raw)

    def test_anchor_failures_degrade_without_search(self):
        _, anchors, snap = example()
        a = anchors[-1]
        inputs = [({'anchor_id': '猜测', 'quote': a['quote'], 'location_verified': True}, '版本甲'),
                  ({'anchor_id': a['id'], 'quote': a['quote']+' ', 'location_verified': True}, '版本甲'),
                  ({'anchor_id': a['id'], 'quote': a['quote'], 'location_verified': True}, '版本乙'),
                  ({'anchor_id': a['id'], 'quote': a['quote'], 'location_verified': False}, '版本甲')]
        for row, version in inputs:
            self.assertIsNone(locate(row, snap['anchors'], version)[0])

    def test_saved_hunk_checked_against_both_blobs(self):
        p, anchors, snapshot = example()
        a = anchors[-1]
        raw = make_diff(snapshot['files'][0]['old'], snapshot['files'][0]['new']).decode().split('@@', 1)[1]
        comment = {'path': 'code.c', 'position': 1, 'original_position': 0, 'diff_hunk': '@@'+raw}
        verify_hunk(comment, a, snapshot)
        verify_hunk(dict(comment, diff_hunk=comment['diff_hunk'].rstrip('\n')), a, snapshot)
        for field, value in [('path', 'other.c'), ('position', 2), ('diff_hunk', comment['diff_hunk'].replace('return 0', 'return 9'))]:
            with self.assertRaises(ReviewError): verify_hunk(dict(comment, **{field: value}), a, snapshot)

    def test_function_boundaries_and_cpp(self):
        text = 'int f(int a) { if (a) { return a; } return 0; }\nint g() { return 2; }\n'
        found, error = functions(text, 'x.cpp')
        self.assertFalse(error); self.assertEqual(len(found), 2)
        _, _, snap = example(text, text.replace('return 2', 'return 3'))
        items = work_items(snap)
        self.assertTrue(all(i['scope']['name'] == 'g()' for i in items))

    def test_repeated_statement_in_same_function_degrades(self):
        _, _, snap = example('', 'void f(void *p) {\n    free(p);\n    free(p);\n}\n')
        # difflib 的空文件补丁仍有 a/path，固定旧文件内容为空即可校验。
        items = work_items(snap)
        duplicate = next(a for item in items for a in item['anchors'] if a['quote'] == '    free(p);')
        self.assertTrue(duplicate['ambiguous'])
        self.assertIsNone(locate({'anchor_id':duplicate['id'],'quote':duplicate['quote'],'location_verified':True},snap['anchors'],snap['id'])[0])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        key = self.root/'主密钥'; key.write_bytes(Fernet.generate_key())
        env = mock_patch.dict(os.environ, {'AI_REVIEW_MASTER_KEY_FILE': str(key)}); env.start(); self.addCleanup(env.stop)
        self.store = Store(self.root/'review.db')
        self.store.save_config({'revision': 1, 'enabled': True, 'debounce_seconds': 0, 'model': '测试模型'})
        self.repo = self.store.save_repo(1, 7, {'enabled': True, 'bot_token': self.store.seal('仅测试凭据'),
            'webhook_secret': self.store.seal('签名测试秘密'), 'repo_name': 'test/code', 'bot_id': 9})['id']

    def enqueue(self, head='a'):
        return self.store.enqueue(self.repo, 1, {'head': head, 'base': 'b'})

    def test_encryption_and_public_projection(self):
        raw = self.store.repo(self.repo)
        self.assertNotIn('仅测试凭据', (self.root/'review.db').read_bytes().decode(errors='ignore'))
        self.assertEqual(self.store.unseal(raw['bot_token']), '仅测试凭据')
        self.assertNotIn('bot_token', self.store.repo(self.repo, public=True))
        self.assertNotIn('api_key', self.store.config(public=True))

    def test_duplicate_events_atomic(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.store.enqueue(self.repo, 1, {'head': 'a'}, delivery='一次投递'), range(16)))
        self.assertEqual(sum(v is not None for v in results), 1)
        self.assertEqual(self.store.jobs()['total'], 1)

    def test_new_generation_fences_old_owner(self):
        self.enqueue(); old = self.store.claim(); self.enqueue('c')
        with self.assertRaises(Stale): self.store.update(old['id'], old['owner'], stage='错误复活')
        self.assertEqual(self.store.job(old['id'])['status'], 'superseded')

    def test_only_one_claim_and_lease_expiry(self):
        self.enqueue(); self.store.enqueue(self.repo, 2, {'head': 'c'})
        first = self.store.claim(); self.assertIsNone(self.store.claim())
        with self.store.tx() as c: c.execute('UPDATE jobs SET lease=0 WHERE id=?', (first['id'],))
        second = self.store.claim()
        self.assertNotEqual(first['owner'], second['owner'])
        with self.assertRaises(Stale): self.store.active(first['id'], first['owner'])

    def test_configuration_changes_fence_running_job(self):
        self.enqueue(); job = self.store.claim()
        self.store.save_config({'revision': 2, 'enabled': False})
        with self.assertRaises(Stale): self.store.active(job['id'], job['owner'])

    def test_expired_owner_finish_preserves_new_owners_model_authorization(self):
        self.enqueue(); old = self.store.claim()
        capability(self.store, old, '测试模型')
        with self.store.tx() as c:
            c.execute('UPDATE jobs SET lease=0 WHERE id=?', (old['id'],))
        current = self.store.claim()
        token = capability(self.store, current, '测试模型')
        self.store.finish(old['id'], old['owner'], 'superseded')
        self.store.active(current['id'], current['owner'])
        with self.store.connect() as c:
            rows = [dict(r) for r in c.execute('SELECT owner,token_hash FROM capabilities WHERE job=?', (old['id'],))]
        self.assertEqual(rows, [{'owner': current['owner'], 'token_hash': hashlib.sha256(token.encode()).hexdigest()}])

    def test_manual_rerun_is_distinct(self):
        first = self.enqueue()
        self.assertIsNone(self.enqueue())
        second = self.store.enqueue(self.repo, 1, {'head': 'a', 'base': 'b'}, manual=True)
        self.assertNotEqual(first, second)

    def test_failed_manual_review_is_not_replayed_by_reconciliation(self):
        ident = self.store.enqueue(self.repo, 1, {'manual':'人工触发占位'}, manual=True)
        job = self.store.claim()
        self.store.update(ident, job['owner'], snapshot={'id':'固定快照','head':'a','base':'b'})
        self.store.finish(ident, job['owner'], 'failed', '模型结果不确定')
        for _ in range(3):
            self.assertIsNone(self.store.enqueue(self.repo, 1, {'head':'a','base':'b'}))
        self.assertEqual(self.store.jobs()['total'], 1)
        changed = self.store.enqueue(self.repo, 1, {'head':'c','base':'b'})
        self.assertIsNotNone(changed)
        # 真正的代码更新与明确人工重审仍然可创建独立执行。
        rerun = self.store.enqueue(self.repo, 1, {'manual':'再次明确触发'}, manual=True)
        self.assertNotEqual(rerun, changed)

    def test_legacy_finished_manual_snapshot_is_not_replayed_after_upgrade(self):
        for number, status in enumerate(('failed', 'publish_unknown', 'succeeded'), 10):
            ident = self.store.enqueue(self.repo, number, {'manual':'旧版人工触发占位'}, manual=True)
            # 精确构造旧版落盘状态；不调用新版 update，避免测试提前修好了数据。
            with self.store.tx() as c:
                c.execute('UPDATE jobs SET status=?,snapshot=? WHERE id=?',
                          (status, json.dumps({'id':'旧固定快照','head':'a','base':'b'}), ident))
            before = self.store.job(ident)
            for _ in range(3):
                self.assertIsNone(self.store.enqueue(self.repo, number, {'head':'a','base':'b'}))
            self.assertEqual(self.store.job(ident), before)
            self.assertIsNotNone(self.store.enqueue(self.repo, number, {'head':'c','base':'b'}))

    def test_legacy_manual_snapshot_does_not_hide_config_revision_change(self):
        ident = self.store.enqueue(self.repo, 1, {'manual':'旧版占位'}, manual=True)
        with self.store.tx() as c:
            c.execute('UPDATE jobs SET status=?,snapshot=?,config_revision=0 WHERE id=?',
                      ('failed', json.dumps({'head':'a','base':'b'}), ident))
        self.assertIsNotNone(self.store.enqueue(self.repo, 1, {'head':'a','base':'b'}))

    def test_uncertain_publication_blocks_same_pr_not_other_pr(self):
        self.enqueue(); job = self.store.claim()
        self.store.update(job['id'], job['owner'], status='publishing')
        with self.store.tx() as c: c.execute('UPDATE jobs SET lease=0 WHERE id=?', (job['id'],))
        self.enqueue('c'); self.store.enqueue(self.repo, 2, {'head': 'd'})
        next_job = self.store.claim()
        self.assertEqual(next_job['number'], 2)
        self.assertEqual(self.store.job(job['id'])['status'], 'publish_unknown')

    def test_broker_rejects_tools_wrong_models_expired_capability(self):
        self.enqueue(); job = self.store.claim()
        token = capability(self.store, job, '测试模型')
        client = create_broker(self.store).test_client()
        base = {'model': '测试模型', 'messages': [{'role': 'user', 'content': '合成测试'}]}
        for payload in [dict(base, tools=[{'name': 'bash'}]), dict(base, model='别的模型'), dict(base, messages=[{'role':'user','content':[]}])]:
            response = client.post('/v1/chat/completions', json=payload, headers={'Authorization': 'Bearer '+token})
            self.assertEqual(response.status_code, 403)
        with self.store.tx() as c: c.execute('UPDATE capabilities SET expires=0')
        self.assertEqual(client.post('/v1/chat/completions', json=base, headers={'Authorization':'Bearer '+token}).status_code, 403)

    def test_broker_preserves_first_connection_failure_without_replaying(self):
        import requests
        from unittest.mock import Mock
        self.enqueue(); job = self.store.claim()
        token = capability(self.store, job, '测试模型')
        client = create_broker(self.store).test_client()
        payload = {'model':'测试模型','messages':[{'role':'user','content':'合成源码，不得写日志'}]}
        transport = Mock(side_effect=requests.ConnectionError('私密上游细节'))
        with mock_patch('ai_review.broker.endpoint', return_value='http://model.invalid/v1'), \
             mock_patch.object(self.store, 'unseal', return_value='私密 Key'), \
             mock_patch('ai_review.broker.fetch_model', transport), \
             self.assertLogs(level='INFO') as logs:
            first = client.post('/v1/chat/completions', json=payload, headers={'Authorization':'Bearer '+token})
            second = client.post('/v1/chat/completions', json=payload, headers={'Authorization':'Bearer '+token})
        self.assertEqual(first.status_code, 502); self.assertEqual(second.status_code, 403)
        transport.assert_called_once()
        current = self.store.job(job['id'])
        self.assertEqual(current['model_calls'], 1)
        self.assertIn('ConnectionError', current['error'])
        self.assertIn('未自动重放', current['error'])
        output = str(logs.output)+current['error']
        for secret in (token, '私密上游细节', '私密 Key', '合成源码'):
            self.assertNotIn(secret, output)

    def test_published_review_recovery_must_verify_formal_result(self):
        from unittest.mock import Mock
        from ai_review.worker import reconcile_unknown
        self.enqueue(); job = self.store.claim()
        snapshot={'id':'快照','head':'a'*40,'base':'b'*40,'merge_base':'b'*40}
        self.store.update(job['id'],job['owner'],status='publishing',snapshot=snapshot,result={'findings':[]})
        self.store.finish(job['id'],job['owner'],'publish_unknown','最终 Review 响应丢失')
        fake=Mock();fake.pages.return_value=[{'id':7,'state':'COMMENT'}]
        fake.pr.return_value={'state':'open','head':{'sha':'a'*40},'base':{'sha':'b'*40},'merge_base':'b'*40}
        fake.request.return_value = {'id': 7, 'state': 'COMMENT'}
        with mock_patch('ai_review.worker.client_for', return_value=fake), mock_patch('ai_review.worker.Publisher') as pub, mock_patch('ai_review.worker.refresh_pr'):
            pub.return_value.path = '/repos/test/code/pulls/1/reviews'
            pub.return_value.owned.return_value = True
            pub.return_value.reference.return_value = {'review_id': 7, 'review_fragment': 'issuecomment-8'}
            reconcile_unknown(self.store)
            pub.return_value.verify.assert_called_once_with(fake.request.return_value)
            pub.return_value.publish.assert_not_called()
        self.assertEqual(self.store.job(job['id'])['status'], 'succeeded')

    def test_webhook_signature_and_no_network_ingress(self):
        app = Flask('审查测试'); app.secret_key = '测试会话密钥'; app.config['AI_REVIEW_DB'] = str(self.root/'review.db')
        init_auth(app); app.register_blueprint(ai_review_bp, url_prefix='/api/ai-review')
        client = app.test_client()
        self.assertEqual(client.get('/api/ai-review/config').status_code, 401)
        value = {'action':'opened', 'repository':{'id':7,'full_name':'test/code'}, 'pull_request':{
            'number':1, 'head':{'sha':'a'*40}, 'base':{'sha':'b'*40,'repo':{'id':7}}}}
        raw = encode(value).encode(); signature = hmac.new('签名测试秘密'.encode(), raw, hashlib.sha256).hexdigest()
        headers = {'X-Gitea-Signature':signature,'X-Gitea-Event':'pull_request','X-Gitea-Delivery':'delivery-1'}
        url = f'/api/ai-review/webhooks/{self.repo}'
        with mock_patch('requests.Session.request', side_effect=AssertionError('事件接收不能访问网络')):
            response = client.post(url, data=raw, headers=headers)
            self.assertEqual(response.status_code, 202); self.assertTrue(response.json['job'])
            self.assertIsNone(client.post(url,data=raw,headers=headers).json['job'])
            self.assertEqual(client.post(url,data=raw+b' ',headers=headers).status_code, 403)
        login_platform(client)
        token = client.get('/api/ai-review/csrf').json['token']
        self.assertEqual(client.put('/api/ai-review/config',json={}).status_code,403)
        self.assertEqual(client.get('/api/ai-review/config',headers={'Origin':'https://untrusted.invalid'}).status_code,403)
        response = client.put('/api/ai-review/config',json={'revision':1},headers={'X-Review-CSRF':token})
        self.assertEqual(response.status_code,400)


class OutputTests(unittest.TestCase):
    def test_unknown_candidates_and_incomplete_refutation_rejected(self):
        for result in ({}, {'findings':'文本'}, {'findings':[{'severity':'P0'}]}):
            with self.assertRaises(ReviewError): candidates(result)
        row = {'candidate_id':'真实候选'}
        with self.assertRaises(ReviewError): confirm([row], {'checks':[]})
        with self.assertRaises(ReviewError): confirm([row], {'checks':[{'candidate_id':'编造候选','verdict':'confirmed','location_verified':True,'reason':'测试'}]})


if __name__ == '__main__':
    unittest.main()
