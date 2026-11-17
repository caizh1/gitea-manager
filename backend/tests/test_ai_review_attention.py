"""自动核对回执的投影与退役接口验证。"""
import unittest
from flask import Flask
from tests.access_helpers import init_test_auth, login_platform
from routes.ai_review_routes import ai_review_bp
from ai_review.attention import listing
import test_ai_review as core


class AttentionTests(unittest.TestCase):
    setUp = core.StoreTests.setUp

    def test_pending_receipts_do_not_require_admin_action(self):
        for number in range(1, 26):
            ident = self.store.enqueue(self.repo, number, {'head': str(number), 'base': 'b'})
            with self.store.tx() as c:
                c.execute("UPDATE jobs SET status='publish_unknown' WHERE id=?", (ident,))
        page = listing(self.store)
        self.assertEqual(page['total'], 25)
        self.assertEqual(page['summary']['blocked_task_count'], 0)
        self.assertEqual(len(listing(self.store, page=2)['items']), 5)
        self.assertFalse(self.store.job(ident)['attention']['required'])
        self.assertEqual(self.store.job(ident)['reconciliation']['state'], 'checking')

    def test_retired_publication_endpoints_return_gone(self):
        app = Flask('自动发布接口验收')
        app.secret_key = '独立接口测试'
        app.config['AI_REVIEW_DB'] = str(self.root / 'review.db')
        init_test_auth(app)
        app.register_blueprint(ai_review_bp, url_prefix='/api/ai-review')
        client = app.test_client()
        self.assertEqual(client.get('/api/ai-review/attention').status_code, 401)
        login_platform(client)
        token = client.get('/api/ai-review/csrf').json['token']
        headers = {'X-Review-CSRF': token}
        for method, suffix in [('GET', 'publication-preview'), ('POST', 'approve-publication'), ('POST', 'reconcile')]:
            response = client.open('/api/ai-review/tasks/legacy/' + suffix, method=method, json={}, headers=headers)
            self.assertEqual(response.status_code, 410)
            self.assertEqual(response.json['code'], 'AUTOMATIC_PUBLICATION')
        self.assertEqual(client.get('/api/ai-review/tasks?status=invalid').status_code, 400)
