"""列表成本摘要与确认操作沿用组织范围和会话保护。"""
import json
import unittest
from unittest.mock import patch

from tests import test_organization_access as organization_access


class CostConfirmationAccessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = organization_access.OrganizationAccessTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        state = self.fixture.state
        state.save_config({'revision': state.config()['revision'], 'enabled': True})
        revision = state.config()['revision']
        plan = {'primary': 1, 'association': 1, 'history': 0, 'base_calls': 2, 'max_calls': 9,
                'input_tokens': 1200, 'changed_lines': 20, 'profile': {'basis': '测试计数', 'revision': '测试版本'},
                'config': {'model_max_output_tokens': 8192}, 'units': [], 'gaps': [], 'unsupported': []}
        with state.tx() as connection:
            for ident in ('a' * 32, 'b' * 32):
                row = connection.execute('SELECT repository,number,generation FROM jobs WHERE id=?', (ident,)).fetchone()
                connection.execute("UPDATE jobs SET status='awaiting_cost',batch_protocol=1,config_revision=? WHERE id=?",
                                   (revision, ident))
                connection.execute('INSERT INTO pr_state(repository,number,generation,signature) VALUES(?,?,?,?)',
                                   (row['repository'], row['number'], row['generation'], '{}'))
                connection.execute('INSERT INTO review_batch_plans(job,hash,value,budget_seconds) VALUES(?,?,?,?)',
                                   (ident, '测试计划摘要', json.dumps(plan, ensure_ascii=False), 3600))

    def test_scoped_list_and_confirmation_reuse_existing_guards(self):
        f = self.fixture
        listing = f.request('/api/ai-review/tasks').json
        self.assertEqual([row['id'] for row in listing['items']], ['a' * 32])
        self.assertEqual(listing['items'][0]['cost_summary']['max_calls'], 9)
        with patch('routes.ai_review_routes.validate_plan_version') as version_check:
            denied = f.request('/api/ai-review/tasks/' + 'b' * 32 + '/confirm-plan', 'post', {'plan_hash': '测试计划摘要'})
            self.assertEqual(denied.status_code, 404)
            version_check.assert_not_called()
            no_csrf = f.client.post('/api/ai-review/tasks/' + 'a' * 32 + '/confirm-plan',
                                    json={'plan_hash': '测试计划摘要'}, headers={'X-Organization-ID': '1'}, base_url='https://localhost')
            self.assertEqual(no_csrf.status_code, 403)
            version_check.assert_not_called()
            allowed = f.request('/api/ai-review/tasks/' + 'a' * 32 + '/confirm-plan', 'post', {'plan_hash': '测试计划摘要'})
            self.assertEqual(allowed.status_code, 200, allowed.get_data(as_text=True))
            self.assertEqual(allowed.json['status'], 'queued')
            self.assertEqual(allowed.json['batch']['authorized_calls'], 9)
            version_check.assert_called_once()
        self.assertEqual(f.state.job('b' * 32)['status'], 'awaiting_cost')
