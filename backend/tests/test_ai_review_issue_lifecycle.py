"""台账与评论的对抗式回归：模拟模型只验证工程机制，不替代模型质量验收。"""
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

import test_ai_review as core
from test_ai_review_publication import MemoryGitea
from ai_review.common import ReviewError, PublishUnknown, Stale, encode
from ai_review.diff_inline import index
from ai_review.issue_checks import run_check
from ai_review.issue_lifecycle import reconcile, listing, overview, validate_check
from ai_review.publish import Publisher


class Client(MemoryGitea):
    def request(self, method, path, data=None, **kwargs):
        if method == 'PATCH' and '/issues/comments/' in path:
            self.calls.append((method, path, data))
            ident = int(path.split('/')[-1])
            for rows in self.code.values():
                for row in rows:
                    if row['id'] == ident:
                        row['body'] = data['body']
                        if self.fail_after == 'edit':
                            self.fail_after = ''; raise PublishUnknown('模拟状态写入回执丢失')
                        return copy.deepcopy(row)
            raise AssertionError('评论不存在')
        result = super().request(method, path, data)
        if method == 'POST' and path.endswith('/reviews'):
            for i, row in enumerate(self.code[result['id']]):
                row['id'] = result['id'] * 1000 + i
                row['diff_hunk'] = '@@' + self.snapshot['diff'].split('@@', 1)[1]
            result['html_url'] = 'http://gitea/test/code/pulls/1#issuecomment-' + str(result['id'])
        return result


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        core.StoreTests.setUp(self)
        self.version = 0
        self.begin()
        self.client = Client(self.snapshot)
        self.repo_value = self.store.repo(self.repo)
        self.calls = 0

    def begin(self, new='int f(void) { return 1; }\n'):
        self.version += 1
        head = f'{self.version:040x}'
        self.store.enqueue(self.repo, 1, {'head': head, 'base': 'b'*40})
        self.job = self.store.claim()
        # 保留旧单会话历史复核协议；分批复核另行验证总额度。
        with self.store.tx() as c: c.execute("UPDATE jobs SET batch_protocol=0 WHERE id=?", (self.job["id"],))
        self.job["batch_protocol"] = 0
        self.snapshot = {'id': '版本' + str(self.version), 'head': head, 'base': 'b'*40, 'merge_base': 'b'*40,
                         'review_scope': 'diff', 'lifecycle_version': 1,
                         'diff': core.make_diff('int f(void) { return 0; }\n', new).decode(), 'files': [],
                         'coverage': {'scope': 'diff', 'files_total': 1, 'files_reviewed': 1, 'complete': True, 'skipped': []}}
        self.snapshot['anchors'], _ = index(self.snapshot)
        self.store.update(self.job['id'], self.job['owner'], snapshot=self.snapshot)
        if hasattr(self, 'client'): self.client.snapshot = self.snapshot

    def report(self, title='返回值错误'):
        anchor = next((a for a in self.snapshot['anchors'].values() if a['side'] == 'new'), None)
        return {'review_scope': 'diff', 'report_markdown': '仅供协议验收的报告', 'coverage': self.snapshot['coverage'],
                'findings': [{'kind': 'diff_inline', 'severity': 'P1', 'title': title, 'comment_markdown': '原始意见不应被覆盖',
                              'fingerprint': title, 'path': 'code.c', 'scope': 'PR diff', 'anchor': anchor}]}

    def answer(self, state='present', relation='same'):
        def complete(store, job, ident, messages, config):
            self.calls += 1
            payload = json.loads(messages[1]['content'])
            evidence = []
            for field in ('historical_diff', 'current_diff'):
                if payload[field]:
                    key = next(k for k, v in payload[field].items() if v.startswith('+int'))
                    evidence.append({'id': key, 'quote': payload[field][key]})
            return {'issue_id': ident, 'snapshot_id': self.snapshot['id'], 'status': state, 'reason': '测试用固定证据结论',
                    'evidence': evidence, 'relations': {f['candidate_id']: relation for f in payload['candidates']},
                    'anchor_id': None, 'obsolete_reason': 'withdrawn' if state == 'obsolete' else None}, {'说明': '协议替身'}
        return complete

    def apply(self, result=None, answer=None):
        with patch('ai_review.broker.issue_completion', answer or self.answer()):
            result = reconcile(self.store, self.client, self.job, self.repo_value, self.snapshot, result or self.report())
        Publisher(self.store, self.client, self.job, self.repo_value, self.snapshot, result).publish()
        self.store.finish(self.job['id'], self.job['owner'], 'succeeded')
        return result

    def test_more_than_ten_versions_title_changes_reuse_one_comment(self):
        self.apply()
        for i in range(12):
            self.begin(); result = self.apply(self.report('同一根因的新标题' + str(i)))
            self.assertEqual(result['lifecycle']['new_ids'], [])
        self.assertEqual(len(self.client.reviews), 1)
        self.assertEqual(self.calls, 12)
        self.assertEqual(listing(self.store, self.repo, 1)['total'], 1)
        self.assertIn('原始意见不应被覆盖', self.client.code[1][0]['body'])

    def test_fixed_then_reappeared_keeps_original_identity(self):
        first = self.apply(); ident = first['lifecycle']['new_ids'][0]
        self.begin('int f(void) { return 2; }\n')
        empty = dict(self.report(), findings=[])
        self.apply(empty, self.answer('fixed'))
        self.assertEqual(listing(self.store, self.repo, 1)['items'][0]['status'], 'fixed')
        self.begin(); result = self.apply()
        self.assertEqual(result['findings'][0]['issue_id'], ident)
        self.assertEqual(len(self.client.reviews), 1)
        self.assertIn('仍存在', self.client.code[1][0]['body'])

    def test_omitted_by_discovery_still_checked(self):
        self.apply(); self.begin()
        self.apply(dict(self.report(), findings=[]))
        self.assertEqual(listing(self.store, self.repo, 1)['items'][0]['status'], 'present')
        self.assertEqual(len(self.client.reviews), 1)

    def test_distinct_root_cause_on_same_line_is_new(self):
        self.apply(); self.begin()
        self.apply(self.report('另外一种根因'), self.answer(relation='different'))
        self.assertEqual(len(self.client.reviews), 2)
        self.assertEqual(listing(self.store, self.repo, 1)['total'], 2)

    def test_bad_evidence_defers_candidate_without_closing_old(self):
        self.apply(); self.begin()
        good = self.answer('fixed', 'different')
        def wrong(*args):
            value, metrics = good(*args); value['evidence'][0]['quote'] = '伪造证据'; return value, metrics
        result = self.apply(answer=wrong)
        self.assertEqual(len(result['lifecycle']['deferred']), 1)
        self.assertEqual(listing(self.store, self.repo, 1)['items'][0]['status'], 'uncertain')
        self.assertEqual(len(self.client.reviews), 1)

    def test_failure_does_not_loop_or_repeat_after_resume(self):
        self.apply(); self.begin()
        with patch('ai_review.broker.issue_completion', side_effect=ReviewError('模拟超时')) as model:
            result = reconcile(self.store, self.client, self.job, self.repo_value, self.snapshot, self.report())
            reconcile(self.store, self.client, self.job, self.repo_value, self.snapshot, result)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(listing(self.store, self.repo, 1)['items'][0]['status'], 'uncertain')

    def test_sent_unpersisted_check_is_not_replayed(self):
        self.apply(); self.begin()
        def interrupted(store, job, ident, snapshot):
            with store.tx() as c:
                c.execute("UPDATE review_issue_checks SET state='started' WHERE job=? AND issue=?", (job['id'], ident))
            raise RuntimeError('模拟进程中断')
        with self.assertRaises(RuntimeError):
            reconcile(self.store, self.client, self.job, self.repo_value, self.snapshot, self.report(), interrupted)
        with patch('ai_review.broker.issue_completion') as model:
            result = reconcile(self.store, self.client, self.job, self.repo_value, self.snapshot, self.report())
        model.assert_not_called()
        self.assertEqual(len(result['lifecycle']['deferred']), 1)

    def test_manual_edit_and_deletion_are_not_overwritten(self):
        self.apply(); self.client.code[1][0]['body'] += '\n人工补充'; self.begin()
        self.apply()
        self.assertTrue(self.client.code[1][0]['body'].endswith('人工补充'))
        self.assertIn('人工编辑', listing(self.store, self.repo, 1)['items'][0]['comments'][0]['error'])
        self.client.code[1].clear(); self.begin(); self.apply()
        self.assertEqual(len(self.client.reviews), 1)
        self.assertIn('已删除', listing(self.store, self.repo, 1)['items'][0]['comments'][0]['error'])

    def test_new_head_marks_history_stale(self):
        self.apply(); self.begin()
        self.assertTrue(listing(self.store, self.repo, 1)['items'][0]['stale'])
        self.assertIn('当前待复核', overview(self.store, self.repo_value, self.job, self.client.pr(1)))

    def test_new_version_during_check_cannot_commit_old_status(self):
        self.apply(); self.begin()
        answer = self.answer('fixed', 'different')
        def moved(*args):
            result = answer(*args)
            self.store.enqueue(self.repo, 1, {'head': 'f'*40, 'base': 'b'*40})
            return result
        with patch('ai_review.broker.issue_completion', moved), self.assertRaises(Stale):
            reconcile(self.store, self.client, self.job, self.repo_value, self.snapshot, self.report())
        self.assertEqual(listing(self.store, self.repo, 1)['items'][0]['status'], 'present')

    def test_lost_creation_receipt_does_not_resend(self):
        result = reconcile(self.store, self.client, self.job, self.repo_value, self.snapshot, self.report())
        publisher = Publisher(self.store, self.client, self.job, self.repo_value, self.snapshot, result)
        self.client.fail_after = 'COMMENT'
        with self.assertRaises(PublishUnknown): publisher.publish()
        publisher.publish(recovery=True)
        self.assertEqual(len(self.client.reviews), 1)

    def test_empty_diff_can_obsolete_without_repository_read(self):
        self.apply(); self.begin('int f(void) { return 0; }\n')
        self.snapshot['diff'] = ''; self.snapshot['anchors'] = {}
        self.apply(dict(self.report(), findings=[]), self.answer('obsolete'))
        self.assertEqual(listing(self.store, self.repo, 1)['items'][0]['status'], 'obsolete')

    def test_unknown_edit_receipt_is_reconciled_before_next_generation(self):
        self.apply(); self.begin()
        self.client.fail_after = 'edit'
        self.apply()
        self.assertTrue(listing(self.store, self.repo, 1)['items'][0]['comments'][0]['error'])
        self.begin(); self.apply()
        item = listing(self.store, self.repo, 1)['items'][0]
        self.assertEqual(item['comments'][0]['error'], '')
        self.assertIn(self.snapshot['head'], self.client.code[1][0]['body'])
        self.assertEqual(len(self.client.reviews), 1)

    def test_unknown_create_import_preserves_stable_identity(self):
        result = reconcile(self.store, self.client, self.job, self.repo_value, self.snapshot, self.report())
        publisher = Publisher(self.store, self.client, self.job, self.repo_value, self.snapshot, result)
        publisher.publish()
        ident = result['lifecycle']['new_ids'][0]
        with self.store.tx() as c: c.execute('DELETE FROM review_issue_comments')
        self.store.finish(self.job['id'], self.job['owner'], 'succeeded')
        self.begin(); self.apply()
        items = listing(self.store, self.repo, 1)
        self.assertEqual(items['total'], 1)
        self.assertEqual(items['items'][0]['id'], ident)
        self.assertEqual(len(self.client.reviews), 1)

    def test_prune_preserves_full_lifecycle_report(self):
        self.apply()
        with self.store.tx() as c:
            c.execute('INSERT INTO review_archive_jobs VALUES (?)', (self.job['id'],))
            c.execute('UPDATE jobs SET updated=1 WHERE id=?', (self.job['id'],))
        self.store.prune()
        saved = self.store.job(self.job['id'])
        self.assertEqual(saved['result']['report_markdown'], self.report()['report_markdown'])
        self.assertEqual(saved['snapshot']['diff'], self.snapshot['diff'])
        self.assertEqual(listing(self.store, self.repo, 1)['total'], 1)

    def test_legacy_manually_edited_comment_blocks_duplicate_publication(self):
        report = self.report()
        self.store.update(self.job['id'], self.job['owner'], result=report)
        Publisher(self.store, self.client, self.job, self.repo_value, self.snapshot, report).publish()
        self.client.code[1][0]['body'] = '人工改写历史意见并移除内部标记'
        self.store.finish(self.job['id'], self.job['owner'], 'succeeded')
        self.begin()
        result = self.apply(answer=lambda *args: (_ for _ in ()).throw(ReviewError('历史正文无法确认身份')))
        self.assertEqual(len(self.client.reviews), 1)
        self.assertEqual(len(result['lifecycle']['deferred']), 1)
        self.assertEqual(self.client.code[1][0]['body'], '人工改写历史意见并移除内部标记')

    def test_no_findings_does_not_create_review(self):
        self.apply(dict(self.report(), findings=[]))
        self.assertEqual(self.client.reviews, [])

    def test_real_transport_is_one_plain_request_ignoring_global_call_count(self):
        self.apply(); self.begin()
        requests_seen = []
        answer = self.answer()
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests_seen.append(body)
                payload = json.loads(body['messages'][1]['content'])
                value, _ = answer(outer.store, outer.job, payload['issue_id'], body['messages'], {})
                data = json.dumps({'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}],
                                   'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        config = dict(self.store.job_config(self.job['id']), api_base_url=f'http://127.0.0.1:{server.server_port}/v1',
                      api_key=self.store.seal('isolated-test-key'), max_calls=1)
        with self.store.tx() as c: c.execute('UPDATE jobs SET model_calls=100 WHERE id=?', (self.job['id'],))
        with patch.object(self.store, 'job_config', return_value=config), patch.dict('os.environ', {'AI_REVIEW_MODEL_HOSTS': '127.0.0.1'}):
            result = reconcile(self.store, self.client, self.job, self.repo_value, self.snapshot, self.report())
        self.assertEqual(result['lifecycle']['new_ids'], [])
        with self.store.connect() as c:
            record = dict(c.execute('SELECT * FROM review_issue_checks WHERE job=?', (self.job['id'],)).fetchone())
        self.assertEqual(len(requests_seen), 1, record['error'])
        for key in ('tools', 'tool_choice', 'functions', 'response_format'):
            self.assertNotIn(key, requests_seen[0])
        self.assertEqual(self.store.job(self.job['id'])['model_calls'], 101)
        with self.store.connect() as c:
            metrics = json.loads(c.execute('SELECT metrics FROM review_issue_checks WHERE job=?', (self.job['id'],)).fetchone()[0])
        self.assertEqual(metrics['usage']['total_tokens'], 20)

    def test_background_unknown_recovery_never_resends(self):
        from ai_review.worker import reconcile_unknown
        result = reconcile(self.store, self.client, self.job, self.repo_value, self.snapshot, self.report())
        publisher = Publisher(self.store, self.client, self.job, self.repo_value, self.snapshot, result)
        self.client.fail_after = 'COMMENT'
        with self.assertRaises(PublishUnknown): publisher.publish()
        self.store.finish(self.job['id'], self.job['owner'], 'publish_unknown')
        with patch('ai_review.worker.client_for', return_value=self.client):
            self.client.close = lambda: None
            reconcile_unknown(self.store)
        self.assertEqual(self.store.job(self.job['id'])['status'], 'succeeded')
        self.assertEqual(len(self.client.reviews), 1)

    def test_unanchored_explicit_problem_tracked_without_comment(self):
        from ai_review.diff_review import adapt
        result = adapt({'review_scope': 'diff', 'report_markdown': '### [P1] 已确认但不可定位\n定位：无\n触发原因与证据',
                        '_review_completion': {'complete': True}}, self.snapshot)
        self.apply(result)
        self.assertEqual(len(self.client.reviews), 0)
        self.assertEqual(listing(self.store, self.repo, 1)['total'], 1)

    def test_later_reliable_anchor_publishes_unposted_issue_once(self):
        result = self.report(); result['findings'][0]['anchor'] = None
        self.apply(result); self.begin(); result = self.apply()
        self.assertEqual(result['lifecycle']['new_ids'], [])
        self.assertEqual(len(self.client.reviews), 1)
        self.begin(); self.apply()
        self.assertEqual(len(self.client.reviews), 1)

    def test_task_list_is_paginated_and_excludes_later_issues(self):
        self.apply(); first_job = self.job['id']; self.begin()
        self.apply(self.report('不同问题'), self.answer(relation='different'))
        self.assertEqual(listing(self.store, self.repo, 1, job=first_job)['total'], 1)
        self.assertEqual(listing(self.store, self.repo, 1)['total'], 2)


if __name__ == '__main__': unittest.main()
