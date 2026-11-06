"""讨论状态必须独立核验；规则差异必须由原管理员绑定快照确认。"""
import copy
import json
import unittest

from models import db
from svn_migration.common import MigrationError
from git_migration.normalize import objects
from git_migration.source_manifest import Census, verify_plan
from git_migration.discussion_history import compatibility
from git_migration import preflights
from git_migration.models import GitMigrationInventory
from tests import test_git_migration_preflights


def sample():
    note = {'id': 8, 'body': '待处理意见', 'author': {'id': 1, 'username': 'source'},
            'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:00:00Z',
            'resolvable': True, 'resolved': False}
    issue = dict(note, id=4, iid=1, title='来源议题', state='opened')
    thread = {'id': 'abc', 'individual_note': False, 'notes': [note]}
    datasets = [{'category': '议题', 'path': '/projects/1/issues', 'rows': [issue]},
                {'category': '议题讨论', 'path': '/projects/1/issues/1/discussions', 'rows': [thread]}]
    return {'project': {'id': 1}, 'datasets': datasets}, thread


class DiscussionSnapshotTests(unittest.TestCase):
    def test_source_discussion_policy_cannot_be_lost_as_false(self):
        manifest={'kind':'gitlab','objects':[],'files':[],'rules':[],'gaps':[],
                  'fields':{'merge_policy:project':{'history.source_require_discussions':True}}}
        value={'objects':[{'kind':'merge_policy','source_id':'project','history':{'source_require_discussions':False}}]}
        with self.assertRaises(MigrationError):verify_plan(manifest,value,[],[])
        value['objects'][0]['history']['source_require_discussions']=True
        verify_plan(manifest,value,[],[])

    def test_missing_false_true_and_thread_order_are_distinct(self):
        item, thread = sample()
        thread['notes'].extend([dict(thread['notes'][0], id=9, resolved=True, resolved_by={'id': 2, 'username': 'reviewer'}, resolved_at=None),
                                {k:v for k,v in dict(thread['notes'][0], id=10).items() if k not in ('resolved','resolvable')},
                                dict(thread['notes'][0], id=11, resolvable=False)])
        result = objects(item, 'gitlab')
        snapshot = result['objects'][-1]
        self.assertEqual(snapshot['kind'], 'discussion_snapshot')
        notes = snapshot['history']['notes']
        self.assertEqual([n['source_id'] for n in notes], ['8','9','10','11'])
        self.assertIs(notes[0]['resolved'], False)
        self.assertIs(notes[1]['resolved'], True)
        self.assertIn('resolved_at', notes[1])
        self.assertNotIn('resolved', notes[2])
        self.assertIs(notes[3]['resolvable'], False)
        self.assertEqual(result['objects'][1]['content'], '待处理意见')

    def test_independent_census_detects_field_and_thread_loss(self):
        item, thread = sample()
        class Source:
            kind='gitlab'
            def get(self, path): return {}
        census=Census(Source(), {'id':1})
        census.rows=lambda path, **kwargs: ([item['datasets'][0]['rows'][0]] if path.endswith('/issues') else
            [thread] if path.endswith('/issues/1/discussions') else [])
        census.gitlab()
        manifest={'kind':'gitlab','objects':list(census.objects),'fields':census.fields,'files':[],'rules':[],'gaps':[]}
        result=objects(item,'gitlab')
        verify_plan(manifest,result,[],[])
        for field in ('resolved','resolvable'):
            bad=copy.deepcopy(result);bad['objects'][-1]['history']['notes'][0].pop(field)
            with self.assertRaises(MigrationError):verify_plan(manifest,bad,[],[])
        bad=copy.deepcopy(result);bad['objects'].pop()
        with self.assertRaises(MigrationError):verify_plan(manifest,bad,[],[])


class CompatibilityTests(unittest.TestCase):
    setUp=test_git_migration_preflights.PreflightTests.setUp

    def create(self):
        inventory=db.session.get(GitMigrationInventory,self.data['inventory_id'])
        frozen=json.loads(inventory.result_json);frozen['kind']='gitlab'
        frozen['projects'][0]['datasets']=[{'category':'项目合并前置条件','rows':[{'only_allow_merge_if_all_discussions_are_resolved':True}]}]
        inventory.result_json=json.dumps(frozen);inventory.capture_code=True;db.session.commit()
        return preflights.enqueue({k:v for k,v in self.data.items() if k!='source_frozen'},1),frozen

    def test_confirmation_binds_digest_actor_and_is_idempotent(self):
        row,frozen=self.create();row.status='blocked';db.session.commit()
        plan=json.loads(row.plan_json);value=compatibility(plan,frozen)
        self.assertFalse(value['acknowledged'])
        with self.assertRaises(MigrationError):preflights.acknowledge_compatibility(row.id,{'digest':'wrong'},1)
        with self.assertRaises(MigrationError):preflights.acknowledge_compatibility(row.id,{'digest':value['digest']},2)
        preflights.acknowledge_compatibility(row.id,{'digest':value['digest']},1)
        self.assertEqual(row.status,'queued')
        self.assertTrue(preflights.public(row)['compatibility']['acknowledged'])
        saved=row.plan_json
        preflights.acknowledge_compatibility(row.id,{'digest':value['digest']},1)
        self.assertEqual(row.plan_json,saved)
        plan=json.loads(saved);plan['target']['url']='https://changed.test'
        self.assertFalse(compatibility(plan,frozen)['acknowledged'])

    def test_forged_ready_state_still_cannot_start_without_ack(self):
        row,_=self.create();row.status='ready';row.checkpoint_json='{"batch_prepared":true}'
        row.result_json='{"rehearsal":"passed","checks":[{"status":"passed"}]}'
        db.session.commit()
        with self.assertRaisesRegex(MigrationError,'差异'):preflights.request_start(row.id,{'source_frozen':True,'request_key':'new-request-00001'},1)

    def test_old_inventory_and_old_preflight_are_not_reusable(self):
        row,_=self.create();plan=json.loads(row.plan_json);plan['preflight_protocol']=5
        row.plan_json=json.dumps(plan);db.session.commit()
        self.assertTrue(preflights.stale(row))
