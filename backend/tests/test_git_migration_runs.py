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
