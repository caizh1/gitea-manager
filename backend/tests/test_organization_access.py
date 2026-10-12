"""组织隔离的对抗回归：真实会话、两套数据库及原业务接口，仅替代外部 Gitea 通信。"""
import hashlib
import json
import secrets
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask
from werkzeug.security import generate_password_hash
from auth import init_auth
from models import (db, Setting, GiteaServer, PolicyTemplate, PolicyBatch, PolicyRepoResult,
                    CommitMessageRule, CommitGateAssignment, ApprovalPolicy, Alert)
from access.models import ManagerAccount, AccessSession, DirectorySource, OrganizationBinding, OrganizationGrant
from access.authentication import initialize_access
from access.control import install
from access.errors import AccessError
from access.scopes import ResourceScope, bind_resource, server_epoch, check_task_actor, require_repo
from access.statistics import RepositoryStatisticsSnapshot
from access.identity import OrganizationIdentity, created_value, register_organization, verify_repository
from routes.auth_routes import auth_bp
from routes.access_routes import access_bp
from routes.policy_routes import policy_bp
from routes.commit_gate_routes import commit_gate_bp
from routes.approval_routes import approval_bp
from routes.ai_review_routes import ai_review_bp
from routes.alert_routes import alert_bp
from routes.server_routes import server_bp
from routes.statistics_routes import statistics_bp
from ai_review.store import Store
from ai_review.common import ReviewError


class DirectoryFixture:
    """外部仓库目录替身；未知调用直接失败，避免默许未覆盖的外部写入。"""
    repos = {}
    organizations = {}
    def __init__(self, server):
        self.server_id = server.id
    def request(self, method, path, **kwargs):
        if method == 'GET' and path in ('/orgs/alpha', '/orgs/beta', '/users/alpha', '/users/beta'):
            name = path.rsplit('/', 1)[-1]
            return dict(self.organizations[name])
        raise AssertionError('不允许未声明的外部请求：' + method + ' ' + path)
    def repo(self, repo_id):
        return dict(self.repos[int(repo_id)])
    def pages(self, path, **kwargs):
        if path == '/orgs':
            return list(self.organizations.values())
        if path in ('/orgs/alpha/repos', '/orgs/beta/repos'):
            owner = 10 if '/alpha/' in path else 20
            return [dict(row) for row in self.repos.values() if row['owner']['id'] == owner]
        raise AssertionError('不允许未声明的目录查询：' + path)


class OrganizationAccessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='组织隔离测试专用密钥-234567890abcdef',
            SQLALCHEMY_DATABASE_URI='sqlite:///' + self.tmp.name + '/manager.db', SQLALCHEMY_TRACK_MODIFICATIONS=False,
            AI_REVIEW_DB=self.tmp.name + '/review.db', ACCESS_KEY_FILE=self.tmp.name + '/access.key')
        db.init_app(self.app)
        init_auth(self.app)
        for blueprint, prefix in [(auth_bp, '/api'), (access_bp, '/api/access'), (policy_bp, '/api/repository-policies'),
                (commit_gate_bp, '/api'), (approval_bp, '/api/approval-rules'), (ai_review_bp, '/api/ai-review'),
                (alert_bp, '/api'), (server_bp, '/api'), (statistics_bp, '/api')]:
            self.app.register_blueprint(blueprint, url_prefix=prefix)
        self.app.add_url_rule('/api/unclassified', 'unclassified', lambda: {'secret': '未分类接口'})
        install(self.app)
        DirectoryFixture.repos = {
            1001: {'id': 1001, 'created_at': '2026-10-01T00:00:00Z', 'full_name': 'alpha/same', 'name': 'same', 'owner': {'id': 10, 'login': 'alpha'}, 'permissions': {'admin': True}},
            1002: {'id': 1002, 'created_at': '2026-10-02T00:00:00Z', 'full_name': 'beta/same', 'name': 'same', 'owner': {'id': 20, 'login': 'beta'}, 'permissions': {'admin': True}},
        }
        DirectoryFixture.organizations = {name: {'id': ident, 'name': name, 'created': '2026-09-01T00:00:00Z'}
                                          for name, ident in [('alpha', 10), ('beta', 20)]}
        self.external = patch('services.policy_gitea.PolicyGitea', DirectoryFixture)
        self.external.start()
        self.addCleanup(self.external.stop)
        with self.app.app_context():
            db.create_all()
            db.session.add_all([Setting(key='admin_password', value=generate_password_hash('隔离测试-password-123')),
                                Setting(key='host_ip', value='127.0.0.1')])
            db.session.commit()
            initialize_access()
            self.platform_id = ManagerAccount.query.filter_by(source_id=None).one().id
            directory = DirectorySource(name='隔离目录', enabled=True, version=1)
            db.session.add(directory); db.session.flush()
            account = ManagerAccount(source_id=directory.id, subject='entryUUID:00000000-0000-0000-0000-000000000001',
                                     username='alpha-admin', display_name='甲部门管理员')
            db.session.add(account); db.session.flush(); self.account_id = account.id
            server = GiteaServer(id=1, name='隔离实例', host='127.0.0.1', ssh_user='test', gitea_port=3000,
                                 gitea_url='https://gitea.test', api_token='测试令牌', access_epoch='测试实例代次', instance_identity='测试实例')
            db.session.add(server); db.session.flush(); self.epoch = server_epoch(server)
            db.session.add_all([OrganizationBinding(id=1, server_id=1, organization_id=10, organization_name='alpha', instance_epoch=self.epoch),
                                OrganizationBinding(id=2, server_id=1, organization_id=20, organization_name='beta', instance_epoch=self.epoch)])
            db.session.flush()
            for binding in OrganizationBinding.query.all():
                register_organization(binding, created_value(DirectoryFixture.organizations[binding.organization_name]['created']))
                verify_repository(binding, DirectoryFixture.repos[1001 if binding.id == 1 else 1002])
            db.session.add(OrganizationGrant(account_id=self.account_id, binding_id=1, granted_by=self.platform_id))
            for index, org in enumerate((10, 20, None), 1):
                db.session.add(PolicyTemplate(id=100+index, server_id=1, name=f'模板{index}', organization_id=org,
                    organization_name='alpha' if org == 10 else 'beta' if org else '', rules_json='[]', content_hash='测试摘要'))
                db.session.add(CommitMessageRule(id=200+index, server_id=1, name=f'门禁{index}', pattern='.*'))
                db.session.add(PolicyBatch(id=300+index, server_id=1, server_name='隔离实例', connection_hash='连接摘要',
                    template_id=100+index, template_name=f'模板{index}', template_hash='测试摘要', template_json='[]',
                    scope_json=json.dumps({'organization': {'id': org}}) if org else '{}', total=1, status='completed'))
                db.session.add(PolicyRepoResult(batch_id=300+index, repo_id=1000+index, status='success'))
                db.session.add(Alert(id=500+index, server_id=1, server_name='隔离实例', alert_type='policy_failed', message=f'告警{index}'))
            for index, org in enumerate((10, 20), 1):
                db.session.add(ApprovalPolicy(id=400+index, server_id=1, repo_id=1000+index,
                    repo_name=f'{"alpha" if org == 10 else "beta"}/same', owner_id=org,
                    rule_json=json.dumps({'name': '审批', 'branches': ['main'], 'total': 1, 'groups': [], 'reset_on_push': True})))
            db.session.flush()
            for index, org in enumerate((10, 20), 1):
                for kind, key, repos in [('policy_template', 100+index, []), ('commit_rule', 200+index, []),
                        ('policy_batch', 300+index, [1000+index]), ('approval_policy', 400+index, [1000+index]),
                        ('alert', 500+index, [])]:
                    bind_resource(kind, key, 1, org, repos, actor={})
            db.session.commit()
            self.state = Store(self.app.config['AI_REVIEW_DB'])
            for index, org in enumerate((10, 20), 1):
                scope = {'binding_id': index, 'instance_epoch': self.epoch, 'server_id': 1, 'organization_id': org}
                record = self.state.save_repo(1, 1000+index, {'enabled': True, 'repo_name': f'{"alpha" if org == 10 else "beta"}/same',
                    'server_name': '隔离实例', 'access_scope': scope})
                bind_resource('ai_repository', record['id'], 1, org, [1000+index], actor={})
                now = time.time()
                with self.state.tx() as connection:
                    connection.execute('INSERT INTO jobs(id,repository,number,generation,status,created,available,updated,config_revision,repo_revision,access_scope) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        ('a'*32 if index == 1 else 'b'*32, record['id'], 1, 1, 'succeeded', now, now, now, 1, 1, json.dumps(scope)))
            db.session.commit()
        self.client = self.login_as(self.account_id)
        self.admin = self.login_as(self.platform_id)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
        self.tmp.cleanup()

    def login_as(self, account_id):
        client = self.app.test_client()
        token, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), time.time()
        with self.app.app_context():
            account = db.session.get(ManagerAccount, account_id)
            db.session.add(AccessSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), account_id=account_id,
                auth_version=account.auth_version, source_version=1 if account.source_id else 0,
                created_at=now, expires_at=now+28800, last_seen_at=now, directory_checked_at=now))
            db.session.commit()
        with client.session_transaction(base_url='https://localhost') as value:
            value['_user_id'] = token
            value['_fresh'] = True
            value['access_csrf'] = csrf
        client.access_csrf = csrf
        return client

    def request(self, path, method='get', payload=None, client=None, binding=1):
        client = client or self.client
        headers = {'X-CSRF-Token': client.access_csrf}
        if binding is not None:
            headers['X-Organization-ID'] = str(binding)
        return getattr(client, method)(path, json=payload, headers=headers, base_url='https://localhost')

    def test_unregistered_endpoint_rejects_every_role_and_cache_is_private(self):
        for client in (self.client, self.admin):
            response = self.request('/api/unclassified', client=client, binding=None)
            self.assertEqual(response.status_code, 403)
            self.assertNotIn('未分类接口', response.get_data(as_text=True))
            self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_registered_business_routes_all_have_explicit_classification(self):
        registry = self.app.extensions['access_route_registry']
        missing = [rule.endpoint for rule in self.app.url_map.iter_rules()
                   if rule.rule.startswith('/api/') and rule.endpoint != 'unclassified' and rule.endpoint not in registry]
        self.assertEqual(missing, [], '现有接口必须逐项登记，不能隐式开放')

    def test_csrf_and_origin_guard_are_enforced_for_real_sessions(self):
        response = self.client.put('/api/commit-rules/201', json={'name': '不可写入'},
                                   headers={'X-Organization-ID': '1'}, base_url='https://localhost')
        self.assertEqual(response.status_code, 403)
        response = self.client.get('/api/servers', headers={'X-Organization-ID': '1', 'Origin': 'https://foreign.test'},
                                   base_url='https://localhost')
        self.assertEqual(response.status_code, 403)
        with self.app.app_context():
            self.assertEqual(db.session.get(CommitMessageRule, 201).name, '门禁1')

    def test_multiple_grants_require_explicit_scope_and_clear_previous_selection(self):
        self.request('/api/access/grants', 'post', {'account_id': self.account_id, 'binding_id': 2}, client=self.admin, binding=None)
        self.assertEqual(self.request('/api/servers', binding=None).status_code, 403)
        self.assertEqual([row['id'] for row in self.request('/api/commit-rules?server_id=1', binding=2).json['rules']], [202])
        self.assertEqual([row['id'] for row in self.request('/api/commit-rules?server_id=1', binding=1).json['rules']], [201])

    def test_direct_platform_endpoints_and_other_binding_are_rejected(self):
        for path in ('/api/servers/1', '/api/access/accounts', '/api/ai-review/servers/1/progress-assets'):
            self.assertEqual(self.request(path).status_code, 403, path)
        self.assertEqual(self.request('/api/servers', binding=2).status_code, 404)
        response = self.request('/api/servers')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json[0]), {'id', 'name', 'version'})

    def test_cross_organization_entity_ids_and_batch_mix_fail_before_writes(self):
        cases = [('/api/repository-policies/templates/102', 'delete', None),
                 ('/api/repository-policies/batches/302', 'get', None),
                 ('/api/repository-policies/batches/302/apply', 'post', {}),
                 ('/api/commit-rules/202', 'put', {'name': '篡改'}),
                 ('/api/approval-rules/policies/402', 'put', {}),
                 ('/api/repository-policies/repositories/1002/rules?server_id=1', 'get', None),
                 ('/api/commit-gates/apply', 'post', {'server_id': 1, 'rule_id': 201, 'repo_names': ['alpha/same', 'beta/same']}),
                 ('/api/repository-policies/batches', 'post', {'template_id': 101, 'repo_ids': [1001, 1002]})]
        for path, method, data in cases:
            with self.subTest(接口=path):
                self.assertEqual(self.request(path, method, data).status_code, 404)
        with self.app.app_context():
            self.assertEqual(db.session.get(CommitMessageRule, 202).name, '门禁2')
            self.assertIsNotNone(db.session.get(PolicyTemplate, 102))

    def test_denied_repo_reference_never_archives_other_organizations_resources(self):
        self.seed_current_repository_records()
        response = self.request('/api/repository-policies/batches', 'post', {'template_id': 101, 'repo_ids': [1002]})
        self.assertEqual(response.status_code, 404)
        with self.app.app_context():
            self.assertEqual(ResourceScope.query.filter_by(binding_id=2, archived=True).count(), 0,
                             '越权请求不得封存另一个组织的正常数据')
            self.assertEqual(CommitGateAssignment.query.filter_by(rule_id=202).one().repo_id, 1002)
            self.assertEqual(db.session.get(RepositoryStatisticsSnapshot, (1, 1002)).instance_epoch, self.epoch)

    def seed_current_repository_records(self):
        with self.app.app_context():
            for index, organization in ((1, 10), (2, 20)):
                name = 'alpha/same' if index == 1 else 'beta/same'
                db.session.add(CommitGateAssignment(server_id=1, repo_id=1000+index, repo_name=name,
                    rule_id=200+index, install_status='success', install_log='原记录保留给平台核对'))
                db.session.add(RepositoryStatisticsSnapshot(server_id=1, repo_id=1000+index,
                    organization_id=organization, instance_epoch=self.epoch, value_json=json.dumps({
                        'repo_id': 1000+index, 'repo_name': name, 'commits': 9, 'complete': True,
                        'authors': {}, 'periods': {}})))
            db.session.commit()

    def assert_transfer_back_does_not_restore_current_records(self, observation_path, status):
        self.seed_current_repository_records()
        original = DirectoryFixture.repos[1001]
        DirectoryFixture.repos[1001] = {**original, 'full_name': 'beta/moved', 'owner': {'id': 20, 'login': 'beta'}}
        self.assertEqual(self.request(observation_path).status_code, status)
        DirectoryFixture.repos[1001] = original
        stats = self.request('/api/statistics/1/overview')
        self.assertFalse(stats.json['ready']); self.assertIsNone(stats.json['total_commits'])
        gates = self.request('/api/commit-gates/repos?server_id=1')
        self.assertIsNone(gates.json[0]['assignment_id'])
        with self.app.app_context():
            old = CommitGateAssignment.query.filter_by(rule_id=201).one()
            self.assertEqual(old.repo_id, 0)
            self.assertEqual(old.install_log, '原记录保留给平台核对')
            self.assertTrue(db.session.get(RepositoryStatisticsSnapshot, (1, 1001)).instance_epoch.startswith('archived:'))
            self.assertEqual(CommitGateAssignment.query.filter_by(rule_id=202).one().repo_id, 1002)
            self.assertEqual(db.session.get(RepositoryStatisticsSnapshot, (1, 1002)).instance_epoch, self.epoch)

    def test_transfer_back_after_entity_lookup_does_not_restore_old_gate_or_statistics(self):
        self.assert_transfer_back_does_not_restore_current_records('/api/repository-policies/batches/301', 404)

    def test_transfer_back_after_batch_listing_does_not_restore_old_gate_or_statistics(self):
        self.assert_transfer_back_does_not_restore_current_records('/api/repository-policies/batches?server_id=1', 200)

    def test_transfer_back_after_gate_listing_does_not_restore_old_gate_or_statistics(self):
        self.assert_transfer_back_does_not_restore_current_records('/api/commit-gates/repos?server_id=1', 200)

    def test_counts_search_and_history_only_include_current_organization(self):
        response = self.request('/api/repository-policies/templates?server_id=1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['id'] for row in response.json], [101])
        response = self.request('/api/repository-policies/batches?server_id=1')
        self.assertEqual(response.json['total'], 1)
        self.assertEqual([row['id'] for row in response.json['items']], [301])
        self.assertEqual([row['id'] for row in self.request('/api/commit-rules?server_id=1').json['rules']], [201])
        response = self.request('/api/alerts/summary')
        self.assertEqual(response.json['active_count'], 1)
        self.assertEqual(response.json['latest_alert']['id'], 501)
        self.assertEqual(self.request('/api/alerts/502/clear', 'post', {}).status_code, 404)
        response = self.request('/api/repository-policies/templates?server_id=1&org_id=20')
        self.assertEqual(response.status_code, 404)

    def test_commit_gate_repository_list_is_usable_and_same_name_rebuild_does_not_inherit(self):
        with self.app.app_context():
            assignment = CommitGateAssignment(server_id=1, repo_name='alpha/same', repo_id=1001,
                rule_id=201, install_status='installed', install_log='仅平台可见的原始安装诊断')
            db.session.add(assignment); db.session.commit()
        response = self.request('/api/commit-gates/repos?server_id=1')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual([(row['repo_id'], row['repo_name'], row['rule_id']) for row in response.json],
                         [(1001, 'alpha/same', 201)])
        self.assertEqual(response.json[0]['install_log'], '')
        DirectoryFixture.repos[1003] = dict(DirectoryFixture.repos.pop(1001), id=1003)
        response = self.request('/api/commit-gates/repos?server_id=1')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json[0]['repo_id'], 1003)
        self.assertIsNone(response.json[0]['rule_id'])
        self.assertIsNone(response.json[0]['assignment_id'])

    def test_unknown_legacy_records_are_platform_only(self):
        response = self.request('/api/repository-policies/templates?server_id=1&legacy=1')
        self.assertEqual(response.json, [])
        response = self.request('/api/repository-policies/templates?server_id=1&legacy=1', client=self.admin, binding=None)
        self.assertEqual([row['id'] for row in response.json], [103])
        with self.app.app_context():
            from access.migration import migrate_binding
            migrate_binding(db.session.get(OrganizationBinding, 1))
            self.assertIsNone(ResourceScope.query.filter_by(kind='policy_template', resource_key='103').first())
            self.assertIsNone(ResourceScope.query.filter_by(kind='policy_batch', resource_key='303').first())

    def test_organization_reused_id_invalidates_instance_and_requires_new_grants(self):
        DirectoryFixture.organizations['alpha']['created'] = '2026-10-11T00:00:00Z'
        response = self.request('/api/commit-rules?server_id=1')
        self.assertEqual(response.status_code, 409)
        with self.app.app_context():
            self.assertEqual(OrganizationBinding.query.filter_by(enabled=True).count(), 0)
            self.assertEqual(ResourceScope.query.filter_by(archived=False).count(), 0)
        sync = self.request('/api/access/organizations', 'post', {'server_id': 1}, client=self.admin, binding=None)
        self.assertEqual(sync.status_code, 200, sync.json)
        new_binding = next(row['binding_id'] for row in sync.json['items'] if row['organization_id'] == 10)
        self.assertNotEqual(new_binding, 1)
        self.assertEqual(self.request('/api/commit-rules?server_id=1', binding=new_binding).status_code, 404)
        with self.app.app_context():
            self.assertIsNone(OrganizationGrant.query.filter_by(account_id=self.account_id, binding_id=new_binding).first())
            self.assertTrue(ResourceScope.query.filter_by(kind='ai_repository', resource_key='1').one().archived)

    def test_platform_sync_cannot_silently_replace_organization_birth(self):
        DirectoryFixture.organizations['alpha']['created'] = '2026-10-11T00:00:00Z'
        sync = self.request('/api/access/organizations', 'post', {'server_id': 1}, client=self.admin, binding=None)
        self.assertEqual(sync.status_code, 409)
        with self.app.app_context():
            self.assertEqual(OrganizationBinding.query.count(), 2)
            self.assertEqual(OrganizationBinding.query.filter_by(enabled=True).count(), 0)

    def test_binding_without_registered_birth_requires_platform_sync(self):
        with self.app.app_context():
            db.session.delete(db.session.get(OrganizationIdentity, 1)); db.session.commit()
        self.assertEqual(self.request('/api/commit-rules?server_id=1').status_code, 409)
        sync = self.request('/api/access/organizations', 'post', {'server_id': 1}, client=self.admin, binding=None)
        self.assertEqual(sync.status_code, 200, sync.json)
        self.assertEqual(self.request('/api/commit-rules?server_id=1').status_code, 200)

    def test_reused_repository_id_archives_old_scopes_gate_and_statistics(self):
        from ai_review import dataset
        sample = dataset.put(self.state, 1, 1, 42, {'status': 'candidate', 'target': '旧样本', 'label': 'unconfirmed'}, 1)
        with self.app.app_context():
            db.session.add(CommitGateAssignment(server_id=1, repo_name='alpha/same', repo_id=1001, rule_id=201,
                install_status='installed', install_log='需要保留的平台历史'))
            db.session.add(RepositoryStatisticsSnapshot(server_id=1, repo_id=1001, organization_id=10,
                instance_epoch=self.epoch, value_json=json.dumps({'complete': True, 'commits': 100,
                'authors': {}, 'periods': {}, 'repo_id': 1001, 'repo_name': 'alpha/same'})))
            db.session.commit()
        DirectoryFixture.repos[1001]['created_at'] = '2026-10-11T00:00:00Z'
        self.assertEqual(self.request('/api/repository-policies/batches/301').status_code, 409)
        self.assertEqual(self.request('/api/repository-policies/batches/301').status_code, 404)
        self.assertEqual(self.request('/api/ai-review/repositories').json, [])
        self.assertTrue(self.state.repo(1)['access_archived'])
        self.assertFalse(self.state.repo(1)['enabled'])
        with self.assertRaises(ReviewError):
            self.state.save_repo(1, 1001, {'enabled': True, 'revision': self.state.repo(1)['revision']}, repository=1)
        created = self.state.save_repo(1, 1001, {'enabled': False, 'repo_name': 'alpha/same'})
        self.assertNotEqual(created['id'], 1)
        self.assertEqual(self.state.repo(1)['repo_id'], 1001)
        self.assertEqual(dataset.list_examples(self.state, created['id'])['total'], 0)
        self.assertEqual(dataset.list_examples(self.state, 1)['items'][0]['id'], sample['id'])
        self.assertEqual(self.state.job('a'*32)['repository'], 1)
        repos = self.request('/api/commit-gates/repos?server_id=1')
        self.assertEqual(repos.status_code, 200, repos.json)
        self.assertIsNone(repos.json[0]['assignment_id'])
        stats = self.request('/api/statistics/1/overview')
        self.assertEqual(stats.status_code, 200, stats.json)
        self.assertFalse(stats.json['ready']); self.assertIsNone(stats.json['total_commits'])
        with self.app.app_context():
            self.assertEqual(CommitGateAssignment.query.one().install_log, '需要保留的平台历史')
            self.assertTrue(db.session.get(RepositoryStatisticsSnapshot, (1, 1001)).instance_epoch.startswith('archived:'))
            self.assertEqual(ResourceScope.query.filter_by(binding_id=2, archived=True).count(), 0)
            self.assertIsNone(bind_resource('policy_batch', 301, 1, 10, [1001], migration=True))
            new_scope = bind_resource('policy_batch', 999, 1, 10, [1001])
            self.assertFalse(new_scope.archived)

    def test_missing_birth_fails_closed_and_equivalent_time_offsets_are_accepted(self):
        DirectoryFixture.organizations['alpha']['created'] = '2026-09-01T08:00:00+08:00'
        DirectoryFixture.repos[1001]['created_at'] = '2026-10-01T08:00:00+08:00'
        self.assertEqual(self.request('/api/repository-policies/batches/301').status_code, 200)
        DirectoryFixture.repos[1001].pop('created_at')
        self.assertEqual(self.request('/api/repository-policies/batches/301').status_code, 409)
        with self.app.app_context():
            self.assertFalse(ResourceScope.query.filter_by(kind='policy_batch', resource_key='301').one().archived)

    def test_ai_repository_job_export_and_totals_are_isolated(self):
        self.assertEqual([row['id'] for row in self.request('/api/ai-review/repositories').json], [1])
        response = self.request('/api/ai-review/tasks')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['total'], 1)
        self.assertEqual([row['id'] for row in response.json['items']], ['a'*32])
        for path in (f'/api/ai-review/tasks/{"b"*32}', f'/api/ai-review/tasks/{"b"*32}/events/export',
                     '/api/ai-review/repositories/2/dataset/export'):
            self.assertEqual(self.request(path).status_code, 404, path)
        own = self.request(f'/api/ai-review/tasks/{"a"*32}/events/export')
        self.assertEqual(own.status_code, 200)
        own.close()
        self.assertEqual(self.request('/api/ai-review/repositories/1', 'put', {'enabled': True, 'revision': 1, 'bot_token': '禁止设置'}).status_code, 403)
        self.assertEqual(set(self.request('/api/ai-review/config').json['config']), {'enabled'})

    def test_revocation_regrant_does_not_resurrect_old_task_snapshot(self):
        snapshot = {'account_id': self.account_id, 'auth_version': 1, 'binding_id': 1, 'grant_version': 1, 'platform': False}
        with self.app.app_context():
            self.assertEqual(check_task_actor(snapshot).id, 1)
        response = self.request(f'/api/access/grants/{self.account_id}/1', 'delete', client=self.admin, binding=None)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.request('/api/servers').status_code, 404)
        response = self.request('/api/access/grants', 'post', {'account_id': self.account_id, 'binding_id': 1}, client=self.admin, binding=None)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.request('/api/servers').status_code, 200)
        with self.app.app_context():
            with self.assertRaises(AccessError):
                check_task_actor(snapshot)
            grant = db.session.get(OrganizationGrant, (self.account_id, 1))
            self.assertGreater(grant.version, 1)
            self.assertEqual(check_task_actor(dict(snapshot, grant_version=grant.version)).id, 1)

    def test_actual_repo_transfer_freezes_old_organization_without_granting_new_history(self):
        DirectoryFixture.repos[1001] = {**DirectoryFixture.repos[1001], 'full_name': 'beta/moved', 'owner': {'id': 20, 'login': 'beta'}}
        self.assertEqual(self.request('/api/repository-policies/batches/301').status_code, 404)
        with self.app.app_context():
            self.assertTrue(ResourceScope.query.filter_by(kind='policy_batch', resource_key='301').one().archived)
            self.assertEqual(ResourceScope.query.filter_by(binding_id=2, archived=True).count(), 0)

    def test_legacy_ai_repository_transfer_cannot_migrate_old_history_into_new_organization(self):
        DirectoryFixture.repos[1001] = {**DirectoryFixture.repos[1001], 'full_name': 'beta/moved', 'owner': {'id': 20, 'login': 'beta'}}
        with self.app.app_context():
            # 模拟升级前未记录权限快照的旧 Review 配置；记录名称仍是原组织路径。
            ResourceScope.query.filter_by(kind='ai_repository', resource_key='1').delete()
            self.state.repo_status(1, access_scope={})
            db.session.commit()
            from access.migration import migrate_binding
            migrate_binding(db.session.get(OrganizationBinding, 2))
            self.assertIsNone(ResourceScope.query.filter_by(kind='ai_repository', resource_key='1').first(),
                              '无法证明历史归属时必须保留平台专属，不能只凭当前仓库 ID 迁移样本')
            self.assertEqual(self.state.repo(1).get('access_scope'), {})

    def test_missing_repository_statistics_are_pending_instead_of_global_zero(self):
        response = self.request('/api/statistics/1/overview')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(response.json['ready'])
        self.assertTrue(response.json['pending_collection'])
        self.assertIsNone(response.json['total_commits'])
        self.assertEqual(response.json['total_repos'], 1)


if __name__ == '__main__':
    unittest.main()
