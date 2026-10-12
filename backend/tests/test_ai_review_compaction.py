"""压缩配置冻结、输入校验和压缩期间的可见状态。"""
import unittest
from ai_review.budgets import CONTEXT_DEFAULTS, validate_context
from ai_review.common import ReviewError
from ai_review.session_events import create_session, mark_submitted, receive, project
from tests import test_ai_review_time_budgets as timing


class ContextValidationTests(unittest.TestCase):
    def test_window_and_threshold_reserve_output(self):
        validate_context(CONTEXT_DEFAULTS)
        for values in ({'compaction_threshold_tokens': True}, {'compaction_threshold_tokens': 262144},
                       {'model_context_tokens': 32768}, {'model_max_output_tokens': 60000},
                       {'compaction_threshold_tokens': -1}):
            with self.subTest(values=values), self.assertRaises(ReviewError):
                validate_context(dict(CONTEXT_DEFAULTS, **values))
        validate_context(dict(CONTEXT_DEFAULTS, model_max_output_tokens=52144))


class ContextTaskTests(unittest.TestCase):
    setUp = timing.TimeBudgetTests.setUp
    enqueue = timing.TimeBudgetTests.enqueue
    change = timing.TimeBudgetTests.change

    def test_context_settings_api_persists_and_rejects_invalid_windows(self):
        from flask import Flask
        from tests.access_helpers import init_test_auth as init_auth, login_platform
        from routes.ai_review_routes import ai_review_bp
        self.change(model='fixture', enabled=False, api_base_url='')
        app = Flask('压缩配置接口验收'); app.secret_key = '测试会话'
        app.config['AI_REVIEW_DB'] = str(self.store.path)
        init_auth(app); app.register_blueprint(ai_review_bp, url_prefix='/api/ai-review')
        client = app.test_client()
        login_platform(client)
        headers = {'X-Review-CSRF': client.get('/api/ai-review/csrf').json['token']}
        initial = client.get('/api/ai-review/config').json['config']
        self.assertEqual(initial['compaction_threshold_tokens'], 210000)
        for invalid in (True, 0, 262144, 210000.5, '210000'):
            response = client.put('/api/ai-review/config', json={'revision': initial['revision'],
                'compaction_threshold_tokens': invalid}, headers=headers)
            self.assertEqual(response.status_code, 400)
        response = client.put('/api/ai-review/config', json={'revision': initial['revision'],
            'compaction_threshold_tokens': 200000, 'model_context_tokens': 262144}, headers=headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(client.get('/api/ai-review/config').json['config']['compaction_threshold_tokens'], 200000)
    def test_context_edit_only_changes_new_jobs(self):
        ident = self.enqueue(); job = self.store.claim()
        before = self.store.job_config(ident)
        self.change(compaction_threshold_tokens=180000, model_context_tokens=262144)
        self.store.active(ident, job['owner'])
        self.assertEqual(self.store.job_config(ident), before)
        new = self.store.enqueue(self.repo, 99, {'head':'新提交','base':'基点'})
        self.assertEqual(self.store.job_config(new)['compaction_threshold_tokens'], 180000)

    def test_compaction_survives_heartbeats_and_model_events_then_completes(self):
        import time
        ident = self.enqueue(); job = self.store.claim()
        session, token, _ = create_session(self.store, ident, job['owner'], time.time()+600, '/app/review-workspaces/'+ident)
        token = mark_submitted(self.store, ident, job['owner'], token)
        for seq, (kind, phase) in enumerate([('session.compaction','running'),('model.started','running'),
                                          ('heartbeat','running'),('session.compaction','completed')], 1):
            receive(self.store, token, {'job_id':ident,'session_id':session,'seq':seq,'event_id':session+':'+str(seq),
                'type':kind,'status':phase,'created_at':time.time(),'summary':'事件',
                'payload':{'phase':phase, 'compaction_threshold_tokens':210000}})
            row = project(self.store.job(ident, evidence=False))
            self.assertEqual(row['counters']['compaction_active'], seq < 4)
            if seq < 4:
                self.assertIn('模型正在压缩', row['current_action'])
        self.assertEqual(row['counters']['compactions'], 1)
