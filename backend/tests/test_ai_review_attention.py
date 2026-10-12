"""紧急待办的计数、代次通知、并发合并、迁移和鉴权回归。"""
import concurrent.futures
import time
import unittest
from unittest.mock import patch, Mock
from flask import Flask
from tests.access_helpers import init_test_auth as init_auth, login_platform
from routes.ai_review_routes import ai_review_bp
from ai_review.attention import listing, request_reconciliation
from ai_review.progress import state_for, payload_for
from ai_review.store import Store
from ai_review.worker import reconcile_unknown, confirm_unpublished
import test_ai_review as core


class AttentionTests(unittest.TestCase):
    setUp = core.StoreTests.setUp

    def queued(self, number=1):
        return self.store.enqueue(self.repo, number, {'head': str(time.time_ns()), 'base': 'b'}, manual=True)

    def unknown(self, number=1):
        ident = self.queued(number)
        with self.store.tx() as c:
            c.execute("UPDATE jobs SET status='publish_unknown',stage='发布结果待核对',error='回执丢失',available=0 WHERE id=?", (ident,))
        return ident

    def test_global_counts_do_not_multiply_or_depend_on_history_page(self):
        a = self.unknown(); self.unknown(); successor = self.queued()
        self.unknown(2)
        for n in range(200):
            ident = self.queued(100+n)
            with self.store.tx() as c:
                c.execute("UPDATE jobs SET status='superseded' WHERE id=?", (ident,))
        result = listing(self.store)
        self.assertEqual(result['summary'], dict(pr_count=2, blocked_task_count=1, pending_task_count=3))
        self.assertEqual(result['items'][0]['number'], 1)
        self.assertEqual(result['items'][0]['pending_count'], 2)
        self.assertEqual(listing(self.store, page=99)['items'], [])
        self.assertEqual(listing(self.store, number=2)['summary'], result['summary'])
        self.assertEqual(self.store.jobs(number=1, status='queued')['items'][0]['id'], successor)
        self.assertNotIn(a, [j['id'] for j in self.store.jobs()['items']])

    def test_pagination_and_priority_without_successors(self):
        for n in range(1, 26): self.unknown(n)
        self.queued(25)
        first, second = listing(self.store), listing(self.store, page=2)
        self.assertEqual(first['total'], 25); self.assertEqual(len(second['items']), 5)
        self.assertEqual(first['items'][0]['number'], 25)
        self.assertEqual(len({r['number'] for r in first['items']+second['items']}), 25)

    def test_old_generations_notify_current_pr_and_claim_waits_for_all(self):
        a = self.unknown(); b = self.unknown(); current = self.queued()
        before, _ = state_for(self.store, self.repo, 1)
        self.assertIsNone(self.store.claim())
        with self.store.tx() as c: c.execute("UPDATE jobs SET status='failed',stage='已确认未发布' WHERE id=?", (a,))
        after, job = state_for(self.store, self.repo, 1)
        self.assertGreater(after['progress_seq'], before['progress_seq'])
        self.assertEqual(job['id'], current); self.assertEqual(job['progress']['label'], '等待人工处理')
        self.assertTrue(payload_for(self.store.repo(self.repo), after, job, True, {'head': {'sha': job['target_head']}})['attention']['required'])
        self.assertIsNone(self.store.claim())
        with self.store.tx() as c: c.execute("UPDATE jobs SET status='succeeded' WHERE id=?", (b,))
        state, job = state_for(self.store, self.repo, 1)
        self.assertGreater(state['progress_seq'], after['progress_seq'])
        self.assertFalse(job['attention']['required'])
        self.assertEqual(self.store.claim()['id'], current)

    def test_old_generation_entering_unknown_notifies_latest(self):
        old = self.queued(); current = self.queued()
        before, _ = state_for(self.store, self.repo, 1)
        with self.store.tx() as c: c.execute("UPDATE jobs SET status='publish_unknown' WHERE id=?", (old,))
        after, job = state_for(self.store, self.repo, 1)
        self.assertGreater(after['progress_seq'], before['progress_seq'])
        self.assertEqual(job['id'], current); self.assertTrue(job['attention']['required'])

    def test_concurrent_requests_merge_and_failed_check_can_be_requested_again(self):
        ident = self.unknown()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: request_reconciliation(self.store, ident), range(20)))
        self.assertEqual(sum(results), 1)
        job = self.store.job(ident, public=True)
        self.assertEqual(job['reconciliation']['requested_by'], '共享管理员')
        self.assertEqual(job['reconciliation']['state'], 'queued')
        self.assertIsNotNone(job['reconciliation']['requested_at'])
        reconcile_unknown(self.store)
        job = self.store.job(ident)
        self.assertEqual(job['reconciliation']['state'], 'needs_confirmation')
        self.assertIsNotNone(job['reconciliation']['started_at'])
        self.assertIsNotNone(job['reconciliation']['finished_at'])
        self.assertTrue(request_reconciliation(self.store, ident))

    def test_network_failure_is_visible_and_does_not_unblock(self):
        ident = self.unknown()
        with self.store.tx() as c: c.execute("UPDATE jobs SET snapshot='{}',result='{}' WHERE id=?", (ident,))
        with patch('ai_review.worker.client_for', side_effect=core.ReviewError('Gitea 连接失败')):
            # 非空快照进入连接核对路径。
            with self.store.tx() as c: c.execute('UPDATE jobs SET snapshot=?,result=? WHERE id=?', ('{"head":"a"}', '{"findings":[]}', ident))
            reconcile_unknown(self.store)
        job = self.store.job(ident)
        self.assertEqual(job['reconciliation']['state'], 'failed')
        self.assertIn('Gitea 连接失败', job['reconciliation']['message'])
        self.assertTrue(job['attention']['required'])

    def test_idempotent_migration_projection_and_worker_unavailable(self):
        ident = self.unknown()
        with self.store.tx() as c: c.execute("UPDATE jobs SET reconcile_state='',reconcile_message='' WHERE id=?", (ident,))
        Store(self.root/'review.db'); Store(self.root/'review.db')
        job = self.store.job(ident)
        self.assertEqual(job['reconciliation']['state'], 'needs_confirmation')
        self.assertFalse(listing(self.store)['worker']['available'])
        self.store.health('worker', '隔离测试心跳')
        self.assertTrue(listing(self.store)['worker']['available'])
        with self.store.tx() as c: c.execute('UPDATE health SET updated=0')
        self.assertFalse(listing(self.store)['worker']['available'])
        expired = self.queued(3); self.queued(3)
        self.assertEqual(self.store.job(expired)['stage'], '版本已过期')

    def test_repeated_confirmation_has_no_second_remote_call(self):
        ident = self.unknown()
        client = Mock(); client.prefix = '/repos/test/code'; client.pages.return_value = []
        with patch('ai_review.worker.client_for', return_value=client) as factory:
            confirm_unpublished(self.store, ident); confirm_unpublished(self.store, ident)
            self.assertEqual(factory.call_count, 1)
        self.assertEqual(self.store.job(ident)['reconciliation']['state'], 'resolved')
        self.assertFalse(request_reconciliation(self.store, ident))
        self.assertFalse(listing(self.store)['summary']['pr_count'])

    def test_endpoints_require_login_csrf_and_validate_filters(self):
        ident = self.unknown()
        app = Flask('待办接口测试'); app.secret_key = '隔离会话'; app.config['AI_REVIEW_DB'] = str(self.root/'review.db')
        init_auth(app); app.register_blueprint(ai_review_bp, url_prefix='/api/ai-review')
        client = app.test_client()
        self.assertEqual(client.get('/api/ai-review/attention').status_code, 401)
        login_platform(client)
        self.assertEqual(client.get('/api/ai-review/attention').json['summary']['pr_count'], 1)
        for query in ('number=-1', 'repository=x', 'page=0'):
            self.assertEqual(client.get('/api/ai-review/attention?'+query).status_code, 400)
        self.assertEqual(client.get('/api/ai-review/tasks?status=unknown').status_code, 400)
        self.assertEqual(client.get('/api/ai-review/tasks?number=1&status=publish_unknown').json['total'], 1)
        path = '/api/ai-review/tasks/'+ident+'/reconcile'
        self.assertEqual(client.post(path, json={}).status_code, 403)
        token = client.get('/api/ai-review/csrf').json['token']
        headers = {'X-Review-CSRF': token}
        self.assertTrue(client.post(path, json={}, headers=headers).json['requested'])
        self.assertFalse(client.post(path, json={}, headers=headers).json['requested'])
        self.assertEqual(client.post(path, json={'confirm_unpublished':'true'}, headers=headers).status_code, 400)
        text = client.get('/api/ai-review/attention').get_data(as_text=True)
        self.assertNotIn('bot_token', text); self.assertNotIn('webhook_secret', text)

    def test_shared_reconciliation_lock_keeps_setup_exclusive(self):
        with self.store.lock('自动接入执行', shared=True):
            with self.store.lock('自动接入执行', shared=True): pass
            with self.assertRaises(core.ReviewError):
                with self.store.lock('自动接入执行'): pass
        with self.store.lock('自动接入执行'):
            with self.assertRaises(core.ReviewError):
                with self.store.lock('自动接入执行', shared=True): pass

    def test_legacy_database_upgrade_preserves_history(self):
        ident = self.unknown()
        fields = ('reconcile_state','reconcile_requested_at','reconcile_started_at','reconcile_finished_at','reconcile_message','reconcile_requested_by')
        before = self.store.job(ident)
        with self.store.tx() as c:
            c.execute('DROP TRIGGER review_attention_transition')
            for field in fields: c.execute('ALTER TABLE jobs DROP COLUMN '+field)
        Store(self.root/'review.db'); Store(self.root/'review.db')
        after = self.store.job(ident)
        for field in ('id','status','created','updated','stage','config_revision','repo_revision','error'):
            self.assertEqual(after[field], before[field])
        self.assertEqual(listing(self.store)['summary']['pr_count'],1)
        self.assertEqual(after['reconciliation']['state'],'needs_confirmation')
