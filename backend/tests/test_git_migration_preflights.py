"""预迁移状态机、变化失效、实例互斥和正式入口门禁。"""
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

from models import db
from svn_migration.common import MigrationError
from svn_migration.guards import target_guard
from git_migration.models import GitMigrationRun, GitMigrationPreflight
from git_migration import preflights, runs
from tests import test_git_migration_runs


class PreflightTests(unittest.TestCase):
    setUp = test_git_migration_runs.RunTests.setUp

    def create(self):
        inventory = db.session.get(__import__('git_migration.models', fromlist=['GitMigrationInventory']).GitMigrationInventory, self.data['inventory_id'])
        inventory.capture_code = True
        db.session.commit()
        return preflights.enqueue({k:v for k,v in self.data.items() if k != 'source_frozen'}, 1)

    def ready(self):
        row = self.create()
        row.status='ready';row.checkpoint_json=json.dumps({'batch_prepared':True});row.expires_at=datetime.utcnow()+timedelta(minutes=30)
        row.result_json=json.dumps({'rehearsal':'passed','checks':[{'key':'fixture','status':'passed'}]})
        db.session.commit()
        return row

    def test_direct_run_even_with_forged_preflight_id_is_denied(self):
        with self.assertRaisesRegex(MigrationError, '预迁移'):
            runs.enqueue(dict(self.data, preflight_id=1),1)
        self.assertEqual(GitMigrationRun.query.count(),0)

    def test_preflight_creates_no_formal_run_and_replays(self):
        row=self.create()
        self.assertEqual(GitMigrationRun.query.count(),0)
        self.assertEqual(self.create().id,row.id)
        self.assertFalse(preflights.public(row)['can_start'])
        with self.assertRaises(MigrationError):
            preflights.request_start(row.id,{'source_frozen':True,'request_key':'start-request-0001'},1)

    def test_both_gates_and_frozen_confirmation_required(self):
        row=self.ready()
        request={'source_frozen':True,'request_key':'start-request-0001'}
        row.result_json=json.dumps({'rehearsal':'passed','checks':[{'status':'blocked'}]});db.session.commit()
        with self.assertRaises(MigrationError):preflights.request_start(row.id,request,1)
        row.result_json=json.dumps({'rehearsal':'blocked','checks':[{'status':'passed'}]});db.session.commit()
        with self.assertRaises(MigrationError):preflights.request_start(row.id,request,1)
        row.result_json=json.dumps({'rehearsal':'passed','checks':[{'status':'passed'}]});db.session.commit()
        with self.assertRaises(MigrationError):preflights.request_start(row.id,dict(request,source_frozen=False),1)
        preflights.request_start(row.id,request,1)
        self.assertEqual(row.phase,'activate')
        self.assertEqual(GitMigrationRun.query.count(),0,'复检前依然不得创建正式任务')
        self.assertEqual(preflights.request_start(row.id,request,1).id,row.id)
        with self.assertRaises(MigrationError):preflights.request_start(row.id,dict(request,request_key='other-request-0001'),1)

    def test_expiration_and_changed_source_invalidate(self):
        row=self.ready();row.expires_at=datetime.utcnow()-timedelta(seconds=1);db.session.commit()
        self.assertFalse(preflights.public(row)['can_start'])
        self.assertIn('三十分钟',preflights.stale(row))
        row.expires_at=datetime.utcnow()+timedelta(minutes=30);self.source.version+=1;db.session.commit()
        self.assertFalse(preflights.public(row)['can_start'])

    def test_changed_inventory_and_target_invalidate(self):
        row=self.ready();previous=self.server.access_epoch;self.server.access_epoch='已改变';db.session.commit()
        self.assertIn('目标',preflights.stale(row))
        self.server.access_epoch=previous
        from git_migration.models import GitMigrationInventory
        inventory=db.session.get(GitMigrationInventory,row.inventory_id)
        inventory.result_json='{}';db.session.commit()
        self.assertIn('盘点',preflights.stale(row))

    def test_ready_and_blocked_preflight_keep_instance_exclusive(self):
        row=self.ready()
        for status in ('ready','blocked','interrupted'):
            row.status=status;db.session.commit()
            with self.assertRaises(MigrationError):
                with target_guard(self.server.id):pass
        row.status='cancelled';db.session.commit()
        with target_guard(self.server.id):pass

    def test_cancel_queues_cleanup_without_releasing_lock(self):
        row=self.ready();preflights.request_cancel(row.id)
        self.assertEqual((row.phase,row.status),('cancel','queued'))
        with self.assertRaises(MigrationError):
            with target_guard(self.server.id):pass

    def test_check_collects_failure_and_keeps_other_evidence(self):
        row=self.create()
        def failed():raise MigrationError('附件不完整','missing')
        self.assertFalse(preflights.check(row,'files','文件完整性',failed,'补齐文件'))
        self.assertTrue(preflights.check(row,'refs','代码引用',lambda:'一致','重新盘点'))
        self.assertEqual([c['status'] for c in json.loads(row.result_json)['checks']],['blocked','passed'])
        self.assertEqual(GitMigrationRun.query.count(),0)

    def test_path_conflicts_fail_closed_and_validate_hierarchy(self):
        receiver=self.receiver.return_value
        receiver.api.return_value={'id':7,'full_path':'target'}
        receiver.child_group_paths.return_value={'target/org'}
        with self.assertRaisesRegex(MigrationError,'同名子群组'):
            preflights.path_check(receiver,{'id':7,'path':'target'},[{'project':{'path':'org/repo'}}])
        with self.assertRaisesRegex(MigrationError,'层级路径'):
            preflights.path_check(receiver,{'id':7,'path':'target'},[{'project':{'path':'../repo'}}])

    def test_every_source_recheck_uses_a_new_snapshot_directory(self):
        row=self.create()
        snapshot={'refs':{},'lfs_objects':[]}
        with patch('git_migration.preflights.GitSnapshot') as capture:
            preflights.verify_source_code(row,None,{'id':1},snapshot,lambda:None)
            preflights.verify_source_code(row,None,{'id':1},snapshot,lambda:None)
            self.assertNotEqual(capture.call_args_list[0].args[2],capture.call_args_list[1].args[2])

    def test_activation_preserves_completed_trial_evidence(self):
        row=self.ready();before=json.loads(row.result_json)['checks'];row.phase='activate';db.session.commit()
        with patch('git_migration.preflights.check',return_value=True):
            preflights.inspect(row,None,None,lambda:None)
        result=json.loads(row.result_json)
        self.assertEqual(result['trial_checks'],before)
        self.assertEqual(result['rehearsal'],'passed')
        preflights.record_failure(row,'来源发生变化','source_changed')
        result=json.loads(row.result_json)
        self.assertEqual(result['rehearsal'],'passed')
        self.assertEqual(result['recheck'],'blocked')

    def test_preflight_credential_recovery_preserves_identity_and_lock(self):
        row=self.ready()
        with patch('git_migration.preflights.Receiver') as receiver:
            receiver.return_value.actor={'id':1,'login':'migration-admin'}
            preflights.rotate_target_credentials(row.id,'renewed-token')
            self.assertEqual(self.server.api_token,'renewed-token')
            receiver.return_value.actor={'id':2,'login':'other-admin'}
            with self.assertRaisesRegex(MigrationError,'原目标管理员'):
                preflights.rotate_target_credentials(row.id,'other-token')
            self.assertEqual(self.server.api_token,'renewed-token')
        with self.assertRaises(MigrationError):
            with target_guard(self.server.id):pass

    def test_expired_interrupted_preflight_requires_fresh_confirmation(self):
        row=self.ready();row.status='interrupted';row.phase='activate';row.expires_at=datetime.utcnow()-timedelta(seconds=1)
        plan=json.loads(row.plan_json);plan['activation']={'source_frozen':True,'request_key':'old-confirmation-0001'};row.plan_json=json.dumps(plan);db.session.commit()
        preflights.resume(row.id)
        self.assertEqual((row.status,row.phase),('queued','trial'))
        self.assertIsNone(row.expires_at)
        self.assertNotIn('activation',json.loads(row.plan_json))
