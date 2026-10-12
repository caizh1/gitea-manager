"""固定策略样本、批量任务和跨进程防重回归。"""
import copy
import json
import multiprocessing
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask
from tests.access_helpers import init_test_auth as init_auth, login_platform
from models import db, GiteaServer, PolicyBatch, PolicyTemplate, PolicyRepoResult, PolicyRequestSlot
from routes.policy_routes import policy_bp
from services.policy_rules import (PolicyError, API_FIELDS, BOOL_FIELDS, LIST_FIELDS, normalize_rule,
                                   normalize_rules, canonical_current, api_rule, matches, overlaps, plan_changes, encode)
from services.policy_gitea import PolicyGitea, reserve_slot
from services.policy_tasks import dispatch_once, instance_lock, recover_interrupted


class FakeGitea:
    repos = {}
    rules = {}
    writes = []
    failures = set()
    lose_response = set()
    permission_failures = set()
    tamper_after_write = False
    observed_active = 0
    active = 0
    guard = threading.Lock()

    def __init__(self, server):
        self.server_id = server.id
        self.url = server.gitea_url
        self.total_count = 0

    def check_version(self):
        pass

    def repo(self, repo_id):
        if repo_id not in self.repos or repo_id in self.permission_failures:
            raise PolicyError('没有仓库管理权限')
        return copy.deepcopy(self.repos[repo_id])

    identity = staticmethod(PolicyGitea.identity)
    path = staticmethod(PolicyGitea.path)

    def protections(self, repo):
        with self.guard:
            type(self).active += 1
            type(self).observed_active = max(type(self).active, type(self).observed_active)
        try:
            time.sleep(0.001)
            return copy.deepcopy(self.rules[repo['id']])
        finally:
            with self.guard:
                type(self).active -= 1

    def validate_people(self, repo, rules, identities=None):
        for rule in rules:
            if rule.get('team_org') and rule['team_org'] != repo['owner']['login']:
                raise PolicyError('团队所属组织不一致')
        return repo.get('test_access', {})

    def matching_branches(self, repo, rules):
        return [{'rule_name': r['rule_name'], 'priority': r['priority'], 'branches': ['main']} for r in rules]

    def request(self, method, path, **kwargs):
        if path == '/orgs/org':
            return {'id': 7, 'name': 'org'}
        if path == '/repos/search':
            params = kwargs['params']; page = params['page']; limit = params['limit']
            items = sorted(self.repos.values(), key=lambda r: r['id'])
            self.total_count = len(items)
            return {'data': items[(page-1)*limit:page*limit]}
        repo = next(r for r in self.repos.values() if path.startswith(self.path(r) + '/'))
        if repo['id'] in self.failures:
            raise PolicyError('模拟 Gitea HTTP 403')
        self.writes.append((repo['id'], method, path))
        data = copy.deepcopy(kwargs['json'])
        if method == 'POST' and not data['enable_push']:
            data = canonical_current([{**data, 'enable_force_push': False}])[0]
        if method == 'PATCH':
            from urllib.parse import unquote
            data['rule_name'] = unquote(path.split('/branch_protections/')[1])
        current = [r for r in self.rules[repo['id']] if r['rule_name'] != data['rule_name']]
        current.append(data)
        if self.tamper_after_write:
            current[-1]['required_approvals'] += 1
        self.rules[repo['id']] = current
        if repo['id'] in self.lose_response:
            raise PolicyError('模拟写入成功后响应丢失')
        return copy.deepcopy(data)


def fixture_app(database, locks):
    app = Flask(__name__)
    app.secret_key = '仅供隔离测试使用'
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + database, SQLALCHEMY_TRACK_MODIFICATIONS=False,
                      SQLALCHEMY_ENGINE_OPTIONS={'connect_args': {'timeout': 30}}, POLICY_LOCK_DIR=locks)
    db.init_app(app)
    init_auth(app)
    app.register_blueprint(policy_bp, url_prefix='/api/repository-policies')
    return app


def lock_probe(database, locks, queue):
    app = fixture_app(database, locks)
    with instance_lock(app, 1) as acquired:
        queue.put(acquired)


def slot_probe(database, locks, queue):
    app = fixture_app(database, locks)
    with app.app_context(), patch('services.policy_gitea.time.time', return_value=100000.0):
        queue.put(reserve_slot(1))


class PolicyRulesTests(unittest.TestCase):
    def test_all_api_fields_present_and_false_zero_empty_are_preserved(self):
        rule = normalize_rule({'rule_name': 'main', 'required_approvals': 0, 'enable_push': False})
        self.assertEqual(set(api_rule(rule)), set(API_FIELDS))
        self.assertFalse(rule['enable_push']); self.assertEqual(rule['required_approvals'], 0)
        self.assertEqual(rule['approvals_whitelist_username'], [])

    def test_unknown_fields_and_invalid_types_are_not_silently_dropped(self):
        for change in ({'future_security_field': True}, {'enable_push': 'false'}, {'priority': True}, {'required_approvals': -1}):
            with self.subTest(change=change), self.assertRaises(PolicyError):
                normalize_rule({'rule_name': 'main', **change})

    def test_team_org_required_and_disabling_allowlist_clears_effective_members(self):
        with self.assertRaises(PolicyError):
            normalize_rule({'rule_name': 'main', 'enable_approvals_whitelist': True, 'approvals_whitelist_teams': ['研发组']})
        r = normalize_rule({'rule_name': 'main', 'approvals_whitelist_username': ['旧审批人']})
        self.assertEqual(r['approvals_whitelist_username'], [])

    def test_enabled_empty_lists_preserve_gitea_semantics(self):
        r = normalize_rule({'rule_name': 'main', 'enable_merge_whitelist': True, 'enable_status_check': True,
                            'merge_whitelist_usernames': [], 'status_check_contexts': []})
        self.assertTrue(r['enable_merge_whitelist'])
        self.assertEqual(r['merge_whitelist_usernames'], [])
        self.assertTrue(r['enable_status_check'])
        self.assertEqual(r['status_check_contexts'], [])

    def test_glob_examples_and_exact_case_semantics(self):
        cases = [('main', 'MAIN', True), ('release/*', 'release/one', True), ('release/*', 'release/a/b', False),
                 ('hotfix/**', 'hotfix/a/b', True), ('{main,release/*}', 'main', True), ('{main,release/*}', 'release/x', True),
                 ('a/**/b', 'a/b', False), ('{**/,}x', 'x', True), ('release/v[1-3]?', 'release/v2a', True),
                 ('release/v[!1-3]?', 'release/v2a', False), ('release/*', 'RELEASE/a', False),
                 ('feature/[ab]', 'feature/a', True), ('v[^1-3]', 'v4', True),
                 (r'v[a\-z]', 'v-', True), ('kelvin', 'KELVIN', True)]
        for pattern, value, expected in cases:
            with self.subTest(pattern=pattern, value=value): self.assertEqual(matches(pattern, value), expected)

    def test_glob_intersection_includes_future_branches(self):
        cases = [('main', 'release/*', False), ('release/*', 'release/archive/*', False),
                 ('release/**', 'release/archive/*', True), ('{main,release/*}', 'MAIN', True),
                 ('v[1-3]', 'v[4-9]', False), ('v[!1-3]', 'v[4-9]', True), ('*', 'main', True)]
        for a, b, expected in cases:
            with self.subTest(a=a, b=b): self.assertEqual(overlaps(a, b), expected)

    def test_conflict_and_priority_tie_block_before_writes(self):
        with self.assertRaises(PolicyError):
            plan_changes([{'rule_name': '*'}], normalize_rules([{'rule_name': 'main'}]))
        with self.assertRaises(PolicyError):
            normalize_rules([{'rule_name': '*'}, {'rule_name': 'main'}])

    def test_unmanaged_rule_retained_and_lists_are_order_independent(self):
        before = [normalize_rule({'rule_name': 'archive/*'}), normalize_rule({'rule_name': 'main', 'required_approvals': 2})]
        old, desired, changes = plan_changes(before, normalize_rules([{'rule_name': 'main', 'required_approvals': 0}]))
        self.assertEqual(next(r for r in old if r['rule_name'] == 'archive/*'), next(r for r in desired if r['rule_name'] == 'archive/*'))
        self.assertEqual(changes[0]['fields'], [{'field': 'required_approvals', 'before': 2, 'after': 0}])
        a = normalize_rule({'rule_name': 'main', 'enable_status_check': True, 'status_check_contexts': ['测试', '构建']})
        b = normalize_rule({'rule_name': 'main', 'enable_status_check': True, 'status_check_contexts': ['构建', '测试', '测试']})
        self.assertEqual(a, b)

    def test_invalid_ci_and_file_patterns_are_rejected(self):
        for values in ({'enable_status_check': True, 'status_check_contexts': ['ci/[']},
                       {'protected_file_patterns': 'docs/{a,b'}, {'rule_name': r'branch/\d+'}):
            with self.assertRaises(PolicyError):
                normalize_rule({'rule_name': 'main', **values})


class PolicyGiteaPeopleTests(unittest.TestCase):
    def setUp(self):
        self.client = PolicyGitea(type('Server', (), {'id': 1, 'gitea_url': 'http://gitea', 'api_token': '测试凭据'})())
        self.repo = {'owner': {'login': 'group'}, 'name': 'repo'}
        self.rules = [normalize_rule({'rule_name': 'main', 'required_approvals': 1, 'enable_approvals_whitelist': True,
                                      'approvals_whitelist_username': ['reviewer']})]

    def test_unit_permissions_and_membership_override_none_aggregate(self):
        def request(method, path, **kwargs):
            if path == '/users/reviewer': return {'active': True}
            if path.endswith('/permission'): return {'permission': 'none'}
            if path == '/orgs/group': return {'id': 2}
            if path == '/teams/3/members/reviewer': return {'id': 4}
            raise AssertionError(path)
        with patch.object(self.client, 'request', side_effect=request), patch.object(self.client, 'pages', return_value=[{'id': 3, 'permission': 'none', 'units_map': {'repo.code': 'write', 'repo.pulls': 'read'}}]):
            self.client.validate_people(self.repo, self.rules)

    def test_explicit_read_access_is_sufficient_for_approval_only(self):
        with patch.object(self.client, 'request', side_effect=[{'active': True}, {'permission': 'read'}]):
            self.client.validate_people(self.repo, self.rules)
        self.rules[0].update(enable_push=True, enable_push_whitelist=True, push_whitelist_usernames=['reviewer'])
        with patch.object(self.client, 'request', side_effect=[{'active': True}, {'permission': 'read'}, None]), self.assertRaises(PolicyError):
            self.client.validate_people(self.repo, self.rules)

    def test_personal_repository_never_queries_organization_teams(self):
        with patch.object(self.client, 'request', side_effect=[{'active': True}, {'permission': 'none'}, None]), patch.object(self.client, 'pages') as teams:
            with self.assertRaises(PolicyError): self.client.validate_people(self.repo, self.rules)
            teams.assert_not_called()

    def test_inactive_and_read_only_users_blocked(self):
        for user, permission in [({'active': False}, 'write'), ({'active': True, 'prohibit_login': True}, 'admin'), ({'active': True}, 'none')]:
            with self.subTest(user=user, permission=permission), patch.object(self.client, 'request', side_effect=[user, {'permission': permission}, None]), self.assertRaises(PolicyError):
                self.client.validate_people(self.repo, self.rules)

    def test_team_membership_missing_does_not_grant_access(self):
        with patch.object(self.client, 'request', side_effect=[{'active': True}, {'permission': 'none'}, {'id': 2}, None]), patch.object(self.client, 'pages', return_value=[{'id': 3, 'units_map': {'repo.code': 'write', 'repo.pulls': 'read'}}]), self.assertRaises(PolicyError):
            self.client.validate_people(self.repo, self.rules)

    def test_http_error_does_not_leak_response_or_token(self):
        response = type('Response', (), {'status_code': 403, 'text': '测试凭据和私有正文'})()
        with patch('services.policy_gitea.reserve_slot', return_value=0), patch('services.policy_gitea.requests.request', return_value=response), self.assertRaises(PolicyError) as error:
            self.client.request('GET', '/version')
        self.assertNotIn('测试凭据', str(error.exception))
        self.assertNotIn('私有正文', str(error.exception))


class PolicyApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = self.tmp.name + '/测试数据库.db'
        self.locks = self.tmp.name + '/锁'
        self.app = fixture_app(self.database, self.locks)
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        server = GiteaServer(name='测试实例', role='primary', host='测试', ssh_user='git', gitea_port=3000, gitea_url='http://仅用于测试', api_token='不应出现在响应中的凭据')
        db.session.add(server); db.session.commit(); self.sid = server.id
        FakeGitea.repos = {}; FakeGitea.rules = {}; FakeGitea.writes = []; FakeGitea.failures = set(); FakeGitea.lose_response = set(); FakeGitea.permission_failures = set()
        FakeGitea.tamper_after_write = False; FakeGitea.active = FakeGitea.observed_active = 0
        self.add_repos(3)
        self.patches = [patch('services.policy_tasks.PolicyGitea', FakeGitea), patch('routes.policy_routes.PolicyGitea', FakeGitea)]
        for p in self.patches: p.start()
        self.client = self.app.test_client()
        login_platform(self.client)
        self.csrf = self.client.get('/api/repository-policies/csrf').get_json()['token']
        self.headers = {'X-Policy-CSRF': self.csrf}

    def tearDown(self):
        for p in self.patches: p.stop()
        db.session.remove(); db.engine.dispose(); self.ctx.pop(); self.tmp.cleanup()

    def add_repos(self, count):
        for i in range(1, count+1):
            FakeGitea.repos[i] = {'id': i, 'name': f'repo-{i}', 'full_name': f'org/repo-{i}', 'owner': {'login': 'org', 'id': 7}, 'default_branch': 'main', 'permissions': {'admin': True}}
            FakeGitea.rules[i] = []

    def create_template(self, rules=None):
        response = self.client.post('/api/repository-policies/templates', json={'server_id': self.sid, 'organization': {'id': 7, 'name': 'org'}, 'name': '标准模板', 'rules': rules or [{'rule_name': 'main', 'required_approvals': 2}]}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.get_json()); return response.get_json()

    def preview(self, template=None, ids=None):
        template = template or self.create_template()
        response = self.client.post('/api/repository-policies/batches', json={'template_id': template['id'], 'content_hash': template['content_hash'], 'repo_ids': ids or [1, 2, 3]}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.get_json()); bid = response.get_json()['id']
        dispatch_once(self.app); db.session.expire_all(); return bid

    def apply(self, bid):
        response = self.client.post(f'/api/repository-policies/batches/{bid}/apply', headers=self.headers)
        self.assertIn(response.status_code, (200, 202), response.get_json())
        dispatch_once(self.app); db.session.expire_all()
        return self.client.get(f'/api/repository-policies/batches/{bid}').get_json()

    def test_login_csrf_and_cross_origin(self):
        # 独立请求上下文，避免测试固定 app_context 中缓存的当前用户泄漏到匿名客户端。
        with self.app.app_context():
            self.assertEqual(self.app.test_client().get('/api/repository-policies/templates').status_code, 401)
        self.assertEqual(self.client.post('/api/repository-policies/templates', json={}).status_code, 403)
        self.assertEqual(self.client.get('/api/repository-policies/csrf', headers={'Origin': 'https://陌生站点.invalid'}).status_code, 403)
        self.assertEqual(self.client.post('/api/repository-policies/templates', json={}, headers={**self.headers, 'Origin': 'http://localhost'}).status_code, 400)

    def test_batch_preview_apply_verify_and_duplicate_submission(self):
        bid = self.preview(); self.assertEqual(len(FakeGitea.writes), 0)
        result = self.apply(bid)
        self.assertEqual(result['batch']['status'], 'completed'); self.assertEqual(result['batch']['counts'], {'success': 3})
        self.apply(bid); self.assertEqual(len(FakeGitea.writes), 3)
        self.assertNotIn('不应出现在响应中的凭据', json.dumps(result, ensure_ascii=False))
        self.assertLessEqual(FakeGitea.observed_active, 2)

    def test_template_change_invalidates_preview_and_historical_snapshot_survives_delete(self):
        template = self.create_template(); bid = self.preview(template)
        changed = {**template, 'name': '新的模板名称'}
        self.assertEqual(self.client.put(f'/api/repository-policies/templates/{template["id"]}', json=changed, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post(f'/api/repository-policies/batches/{bid}/apply', headers=self.headers).status_code, 409)
        self.client.delete(f'/api/repository-policies/templates/{template["id"]}', headers=self.headers)
        data = self.client.get(f'/api/repository-policies/batches/{bid}').get_json()
        self.assertEqual(data['batch']['template_name'], '标准模板'); self.assertEqual(data['template_snapshot'][0]['required_approvals'], 2)

    def test_repo_change_blocks_only_affected_repository(self):
        bid = self.preview(); FakeGitea.rules[2] = [normalize_rule({'rule_name': 'main', 'required_approvals': 3})]
        result = self.apply(bid)
        self.assertEqual(result['batch']['counts'], {'success': 2, 'blocked': 1}); self.assertEqual(len(FakeGitea.writes), 2)

    def test_renamed_repo_and_permission_loss_are_rechecked(self):
        bid = self.preview(); FakeGitea.repos[1]['full_name'] = 'org/改名后'; FakeGitea.permission_failures = {2}
        result = self.apply(bid)
        self.assertEqual(result['batch']['counts'], {'success': 1, 'blocked': 2})

    def test_partial_failure_and_response_loss_have_truthful_results(self):
        FakeGitea.failures = {2}; FakeGitea.lose_response = {3}
        result = self.apply(self.preview())
        self.assertEqual(result['batch']['counts'], {'success': 2, 'failed': 1}); self.assertEqual(len(FakeGitea.writes), 2)

    def test_post_write_mismatch_is_never_success(self):
        bid = self.preview(); FakeGitea.tamper_after_write = True
        result = self.apply(bid)
        self.assertEqual(result['batch']['counts'], {'partial': 3})

    def test_template_delete_after_submission_does_not_change_frozen_task(self):
        template = self.create_template(); bid = self.preview(template)
        self.client.post(f'/api/repository-policies/batches/{bid}/apply', headers=self.headers)
        self.client.delete(f'/api/repository-policies/templates/{template["id"]}', headers=self.headers)
        result = self.apply(bid); self.assertEqual(result['batch']['status'], 'completed')

    def test_restart_records_interruption_without_replaying_write(self):
        bid = self.preview(); item = db.session.get(PolicyBatch, bid); item.status = 'applying'
        PolicyRepoResult.query.filter_by(batch_id=bid).update({'status': 'running'}); db.session.commit()
        dispatch_once(self.app); db.session.expire_all()
        self.assertEqual(db.session.get(PolicyBatch, bid).status, 'interrupted'); self.assertEqual(FakeGitea.writes, [])

    def test_read_only_template_rules_do_not_remove_unmanaged_rules(self):
        for rid in FakeGitea.rules: FakeGitea.rules[rid] = [normalize_rule({'rule_name': 'archive/*', 'priority': 9})]
        self.apply(self.preview())
        self.assertTrue(all({r['rule_name'] for r in rules} == {'archive/*', 'main'} for rules in FakeGitea.rules.values()))

    def test_1000_repository_pagination_queue_and_failure_isolation(self):
        self.add_repos(1000)
        for page in (1, 10, 20):
            data = self.client.get(f'/api/repository-policies/repositories?server_id={self.sid}&org=org&org_id=7&page={page}').get_json()
            self.assertEqual(data['total'], 1000); self.assertEqual(len(data['items']), 50)
        FakeGitea.failures = {i for i in range(1, 1001) if i % 37 == 0}
        started = time.monotonic(); bid = self.preview(ids=list(range(1, 1001))); result = self.apply(bid)
        self.assertEqual(result['batch']['counts'], {'success': 973, 'failed': 27})
        self.assertEqual(result['batch']['progress_percent'], 100); self.assertEqual(len(FakeGitea.writes), 973)
        self.assertEqual(len(result['items']), 50); self.assertEqual(result['total'], 1000)
        self.assertLessEqual(FakeGitea.observed_active, 2)
        print(f'模拟规模验证：1000 个仓库，973 成功、27 隔离失败，用时 {time.monotonic()-started:.2f} 秒；不含真实网络限速等待。')

    def test_process_lock_excludes_second_worker(self):
        ctx = multiprocessing.get_context('spawn'); queue = ctx.Queue()
        with instance_lock(self.app, self.sid) as locked:
            self.assertTrue(locked); process = ctx.Process(target=lock_probe, args=(self.database, self.locks, queue)); process.start()
            self.assertFalse(queue.get(timeout=15)); process.join(15); self.assertEqual(process.exitcode, 0)

    def test_request_slots_are_shared_by_multiple_processes(self):
        db.session.add(PolicyRequestSlot(server_id=1, next_at=0)); db.session.commit()
        ctx = multiprocessing.get_context('spawn'); queue = ctx.Queue()
        processes = [ctx.Process(target=slot_probe, args=(self.database, self.locks, queue)) for _ in range(4)]
        for p in processes: p.start()
        slots = sorted(queue.get(timeout=15) for _ in processes)
        for p in processes: p.join(15); self.assertEqual(p.exitcode, 0)
        self.assertEqual(slots, [0, 0.5, 1, 1.5])


if __name__ == '__main__':
    unittest.main()
