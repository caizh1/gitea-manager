"""显式目标群组、既有权限保留、路径冲突及来源入口的回归检查。"""
from contextlib import contextmanager
import copy
import json
import unittest
from unittest.mock import patch

from svn_migration import discovery, service, targets, worker
from svn_migration.common import MigrationError
from svn_migration.gitea import Gitea
from tests import test_svn_migrations as fixtures


def group(ident=2, parent=1, path='firmware/storage'):
    return {'id': ident, 'parent_id': parent, 'full_path': path, 'kind': 'group',
            'name': '存储固件', 'visibility': 2, 'archived': False, 'delete_after': 0,
            'abilities': {'create_project': True, 'create_group': True}}


class GroupAccessTests(unittest.TestCase):
    def setUp(self):
        self.rows = {1: group(1, 0, 'firmware'), 2: group()}
        self.members = [{'user_id': 1, 'active': True, 'effective_role': 'owner', 'grants': []},
                        {'user_id': 8, 'active': True, 'effective_role': 'reporter', 'grants': []}]
        self.calls = []
        self.api = Gitea.__new__(Gitea)
        self.api.api = self.remote

    def remote(self, method, path, *args, **kwargs):
        self.calls.append((method, path))
        if '/shares' in path:
            return {'shares': [], 'next_id': 0}
        if '/members' in path:
            return copy.deepcopy(self.members) if 'after_id=0' in path else []
        return copy.deepcopy(self.rows[int(path.rsplit('/', 1)[1])])

    def test_selected_nested_group_keeps_existing_business_members(self):
        selected = self.api.selected_scope(2)
        self.assertEqual(selected['path'], 'firmware/storage')
        self.assertEqual(selected['member_ids'], [1, 8])
        self.api.verify_selected_scope(selected)
        self.assertTrue(all(method == 'GET' for method, _ in self.calls))

    def test_permission_change_after_preflight_blocks(self):
        selected = self.api.selected_scope(2)
        self.members.append({'user_id': 9, 'active': True, 'effective_role': 'reporter'})
        with self.assertRaisesRegex(MigrationError, '权限已变化'):
            self.api.verify_selected_scope(selected)

    def test_group_move_and_archiving_block(self):
        selected = self.api.selected_scope(2)
        self.rows[2]['full_path'] = 'other/storage'
        with self.assertRaises(MigrationError): self.api.verify_selected_scope(selected)
        self.rows[2]['archived'] = True
        with self.assertRaises(MigrationError): self.api.selectable_group(2)

    def test_missing_create_ability_blocks(self):
        self.rows[2]['abilities']['create_project'] = False
        with self.assertRaisesRegex(MigrationError, '没有.*创建仓库'):
            self.api.selectable_group(2)

    def test_private_repository_accepts_only_selected_group_members(self):
        selected = self.api.selected_scope(2)
        repo = {'id': 40, 'private': True, 'full_path': 'firmware/storage/project',
                'full_name': 'g_internal/project', 'owner': {'id': 2}}
        self.api.repository_private(repo, 'firmware/storage', 'project', 2, 1, selected)
        with patch.object(self.api, 'verify_selected_scope'), patch.object(self.api, 'members', return_value=[{'user_id': 90}]):
            with self.assertRaisesRegex(MigrationError, '群组之外'):
                self.api.repository_private(repo, 'firmware/storage', 'project', 2, 1, selected)

    def test_legacy_private_group_remains_strict(self):
        with self.assertRaises(MigrationError):
            self.api.group_private(1, 'firmware', 1)

    def test_worker_does_not_create_or_bind_selected_group(self):
        selected = self.api.selected_scope(2)
        with patch('svn_migration.worker.checkpoint') as checkpoint:
            result = worker.ensure_group(self.api, {'target_group': selected, 'group_id': 2, 'owner': selected['path']}, None, None)
        self.assertEqual(result, 2)
        checkpoint.assert_called_once_with(None, group_id=2)
        self.assertTrue(all(method == 'GET' for method, _ in self.calls))


class TargetRoutesTests(unittest.TestCase):
    setUp = fixtures.MigrationTests.setUp
    post = fixtures.MigrationTests.post
    source = fixtures.MigrationTests.source

    def test_plan_selection_persisted_and_path_not_trusted(self):
        source = self.source()
        repo = service.edit_repository(source.id, {'name': 'project', 'url': source.url + '/project'})
        data = {'name': '显式群组计划', 'server_id': self.target.id, 'repositories': [{'id': repo.id}], 'target_group': {'id': 2}}
        plan = service.plan_edit(data)
        self.assertEqual(json.loads(plan.config_json)['target_group'], {'id': 2})
        data['target_group']['path'] = 'forged'
        with self.assertRaises(MigrationError): service.plan_edit(data)

    def test_target_endpoints_enforce_csrf(self):
        for suffix in ('groups/query', 'groups', 'paths'):
            result = self.client.post(f'/api/svn-migrations/targets/{self.target.id}/{suffix}', json={})
            self.assertEqual(result.status_code, 403)

    def test_duplicate_names_and_existing_repositories_are_conflicts(self):
        class API:
            def selectable_group(self, _): return group()
            def repository(self, _, name, **kw): return {'id': 4} if name == 'existing' else None
            def child_group_paths(self, _): return {'firmware/storage/child'}
        @contextmanager
        def connection(*args): yield API()
        with patch('svn_migration.targets.connection', connection):
            result = targets.paths(self.target, {'group_id': 2, 'names': ['new', 'NEW', 'existing', 'child']})
        self.assertEqual([row['available'] for row in result['repositories']], [True, False, False, False])
        self.assertEqual(result['repositories'][0]['path'], 'firmware/storage/new')


class NavigationTests(unittest.TestCase):
    XML = '''<?xml version="1.0"?>
<?xml-stylesheet type="text/xsl" href="/!/assets/xsl/svnindex-ng.xsl?v=2"?>
<!DOCTYPE svn [<!ELEMENT svn (index)><!ELEMENT index (dir*)><!ELEMENT dir EMPTY><!ATTLIST dir name CDATA #REQUIRED href CDATA #REQUIRED>]>
<svn version="1.10.3 (r1842928)" href="http://subversion.apache.org/"><index rev="0" path="Collection of Repositories"><dir name="emmc_production" href="emmc_production/" /></index></svn>'''

    def run_discovery(self, prefix, homepage):
        calls, root = [], 'http://svn.example.test'
        class Source:
            def __init__(self, *args): pass
            def info(self, url):
                if url == root + prefix + '/emmc_production': return {'uuid': 'fixed', 'revision': 20}
                raise MigrationError('不是仓库入口', 'command')
        class Process:
            def tick(self): pass
        def read(_, url, tick):
            calls.append(url)
            return discovery.index_links(homepage if url == root else self.XML if url == root + prefix else '<html/>', url)
        with patch('svn_migration.engine.Source', Source), patch('svn_migration.discovery.read_index', read), patch('svn_migration.discovery.projects_at', return_value=[{'key': 'a', 'name': 'emmc_production'}]):
            result = discovery.discover({'url': root}, root, Process(), '/tmp', lambda *args: None)
        return calls, result

    def test_visualsvn_xml_with_doctype(self):
        self.assertEqual(discovery.index_links(self.XML, 'http://svn.example.test/svn/'), ['http://svn.example.test/svn/emmc_production'])

    def test_root_navigation_does_not_stop_default_entry(self):
        calls, result = self.run_discovery('/svn', '<a href="/svn/">Repositories</a>')
        self.assertIn('http://svn.example.test/svn', calls)
        self.assertEqual(len(result['projects']), 1)
        self.assertEqual(result['entry_urls'], ['http://svn.example.test/svn'])

    def test_custom_link_entry_is_followed(self):
        calls, result = self.run_discovery('/repos', '<a href="/repos/">Repositories</a><a href="https://other.test/private/">外部链接</a>')
        self.assertIn('http://svn.example.test/repos', calls)
        self.assertEqual(len(result['projects']), 1)
        self.assertFalse(any('other.test' in url for url in calls))

    def test_unrelated_homepage_link_does_not_hide_default_entry(self):
        _, result = self.run_discovery('/svn', '<a href="/help/">Help</a>')
        self.assertEqual(len(result['projects']), 1)


if __name__ == '__main__': unittest.main()
