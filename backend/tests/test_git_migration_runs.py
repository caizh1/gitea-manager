"""持久执行计划、重试身份与实例互斥的回归。"""
import json
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from models import db, GiteaServer
from tests.access_helpers import init_test_auth
from svn_migration.common import MigrationError
from svn_migration.guards import target_guard
from git_migration.models import GitMigrationInventory, GitMigrationRun
from git_migration.store import save_source, enqueue_discovery
from git_migration.runs import enqueue, resume
from git_migration.worker import recover_interrupted


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        app = Flask(__name__)
        self.app = app
        app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + self.temp.name + '/test.db', TESTING=True)
        init_test_auth(app)
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.addCleanup(db.session.remove)
        db.create_all()
        self.source = save_source({'kind': 'gitea', 'name': '测试来源', 'url': 'https://source.test',
                                   'username': 'admin', 'secret': '来源口令'})
        discovery = enqueue_discovery(self.source.id, {'source_version': self.source.version, 'request_key': 'discover-test-0001'}, 1)
        discovery.status = 'succeeded'
        inventory = GitMigrationInventory(source_id=self.source.id, source_version=self.source.version,
            discovery_id=discovery.id, project_ids_json='[1]', request_key='inventory-test-0001', actor_id=1,
            status='succeeded', result_json=json.dumps({'projects': [{'project': {'id': 1, 'path': 'org/repo'}, 'datasets': []}]}))
        self.server = GiteaServer(name='隔离目标', gitea_url='https://target.test', api_token='目标令牌', host='127.0.0.1', ssh_user='root', gitea_port=3000)
        db.session.add_all([inventory, self.server]); db.session.commit()
        self.data = {'inventory_id': inventory.id, 'server_id': self.server.id, 'group_id': 7,
                     'request_key': 'migration-test-0001', 'source_frozen': True}
        self.receiver = patch('git_migration.runs.Receiver').start()
        self.addCleanup(patch.stopall)
        self.receiver.return_value.selected_scope.return_value = {'id': 7, 'path': 'target', 'access_fingerprint': '固定摘要'}
        self.receiver.return_value.actor = {'id': 1, 'login': 'migration-admin'}

    def test_same_request_replays_and_changed_input_fails(self):
        row = enqueue(self.data, 1)
        self.assertEqual(enqueue(self.data, 1).id, row.id)
        with self.assertRaises(MigrationError):
            enqueue(dict(self.data, group_id=8), 1)
        self.assertNotIn('目标令牌', row.plan_json)
        self.assertNotIn('来源口令', row.plan_json)

    def test_interrupted_checkpoint_blocks_destructive_operations_and_survives_resume(self):
        row = enqueue(self.data, 1)
        row.status, row.checkpoint_json = 'running', '{"1":{"repo_id":42}}'
        db.session.commit()
        recover_interrupted()
        db.session.refresh(row)
        self.assertEqual(row.status, 'interrupted')
        with self.assertRaises(MigrationError):
            with target_guard(self.server.id):
                self.fail('不应允许破坏性操作')
        with self.assertRaises(MigrationError):
            with target_guard(self.server.id, migration=True):
                self.fail('不应允许 SVN 迁移争用')
        resumed = resume(row.id)
        self.assertEqual(json.loads(resumed.checkpoint_json)['1']['repo_id'], 42)
        self.assertEqual(resumed.status, 'queued')
        self.assertEqual(GitMigrationRun.query.count(), 1)

    def test_requires_explicit_freeze_and_current_inventory(self):
        with self.assertRaises(MigrationError):
            enqueue(dict(self.data, source_frozen=False), 1)
        self.source.version += 1
        db.session.commit()
        with self.assertRaises(MigrationError):
            enqueue(self.data, 1)

    def test_same_actor_credential_rotation_preserves_plan_and_resumes(self):
        from git_migration.store import rotate_credentials, decrypt
        from git_migration.models import GitMigrationDiscovery
        identity = {'actor': {'id': 8, 'username': 'admin'}, 'version': '1.26.4'}
        discovery = GitMigrationDiscovery.query.first()
        discovery.result_json = json.dumps(identity)
        row = enqueue(self.data, 1)
        row.status = 'uncertain'
        db.session.commit()
        version, plan = self.source.version, row.plan_json
        with patch('git_migration.providers.Provider.authenticate', return_value=identity):
            updated = rotate_credentials(self.source.id, {'version': version, 'secret': '新的来源凭据'})
        self.assertEqual(updated.version, version)
        self.assertEqual(decrypt(updated.secret_ciphertext), '新的来源凭据')
        self.assertEqual(resume(row.id).plan_json, plan)
        with self.assertRaises(MigrationError):
            rotate_credentials(self.source.id, {'version': version, 'secret': '排队时不能替换'})

    def test_rotated_credentials_cannot_change_source_actor(self):
        from git_migration.store import rotate_credentials, decrypt
        from git_migration.models import GitMigrationDiscovery
        discovery = GitMigrationDiscovery.query.first()
        discovery.result_json = json.dumps({'actor': {'id': 8, 'username': 'admin'}, 'version': '1.26.4'})
        row = enqueue(self.data, 1)
        row.status = 'uncertain'
        db.session.commit()
        with patch('git_migration.providers.Provider.authenticate', return_value={'actor': {'id': 9, 'username': 'other'}, 'version': '1.26.4'}):
            with self.assertRaisesRegex(MigrationError, '管理员或版本'):
                rotate_credentials(self.source.id, {'version': self.source.version, 'secret': '其他人的凭据'})
        self.assertEqual(decrypt(self.source.secret_ciphertext), '来源口令')

    def test_abandon_without_target_writes_releases_guard_and_is_terminal(self):
        from git_migration.runs import abandon
        row = enqueue(self.data, 1)
        row.status = 'uncertain'
        db.session.commit()
        self.assertEqual(abandon(row.id, '来源已停用，保留现场').status, 'abandoned')
        with target_guard(self.server.id):
            pass
        with self.assertRaises(MigrationError):
            resume(row.id)

    def test_target_token_can_rotate_only_for_the_same_frozen_actor(self):
        from git_migration.runs import rotate_target_credentials
        row = enqueue(self.data, 1)
        row.status = 'uncertain'
        db.session.commit()
        original = self.server.access_epoch
        rotate_target_credentials(row.id, 'new-target-token')
        self.assertEqual(self.server.api_token, 'new-target-token')
        self.assertEqual(self.server.access_epoch, original)
        self.receiver.return_value.actor = {'id': 2, 'login': 'other-admin'}
        with self.assertRaisesRegex(MigrationError, '固定的目标管理员'):
            rotate_target_credentials(row.id, 'different-admin-token')
        self.assertEqual(self.server.api_token, 'new-target-token')

    def test_existing_url_alias_blocks_migration_before_any_target_write(self):
        duplicate = GiteaServer(name='重复连接', gitea_url='https://TARGET.test:443/', api_token='令牌',
            host='other.test', ssh_user='root', gitea_port=3000)
        db.session.add(duplicate); db.session.commit()
        with self.assertRaisesRegex(MigrationError, '重复连接'):
            enqueue(self.data, 1)
        self.receiver.assert_not_called()
        self.assertEqual(GitMigrationRun.query.count(), 0)

    def test_ssh_container_alias_blocks_a_different_proxy_url(self):
        duplicate = GiteaServer(name='代理连接', gitea_url='https://proxy.test', api_token='令牌',
            host=self.server.host, ssh_port=22, gitea_container='gitea', ssh_user='root', gitea_port=3000)
        db.session.add(duplicate); db.session.commit()
        with self.assertRaisesRegex(MigrationError, '重复连接'):
            enqueue(self.data, 1)

    def test_connection_routes_reject_late_alias_and_allow_unrelated_instance(self):
        from routes.git_migration_routes import git_migration_bp
        from flask import jsonify
        self.app.register_blueprint(git_migration_bp, url_prefix='/api/git-migrations')
        self.app.add_url_rule('/api/servers', endpoint='servers.add_server', view_func=lambda: jsonify(ok=True), methods=['POST'])
        self.app.add_url_rule('/api/servers/<int:sid>', endpoint='servers.update_server', view_func=lambda sid: jsonify(ok=True), methods=['PUT'])
        enqueue(self.data, 1)
        client = self.app.test_client()
        reply = client.post('/api/servers', json={'gitea_url': 'https://target.test:443/'})
        self.assertEqual(reply.status_code, 409)
        self.assertEqual(reply.json['code'], 'instance_alias')
        self.assertEqual(client.post('/api/servers', json={'gitea_url': 'https://unrelated.test', 'host': 'other.test'}).status_code, 200)
        other = GiteaServer(name='其他实例', gitea_url='https://other.test', host='other.test', ssh_user='root', api_token='令牌', gitea_port=3000)
        db.session.add(other); db.session.commit()
        self.assertEqual(client.put('/api/servers/' + str(other.id), json={'gitea_url': 'https://target.test'}).status_code, 409)
        self.assertEqual(client.put('/api/servers/' + str(other.id), json={'name': '更名'}).status_code, 200)


class DerivedStateTests(unittest.TestCase):
    def test_gitlab_background_merge_checks_do_not_mask_real_changes(self):
        from git_migration.runs import content_digest
        row={'source_project_id':1,'target_project_id':2,'merge_status':'unchecked','detailed_merge_status':'checking','sha':'a'*40,'title':'原始正文'}
        later={**row,'merge_status':'can_be_merged','detailed_merge_status':'mergeable'}
        self.assertEqual(content_digest(row),content_digest(later))
        self.assertNotEqual(content_digest(row),content_digest({**later,'sha':'b'*40}))
        self.assertNotEqual(content_digest(row),content_digest({**later,'title':'改变正文'}))

    def test_source_web_login_does_not_hide_account_identity_change(self):
        from git_migration.runs import content_digest
        user={'id':1,'login':'reader','last_login':'2020-01-01T00:00:00Z'}
        later={**user,'last_login':'2020-01-02T00:00:00Z'}
        self.assertEqual(content_digest(user),content_digest(later))
        self.assertNotEqual(content_digest(user),content_digest({**later,'id':2}))
