"""规则主体映射、保守阻断与制品完整性清单的回归。"""
import unittest
from unittest.mock import Mock
from git_migration.rules import requirements, validate_mapping, convert
from git_migration.packages import package_assets
from svn_migration.common import MigrationError


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.source = {'name': 'main', 'allow_force_push': False,
                       'push_access_levels': [{'access_level': 40}],
                       'merge_access_levels': [{'access_level': 40}]}
        self.inventory = {'datasets': [{'category': '分支保护', 'path': '/protected_branches', 'rows': [self.source]}]}
        self.mapping = {'gitlab:access:40': [{'id': 7, 'login': 'maintainer'}]}

    def test_explicit_mapping_has_stable_identity_and_restricts_push_merge(self):
        receiver = Mock()
        receiver.api.return_value = {'id': 7, 'login': 'maintainer', 'active': True}
        required = requirements({'projects': [self.inventory]})
        self.assertEqual(validate_mapping({'gitlab:access:40': ['maintainer']}, required, receiver), self.mapping)
        normalized = {'pending': [{'path': '/protected_branches'}]}
        rule = convert(self.inventory, 'gitlab', self.mapping, normalized)[0]
        self.assertEqual(rule['push_whitelist_usernames'], ['maintainer'])
        self.assertEqual(rule['merge_whitelist_usernames'], ['maintainer'])
        self.assertFalse(rule['enable_force_push'])
        self.assertTrue(rule['block_admin_merge_override'])
        self.assertFalse(normalized['pending'])

    def test_missing_mapping_and_non_equivalent_rules_cannot_silently_pass(self):
        with self.assertRaises(MigrationError):
            validate_mapping({}, ['gitlab:access:40'], Mock())
        for changes in ({'code_owner_approval_required': True},):
            source = dict(self.source, **changes)
            inventory = {'datasets': [{'category': '分支保护', 'path': '/protected_branches', 'rows': [source]}]}
            normalized = {'pending': [{'path': '/protected_branches'}]}
            self.assertFalse(convert(inventory, 'gitlab', self.mapping, normalized))
            self.assertTrue(normalized['pending'])

    def test_wildcard_and_nobody_merge_preserve_native_source_semantics(self):
        for changes in ({'name':'release/*'}, {'merge_access_levels':[{'access_level':0}]}):
            inventory={'datasets':[{'category':'分支保护','path':'/rules','rows':[dict(self.source,**changes)]}]}
            normalized={'pending':[{'path':'/rules'}]}
            rule=convert(inventory,'gitlab',self.mapping,normalized)[0]
            self.assertTrue(rule['migration_gitlab'])
            self.assertTrue(rule['enable_merge_whitelist'])
            self.assertFalse(normalized['pending'])
            if 'merge_access_levels' in changes:self.assertEqual(rule['merge_whitelist_usernames'],[])

    def test_package_manifest_requires_source_hash_and_preserves_unknown_protocols(self):
        time = '2020-01-01T00:00:00Z'
        file = {'id': 3, 'file_name': 'artifact.bin', 'file_sha256': 'a' * 64, 'size': 12, 'created_at': time}
        inventory = {'project': {'id': 1}, 'datasets': [
            {'category': '制品包', 'path': '/projects/1/packages', 'rows': [
                {'id': 2, 'name': 'firmware', 'version': '1.0', 'package_type': 'generic', 'created_at': time},
                {'id': 4, 'name': 'library', 'version': '1.0', 'package_type': 'unknown-protocol', 'created_at': time}]},
            {'category': '制品文件', 'path': '/projects/1/packages/2/package_files', 'rows': [file]}]}
        normalized = {'objects': [], 'pending': [{'path': d['path']} for d in inventory['datasets']]}
        files = package_assets(inventory, 'gitlab', normalized, 'https://source.test')
        self.assertEqual(files[0]['expected_sha256'], 'a' * 64)
        self.assertEqual(normalized['objects'][0]['history']['name'], 'firmware')
        self.assertEqual(normalized['pending'], [{'path': '/projects/1/packages'}])
        file.pop('file_sha256')
        with self.assertRaises(MigrationError):
            package_assets(inventory, 'gitlab', {'objects': [], 'pending': []}, 'https://source.test')

class TagRulesTests(unittest.TestCase):
    def test_gitlab_tag_patterns_and_delete_maintainers_are_kept(self):
        from git_migration.rules import requirements, convert_tags
        inventory={'datasets':[{'category':'标签保护','path':'/tags','rows':[{'name':'v[1]*','create_access_levels':[{'access_level':30}]}]}]}
        self.assertEqual(requirements({'projects':[inventory]}),['gitlab:access:30','gitlab:access:40'])
        mapping={'gitlab:access:30':[{'login':'developer'}],'gitlab:access:40':[{'login':'maintainer'}]}
        normalized={'pending':[{'path':'/tags'}]};rule=convert_tags(inventory,'gitlab',mapping,normalized)[0]
        self.assertEqual(rule['name_pattern'],r'/^v\[1\].*$/')
        self.assertEqual(rule['whitelist_usernames'],['developer'])
        self.assertEqual(rule['delete_whitelist_usernames'],['maintainer'])
        self.assertTrue(rule['migration_gitlab']);self.assertFalse(normalized['pending'])
