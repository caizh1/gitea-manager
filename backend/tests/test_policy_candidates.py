"""名单候选接口分页、身份边界和失败保护验收。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask
import requests
from auth import init_auth
from models import db, GiteaServer
from routes.policy_routes import policy_bp
from services.policy_gitea import PolicyGitea, _organization_cache
from services.policy_rules import PolicyError


class PolicyCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config.update(SECRET_KEY='仅用于测试', SQLALCHEMY_DATABASE_URI='sqlite:///' + self.tmp.name + '/名单.db')
        db.init_app(self.app); init_auth(self.app)
        self.app.register_blueprint(policy_bp, url_prefix='/api/repository-policies')
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        self.server = GiteaServer(name='名单测试实例', host='仅用于测试', ssh_user='git', gitea_port=3000, gitea_url='http://gitea', api_token='不应泄漏的凭据')
        db.session.add(self.server); db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session: session['_user_id'] = '1'; session['_fresh'] = True

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.ctx.pop(); self.tmp.cleanup()

    def get(self, kind, **params):
        return self.client.get('/api/repository-policies/' + kind, query_string={'server_id': self.server.id, **params})

    def test_authentication_guard(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get('/api/repository-policies/users').status_code, 401)

    def test_cross_origin_guard(self):
        self.assertEqual(self.client.get('/api/repository-policies/teams', headers={'Origin': 'https://陌生站点'}).status_code, 403)

    def test_users_are_paged_and_private_fields_are_removed(self):
        with patch.object(PolicyGitea, 'request', return_value={'ok': True, 'data': [{'id': 4, 'login': 'reviewer', 'email': '私有邮箱', 'password': '私有密码'}]}) as call:
            data = self.get('users', q='  review  ', page=2).get_json()
        call.assert_called_once_with('GET', '/users/search', params={'q': 'review', 'page': 2, 'limit': 20})
        self.assertEqual(data, {'items': [{'id': 4, 'server_id': self.server.id, 'name': 'reviewer', 'label': 'reviewer'}], 'page': 2, 'has_more': False})

    def test_team_candidates_keep_organization_and_name(self):
        with patch.object(PolicyGitea, 'request', side_effect=[{'id': 7, 'name': 'group-a'}, {'data': [{'id': 3, 'name': 'reviewers', 'organization': {'id': 7}}]}]) as call:
            data = self.get('teams', org='group-a', org_id=7, q='review').get_json()
        self.assertEqual(call.call_count, 2)
        self.assertEqual(data['items'], [{'id': 3, 'server_id': self.server.id, 'organization_id': 7, 'name': 'reviewers', 'label': 'group-a / reviewers'}])

    def test_invalid_org_page_and_query_do_not_reach_gitea(self):
        with patch.object(PolicyGitea, 'request') as call:
            for params in ({}, {'org': '..'}, {'org': 'a/b'}, {'org': '%2e'}, {'org': 'group', 'page': 0},
                           {'org': 'group', 'page': 2001}, {'org': 'group', 'q': 'x' * 101}, {'org': 'group', 'q': 'a\nb'}):
                with self.subTest(params=params): self.assertEqual(self.get('teams', **params).status_code, 400)
            call.assert_not_called()

    def test_real_gitea_team_search_can_omit_organization_object(self):
        with patch.object(PolicyGitea, 'request', side_effect=[{'id': 7, 'name': 'group-a'}, {'data': [{'id': 3, 'name': 'reviewers', 'organization': None}]}]):
            response = self.get('teams', org='group-a', org_id=7)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['items'][0]['organization_id'], 7)

    def test_default_users_are_org_members_and_typed_search_allows_external_users(self):
        org = {'id': 7, 'name': 'group-a'}
        with patch.object(PolicyGitea, 'request', side_effect=[org, [{'id': 4, 'login': 'member'}]]) as calls:
            result = self.get('users', org='group-a', org_id=7).get_json()
        self.assertEqual(result['items'][0]['name'], 'member')
        self.assertEqual(calls.call_args.args[1], '/orgs/group-a/members')
        with patch.object(PolicyGitea, 'request', side_effect=[org, {'data': [{'id': 5, 'login': 'outside'}]}]) as calls:
            result = self.get('users', org='group-a', org_id=7, q='outside').get_json()
        self.assertEqual(result['items'][0]['name'], 'outside')
        self.assertEqual(calls.call_args.args[1], '/users/search')

    def test_organization_search_paginates_without_losing_stable_ids(self):
        _organization_cache.clear()
        with patch.object(PolicyGitea, 'pages', return_value=[{'id': i + 1, 'name': f'group-{i:02}'} for i in range(45)]) as pages:
            first = self.get('organizations', q='group-').get_json()
            second = self.get('organizations', q='group-', page=2).get_json()
            third = self.get('organizations', q='group-', page=3).get_json()
        self.assertEqual([len(x['items']) for x in (first, second, third)], [20, 20, 5])
        self.assertEqual(len({i['id'] for x in (first, second, third) for i in x['items']}), 45)
        self.assertFalse(third['has_more']); self.assertEqual(pages.call_count, 1)
        _organization_cache.clear()

    def test_failure_is_not_returned_as_empty_success(self):
        with patch.object(PolicyGitea, 'request', side_effect=PolicyError('Gitea HTTP 403：权限不足')):
            response = self.get('users')
        self.assertEqual(response.status_code, 400)
        self.assertIn('403', response.get_json()['error'])
        self.assertNotIn('items', response.get_json())
        with patch('services.policy_gitea.reserve_slot', return_value=0), patch('services.policy_gitea.requests.request', side_effect=requests.ConnectionError('不应向客户端泄漏的连接细节')):
            response = self.get('users')
        self.assertEqual(response.status_code, 400)
        self.assertIn('网络请求失败', response.get_json()['error'])
        self.assertNotIn('不应向客户端泄漏', response.get_json()['error'])
        self.assertNotIn('items', response.get_json())

    def test_malformed_upstream_is_rejected(self):
        for data in ({'ok': False, 'data': []}, {'data': '无效'}, {'data': [{}]}, []):
            with self.subTest(data=data), patch.object(PolicyGitea, 'request', return_value=data):
                self.assertEqual(self.get('users').status_code, 400)

    def test_candidate_reads_use_shared_rate_limiter(self):
        response = type('Response', (), {'status_code': 200, 'headers': {'X-Total-Count': '41'},
                                         'json': lambda self: {'data': [{'id': 5, 'login': 'u'}]}})()
        with patch('services.policy_gitea.reserve_slot', return_value=0) as slot, patch('services.policy_gitea.requests.request', return_value=response):
            data = self.get('users', page=2).get_json()
        self.assertTrue(data['has_more']); slot.assert_called_once_with(self.server.id)

    def test_missing_instance_and_empty_results(self):
        self.assertEqual(self.client.get('/api/repository-policies/users?server_id=999999').status_code, 404)
        with patch.object(PolicyGitea, 'request', return_value={'data': []}):
            self.assertEqual(self.get('users').get_json()['items'], [])


if __name__ == '__main__': unittest.main()
