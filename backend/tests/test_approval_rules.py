"""外接审批固定基准：计票、身份、旧代码、接入状态与服务端发布门禁。"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask, g
from tests.access_helpers import init_test_auth as init_auth, login_platform
from models import db, GiteaServer, ApprovalPolicy, ApprovalNonce
from approval.rules import ApprovalError, normalize, evaluate, native_rule, safe_native, encode
from approval.gitea import Reader, reset_event_id, review_revision
from approval.native_uninstall import SCRIPT as NATIVE_UNINSTALL_SCRIPT
from routes.approval_routes import approval_bp, approval_internal_bp, approval_view_bp, legacy_hook_pending

HEAD = 'a'*40


def rule():
    return normalize({'name': '研发审批', 'branches': ['main'], 'total': 3,
                      'groups': [{'team_id': 10, 'required': 1}, {'team_id': 20, 'required': 1}]})


def review(uid, rid=None, **changes):
    return dict({'id': rid or uid, 'user': {'id': uid, 'login': 'user'+str(uid)}, 'commit_id': HEAD,
                 'state': 'APPROVED', 'dismissed': False, 'stale': False}, **changes)


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.rule = rule()
        self.pr = {'id': 1, 'user': {'id': 99}, 'head': {'sha': HEAD}, 'base': {'ref': 'main'}, 'state': 'open', 'merged': False}
        self.teams = {10: {'name': 'Committer1', 'members': [1, 4]}, 20: {'name': 'Committer2', 'members': [2, 4]}}
        self.native = {'rule_name': 'main', 'required_approvals': 5, 'block_on_rejected_reviews': True}

    def evaluate(self, values, eligible=None):
        return evaluate(self.rule, self.pr, values, set(range(1, 10)) if eligible is None else eligible, self.teams, self.native)

    def test_total_zero_one_two(self):
        for n in (0, 1, 2):
            with self.subTest(票数=n):
                self.assertFalse(self.evaluate([review(i) for i in range(1, n+1)])['satisfied'])

    def test_three_and_both_groups(self):
        result = self.evaluate([review(1), review(2), review(3)])
        self.assertTrue(result['satisfied'])
        self.assertEqual(result['native_required'], 5)  # 新增通过不能削弱更严格的原生规则。

    def test_missing_each_group(self):
        for voters in ([1, 3, 5], [2, 3, 5]):
            with self.subTest(人员=voters):
                self.assertFalse(self.evaluate([review(i) for i in voters])['satisfied'])

    def test_cross_group_counted_once(self):
        result = self.evaluate([review(4), review(3)])
        self.assertEqual([g['count'] for g in result['groups']], [1, 1])
        self.assertEqual(result['total']['count'], 2)
        self.assertFalse(result['satisfied'])
        self.assertTrue(self.evaluate([review(4), review(3), review(5)])['satisfied'])

    def test_author_and_unqualified(self):
        for uid in (99, 30):
            with self.subTest(人员=uid):
                self.assertFalse(self.evaluate([review(1), review(2), review(uid)])['satisfied'])

    def test_repeated_approvals_one_vote(self):
        result = self.evaluate([review(1), review(1, 11), review(2)])
        self.assertEqual(result['total']['count'], 2)

    def test_duplicate_page_rejected(self):
        with self.assertRaises(ApprovalError):
            self.evaluate([review(1), review(1)])

    def test_comment_and_draft_do_not_remove_approval(self):
        for state in ('COMMENT', 'PENDING', 'REQUEST_REVIEW'):
            with self.subTest(状态=state):
                self.assertTrue(self.evaluate([review(1), review(2), review(3), review(1, 10, state=state)])['satisfied'])

    def test_team_review_request_without_user(self):
        self.assertTrue(self.evaluate([review(1), review(2), review(3), review(4, state='REQUEST_REVIEW', user=None)])['satisfied'])

    def test_latest_revoke_cannot_resurrect_old_approval(self):
        self.assertFalse(self.evaluate([review(1), review(2), review(3), review(1, 10, dismissed=True)])['satisfied'])

    def test_old_revoke_does_not_cancel_new_approval(self):
        self.assertTrue(self.evaluate([review(1, dismissed=True), review(2), review(3), review(1, 10)])['satisfied'])

    def test_request_changes_removes_vote(self):
        self.assertFalse(self.evaluate([review(1), review(2), review(3), review(1, 10, state='REQUEST_CHANGES')])['satisfied'])

    def test_old_commit_stale_and_missing_flags(self):
        for change in ({'commit_id': 'b'*40}, {'stale': True}, {'dismissed': True}):
            with self.subTest(变化=change):
                self.assertFalse(self.evaluate([review(1, **change), review(2), review(3)])['satisfied'])
        with self.assertRaises(ApprovalError):
            self.evaluate([review(1, stale=None)])

    def test_a_b_a_code_generation(self):
        self.pr['last_code_event_id'] = 20
        self.assertFalse(self.evaluate([review(i, decision_event_id=i) for i in (1, 2, 3)])['satisfied'])
        self.assertTrue(self.evaluate([review(i, decision_event_id=20+i) for i in (1, 2, 3)])['satisfied'])

    def test_group_deleted_empty_and_member_removed(self):
        del self.teams[10]
        with self.assertRaises(ApprovalError):
            self.evaluate([review(1), review(2), review(3)])
        for members in ([], [4]):
            self.teams[10] = {'name': 'Committer1', 'members': members}
            self.assertFalse(self.evaluate([review(1), review(2), review(3)])['satisfied'])

    def test_changed_branch_closed_merged(self):
        for field, value in (('state', 'closed'), ('merged', True), ('base', {'ref': 'release'})):
            previous = self.pr[field]; self.pr[field] = value
            self.assertFalse(self.evaluate([review(1), review(2), review(3)])['satisfied'])
            self.pr[field] = previous

    def test_rule_validation(self):
        for update in ({'total': True}, {'total': 0}, {'groups': []}, {'groups': [{'team_id': 1, 'required': -1}]},
                       {'branches': ['../main']}, {'branches': ['release/*']}, {'reset_on_push': False}, {'unknown': 1}):
            with self.subTest(输入=update), self.assertRaises(ApprovalError):
                normalize(dict(rule(), **update))

    def test_native_priority_before_exact_name(self):
        values = [{'rule_name': 'main', 'priority': 2}, {'rule_name': '*', 'priority': 1}]
        self.assertEqual(native_rule(values, 'main')['rule_name'], '*')
        values[0]['priority'] = 1
        self.assertEqual(native_rule(values, 'main')['rule_name'], 'main')

    def test_native_uninstall_script_is_valid_python(self):
        compile(NATIVE_UNINSTALL_SCRIPT, '卸载旧审批Hook.py', 'exec')

    def test_native_bypass_paths_stop(self):
        for value in ({'enable_push': True}, {'block_admin_merge_override': False}, {'unprotected_file_patterns': '*.md'}):
            native = dict(enable_push=False, block_admin_merge_override=True)
            native.update(value)
            with self.assertRaises(ApprovalError):
                safe_native(native)

    def test_native_rule_plan_uses_stable_id_full_path_and_valid_subjects(self):
        client = object.__new__(Reader)
        client.url = 'http://gitea:3000'
        client.governance_capabilities = lambda: {'version': 1, 'approval_rules': True}
        client.request = lambda path, params=None: ({'id':int(path.split('/')[-1]), 'name':'team'+path.split('/')[-1]}
                                                     if path.startswith('/teams/') else {'id': 7, 'full_name': 'internal/repo',
                                                     'full_path': 'group/subgroup/repo', 'owner': {'login': 'internal'}, 'name': 'repo'})
        client.pages = lambda path, params=None, key='id': ([{'id':10},{'id':20}] if path.endswith('/teams') else [])
        client._legacy_approval_pools = lambda repo, branches: ({b:[1,2,3,4] for b in branches}, {'team10':{1,4}, 'team20':{2,4}})
        client.governance_read = lambda path: {'settings_revision':0, 'settings':{'settings':{'prevent_author':True, 'reset_on_change':True}, 'sources':{}}}
        policy = type('Policy', (), {'repo_id': 7, 'rule_json': encode(rule())})()
        plan = client.native_rule_plan(policy)
        self.assertEqual(plan['repository_id'], 7)
        self.assertEqual(plan['full_path'], 'group/subgroup/repo')
        self.assertTrue(plan['rules'][0]['all_eligible'])
        self.assertEqual(plan['rules'][0]['branch_mode'], 'branches')
        self.assertTrue(all(r['respect_native_approval_pool'] for r in plan['rules']))
        self.assertTrue(all(not r['user_ids'] for r in plan['rules']))
        self.assertEqual(plan['rules'][1]['team_ids'], [10])
        self.assertEqual(plan['settings']['prevent_author'], True)
        self.assertEqual(plan['settings']['reset_on_change'], True)
        self.assertEqual(plan['settings']['prevent_overrides'], True)

    def test_native_long_rule_names_remain_distinct_and_bounded(self):
        client = object.__new__(Reader)
        client.url = 'http://gitea:3000'
        client.governance_capabilities = lambda: {'version': 1, 'approval_rules': True}
        client.request = lambda path, params=None: ({'id':int(path.split('/')[-1]), 'name':'team'+path.split('/')[-1]}
                                                     if path.startswith('/teams/') else {'id': 7, 'full_name': 'org/repo', 'owner': {'login': 'org'}, 'name': 'repo'})
        client.pages = lambda path, params=None, key='id': ([{'id':10},{'id':20}] if path.endswith('/teams') else [])
        client._legacy_approval_pools = lambda repo, branches: ({b:[1,2,3,4] for b in branches}, {'team10':{1,4}, 'team20':{2,4}})
        client.governance_read = lambda path: {'settings_revision':0, 'settings':{'settings':{'prevent_author':True, 'reset_on_change':True}, 'sources':{}}}
        value = rule(); value['name'] = '长' * 100
        policy = type('Policy', (), {'repo_id': 7, 'rule_json': encode(value)})()
        names = [r['name'] for r in client.native_rule_plan(policy)['rules']]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(len(name) <= 100 for name in names))

    def test_native_rule_identity_does_not_follow_member_snapshot(self):
        client = object.__new__(Reader)
        client.url = 'http://gitea:3000'
        client.governance_capabilities = lambda: {'version': 1, 'approval_rules': True}
        client.request = lambda path, params=None: ({'id':10, 'name':'team10'} if path == '/teams/10' else
            {'id':7, 'full_name':'org/repo', 'owner':{'login':'org'}, 'name':'repo'})
        client.pages = lambda path, params=None, key='id': ([{'id':10}] if path.endswith('/teams') else [])
        client.governance_read = lambda path: {'settings':{'settings':{}, 'sources':{}}}
        policy_rule = normalize({'name':'稳定规则', 'branches':['release', 'main'], 'total':1,
                                 'groups':[{'team_id':10, 'required':1}]})
        policy = type('Policy', (), {'repo_id':7, 'rule_json':encode(policy_rule)})()
        snapshots = [
            ({'main':[1], 'release':[2]}, {'team10':{1, 2}}),
            ({'main':[3, 4], 'release':[4]}, {'team10':{3, 4}}),
        ]
        client._legacy_approval_pools = lambda repo, branches: snapshots.pop(0)
        first = client.native_rule_plan(policy)['rules']
        second = client.native_rule_plan(policy)['rules']
        self.assertEqual(first, second)
        self.assertEqual(first[0]['branches'], ['main', 'release'])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.secret_key = '隔离测试密钥'
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///'+self.tmp.name+'/test.db', TESTING=True)
        db.init_app(self.app); init_auth(self.app)
        self.app.register_blueprint(approval_bp, url_prefix='/api/approval-rules')
        self.app.register_blueprint(approval_internal_bp, url_prefix='/api/approval-internal')
        self.app.register_blueprint(approval_view_bp, url_prefix='/api/approval-view')
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        server = GiteaServer(name='隔离', host='localhost', ssh_user='root', gitea_port=3000, gitea_url='http://gitea:3000', api_token='隔离令牌')
        db.session.add(server); db.session.flush()
        self.token = '隔离随机令牌'+('x'*40)
        self.policy = ApprovalPolicy(server_id=server.id, repo_id=7, repo_name='org/repo', owner_id=8,
                                     rule_json=encode(rule()), hook_token_hash=hashlib.sha256(self.token.encode()).hexdigest())
        db.session.add(self.policy); db.session.commit(); self.pid = self.policy.id
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop(); self.tmp.cleanup()

    def login(self):
        g.pop('_login_user', None)
        login_platform(self.client)
        token = self.client.get('/api/approval-rules/csrf').json['token']
        return {'X-Approval-CSRF': token}

    def test_admin_auth_csrf_and_origin(self):
        self.assertEqual(self.client.get('/api/approval-rules/policies').status_code, 401)
        headers = self.login()
        self.assertEqual(self.client.post(f'/api/approval-rules/policies/{self.pid}/enable', json={}).status_code, 403)
        self.assertEqual(self.client.get('/api/approval-rules/policies', headers={'Origin': 'https://attacker.invalid'}).status_code, 403)
        self.assertEqual(self.client.get('/api/approval-rules/policies', headers=headers).status_code, 200)

    def test_production_enable_cannot_be_overridden(self):
        headers = self.login()
        for data in ({}, {'test': True}, {'release_override': True}, {'mode': 'isolated_enforced'}):
            with self.subTest(请求=data):
                r = self.client.post(f'/api/approval-rules/policies/{self.pid}/enable', json=data, headers=headers)
                self.assertEqual(r.status_code, 409); self.assertIn('APR-RACE-001', r.json['error'])

    def test_edit_revision_and_audit(self):
        headers = self.login()
        value = dict(rule(), total=4)
        response = self.client.put(f'/api/approval-rules/policies/{self.pid}', json={'revision': 1, 'rule': value}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['revision'], 2)
        self.assertEqual(self.client.put(f'/api/approval-rules/policies/{self.pid}', json={'revision': 1, 'rule': rule()}, headers=headers).status_code, 409)
        self.assertEqual(len(self.client.get(f'/api/approval-rules/policies/{self.pid}/audit').json['items']), 1)

    def request_hook(self, **changes):
        data = dict(repo_id=7, revision=1, nonce='a'*32, timestamp=int(time.time()), number=1,
                    pr_id=99, old='a'*40, new='b'*40, ref='refs/heads/main')
        data.update(changes)
        return self.client.post(f'/api/approval-internal/{self.pid}/check', json=data,
                                headers={'Authorization': 'Bearer '+self.token})

    def test_hook_auth_wrong_repo_and_replay(self):
        self.assertEqual(self.client.post(f'/api/approval-internal/{self.pid}/check', json={}).status_code, 409)
        self.assertEqual(self.request_hook(repo_id=8).status_code, 409)
        self.assertEqual(self.request_hook(timestamp=1).status_code, 409)
        self.assertEqual(self.request_hook().status_code, 409)
        self.assertIn('重放', self.request_hook().json['error'])
        self.assertEqual(ApprovalNonce.query.count(), 1)

    def test_hook_unknown_and_incomplete_state_never_allows(self):
        self.policy.mode = 'isolated_enforced'; db.session.commit()
        with patch('routes.approval_routes.status', side_effect=ApprovalError('分页不完整')):
            response = self.request_hook()
            self.assertFalse(response.json['allow']); self.assertEqual(response.status_code, 409)

    def test_native_pending_uninstall_bypasses_old_gate_until_removal(self):
        self.policy.mode = 'native_pending_uninstall'; db.session.commit()
        response = self.request_hook()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['allow'])

    def test_repeated_native_import_keeps_uninstall_pending(self):
        self.policy.mode = 'native_pending_uninstall'; db.session.commit()
        self.assertTrue(legacy_hook_pending(self.policy))

    def test_native_uninstall_token_does_not_replace_running_hook_identity(self):
        uninstall_token = '专用卸载令牌' + 'z'*40
        original_hash = self.policy.hook_token_hash
        self.policy.mode = 'native_pending_uninstall'
        self.policy.native_uninstall_token_hash = hashlib.sha256(uninstall_token.encode()).hexdigest()
        db.session.commit()
        data = dict(repo_id=7, revision=1, nonce='c'*32, timestamp=int(time.time()))
        response = self.client.post(f'/api/approval-internal/{self.pid}/uninstall-ready', json=data,
                                    headers={'Authorization':'Bearer '+uninstall_token})
        self.assertEqual(response.status_code, 200)
        db.session.refresh(self.policy)
        self.assertEqual(self.policy.hook_token_hash, original_hash)

    def test_native_uninstall_removal_ack_can_be_retried_after_response_loss(self):
        uninstall_token = '专用卸载重试令牌' + 'y'*40
        self.policy.mode = 'native_pending_uninstall'
        self.policy.native_uninstall_token_hash = hashlib.sha256(uninstall_token.encode()).hexdigest()
        db.session.commit()
        payload = dict(repo_id=7, revision=1, nonce='d'*32, timestamp=int(time.time()), removal=True)
        response = self.client.post(f'/api/approval-internal/{self.pid}/installed', json=payload,
                                    headers={'Authorization':'Bearer '+uninstall_token})
        self.assertEqual(response.status_code, 200)
        db.session.refresh(self.policy)
        self.assertEqual(self.policy.mode, 'native')
        self.assertTrue(self.policy.native_uninstall_token_hash)
        payload.update(nonce='e'*32, timestamp=int(time.time()))
        response = self.client.post(f'/api/approval-internal/{self.pid}/installed', json=payload,
                                    headers={'Authorization':'Bearer '+uninstall_token})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['already_removed'])

    def test_view_requires_gitea_cookie(self):
        self.assertEqual(self.client.get('/api/approval-view/1/7/1').status_code, 401)
        self.assertEqual(self.client.get('/api/approval-view/1/7/1', headers={'Origin': 'https://attacker.invalid'}).status_code, 403)

    def test_installed_shadow_cannot_lose_uninstall_credentials_by_edit(self):
        headers=self.login()
        self.policy.mode='shadow';db.session.commit()
        response=self.client.put(f'/api/approval-rules/policies/{self.pid}',
                                json={'revision':1,'rule':rule()},headers=headers)
        self.assertEqual(response.status_code,409)
        db.session.refresh(self.policy);self.assertEqual(self.policy.revision,1)

    def test_prepared_bundle_not_regenerated(self):
        from approval.service import prepare_bundle
        for mode in ('prepared_shadow','prepared_isolated_enforced','isolated_enforced','disable_pending'):
            self.policy.mode=mode;db.session.commit()
            with self.subTest(模式=mode),self.assertRaises(ApprovalError):
                prepare_bundle(self.policy,{'mode':'shadow'})

    def test_malformed_trusted_fields_are_rejected(self):
        self.policy.mode='isolated_enforced';db.session.commit()
        for i,change in enumerate(({'nonce':[]},{'nonce':None},{'old':3},{'new':{}})):
            with self.subTest(字段=change):
                r=self.request_hook(**dict({'nonce':format(i,'032x')},**change))
                self.assertEqual(r.status_code,409)
                self.assertFalse(r.json['allow'])

    def test_delete_requires_completed_uninstall(self):
        headers=self.login()
        for mode in ('shadow','isolated_enforced','prepared_shadow','disable_pending'):
            self.policy.mode=mode;db.session.commit()
            self.assertEqual(self.client.delete(f'/api/approval-rules/policies/{self.pid}',headers=headers).status_code,409)

    def test_closed_pr_page_without_head_requires_native_closed_state(self):
        self.client.set_cookie('i_like_gitea','隔离会话')
        for closed in (True,False):
            with self.subTest(原生关闭=closed),patch('requests.Session') as http,patch('routes.approval_routes.status') as status,patch('routes.approval_routes.Reader') as reader:
                from models import ApprovalViewCache
                ApprovalViewCache.query.delete();db.session.commit()
                response=http.return_value.__enter__.return_value.get.return_value.__enter__.return_value
                response.status_code=200
                response.iter_content.side_effect=[
                    [json.dumps({'convertedIssue':{'id':1,'repository':{'id':7},'number':1,'state':'closed','pull_request':{'merged':True}}}).encode()],
                    [b'<meta name="gm-approval-viewer" content="9">']]
                reader.return_value.request.return_value={'id':1,'number':1,'head':{'sha':HEAD},
                    'base':{'repo':{'id':7},'sha':HEAD,'ref':'main'},'state':'closed','merged':True}
                status.return_value={'head':HEAD,'base':HEAD,'closed':closed,'valid_until':time.time()+15}
                result=self.client.get('/api/approval-view/1/7/1')
                self.assertEqual(result.status_code,200 if closed else 409)

    def test_view_budget_includes_authentication_and_rejects_late_success(self):
        from models import ApprovalViewCache
        class Clock:
            now=1000.0
            def monotonic(self):return self.now
            def time(self):return self.now
            def sleep(self,seconds):self.now+=seconds
        self.client.set_cookie('i_like_gitea','隔离会话')
        for calculation_seconds in (1,7.1):
            with self.subTest(计算秒=calculation_seconds):
                clock=Clock();ApprovalViewCache.query.delete();db.session.commit()
                info={'convertedIssue':{'id':1,'repository':{'id':7},'number':1,'state':'open',
                                        'pull_request':{'merged':False}}}
                chunks=iter([json.dumps(info).encode(),b'<meta name="gm-approval-viewer" content="9">'])
                def native_chunks(*args):
                    clock.sleep(1.5)
                    return [next(chunks)]
                def calculate(policy,number,*,deadline):
                    self.assertEqual(deadline,1010)
                    self.assertEqual(clock.now,1003)
                    clock.sleep(calculation_seconds)
                    return {'satisfied':True,'closed':False,'checked_at':clock.time(),'valid_until':clock.time()+15}
                with patch('routes.approval_routes.time',clock),patch('approval.display_cache.time',clock), \
                     patch('requests.Session') as http,patch('routes.approval_routes.status',side_effect=calculate):
                    response=http.return_value.__enter__.return_value.get.return_value.__enter__.return_value
                    response.status_code=200;response.iter_content.side_effect=native_chunks
                    result=self.client.get('/api/approval-view/1/7/1')
                if calculation_seconds==1:
                    self.assertEqual(result.status_code,200)
                    self.assertEqual(result.json['viewer_id'],9)
                else:
                    self.assertEqual(result.status_code,409)
                    self.assertNotIn('satisfied',result.json)
                    self.assertFalse(result.json['allow'])
                    rows=ApprovalViewCache.query.all()
                    self.assertFalse(any(json.loads(row.payload).get('value',{}).get('satisfied') for row in rows))

    def test_status_preserves_remaining_budget_without_changing_default_hook_budget(self):
        from approval.service import status as calculate
        class Clock:
            now=1000.0
            def monotonic(self):return self.now
            def time(self):return self.now
        clock=Clock()
        context={'pr':{'id':1,'number':1,'base':{'ref':'不受管分支','sha':HEAD},'head':{'sha':HEAD},
                       'state':'open','merged':False}}
        with patch('approval.service.time',clock),patch('approval.service.reader') as source:
            source.return_value.context.return_value=context
            calculate(self.policy,1,deadline=1002)
            source.assert_called_once_with(self.policy,2)
            source.reset_mock()
            calculate(self.policy,1)
            source.assert_called_once_with(self.policy,8)
            source.reset_mock()
            with self.assertRaises(ApprovalError):calculate(self.policy,1,deadline=999)
            source.assert_not_called()
            def late(*args):
                clock.now=1003
                return context
            source.return_value.context.side_effect=late
            with self.assertRaises(ApprovalError):calculate(self.policy,1,deadline=1002)


    def test_native_reads_do_not_hold_manager_connection(self):
        from models import db
        from sqlalchemy import text
        import routes.approval_routes as routes
        self.client.set_cookie('i_like_gitea', '隔离会话')
        info = {'convertedIssue': {'id': 1, 'repository': {'id': 7}, 'number': 1,
                                  'state': 'open', 'pull_request': {'merged': False}}}
        responses = iter([json.dumps(info).encode(), b'<meta name="gm-approval-viewer" content="9">'])
        def chunks(*args):
            self.assertFalse(db.session().in_transaction(), '原生等待仍占用Manager事务')
            return [next(responses)]
        with patch('requests.Session') as http, patch.object(routes, 'status') as status:
            response = http.return_value.__enter__.return_value.get.return_value.__enter__.return_value
            response.status_code = 200
            response.iter_content.side_effect = chunks
            status.return_value = {'satisfied': True, 'closed': False, 'valid_until': time.time() + 15}
            result = self.client.get('/api/approval-view/1/7/1')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json['viewer_id'], 9)

    def test_configuration_change_during_native_read_rejects(self):
        from models import db
        from sqlalchemy import text
        import routes.approval_routes as routes
        self.client.set_cookie('i_like_gitea', '隔离会话')
        info = {'convertedIssue': {'id': 1, 'repository': {'id': 7}, 'number': 1,
                                  'state': 'open', 'pull_request': {'merged': False}}}
        responses = iter([json.dumps(info).encode(), b'<meta name="gm-approval-viewer" content="9">'])
        calls = 0
        def chunks(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                with db.engine.begin() as connection:
                    connection.execute(text('UPDATE approval_policies SET revision=revision+1'))
            return [next(responses)]
        with patch('requests.Session') as http, patch.object(routes, 'status') as status:
            response = http.return_value.__enter__.return_value.get.return_value.__enter__.return_value
            response.status_code = 200
            response.iter_content.side_effect = chunks
            result = self.client.get('/api/approval-view/1/7/1')
            self.assertEqual(result.status_code, 409)
            self.assertIn('配置变化', result.json['error'])
            status.assert_not_called()


class ReaderTests(unittest.TestCase):
    def test_final_review_revision_detects_native_decision_changes(self):
        base={'id':1,'state':'APPROVED','commit_id':'a'*40,'stale':False,'dismissed':False,'official':True,
              'user':{'id':2,'login':'alice','active':True,'prohibit_login':False}}
        original=review_revision([base])
        for field,value in (('dismissed',True),('stale',True),('state','REQUEST_CHANGES'),('commit_id','b'*40)):
            with self.subTest(变化=field):self.assertNotEqual(original,review_revision([{**base,field:value}]))
        self.assertNotEqual(original,review_revision([]))
        self.assertNotEqual(original,review_revision([base,{**base,'id':3}]))
        self.assertEqual(original,review_revision([{**base,'body':'评论正文变化','decision_event_id':100}]))

    def test_delayed_normal_push_preserves_current_sha_approvals(self):
        events = [{'id': 70, 'type': 'review', 'review_id': 43},
                  {'id': 71, 'type': 'review', 'review_id': 44},
                  {'id': 72, 'type': 'pull_push', 'body': '{"is_force_push":false,"commit_ids":["'+HEAD+'"]}'},
                  {'id': 73, 'type': 'review', 'review_id': 45}]
        self.assertEqual(reset_event_id(events), 0)
        events.append({'id': 74, 'type': 'pull_push', 'body': '{"is_force_push":true,"commit_ids":[]}'})
        self.assertEqual(reset_event_id(events), 74)
        events.append({'id': 75, 'type': 'change_target_branch'})
        self.assertEqual(reset_event_id(events), 75)

    def test_unrecognized_push_event_fails_closed(self):
        for body in (None, '损坏', '{}', '[]', '{"is_force_push":1}'):
            with self.subTest(事件=body), self.assertRaises(ApprovalError):
                reset_event_id([{'id': 1, 'type': 'pull_push', 'body': body}])

    def pages(self, values, totals):
        client=object.__new__(Reader)
        def request(path, params):
            index=params['page']-1
            client.total=totals[index]
            return values[index]
        client.request=request
        return client.pages('/隔离分页')

    def test_complete_multiple_pages(self):
        rows=[{'id':i} for i in range(1,52)]
        self.assertEqual(self.pages([rows[:50],rows[50:]],[51,51]),rows)

    def test_incomplete_duplicate_changed_count_and_malformed(self):
        rows=[{'id':i} for i in range(1,51)]
        cases=[([rows,rows[:1]],[51,51]),([rows,[]],[51,51]),
               ([rows,[{'id':51}]],[51,52]),([[None]],[1]),([[{'id':False}]],[1]),([{}],[0])]
        for values,totals in cases:
            with self.subTest(数据=values[-1]),self.assertRaises(ApprovalError):self.pages(values,totals)


class InstallerTests(unittest.TestCase):
    def test_managed_fragment_preserves_other_content(self):
        path = Path(__file__).resolve().parents[1]/'approval/assets/install.py'
        spec = importlib.util.spec_from_file_location('approval_install', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        original = '<footer>已有页脚</footer>\n<!-- 其他组件 -->'
        once = module.merged_footer(original, '<script>扩展</script>')
        twice = module.merged_footer(once, '<script>扩展</script>')
        self.assertEqual(once, twice)
        self.assertIn(original, module.merged_footer(twice, None))
        for broken in (module.BEGIN, module.END, module.END+module.BEGIN, once+module.BEGIN):
            with self.assertRaises(ValueError):
                module.merged_footer(broken, '不能覆盖')


if __name__ == '__main__':
    unittest.main(verbosity=2)
