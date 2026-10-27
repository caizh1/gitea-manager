"""自动失败检查的固定样本、原始错误保留、权限与只读边界。"""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from flask import Flask
from ai_review.common import ReviewError
from ai_review.failure_checks import analyze, attach
from ai_review.store import Store
from ai_review.worker import _failure_diagnostic, _finish_failure
from routes.ai_review_routes import ai_review_bp
from tests.access_helpers import init_test_auth, login_platform


class FailureChecksTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        root = Path(folder.name); key = root/'master.key'; key.write_bytes(Fernet.generate_key())
        env = patch.dict(os.environ, AI_REVIEW_MASTER_KEY_FILE=str(key)); env.start(); self.addCleanup(env.stop)
        self.store = Store(root/'review.db')
        self.store.save_config({'revision': 1, 'enabled': True, 'model': '固定模型', 'debounce_seconds': 0})
        repo = self.store.save_repo(1, 7, {'enabled': True, 'repo_name': 'test/code'})
        ident = self.store.enqueue(repo['id'], 1, {'head': 'a', 'base': 'b'})
        self.job = self.store.job(ident, evidence=False)
        self.job['session_id'] = 'rvw_fixture'
        self.unit = {'id': 'unit-one', 'kind': 'primary', 'evidence': [{'id': 'F1H1L1', 'anchor_id': 'R1', 'text': '不得出现在诊断中的源码'}]}
        self.report = {'summary': '固定结论', 'limitations': [], 'findings': [{'severity': 'P1', 'title': '固定问题',
            'trigger': '固定触发', 'explanation': '固定因果', 'impact': '固定影响', 'recommendation': '固定建议',
            'evidence_ids': ['R1'], 'anchor_id': 'R1'}]}
        self.raw = {'report_markdown': json.dumps(self.report), '_review_completion': {'complete': True, 'finish_reason': 'stop'}}
        self.diagnostic = {'error_code': 'BATCH_OUTPUT_INVALID', 'reason': 'batch_evidence_unknown', 'session_id': 'rvw_fixture'}

    def seed(self):
        with self.store.tx() as c:
            c.execute('INSERT INTO review_batch_units(job,id,position,kind,value) VALUES(?,?,0,?,?)',
                      (self.job['id'], self.unit['id'], 'primary', json.dumps(self.unit)))
            c.execute('INSERT INTO review_batch_attempts(job,unit,attempt,session_id,raw,created) VALUES(?,?,1,?,?,0)',
                      (self.job['id'], self.unit['id'], 'rvw_fixture', json.dumps(self.raw)))

    def other(self, refs, job=None, ident='unit-two'):
        with self.store.tx() as c:
            c.execute('INSERT INTO review_batch_units(job,id,position,kind,value) VALUES(?,?,1,?,?)',
                      (job or self.job['id'], ident, 'primary', json.dumps({'id': ident, 'evidence': refs})))

    def check(self, result, code):
        return next(row for row in result['checks'] if row['code'] == code)

    def test_unknown_reference_is_compared_to_current_job_only(self):
        self.report['findings'][0].update(evidence_ids=['R9'], anchor_id=None)
        self.raw['report_markdown'] = json.dumps(self.report); self.seed()
        self.other([{'id': 'F2H1L1', 'anchor_id': 'R9'}])
        result = analyze(self.store, self.job, self.diagnostic)
        self.assertIn('R9', self.check(result, 'current_unit_reference')['detail'])
        self.assertEqual(self.check(result, 'other_unit_reference')['state'], 'confirmed')
        self.assertNotIn('不得出现在诊断中的源码', json.dumps(result, ensure_ascii=False))

    def test_other_job_is_not_used_to_explain_reference(self):
        self.report['findings'][0].update(evidence_ids=['R9'], anchor_id=None)
        self.raw['report_markdown'] = json.dumps(self.report); self.seed()
        self.other([{'id': 'F2H1L1', 'anchor_id': 'R9'}], job='another-job')
        result = analyze(self.store, self.job, self.diagnostic)
        self.assertIn('0 个引用可找到', self.check(result, 'other_unit_reference')['detail'])

    def test_observed_f4h1l308_is_outside_154_rows_and_matches_ten_units(self):
        self.unit['evidence'] = [{'id': f'F4H1L{i}', 'anchor_id': None} for i in range(95, 249)]
        self.report['findings'][0].update(evidence_ids=['F4H1L308'], anchor_id=None)
        self.raw['report_markdown'] = json.dumps(self.report); self.seed()
        for i in range(10):
            self.other([{'id': 'F4H1L308', 'anchor_id': 'R390'}], ident=f'other-{i}')
        result = analyze(self.store, self.job, self.diagnostic)
        self.assertIn('共 154 条', self.check(result, 'provided_evidence')['detail'])
        self.assertIn('F4H1L95、F4H1L248', self.check(result, 'provided_evidence')['detail'])
        self.assertIn('F4H1L308', self.check(result, 'current_unit_reference')['detail'])
        self.assertIn('匹配 10 个其他单元', self.check(result, 'other_unit_reference')['detail'])
        self.assertEqual(self.check(result, 'current_parser')['state'], 'confirmed')

    def test_current_parser_can_confirm_old_failure_no_longer_reproduces(self):
        self.raw['report_markdown'] = '<think>固定思考</think>\n' + json.dumps(self.report)
        self.seed(); result = analyze(self.store, self.job, self.diagnostic)
        self.assertEqual(self.check(result, 'current_parser')['state'], 'clear')
        self.assertEqual(self.store.job(self.job['id'])['status'], 'queued')

    def test_ambiguous_and_invalid_anchor_are_explained(self):
        self.unit['evidence'].append({'id': 'F2H1L1', 'anchor_id': 'R1'})
        self.report['findings'][0]['anchor_id'] = 'R9'
        self.raw['report_markdown'] = json.dumps(self.report); self.seed()
        result = analyze(self.store, self.job, self.diagnostic)
        self.assertIn('多个对应位置', self.check(result, 'current_unit_reference')['detail'])
        self.assertEqual(self.check(result, 'anchor_binding')['state'], 'confirmed')

    def test_json_syntax_is_diagnosed_without_echoing_content(self):
        self.raw['report_markdown'] = '不得公开的模型正文'; self.seed()
        result = analyze(self.store, self.job, self.diagnostic)
        self.assertEqual(self.check(result, 'json_format')['state'], 'confirmed')
        self.assertNotIn('不得公开的模型正文', json.dumps(result, ensure_ascii=False))

    def test_missing_fields_have_specific_check(self):
        self.raw['report_markdown'] = json.dumps({'findings': [{}]}); self.seed()
        result = analyze(self.store, self.job, self.diagnostic)
        self.assertEqual(self.check(result, 'report_fields')['state'], 'confirmed')
        self.assertEqual(self.check(result, 'finding_fields')['state'], 'confirmed')

    def test_nonlist_findings_are_not_reported_as_clear(self):
        self.raw['report_markdown'] = json.dumps({'summary': '', 'limitations': [], 'findings': '类型错误'})
        self.seed(); result = analyze(self.store, self.job, self.diagnostic)
        self.assertEqual(self.check(result, 'finding_fields')['state'], 'unknown')

    def test_length_and_other_incomplete_causes_are_not_confused(self):
        self.raw['_review_completion'] = {'complete': False, 'finish_reason': 'length'}; self.seed()
        diagnostic = dict(self.diagnostic, error_code='BATCH_OUTPUT_INCOMPLETE')
        result = analyze(self.store, self.job, diagnostic)
        self.assertEqual(self.check(result, 'output_budget')['state'], 'confirmed')
        with self.store.tx() as c:
            self.raw['_review_completion']['finish_reason'] = 'error'
            c.execute('UPDATE review_batch_attempts SET raw=?', (json.dumps(self.raw),))
        result = analyze(self.store, self.job, diagnostic)
        self.assertEqual(self.check(result, 'output_budget')['state'], 'unknown')

    def test_wrong_session_does_not_fall_back_to_latest_attempt(self):
        self.seed()
        result = analyze(self.store, self.job, dict(self.diagnostic, session_id='rvw_missing'))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.check(result, 'session_binding')['state'], 'unknown')

    def test_limits_produce_partial_report_without_breaking_original_error(self):
        self.seed()
        with patch('ai_review.failure_checks.MAX_BYTES', 8):
            original = dict(self.diagnostic, message='原始失败')
            result = attach(self.store, self.job, original)
        self.assertEqual(result['message'], '原始失败')
        self.assertEqual(result['automatic_checks']['status'], 'partial')
        self.assertNotIn('automatic_checks', original)

    def test_input_budget_uses_frozen_configuration(self):
        with self.store.tx() as c:
            config = self.store.job_config(self.job['id'])
            config.update(batch_input_tokens=32768, model_max_output_tokens=32768)
            c.execute('UPDATE configuration_history SET value=? WHERE revision=?', (json.dumps(config), self.job['config_revision']))
        result = analyze(self.store, self.job, {'error_code': 'MODEL_INPUT_BUDGET'})
        self.assertEqual(self.check(result, 'input_allocation')['state'], 'confirmed')
        self.assertIn('主审单元额度 -256', self.check(result, 'input_allocation')['detail'])

    def test_failure_handler_attaches_checks_and_keeps_original_cause(self):
        self.seed(); before = self.store.job(self.job['id'], evidence=False)
        error = ReviewError('原始错误'); error.error_code = 'BATCH_OUTPUT_INVALID'; error.diagnostic = self.diagnostic
        actual = dict(self.job, diagnostic={}, stage='执行分批单元')
        result, message = _failure_diagnostic(actual, error, self.job, store=self.store)
        self.assertEqual(result['reason'], 'batch_evidence_unknown')
        self.assertIn('automatic_checks', result)
        self.assertEqual(self.store.job(self.job['id'], evidence=False)['status'], before['status'])
        self.assertEqual(result['message'], message)

    def test_automatic_failure_report_is_persisted_with_original_error(self):
        self.seed()
        job = self.store.claim()
        with self.store.tx() as c:
            c.execute('UPDATE jobs SET session_id=? WHERE id=?', ('rvw_fixture', job['id']))
        actual = self.store.job(job['id'], evidence=False)
        error = ReviewError('原始错误'); error.error_code = 'BATCH_OUTPUT_INVALID'; error.diagnostic = self.diagnostic
        _finish_failure(self.store, actual, error, job, 'failed')
        saved = self.store.job(job['id'], evidence=False)
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['diagnostic']['reason'], 'batch_evidence_unknown')
        self.assertEqual(saved['diagnostic']['automatic_checks']['version'], 1)

    def test_other_unit_limit_marks_unchecked_range(self):
        self.report['findings'][0].update(evidence_ids=['R9'], anchor_id=None)
        self.raw['report_markdown'] = json.dumps(self.report); self.seed()
        self.other([], ident='first')
        self.other([{'id': 'F2H1L1', 'anchor_id': 'R9'}], ident='second')
        with patch('ai_review.failure_checks.MAX_UNITS', 1):
            result = analyze(self.store, self.job, self.diagnostic)
        self.assertEqual(result['status'], 'partial')
        self.assertIn('未检查范围不作结论', self.check(result, 'other_unit_reference')['detail'])

    def test_historical_endpoint_is_read_only_and_requires_login(self):
        from access.control import install
        self.seed()
        with self.store.tx() as c:
            c.execute('UPDATE jobs SET diagnostic=?,session_id=? WHERE id=?', (json.dumps(self.diagnostic), 'rvw_fixture', self.job['id']))
        app = Flask(__name__); app.secret_key = '固定会话'
        app.config.update(AI_REVIEW_DB=str(self.store.path), TESTING=True)
        init_test_auth(app); app.register_blueprint(ai_review_bp, url_prefix='/api/ai-review'); install(app)
        client = app.test_client(); url = '/api/ai-review/tasks/' + self.job['id'] + '/failure-checks'
        self.assertEqual(client.get(url).status_code, 401)
        login_platform(client)
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['version'], 1)
        self.assertEqual(self.store.job(self.job['id'])['status'], 'queued')

    def test_unhandled_error_has_no_automatic_side_effect(self):
        value = {'error_code': 'OTHER', 'message': '保留的错误'}
        self.assertEqual(attach(self.store, self.job, value), value)
        self.assertEqual(analyze(self.store, self.job, value)['status'], 'unsupported')

    def test_analysis_never_changes_saved_data_or_uses_credentials(self):
        self.seed()
        def fingerprint():
            with self.store.connect() as c:
                return hashlib.sha256('\n'.join(c.iterdump()).encode()).hexdigest()
        before = fingerprint()
        with patch.object(self.store, 'unseal', side_effect=AssertionError('不得读取凭据')):
            result = analyze(self.store, self.job, self.diagnostic)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(before, fingerprint())


if __name__ == '__main__': unittest.main()
