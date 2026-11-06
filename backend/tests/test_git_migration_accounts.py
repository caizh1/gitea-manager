"""人员匹配固定基准、持久确认、批量边界与执行入口门禁。"""
from contextlib import contextmanager
import json
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from models import db, GiteaServer
from tests.access_helpers import init_test_auth
from svn_migration.common import MigrationError, encode
from git_migration import account_mapping as service
from git_migration.models import GitMigrationInventory, GitMigrationAccountMapping
from git_migration.store import save_source
from git_migration.worker import step, recover_interrupted


def person(ident, login, email='', name='测试人员', **kwargs):
    return {'id': ident, 'login': login, 'email': email, 'full_name': name, 'active': True, **kwargs}


def snapshot(users):
    return {'kind': 'gitea', 'actor': {'id': 1, 'username': 'admin'}, 'version': '1.26.4',
            'projects': [{'project': {'id': 1, 'path': 'team/repo'}, 'datasets': [
                {'category': '议题', 'rows': [{'id': u['id'], 'assignees': [u]} for u in users]}]}]}


class MatchingTests(unittest.TestCase):
    def match(self, source, target):
        accounts = [service.profile(u) for u in target]
        rights = {u['id']: ['read_issues'] for u in target}
        rows = service.needed_rows(snapshot(source), [service.profile(u) for u in source])
        return service.suggest(rows, accounts, rights)

    def test_unique_email_maps_different_login(self):
        row = self.match([person(1, 'old', 'one@example.test')], [person(20, 'new', 'one@example.test')])[0]
        self.assertEqual((row['match_status'], row['selected_ids'], row['confirmed']), ('automatic', [20], False))

    def test_conflicting_login_and_email_require_manual_review(self):
        row = self.match([person(1, 'same', 'one@example.test')], [person(20, 'same', 'two@example.test'), person(21, 'other', 'one@example.test')])[0]
        self.assertEqual(row['match_status'], 'ambiguous')
        self.assertEqual(row['selected_ids'], [])

    def test_names_only_never_auto_match_even_one_candidate(self):
        row = self.match([person(1, 'old')], [person(20, 'new')])[0]
        self.assertEqual(row['match_status'], 'ambiguous')

    def test_inactive_and_unauthorized_accounts_are_not_automatic(self):
        row = self.match([person(1, 'same')], [person(20, 'same', active=False)])[0]
        self.assertEqual(row['match_status'], 'unmatched')
        self.assertFalse(service.eligible(service.profile(person(20, 'same')), {'required_rights': ['push_code']}, {20: ['read_issues']}))

    def test_multiple_people_cannot_silently_collapse_to_one_target(self):
        rows = self.match([person(1, 'a', 'same@test'), person(2, 'b', 'same@test')], [person(20, 'same', 'same@test')])
        self.assertTrue(all(r['match_status'] == 'ambiguous' and not r['selected_ids'] for r in rows))

    def test_punctuation_is_not_discarded(self):
        row = self.match([person(1, 'a.b', name='甲')], [person(20, 'ab', name='乙')])[0]
        self.assertEqual(row['match_status'], 'unmatched')

    def test_rule_user_and_issue_person_are_one_review_object(self):
        user = person(1, 'same')
        data = snapshot([user])
        data['projects'][0]['datasets'].append({'category': '分支保护', 'rows': [{'push_whitelist_usernames': ['same']}]})
        rows = service.needed_rows(data, [service.profile(user)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]['principal_keys']), {'gitea:person:1', 'gitea:user:same'})
        self.assertIn('push_code', rows[0]['required_rights'])

    def test_gitlab_ids_map_without_requiring_equal_logins(self):
        data = snapshot([person(1, 'source')]); data['kind'] = 'gitlab'
        rows = service.needed_rows(data, [service.profile(person(1, 'source', 'person@test'))])
        service.suggest(rows, [service.profile(person(40, 'target', 'person@test'))], {40: ['read_issues']})
        self.assertEqual(rows[0]['selected_ids'], [40])

    def test_large_fixed_census_has_exact_denominators(self):
        source = [person(i, f'person-{i}', name=f'人员{i}') for i in range(1, 751)]
        target = [person(1000+i, f'person-{i}', name=f'人员{i}') for i in range(1, 701)]
        for i in range(701, 726):
            target += [person(2000+i, f'first-{i}', name=f'人员{i}'), person(3000+i, f'second-{i}', name=f'人员{i}')]
        rows = self.match(source, target)
        self.assertEqual({status: sum(r['match_status'] == status for r in rows) for status in ('automatic','ambiguous','unmatched')}, {'automatic':700,'ambiguous':25,'unmatched':25})

    def test_target_pagination_cannot_accept_a_repeated_page(self):
        class BadReceiver:
            def api(self, *_): return [person(1, 'same')]
        with self.assertRaisesRegex(MigrationError, '分页重复'):
            service.target_accounts(BadReceiver())


class PersistentMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///' + self.temp.name + '/test.db')
        init_test_auth(self.app)
        ctx = self.app.app_context(); ctx.push(); self.addCleanup(ctx.pop); self.addCleanup(db.session.remove)
        self.source = save_source({'name':'测试来源', 'kind':'gitea', 'url':'https://source.test', 'username':'admin', 'secret':'测试来源口令'})
        self.sources = [person(i, f'user{i}', name=f'人员{i}') for i in range(1, 121)]
        self.targets = [person(i+1000, f'user{i}', name=f'人员{i}') for i in range(1, 119)]
        self.data = snapshot(self.sources)
        self.inventory = GitMigrationInventory(source_id=self.source.id, source_version=self.source.version, discovery_id=1,
            project_ids_json='[1]', request_key='inventory-account-test', status='succeeded', result_json=encode(self.data), actor_id=1)
        self.server = GiteaServer(name='目标', gitea_url='https://target.test', api_token='测试目标令牌', host='target.test', ssh_user='root', gitea_port=3000)
        db.session.add_all([self.inventory, self.server]); db.session.commit()
        outer = self
        class FakeReceiver:
            actor = {'id': 99, 'login':'admin'}
            def selected_scope(self, _): return {'id':7,'path':'target','access_fingerprint':'fixed','member_ids':[u['id'] for u in outer.targets]}
            def verify_selected_scope(self, value):
                if value != self.selected_scope(7): raise MigrationError('目标群组的路径或权限已变化')
            def members(self, _): return [{'user_id':u['id'],'active':True,'grants':[{'abilities':{'read_issues':True}}]} for u in outer.targets]
            def api(self, method, path):
                if path.startswith('/admin/users?'):
                    page = int(path.split('page=')[1].split('&')[0]); return outer.targets[(page-1)*100:page*100]
                if path.startswith('/users/'):
                    return next(u for u in outer.targets if u['login'] == path.split('/')[-1])
                raise AssertionError(path)
        self.receiver = FakeReceiver()
        @contextmanager
        def fake_connection(*_): yield self.receiver
        patcher = patch.object(service, 'connection', fake_connection); patcher.start(); self.addCleanup(patcher.stop)
        class FakeProvider:
            kind='gitea'
            def authenticate(self): return {'actor': outer.data['actor'], 'version':outer.data['version']}
            def collection(self, _): return outer.sources
        self.provider = FakeProvider()
        self.row = service.enqueue({'inventory_id':self.inventory.id,'server_id':self.server.id,'group_id':7,'request_key':'mapping-account-test'},1)
        self.complete_scan()

    def complete_scan(self):
        self.row.result_json = encode(service.scan(self.row, self.provider, lambda:None))
        self.row.status = 'succeeded'; db.session.commit()

    def change(self, action, **kwargs):
        self.row = service.mutate(self.row.id, {'revision':self.row.revision,'action':action, **kwargs})

    def test_bulk_confirms_all_pages_not_current_filter(self):
        self.assertEqual(len(service.public(self.row, size=10)['rows']),10)
        self.change('confirm_automatic')
        self.assertEqual(self.row.status, 'queued')
        self.assertEqual(service.public(self.row)['stats']['confirmed'],0)
        self.complete_scan()
        result = service.public(self.row)
        self.assertEqual(result['stats']['confirmed'],118)
        self.assertFalse(result['ready'])

    def test_changing_a_confirmed_mapping_revokes_its_confirmation(self):
        self.change('confirm', key='gitea:person:1')
        self.assertEqual(service.public(self.row)['stats']['confirmed'],1)
        self.change('select', key='gitea:person:1', target_ids=[1002])
        self.assertEqual(service.public(self.row)['stats']['confirmed'],0)
        self.change('confirm_automatic'); self.complete_scan()
        item = next(r for r in json.loads(self.row.result_json)['rows'] if r['key']=='gitea:person:1')
        self.assertFalse(item['confirmed'], '批量操作不能覆盖人工改选项')

    def test_stale_revision_is_rejected(self):
        revision = self.row.revision
        self.change('confirm', key='gitea:person:1')
        with self.assertRaisesRegex(MigrationError,'草稿已变化'):
            service.mutate(self.row.id, {'revision':revision,'action':'confirm','key':'gitea:person:2'})

    def test_target_replacement_is_rejected(self):
        self.targets[0] = person(9000,'user1',name='人员1')
        with self.assertRaises(MigrationError): self.change('confirm', key='gitea:person:1')
        self.assertFalse(service.public(self.row)['ready'])

    def test_refresh_finds_new_accounts_and_preserves_unchanged_confirmations(self):
        self.change('confirm',key='gitea:person:1')
        self.targets += [person(1119,'user119',name='人员119'),person(1120,'user120',name='人员120')]
        self.change('refresh'); self.complete_scan()
        self.assertEqual(service.public(self.row)['stats']['automatic'],120)
        # 新增账号不抹掉未变化人员的既有确认。
        self.assertEqual(service.public(self.row)['stats']['confirmed'],1)

    def test_full_progress_required_for_resolve_and_forged_raw_input(self):
        from git_migration.runs import enqueue
        with self.assertRaisesRegex(MigrationError,'未全部确认'):
            service.resolve(self.row.id,self.inventory,self.server,7,self.receiver)
        with patch('git_migration.runs.Receiver') as receiver:
            receiver.return_value.actor = self.receiver.actor
            with self.assertRaisesRegex(MigrationError,'完成全部人员'):
                enqueue({'inventory_id':self.inventory.id,'server_id':self.server.id,'group_id':7,'request_key':'forged-raw-mapping','source_frozen':True,'principal_map':{'gitea:person:1':['user1']}},1)

    def test_interrupt_does_not_report_unfinished_bulk_as_confirmed(self):
        self.change('confirm_automatic'); self.row.status='running'; db.session.commit()
        recover_interrupted(); db.session.refresh(self.row)
        result=service.public(self.row)
        self.assertEqual(result['status'],'interrupted'); self.assertFalse(result['ready'])
        self.assertEqual(result['stats']['confirmed'],0)

    def test_empty_required_census_is_ready_after_successful_scan(self):
        self.inventory.result_json=encode(snapshot([]));db.session.commit()
        # 新盘点内容需要新的草稿。
        row=service.enqueue({'inventory_id':self.inventory.id,'server_id':self.server.id,'group_id':7,'request_key':'mapping-empty-test'},1)
        row.result_json=encode(service.scan(row,self.provider,lambda:None));row.status='succeeded';db.session.commit()
        self.assertTrue(service.public(row)['ready'])


class RetentionAndSelectionTests(unittest.TestCase):
    setUp = PersistentMappingTests.setUp
    complete_scan = PersistentMappingTests.complete_scan
    change = PersistentMappingTests.change

    def test_explicit_cross_page_selection_does_not_confirm_everyone(self):
        self.change('confirm_selected', selection={'mode':'keys', 'keys':['gitea:person:1','gitea:person:101']})
        self.complete_scan()
        self.assertEqual(service.public(self.row)['stats']['confirmed'], 2)

    def test_filter_scope_and_exclusions_are_frozen(self):
        self.change('confirm_selected', selection={'mode':'filtered','status':'automatic','query':'人员1','excluded':['gitea:person:1']})
        self.complete_scan()
        rows=json.loads(self.row.result_json)['rows']
        self.assertTrue(next(r for r in rows if r['key']=='gitea:person:101')['confirmed'])
        self.assertFalse(next(r for r in rows if r['key']=='gitea:person:1')['confirmed'])
        self.assertFalse(next(r for r in rows if r['key']=='gitea:person:2')['confirmed'])

    def test_invalid_member_rejects_entire_batch(self):
        with self.assertRaisesRegex(MigrationError, '未确定或不可用'):
            self.change('confirm_selected',selection={'mode':'keys','keys':['gitea:person:1','gitea:person:119']})
        self.assertEqual(service.public(self.row)['stats']['confirmed'],0)

    def test_batch_rechecks_changed_target_without_partial_confirm(self):
        self.change('confirm_selected',selection={'mode':'keys','keys':['gitea:person:1','gitea:person:2']})
        self.targets[1] = person(9900,'user2',name='人员2')
        with self.assertRaisesRegex(MigrationError,'批量确认期间'):
            self.complete_scan()
        self.assertEqual(service.public(self.row)['stats']['confirmed'],0)

    def test_retain_missing_identities_and_resolve_empty_native_links(self):
        preview=service.public(self.row)['retention_preview']
        self.assertEqual(preview['count'],2)
        with self.assertRaises(MigrationError):
            self.change('retain_unmatched',impact_hash=preview['impact_hash'],acknowledged=False)
        self.change('retain_unmatched',impact_hash=preview['impact_hash'],acknowledged=True)
        self.change('confirm_automatic');self.complete_scan()
        value=service.public(self.row)
        self.assertTrue(value['ready']);self.assertEqual(value['stats']['retained'],2)
        mapping, retained, receipt=service.resolve(self.row.id,self.inventory,self.server,7,self.receiver,True)
        self.assertEqual(mapping['gitea:person:119'],[])
        self.assertEqual(retained['gitea:person:119']['source']['id'],119)
        self.assertEqual(receipt['revision'],self.row.revision)
        self.change('select',key='gitea:person:119',target_ids=[1001])
        self.assertFalse(service.public(self.row)['ready'])
        self.assertEqual(service.public(self.row)['stats']['retained'],1)

    def test_retention_survives_refresh_without_creating_an_account(self):
        preview=service.public(self.row)['retention_preview']
        self.change('retain_unmatched',impact_hash=preview['impact_hash'],acknowledged=True)
        self.change('refresh');self.complete_scan()
        self.assertEqual(service.public(self.row)['stats']['retained'],2)
        self.assertEqual(len(self.targets),118)

    def test_rule_alias_is_never_eligible_for_retention(self):
        self.data['projects'][0]['datasets'].append({'category':'分支保护','rows':[{'push_whitelist_usernames':['user119']}]})
        self.inventory.result_json=encode(self.data);db.session.commit()
        self.assertTrue(service.public(self.row)['stale'])
        self.row=service.enqueue({'inventory_id':self.inventory.id,'server_id':self.server.id,'group_id':7,'request_key':'retention-rule-test'},1)
        self.complete_scan()
        preview=service.public(self.row)['retention_preview']
        self.assertEqual((preview['count'],preview['blocked']),(1,1))
        self.change('retain_unmatched',impact_hash=preview['impact_hash'],acknowledged=True)
        self.assertFalse(service.public(self.row)['ready'])
        blocked=service.public(self.row,status='rules')['rows']
        self.assertEqual(blocked[0]['source']['id'],119)
        self.assertFalse(blocked[0]['preservable'])

    def test_old_retention_preview_is_rejected(self):
        preview=service.public(self.row)['retention_preview']
        self.change('confirm',key='gitea:person:1')
        with self.assertRaisesRegex(MigrationError,'最新影响范围'):
            self.change('retain_unmatched',impact_hash=preview['impact_hash'],acknowledged=True)

    def test_manually_selected_missing_person_is_not_overwritten(self):
        self.change('select',key='gitea:person:119',target_ids=[1001])
        preview=service.public(self.row)['retention_preview']
        self.assertEqual(preview['count'],1)
        self.change('retain_unmatched',impact_hash=preview['impact_hash'],acknowledged=True)
        row=next(r for r in json.loads(self.row.result_json)['rows'] if r['key']=='gitea:person:119')
        self.assertEqual(row['selected_ids'],[1001]);self.assertFalse(row['confirmed'])

    def test_selection_cannot_include_unknown_or_confirmed_rows(self):
        self.change('confirm',key='gitea:person:1')
        for key in ('gitea:person:1','gitea:person:99999'):
            with self.assertRaises(MigrationError):
                self.change('confirm_selected',selection={'mode':'keys','keys':[key]})

    def test_two_thousand_people_keep_full_count_and_four_rule_exceptions(self):
        self.sources=[person(i,f'user{i}',name=f'人员{i}') for i in range(1,2001)]
        self.targets=[person(i+5000,f'user{i}',name=f'人员{i}') for i in range(1,1601)]
        self.data=snapshot(self.sources)
        self.data['projects'][0]['datasets'].append({'category':'分支保护','rows':[{'push_whitelist_usernames':[f'user{i}' for i in range(1997,2001)]}]})
        self.inventory.result_json=encode(self.data);db.session.commit()
        self.row=service.enqueue({'inventory_id':self.inventory.id,'server_id':self.server.id,'group_id':7,'request_key':'retention-large-test'},1)
        self.complete_scan()
        result=service.public(self.row,query='人员1',status='automatic')
        self.assertEqual((result['stats']['total'],result['stats']['automatic_pending']), (2000,1600))
        self.change('confirm_selected',selection={'mode':'keys','keys':[f'gitea:person:{i}' for i in range(1,21)]});self.complete_scan()
        self.assertEqual(service.public(self.row)['stats']['confirmed'],20)
        self.change('confirm_automatic');self.complete_scan()
        preview=service.public(self.row)['retention_preview']
        self.assertEqual((preview['count'],preview['blocked']), (396,4))
        self.change('retain_unmatched',impact_hash=preview['impact_hash'],acknowledged=True)
        result=service.public(self.row)
        self.assertEqual((result['stats']['confirmed'],result['stats']['retained']), (1996,396))
        self.assertFalse(result['ready'])

    def test_official_reviewer_is_an_effective_gate(self):
        from git_migration.people import protected_requirements
        data={'kind':'gitea','projects':[{'datasets':[{'category':'评审','rows':[{'state':'REQUEST_REVIEW','official':True,'user':{'id':119}}]}]}]}
        self.assertEqual(protected_requirements(data),{'gitea:person:119'})
