"""固定预演已完成的批次，验证正式阶段只开放且不会重做迁移。"""
from contextlib import ExitStack
from datetime import datetime, timedelta
import copy
import json
from unittest.mock import Mock, patch
import unittest

from models import db
from git_migration import preflights, runs
from git_migration.models import GitMigrationInventory, GitMigrationRun
from svn_migration.common import MigrationError, Uncertain, digest
from tests import test_git_migration_runs


class PublicationTests(unittest.TestCase):
    setUp = test_git_migration_runs.RunTests.setUp

    def prepared(self):
        inventory = db.session.get(GitMigrationInventory, self.data['inventory_id'])
        inventory.capture_code = True
        frozen = json.loads(inventory.result_json)
        frozen['projects'][0]['source_manifest'] = {'schema': 3, 'objects': [], 'files': [], 'rules': [], 'gaps': []}
        inventory.result_json = json.dumps(frozen)
        db.session.commit()
        row = preflights.enqueue({k: v for k, v in self.data.items() if k != 'source_frozen'}, 1)
        plan = json.loads(row.plan_json)
        progress = {'key': 'a' * 32, 'repo_id': 42, 'prepared': True, 'sealed': True,
                    'code_verified': True, 'objects_verified': True, 'source_verified': {'objects': 0},
                    'final_path': 'target/org/repo', 'pending': [], 'gaps': [],
                    'snapshot': {'refs': {}, 'default_ref': '', 'lfs_objects': []}}
        row.checkpoint_json = json.dumps({'key': plan['key'], 'batch_size': 1, 'batch_prepared': True, '1': progress})
        row.result_json = json.dumps({'rehearsal': 'passed', 'checks': [{'status': 'passed'}]})
        row.status = 'ready'
        row.expires_at = datetime.utcnow() + timedelta(seconds=30)
        db.session.commit()
        return row, plan, frozen

    def publication(self, *, published=False):
        trial, plan, frozen = self.prepared()
        request = {'source_frozen': True, 'request_key': 'publication-request-001'}
        preflights.request_start(trial.id, request, 1)
        trial.status = 'running'
        db.session.commit()
        row = runs.enqueue({**plan['request'], **request, 'preflight_id': trial.id}, 1, preflight=trial)
        row.status = 'running'
        db.session.commit()
        receiver = self.receiver.return_value
        state = {'key': plan['key'], 'actor_id': 1, 'expected': 1, 'state': 'published' if published else 'prepared'}
        repo = {'id': 42, 'full_name': 'internal/repo', 'full_path': 'target/org/repo',
                'private': True, 'archived': not published, 'has_actions': False}
        def api(method, path, body=None, **kwargs):
            if method == 'GET' and path == '/governance/migration-sessions/' + 'a' * 32 + '/status':
                return {'key': 'a' * 32, 'batch_key': plan['key'], 'repo_id': 42,
                        'actor_id': 1, 'state': 'handed_off', 'publish_barrier': True}
            if method == 'GET' and path == '/governance/migration-batches/' + plan['key']:
                return copy.deepcopy(state)
            if method == 'GET' and path == '/repositories/42':
                return copy.deepcopy(repo)
            if method == 'POST' and path.endswith('/publish'):
                state['state'] = 'published'
                repo['archived'] = False
                # 与真实接收端接口一致，发布响应不包含批次身份。
                return {'state': 'published'}
            raise AssertionError('正式阶段出现了未规划的写入或请求：' + method + ' ' + path)
        receiver.api.side_effect = api
        provider = Mock()
        provider.authenticate.return_value = {'actor': {'id': 1}, 'version': '1.26.4'}
        frozen.update(actor={'id': 1}, version='1.26.4')
        inventory = db.session.get(GitMigrationInventory, row.inventory_id)
        inventory.result_json = json.dumps(frozen)
        plan = json.loads(row.plan_json)
        plan['inventory_digest'] = digest(frozen)
        row.plan_json = json.dumps(plan)
        db.session.commit()
        stack = ExitStack()
        self.addCleanup(stack.close)
        inspect = stack.enter_context(patch('git_migration.runs.Inventory'))
        inspect.return_value.inspect.return_value = {key: value for key, value in frozen['projects'][0].items()
                                                    if key not in ('code_snapshot', 'dependency', 'source_manifest')}
        stack.enter_context(patch('git_migration.source_manifest.capture', return_value=frozen['projects'][0]['source_manifest']))
        stack.enter_context(patch('git_migration.capacity.verify', return_value={'说明': '容量充足'}))
        stack.enter_context(patch('git_migration.runs.GitSnapshot'))
        return row, provider, receiver, state

    def test_formal_publication_never_reenters_handoff_or_transfers(self):
        row, provider, receiver, _ = self.publication()
        with patch('git_migration.handoff.finish', side_effect=AssertionError('正式阶段不得重做交接')):
            runs.execute(row, provider, lambda: None)
        self.assertEqual(row.status, 'succeeded')
        writes = [call for call in receiver.api.call_args_list if call.args[0] != 'GET']
        self.assertEqual(len(writes), 1)
        self.assertTrue(writes[0].args[1].endswith('/publish'))
        receiver.stage.assert_not_called()
        receiver.transfer_code.assert_not_called()
        receiver.import_objects.assert_not_called()

    def test_published_receipt_recovers_without_second_write(self):
        row, provider, receiver, _ = self.publication(published=True)
        runs.execute(row, provider, lambda: None)
        self.assertEqual(row.status, 'succeeded')
        self.assertFalse(any(call.args[0] != 'GET' for call in receiver.api.call_args_list))

    def gitlab_publication(self, protocol=5, published=True, acknowledged=False, required=True):
        row, provider, receiver, state = self.publication(published=published)
        inventory = db.session.get(GitMigrationInventory, row.inventory_id)
        frozen = json.loads(inventory.result_json)
        frozen['kind'] = 'gitlab'
        frozen['projects'][0]['datasets'] = [{'category': '项目合并前置条件',
            'rows': [{'only_allow_merge_if_all_discussions_are_resolved': required}]}]
        inventory.result_json = json.dumps(frozen)
        plan = json.loads(row.plan_json)
        plan['preflight_protocol'] = protocol
        plan['inventory_digest'] = digest(frozen)
        plan.pop('compatibility_acknowledgement', None)
        if acknowledged:
            from git_migration.discussion_history import compatibility
            plan['compatibility_acknowledgement'] = {'digest': compatibility(plan, frozen)['digest'],
                'actor_id': row.actor_id, 'confirmed_at': '2026-10-29T00:00:00Z'}
        row.plan_json = json.dumps(plan)
        db.session.commit()
        return row, provider, receiver, state

    def test_old_published_gitlab_recovers_and_releases_instance(self):
        from svn_migration.guards import target_guard
        for protocol in (5,):
            with self.subTest(protocol=protocol):
                row, provider, receiver, state = self.gitlab_publication(protocol=protocol)
                row.status = 'uncertain'
                db.session.commit()
                with self.assertRaises(MigrationError):
                    with target_guard(row.server_id):
                        pass
                runs.resume(row.id)
                runs.execute(row, provider, lambda: None)
                self.assertEqual(row.status, 'succeeded')
                self.assertFalse(runs.public(row)['discussion_history_verified'])
                with target_guard(row.server_id):
                    pass
                receiver.api.reset_mock()
                runs.execute(row, provider, lambda: None)
                self.assertFalse(any(c.args[0] != 'GET' for c in receiver.api.call_args_list))
                # 同一夹具使用唯一请求键；下一子例移除已完成记录后重新建立。
                db.session.delete(row)
                from git_migration.models import GitMigrationPreflight
                for trial in GitMigrationPreflight.query.all():
                    db.session.delete(trial)
                db.session.commit()

    def test_current_published_gitlab_still_requires_confirmation(self):
        row, provider, receiver, _ = self.gitlab_publication(protocol=7)
        with self.assertRaisesRegex(MigrationError, '差异'):
            runs.execute(row, provider, lambda: None)
        self.assertFalse(any(c.args[0] != 'GET' for c in receiver.api.call_args_list))

    def test_current_confirmed_gitlab_recovers_without_writes(self):
        row, provider, receiver, _ = self.gitlab_publication(protocol=7, acknowledged=True)
        runs.execute(row, provider, lambda: None)
        self.assertEqual(row.status, 'succeeded')
        self.assertFalse(any(c.args[0] != 'GET' for c in receiver.api.call_args_list))

    def test_old_unpublished_gitlab_cannot_publish(self):
        row, provider, receiver, _ = self.gitlab_publication(published=False)
        with self.assertRaises(MigrationError):
            runs.execute(row, provider, lambda: None)
        self.assertFalse(any(c.args[0] != 'GET' for c in receiver.api.call_args_list))

    def test_legacy_published_without_discussion_requirement_recovers(self):
        row, provider, receiver, _ = self.gitlab_publication(protocol=4, required=False)
        runs.execute(row, provider, lambda: None)
        self.assertEqual(row.status, 'succeeded')
        self.assertFalse(any(c.args[0] != 'GET' for c in receiver.api.call_args_list))

    def test_current_published_stale_confirmation_is_rejected(self):
        row, provider, receiver, _ = self.gitlab_publication(protocol=7, acknowledged=True)
        plan = json.loads(row.plan_json)
        plan['compatibility_acknowledgement']['digest'] = 'stale'
        row.plan_json = json.dumps(plan)
        db.session.commit()
        with self.assertRaisesRegex(MigrationError, '差异'):
            runs.execute(row, provider, lambda: None)
        self.assertFalse(any(c.args[0] != 'GET' for c in receiver.api.call_args_list))

    def test_published_session_identity_cannot_be_substituted(self):
        row, provider, receiver, state = self.gitlab_publication()
        original = receiver.api.side_effect
        for field, changed in {'key':'wrong', 'batch_key':'wrong', 'repo_id':43, 'actor_id':2,
                               'publish_barrier':False, 'state':'loading'}.items():
            with self.subTest(field=field):
                def altered(method, path, body=None, **kwargs):
                    value = original(method, path, body, **kwargs)
                    if path.endswith('/status'):
                        value[field] = changed
                    return value
                receiver.api.side_effect = altered
                with self.assertRaisesRegex(MigrationError, '回执身份'):
                    runs.execute(row, provider, lambda: None)
                self.assertNotEqual(row.status, 'succeeded')
        self.assertFalse(any(c.args[0] != 'GET' for c in receiver.api.call_args_list))

    def test_unknown_persisted_batch_state_cannot_release_guard(self):
        row, provider, receiver, state = self.gitlab_publication()
        state['state'] = 'unknown'
        with self.assertRaises(MigrationError):
            runs.execute(row, provider, lambda: None)
        self.assertNotEqual(row.status, 'succeeded')
        self.assertFalse(any(c.args[0] != 'GET' for c in receiver.api.call_args_list))

    def test_published_receipt_must_match_batch_and_repo_identity(self):
        row, provider, receiver, state = self.publication(published=True)
        state['expected'] = 2
        with self.assertRaisesRegex(MigrationError, '回执身份'):
            runs.execute(row, provider, lambda: None)
        self.assertNotEqual(row.status, 'succeeded')
        self.assertFalse(any(call.args[0] != 'GET' for call in receiver.api.call_args_list))

    def test_changed_source_blocks_publication_without_any_target_write(self):
        row, provider, receiver, _ = self.publication()
        with patch('git_migration.runs.Inventory') as inventory:
            inventory.return_value.inspect.return_value = {'内容': '已经改变'}
            with self.assertRaisesRegex(MigrationError, '来源资料'):
                runs.execute(row, provider, lambda: None)
        self.assertFalse(any(call.args[0] != 'GET' for call in receiver.api.call_args_list))

    def test_rule_permissions_are_still_checked_without_reinstalling_rules(self):
        row, provider, receiver, _ = self.publication()
        with patch('git_migration.rules.verify_rule_members', side_effect=MigrationError('规则人员权限已变化')):
            with self.assertRaisesRegex(MigrationError, '权限已变化'):
                runs.execute(row, provider, lambda: None)
        self.assertFalse(any(call.args[0] != 'GET' for call in receiver.api.call_args_list))

    def test_lost_publication_reply_recovers_original_batch_without_republish(self):
        row, provider, receiver, state = self.publication()
        api = receiver.api.side_effect
        def lost_reply(method, path, body=None, **kwargs):
            result = api(method, path, body, **kwargs)
            if method == 'POST':
                raise Uncertain('整批开放回执丢失')
            return result
        receiver.api.side_effect = lost_reply
        with self.assertRaises(Uncertain):
            runs.execute(row, provider, lambda: None)
        self.assertEqual(state['state'], 'published')
        self.assertNotEqual(row.status, 'succeeded')
        writes = len([c for c in receiver.api.call_args_list if c.args[0] != 'GET'])
        receiver.api.side_effect = api
        runs.execute(row, provider, lambda: None)
        self.assertEqual(row.status, 'succeeded')
        self.assertEqual(len([c for c in receiver.api.call_args_list if c.args[0] != 'GET']), writes)

    def test_persisted_batch_identity_is_checked_after_minimal_publish_reply(self):
        row, provider, receiver, state = self.publication()
        api = receiver.api.side_effect
        def mismatched_receipt(method, path, body=None, **kwargs):
            result = api(method, path, body, **kwargs)
            if method == 'POST':
                state['expected'] = 2
            return result
        receiver.api.side_effect = mismatched_receipt
        with self.assertRaisesRegex(MigrationError, '回执身份'):
            runs.execute(row, provider, lambda: None)
        self.assertNotEqual(row.status, 'succeeded')

    def test_incomplete_prepared_checkpoint_cannot_publish(self):
        row, provider, receiver, _ = self.publication()
        checkpoint = json.loads(row.checkpoint_json)
        checkpoint['1']['objects_verified'] = False
        row.checkpoint_json = json.dumps(checkpoint)
        db.session.commit()
        with self.assertRaises(MigrationError):
            runs.execute(row, provider, lambda: None)
        self.assertFalse(any(call.args[0] != 'GET' for call in receiver.api.call_args_list))

    def test_previous_field_contract_requires_new_rehearsal(self):
        row, provider, receiver, _ = self.publication()
        plan = json.loads(row.plan_json)
        plan['preflight_protocol'] = 4
        row.plan_json = json.dumps(plan)
        db.session.commit()
        with self.assertRaisesRegex(MigrationError, '旧预演'):
            runs.execute(row, provider, lambda: None)
        self.assertFalse(any(call.args[0] != 'GET' for call in receiver.api.call_args_list))

    def test_activation_accepted_in_time_can_finish_after_deadline(self):
        row, plan, _ = self.prepared()
        preflights.request_start(row.id, {'source_frozen': True, 'request_key': 'late-recheck-request-001'}, 1)
        row.status = 'running'
        db.session.commit()
        def inspect(*args):
            row.expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.session.commit()
            return True
        receiver = Mock()
        receiver.api.side_effect = lambda method, path: {'state': 'handed_off', 'repo_id': 42} if path.endswith('/status') else {
            'id': 42, 'private': True, 'full_path': 'target/org/repo'}
        with patch('git_migration.preflights.inspect', side_effect=inspect), patch('git_migration.preflights.Receiver', return_value=receiver):
            preflights.execute(row, Mock(), lambda: None)
        self.assertEqual(row.status, 'consumed')
        self.assertEqual(GitMigrationRun.query.count(), 1)

    def test_expired_result_cannot_be_submitted_as_new_activation(self):
        row, _, _ = self.prepared()
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        with self.assertRaisesRegex(MigrationError, '有效期'):
            preflights.request_start(row.id, {'source_frozen': True, 'request_key': 'expired-request-001'}, 1)
        self.assertEqual(row.phase, 'trial')
        self.assertEqual(GitMigrationRun.query.count(), 0)

    def test_queued_activation_rechecks_identity_after_the_admission_deadline(self):
        row, _, _ = self.prepared()
        preflights.request_start(row.id, {'source_frozen': True, 'request_key': 'queued-request-001'}, 1)
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        checks = []
        def input_only(row, key, title, operation, *args):
            if key == 'inputs':
                checks.append(operation())
            return True
        with patch('git_migration.preflights.check', side_effect=input_only):
            self.assertTrue(preflights.inspect(row, Mock(), Mock(), lambda: None))
        self.assertEqual(checks, ['快照一致'])
        self.source.version += 1
        db.session.commit()
        with patch('git_migration.preflights.check', side_effect=input_only):
            with self.assertRaisesRegex(MigrationError, '来源'):
                preflights.inspect(row, Mock(), Mock(), lambda: None)

