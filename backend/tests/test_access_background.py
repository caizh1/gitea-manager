"""后台组织授权回归：拒绝范围伪造、撤权后的写入和失效记录导致的跨组织阻塞。"""
import contextlib
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests import test_organization_access as fixture
from access.internal import internal_bp
from access.models import OrganizationBinding, OrganizationGrant
from access.scopes import ResourceScope
from ai_review import dataset, feedback
from ai_review.common import ReviewError
from ai_review.store import Store
from models import db


class BackgroundAccessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.OrganizationAccessTest()
        self.fixture.setUp()
        self.app, self.store = self.fixture.app, self.fixture.state
        self.app.register_blueprint(internal_bp, url_prefix='/api/access-internal')
        self.client = self.app.test_client()
        token_path = Path(self.fixture.tmp.name) / 'worker-token'
        self.token = 'a' * 64
        token_path.write_text(self.token)
        self.environment = patch.dict(os.environ, {'AI_REVIEW_AUTHORIZATION_TOKEN_FILE': str(token_path),
            'AI_REVIEW_GITEA_HOSTS': 'gitea.test'})
        self.environment.start()
        self.store.save_config({'revision': self.store.config()['revision'], 'enabled': True})
        for repo in self.store.repos():
            self.store.repo_status(repo['id'], gitea_url='https://gitea.test', bot_token='', bot_id=3)

    def tearDown(self):
        self.environment.stop()
        self.fixture.tearDown()
        self.fixture.doCleanups()

    def check(self, payload, token=None):
        return self.client.post('/api/access-internal/check', json=payload,
            headers={'Authorization': 'Bearer ' + (token if token is not None else self.token)},
            base_url='https://localhost')

    def test_internal_service_identity_and_payload_cannot_impersonate_users(self):
        self.assertEqual(self.check({'repository_id': 1, 'action': 'read'}, token='invalid-token').status_code, 401)
        self.assertEqual(self.check({'repository_id': 1, 'action': 'read', 'account_id': self.fixture.platform_id}).status_code, 400)
        self.assertEqual(self.check({'repository_id': 1, 'action': 'read'}).json, {'allowed': True})
        self.assertEqual(self.check({'repository_id': 2, 'job_id': 'a'*32, 'action': 'write'}).status_code, 403)

    def test_disabled_binding_or_archived_repository_stops_existing_service(self):
        with self.app.app_context():
            db.session.get(OrganizationBinding, 1).enabled = False
            db.session.commit()
        self.assertEqual(self.check({'repository_id': 1, 'action': 'publish'}).status_code, 409)
        self.assertEqual(self.check({'repository_id': 2, 'action': 'publish'}).status_code, 200)
        with self.app.app_context():
            db.session.get(OrganizationBinding, 1).enabled = True
            ResourceScope.query.filter_by(kind='ai_repository', resource_key='1').one().archived = True
            db.session.commit()
        self.assertEqual(self.check({'repository_id': 1, 'action': 'write'}).status_code, 403)

    def test_reused_repository_identity_pauses_service_without_stopping_other_organization(self):
        fixture.DirectoryFixture.repos[1001]['created_at'] = '2026-10-11T00:00:00Z'
        self.assertEqual(self.check({'repository_id': 1, 'action': 'publish'}).status_code, 409)
        self.assertEqual(self.check({'repository_id': 1, 'action': 'publish'}).status_code, 403)
        self.assertEqual(self.check({'repository_id': 2, 'action': 'publish'}).status_code, 200)

    def test_personal_revocation_stops_manual_task_but_preserves_organization_service(self):
        actor = {'account_id': self.fixture.account_id, 'auth_version': 1, 'binding_id': 1,
                 'grant_version': 1, 'platform': False}
        with self.store.tx() as connection:
            connection.execute('UPDATE jobs SET manual=1,access_actor=? WHERE id=?', (json.dumps(actor), 'a'*32))
        self.assertEqual(self.check({'repository_id': 1, 'job_id': 'a'*32, 'action': 'write'}).status_code, 200)
        with self.app.app_context():
            grant = OrganizationGrant.query.filter_by(account_id=self.fixture.account_id, binding_id=1).one()
            grant.enabled = False; grant.version += 1
            db.session.commit()
        self.assertEqual(self.check({'repository_id': 1, 'job_id': 'a'*32, 'action': 'write'}).status_code, 404)
        self.assertEqual(self.check({'repository_id': 1, 'action': 'write'}).status_code, 200)
        with self.app.app_context():
            grant = OrganizationGrant.query.filter_by(account_id=self.fixture.account_id, binding_id=1).one()
            grant.enabled = True; grant.version += 1
            db.session.commit()
        self.assertEqual(self.check({'repository_id': 1, 'job_id': 'a'*32, 'action': 'write'}).status_code, 403)

    def test_new_review_setup_is_bound_immediately_without_resynchronizing(self):
        from access.scopes import bind_review_repository
        actual = dict(fixture.DirectoryFixture.repos[1001], id=1003, full_name='alpha/new')
        fixture.DirectoryFixture.repos[1003] = actual
        with self.app.app_context():
            created = self.store.save_repo(1, 1003, {'enabled': True, 'repo_name': 'alpha/new'})
            bind_review_repository(self.store, created['id'], actual, actor={'name': '实际接入者'})
            scope = ResourceScope.query.filter_by(kind='ai_repository', resource_key=str(created['id'])).one()
            self.assertEqual(scope.binding_id, 1)
            self.assertEqual(json.loads(scope.actor_json)['name'], '实际接入者')
        self.assertEqual(self.check({'repository_id': created['id'], 'action': 'publish'}).status_code, 200)

    def test_failed_manual_binding_never_leaves_an_enabled_unscoped_service(self):
        from access.errors import AccessError
        from access.scopes import bind_review_repository
        for index, fail in ((1003, True), (1004, False)):
            actual = dict(fixture.DirectoryFixture.repos[1001], id=index, full_name=f'alpha/new{index}')
            fixture.DirectoryFixture.repos[index] = actual
            def bind(state, repository, source):
                self.assertFalse(state.repo(repository)['enabled'])
                self.assertEqual(self.check({'repository_id': repository, 'action': 'publish'}).status_code, 403)
                if fail:
                    raise AccessError('归属核验失败', 409)
                return bind_review_repository(state, repository, source)
            with patch('routes.ai_review_routes.repository_values', return_value=(1, index,
                    {'enabled': True, 'repo_name': actual['full_name']})), patch('access.scopes.bind_review_repository', side_effect=bind):
                response = self.fixture.request('/api/ai-review/repositories', 'post', {},
                    client=self.fixture.admin, binding=None)
            self.assertEqual(response.status_code, 409 if fail else 201, response.json)
            row = next(repo for repo in self.store.repos() if repo['repo_id'] == index)
            self.assertEqual(row['enabled'], not fail)
            self.assertEqual(self.check({'repository_id': row['id'], 'action': 'publish'}).status_code, 403 if fail else 200)

    def test_task_original_scope_cannot_be_replaced_with_another_repository_scope(self):
        with self.store.tx() as connection:
            connection.execute('UPDATE jobs SET access_scope=? WHERE id=?',
                               (json.dumps(self.store.repo(2)['access_scope']), 'a'*32))
        self.assertEqual(self.check({'repository_id': 1, 'job_id': 'a'*32, 'action': 'read'}).status_code, 403)

    def test_real_version_12_upgrade_adds_authorization_columns_and_is_idempotent(self):
        self.store.repo_status(1, bot_token='必须保留的原始密文')
        original_config = self.store.config()
        with self.store.tx() as connection:
            connection.execute('ALTER TABLE jobs DROP COLUMN access_scope')
            connection.execute('ALTER TABLE jobs DROP COLUMN access_actor')
            connection.execute('PRAGMA user_version=12')
        upgraded = Store(self.store.path)
        with contextlib.closing(upgraded.connect()) as connection:
            # 权限列升级后仍会执行后续迁移；当前完整结构版本为 17，不能停在旧版本 13。
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 18)
            columns = {row[1] for row in connection.execute('PRAGMA table_info(jobs)')}
            self.assertTrue({'access_scope', 'access_actor'} <= columns)
            self.assertEqual(connection.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)
            self.assertEqual(connection.execute('SELECT access_scope,access_actor FROM jobs WHERE id=?', ('a'*32,)).fetchone()[:], ('{}', '{}'))
        self.assertEqual(upgraded.config(), original_config)
        self.assertEqual(upgraded.repo(1)['bot_token'], '必须保留的原始密文')
        with upgraded.tx() as connection:
            connection.execute('UPDATE jobs SET access_actor=? WHERE id=?', ('{"account_id":99}', 'a'*32))
        repeated = Store(self.store.path)
        self.assertEqual(repeated.job('a'*32)['access_actor'], '{"account_id":99}')
        self.assertEqual(repeated.repo(1)['bot_token'], '必须保留的原始密文')

    def add_event(self, repository, due):
        with self.store.tx() as connection:
            connection.execute('INSERT INTO review_feedback_events(repository,delivery,value,due) VALUES(?,?,?,?)',
                               (repository, '隔离反馈', '{}', due))

    def test_denied_feedback_is_deferred_so_other_organizations_continue(self):
        now = time.time()
        self.add_event(1, now-20); self.add_event(2, now-10)
        def authorize(repo, *args, **kwargs):
            if repo['id'] == 1:
                raise ReviewError('原组织绑定已失效')
        processed = []
        with patch('ai_review.authorization.authorize', side_effect=authorize), \
                patch('ai_review.feedback.process_event', side_effect=lambda store, repo, *args: processed.append(repo['id'])), \
                patch.object(self.store, 'unseal', return_value='测试令牌'):
            self.assertTrue(feedback.run_one(self.store, factory=lambda repo: MagicMock()))
            self.assertTrue(feedback.run_one(self.store, factory=lambda repo: MagicMock()))
        self.assertEqual(processed, [2])
        with contextlib.closing(self.store.connect()) as connection:
            denied = connection.execute('SELECT * FROM review_feedback_events WHERE repository=1').fetchone()
            self.assertEqual(denied['attempts'], 1)
            self.assertGreater(denied['due'], now)
            self.assertEqual(denied['status'], 'pending')

    def test_denied_sample_audit_advances_priority_without_renewing_trust(self):
        samples = [dataset.put(self.store, repo, 1, 10+repo,
            {'status': 'active', 'confirmed_by': 1, 'confirmed_login': 'writer', 'source_comment': 2,
             'confirmation_hash': '固定摘要', 'target': '固定意见'}, 1) for repo in (1, 2)]
        with self.store.tx() as connection:
            connection.execute('UPDATE review_example_validation SET expires=0,checked=0 WHERE id=?', (samples[0]['id'],))
            connection.execute('UPDATE review_example_validation SET expires=0,checked=1 WHERE id=?', (samples[1]['id'],))
        seen = []
        def authorize(repo, *args, **kwargs):
            seen.append(repo['id'])
            raise ReviewError('授权服务明确拒绝')
        with patch('ai_review.authorization.authorize', side_effect=authorize):
            for _ in range(2):
                try:
                    feedback.audit_one(self.store, factory=lambda repo: self.fail('拒绝后不应读取仓库'))
                except ReviewError:
                    pass
        self.assertEqual(seen, [1, 2], '失效样本不能持续占据所有组织的复核入口')
        with contextlib.closing(self.store.connect()) as connection:
            rows = connection.execute('SELECT expires,checked FROM review_example_validation').fetchall()
            self.assertTrue(all(row['expires'] == 0 and row['checked'] > 1 for row in rows))

    def test_feedback_rechecks_scope_immediately_before_remote_receipt_write(self):
        self.add_event(1, time.time()-20)
        revoked = False
        def authorize(repo, *args, **kwargs):
            if revoked:
                raise ReviewError('读取期间仓库组织归属失效')
        def process(store, repo, event, admin, bot):
            nonlocal revoked
            revoked = True
            bot.request('POST', bot.prefix + '/issues/1/comments', {'body': '禁止发出的反馈回执'})
        with patch('ai_review.authorization.authorize', side_effect=authorize), \
                patch('ai_review.feedback.process_event', side_effect=process), \
                patch.object(self.store, 'unseal', return_value='测试令牌'), \
                patch('requests.Session.request') as external:
            feedback.run_one(self.store, factory=lambda repo: MagicMock())
        external.assert_not_called()
        with contextlib.closing(self.store.connect()) as connection:
            row = connection.execute('SELECT status,error FROM review_feedback_events').fetchone()
            self.assertEqual(row['status'], 'pending')
            self.assertIn('组织归属失效', row['error'])


if __name__ == '__main__':
    unittest.main()
