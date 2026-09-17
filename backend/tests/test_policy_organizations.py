"""组织范围、稳定身份、旧模板兼容和创建接口差异的固定反证样本。"""
import copy
import json
import unittest
from unittest.mock import patch

import test_repository_policies as baseline
from test_repository_policies import FakeGitea
from services.policy_scope import organization, scoped_rules, verify_identities, validate_repo_scope
from services.policy_gitea import PolicyGitea, _organization_cache
from services.policy_rules import PolicyError, normalize_rule, api_rule, encode
from models import db, PolicyTemplate, PolicyBatch


class IdentityClient:
    server_id = 1
    url = 'http://gitea'

    def __init__(self):
        self.users = {11: {'id': 11, 'login': 'alice', 'active': True}}
        self.teams = {21: {'id': 21, 'name': 'reviewers', 'organization': {'id': 7}},
                      22: {'id': 22, 'name': 'reviewers', 'organization': {'id': 8}},
                      23: {'id': 23, 'name': '固件审查组', 'organization': {'id': 7}}}
        self.orgs = {'org': {'id': 7, 'name': 'org'}, 'other': {'id': 8, 'name': 'other'}}
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.startswith('/orgs/'):
            return self.orgs[path.split('/')[2]]
        if path == '/users/search':
            user = self.users.get(kwargs['params']['uid'])
            return {'data': [user] if user else []}
        if path.startswith('/users/'):
            return next((x for x in self.users.values() if x['login'] == path.split('/')[2]), None)
        if path.startswith('/teams/'):
            return self.teams.get(int(path.split('/')[2]))
        raise AssertionError(path)

    def pages(self, path, params=None):
        return [t for t in self.teams.values() if t['organization']['id'] == 7]


class PolicyIdentityTests(unittest.TestCase):
    def setUp(self):
        self.client = IdentityClient()
        self.org = {'id': 7, 'name': 'org', 'server_id': 1}
        self.rules = scoped_rules([{'rule_name': 'main', 'enable_approvals_whitelist': True,
            'approvals_whitelist_username': ['alice'], 'approvals_whitelist_teams': ['reviewers']}], self.org)
        self.refs = {'users': [{'id': 11, 'name': 'alice', 'server_id': 1}],
                     'teams': [{'id': 21, 'name': 'reviewers', 'server_id': 1, 'organization_id': 7}]}

    def test_same_named_team_in_other_org_never_replaces_original(self):
        refs = copy.deepcopy(self.refs); refs['teams'][0]['id'] = 22
        with self.assertRaisesRegex(PolicyError, '不属于模板组织'):
            verify_identities(self.client, self.org, self.rules, refs)
        refs['teams'][0]['organization_id'] = 8
        with self.assertRaisesRegex(PolicyError, '其他实例或组织'):
            verify_identities(self.client, self.org, self.rules, refs)

    def test_deleted_and_same_named_recreated_users_and_teams_are_blocked(self):
        for kind, table, old_id, new_id in [('users', 'users', 11, 12), ('teams', 'teams', 21, 24)]:
            with self.subTest(kind=kind):
                client = IdentityClient(); values = getattr(client, table); old = values.pop(old_id); values[new_id] = {**old, 'id': new_id}
                with self.assertRaisesRegex(PolicyError, '原身份已删除'):
                    verify_identities(client, self.org, self.rules, self.refs, resolve_names=True)

    def test_rename_requires_explicit_refresh_and_preserves_id(self):
        self.client.users[11]['login'] = 'alice-new'; self.client.teams[21]['name'] = 'reviewers-new'
        with self.assertRaisesRegex(PolicyError, '已改名'):
            verify_identities(self.client, self.org, self.rules, self.refs)
        refs, rules = verify_identities(self.client, self.org, self.rules, self.refs, refresh_names=True)
        self.assertEqual(refs['users'][0]['id'], 11); self.assertEqual(refs['teams'][0]['id'], 21)
        self.assertEqual(rules[0]['approvals_whitelist_username'], ['alice-new'])
        self.assertEqual(rules[0]['approvals_whitelist_teams'], ['reviewers-new'])

    def test_names_require_explicit_legacy_resolution(self):
        with self.assertRaisesRegex(PolicyError, '尚未绑定身份'):
            verify_identities(self.client, self.org, self.rules)
        refs, rules = verify_identities(self.client, self.org, self.rules, resolve_names=True)
        self.assertEqual(refs['users'], self.refs['users']); self.assertEqual(refs['teams'], self.refs['teams']); self.assertEqual(rules, self.rules)

    def test_chinese_team_name_is_preserved(self):
        rules = copy.deepcopy(self.rules); rules[0]['approvals_whitelist_teams'] = ['固件审查组']
        refs, _ = verify_identities(self.client, self.org, rules, resolve_names=True)
        self.assertEqual(refs['teams'][0]['id'], 23)

    def test_multiple_team_organizations_cannot_be_adopted_implicitly(self):
        other = {**self.rules[0], 'rule_name': 'release/*', 'team_org': 'other'}
        with self.assertRaisesRegex(PolicyError, '其他组织'):
            scoped_rules(self.rules + [other], self.org)

    def test_org_recreation_and_repo_transfer_are_blocked(self):
        self.client.orgs['org']['id'] = 99
        with self.assertRaisesRegex(PolicyError, '同名重建'):
            organization(self.client, self.org)
        with self.assertRaisesRegex(PolicyError, '不在模板所属组织'):
            validate_repo_scope(self.client, {'owner': {'id': 8, 'login': 'other'}}, {'organization': self.org})

    def test_instance_mismatch_and_inactive_account_are_blocked(self):
        refs = copy.deepcopy(self.refs); refs['users'][0]['server_id'] = 2
        with self.assertRaisesRegex(PolicyError, '其他实例'):
            verify_identities(self.client, self.org, self.rules, refs)
        self.client.users[11]['active'] = False
        with self.assertRaisesRegex(PolicyError, '停用'):
            verify_identities(self.client, self.org, self.rules, self.refs)

    def test_duplicate_name_with_conflicting_ids_is_rejected(self):
        refs = copy.deepcopy(self.refs); refs['teams'].append({**refs['teams'][0], 'id': 22})
        with self.assertRaisesRegex(PolicyError, '多个身份'):
            verify_identities(self.client, self.org, self.rules, refs)

    def test_repointed_instance_cannot_reuse_same_numbered_ids(self):
        refs, _ = verify_identities(self.client, self.org, self.rules, self.refs)
        self.client.url = 'http://different-gitea'
        with self.assertRaisesRegex(PolicyError, '实例地址已变化'):
            verify_identities(self.client, self.org, self.rules, refs)

    def test_real_search_null_organization_requires_team_detail(self):
        with patch.object(self.client, 'pages', return_value=[{'id': 21, 'name': 'reviewers', 'organization': None}]):
            refs, _ = verify_identities(self.client, self.org, self.rules, resolve_names=True)
        self.assertEqual(refs['teams'][0]['organization_id'], 7)
        self.assertIn('/teams/21', [x[1] for x in self.client.calls])


class PolicyOrganizationApiTests(unittest.TestCase):
    # 复用既有隔离数据库、双 worker 和队列基线，额外覆盖本期的新边界。
    setUp = baseline.PolicyApiTests.setUp
    tearDown = baseline.PolicyApiTests.tearDown
    add_repos = baseline.PolicyApiTests.add_repos
    create_template = baseline.PolicyApiTests.create_template
    preview = baseline.PolicyApiTests.preview
    apply = baseline.PolicyApiTests.apply
    def test_unowned_template_is_preserved_and_requires_explicit_adoption(self):
        old = PolicyTemplate(server_id=self.sid, name='旧模板', description='', rules_json='[{"rule_name":"main"}]', content_hash='old')
        db.session.add(old); db.session.commit()
        data = self.client.get(f'/api/repository-policies/templates?server_id={self.sid}&legacy=1').get_json()
        self.assertEqual(data[0]['organization'], None)
        response = self.client.post('/api/repository-policies/batches', json={'template_id': old.id, 'content_hash': 'old', 'repo_ids': [1]}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        update = {**data[0], 'organization': {'id': 7, 'name': 'org'}}
        self.assertEqual(self.client.put(f'/api/repository-policies/templates/{old.id}', json=update, headers=self.headers).status_code, 400)
        update['confirm_identity_changes'] = True
        self.assertEqual(self.client.put(f'/api/repository-policies/templates/{old.id}', json=update, headers=self.headers).status_code, 200)

    def test_new_template_cannot_omit_organization(self):
        result = self.client.post('/api/repository-policies/templates', json={'server_id': self.sid, 'name': '无归属', 'rules': [{'rule_name': 'main'}]}, headers=self.headers)
        self.assertEqual(result.status_code, 400)

    def test_repo_transfer_and_permission_change_invalidate_old_preview(self):
        bid = self.preview()
        FakeGitea.repos[1]['owner'] = {'id': 8, 'login': 'other'}
        FakeGitea.repos[2]['test_access'] = {'changed': True}
        result = self.apply(bid)
        self.assertEqual(result['batch']['counts'], {'blocked': 2, 'success': 1})

    def test_out_of_scope_repository_is_blocked_without_affecting_others(self):
        FakeGitea.repos[1]['owner'] = {'id': 8, 'login': 'other'}
        bid = self.preview(); result = self.apply(bid)
        self.assertEqual(result['batch']['counts'], {'blocked': 1, 'success': 2})

    def test_source_deletion_has_no_dependency_and_snapshot_survives(self):
        template = self.create_template()
        template['source'] = {'id': 99999, 'full_name': 'org/已删除的来源仓库'}
        response = self.client.put(f'/api/repository-policies/templates/{template["id"]}', json=template, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        template = response.get_json(); bid = self.preview(template); result = self.apply(bid)
        self.client.delete(f'/api/repository-policies/templates/{template["id"]}', headers=self.headers)
        history = self.client.get(f'/api/repository-policies/batches/{bid}').get_json()
        self.assertEqual(result['batch']['status'], 'completed')
        self.assertEqual(history['scope_snapshot']['source']['id'], 99999)

    def test_force_push_create_uses_verified_intermediate_then_patch(self):
        template = self.create_template([{'rule_name': 'main', 'enable_push': False, 'enable_force_push': True}])
        result = self.apply(self.preview(template, [1]))
        self.assertEqual(result['batch']['counts'], {'success': 1})
        self.assertEqual([x[1] for x in FakeGitea.writes], ['POST', 'PATCH'])
        self.assertFalse(FakeGitea.rules[1][0]['enable_push']); self.assertTrue(FakeGitea.rules[1][0]['enable_force_push'])

    def test_force_push_lost_create_response_never_replays_or_completes_blindly(self):
        template = self.create_template([{'rule_name': 'main', 'enable_force_push': True}])
        bid = self.preview(template, [1]); FakeGitea.lose_response = {1}
        result = self.apply(bid)
        self.assertEqual(result['batch']['counts'], {'partial': 1})
        self.assertEqual([x[1] for x in FakeGitea.writes], ['POST'])

    def test_force_push_intermediate_with_other_team_allowlists(self):
        # 真实 30 仓库首次验收反例：API 快照没有 team_org，审批/合并团队仍须保留。
        template = self.create_template([{'rule_name': 'main', 'enable_force_push': True}])
        rules = [normalize_rule({'rule_name': 'main', 'enable_force_push': True,
            'enable_force_push_allowlist': True, 'force_push_allowlist_teams': ['开发组'],
            'enable_approvals_whitelist': True, 'approvals_whitelist_teams': ['审查组'],
            'enable_merge_whitelist': True, 'merge_whitelist_teams': ['开发组'], 'team_org': 'org'})]
        db.session.get(PolicyTemplate, template['id']).rules_json = encode(rules)
        db.session.commit()
        with patch('routes.policy_routes.template_scope', return_value={'organization': {'id': 7, 'name': 'org', 'server_id': self.sid}}):
            result = self.apply(self.preview(template, [1]))
        self.assertEqual(result['batch']['counts'], {'success': 1})
        self.assertEqual([x[1] for x in FakeGitea.writes], ['POST', 'PATCH'])
        self.assertEqual(FakeGitea.rules[1][0], api_rule(rules[0]))

    def test_permission_changed_during_write_cannot_report_success(self):
        template = self.create_template()
        bid = self.preview(template, [1])
        original = FakeGitea.request
        def changed(client, method, path, **kwargs):
            value = original(client, method, path, **kwargs)
            if method in ('POST', 'PATCH'):
                FakeGitea.repos[1]['test_access'] = {'changed': True}
            return value
        with patch.object(FakeGitea, 'request', changed):
            result = self.apply(bid)
        self.assertNotIn('success', result['batch']['counts'])

    def test_legacy_preview_cannot_be_submitted(self):
        bid = self.preview(); item = db.session.get(PolicyBatch, bid); item.scope_json = '{}'; db.session.commit()
        result = self.client.post(f'/api/repository-policies/batches/{bid}/apply', headers=self.headers)
        self.assertEqual(result.status_code, 409)


if __name__ == '__main__': unittest.main()
