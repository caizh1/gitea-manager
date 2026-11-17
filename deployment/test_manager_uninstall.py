"""不依赖旧安装目录、双重确认和共享资源保护的回归。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import install as installer
import manager_uninstall as module
from uninstall import Cleaner


class Tests(unittest.TestCase):
    def setUp(self):
        self.cleaner = Mock()
        self.cleaner.docker.return_value = ''
        self.cleaner.discover.return_value = [{'实例':'gitea-manager-a7a134635e','名称':'测试 Manager','状态':'运行','端口':'5480','原安装目录':'/tmp/已删除目录'}]
        self.plan = {'container':['abc'], 'volume':['卷'], 'network':['网'], 'mounts':[]}

    def execute(self, answers, changed=False):
        with tempfile.TemporaryDirectory() as folder, patch.object(module, 'inventory', side_effect=[self.plan, {} if changed else self.plan, {}]):
            self.cleaner.inventory.return_value = {'container':[], 'volume':[], 'network':[]}
            return module.run(self.cleaner, Mock(), ask=Mock(side_effect=answers), lock_root=Path(folder))

    def test_first_confirmation_cancel(self):
        self.assertFalse(self.execute(['no']))
        self.cleaner.remove_kind.assert_not_called()

    def test_second_confirmation_requires_exact_project(self):
        self.assertFalse(self.execute(['UNINSTALL','DELETE gitea-gitea-6f16d664c7']))
        self.cleaner.remove_kind.assert_not_called()

    def test_missing_directory_does_not_block(self):
        self.assertTrue(self.execute(['UNINSTALL','DELETE gitea-manager-a7a134635e']))
        self.assertEqual(self.cleaner.remove_kind.call_count,3)

    def test_legacy_project_names(self):
        for project in ('gitea-manager', 'gitea-manager-old', 'gitea-manager_deploy'):
            with self.subTest(project=project):
                self.cleaner.reset_mock()
                self.cleaner.discover.return_value[0]['实例'] = project
                self.assertTrue(self.execute(['UNINSTALL', 'DELETE ' + project]))
                self.assertEqual(self.cleaner.project, project)
                self.assertEqual(self.cleaner.remove_kind.call_count, 3)

    def test_legacy_project_confirmation_cancel(self):
        self.cleaner.discover.return_value[0]['实例'] = 'gitea-manager'
        self.assertFalse(self.execute(['UNINSTALL', 'DELETE gitea-manager-old']))
        self.cleaner.remove_kind.assert_not_called()

    def test_legacy_orphan_resources_can_be_selected(self):
        self.cleaner.discover.return_value = []
        for suffix in ('review-native', 'gitea-manager-data'):
            with self.subTest(suffix=suffix):
                self.cleaner.docker.return_value = 'gitea-manager_' + suffix + ' gitea-manager'
                self.assertTrue(self.execute(['UNINSTALL', 'DELETE gitea-manager']))
                self.assertEqual(self.cleaner.project, 'gitea-manager')

    def test_orphan_upgrade_volumes_discovery(self):
        self.cleaner.discover.return_value = []
        for suffix in module.UPGRADE_VOLUMES:
            with self.subTest(suffix=suffix):
                name = 'gitea-manager-' + suffix
                self.cleaner.docker.side_effect = lambda *args: name if args == (
                    'volume', 'ls', '--format', '{{.Name}}') else ''
                self.assertTrue(self.execute(['UNINSTALL', 'DELETE gitea-manager']))
                self.assertEqual(self.cleaner.project, 'gitea-manager')

    def test_similar_volume_suffix_is_not_selected(self):
        self.cleaner.discover.return_value = []
        self.cleaner.docker.side_effect = lambda *args: 'gitea-manager-review-data-backup' if args == (
            'volume', 'ls', '--format', '{{.Name}}') else ''
        with self.assertRaisesRegex(RuntimeError, '未找到 Manager'):
            self.execute([])
        self.cleaner.remove_kind.assert_not_called()

    def test_unlabelled_or_unrelated_project_is_not_selected(self):
        for project in (None, '', 'gitea-managerial', 'gitea-gitea-0123456789', '../gitea-manager'):
            with self.subTest(project=project):
                self.cleaner.discover.return_value[0]['实例'] = project
                with self.assertRaisesRegex(RuntimeError, '未找到 Manager'):
                    self.execute([])
        self.cleaner.remove_kind.assert_not_called()

    def test_changed_inventory_refuses_delete(self):
        with self.assertRaises(RuntimeError):self.execute(['UNINSTALL','DELETE gitea-manager-a7a134635e'],True)
        self.cleaner.remove_kind.assert_not_called()

    def test_gitea_is_not_selected(self):
        self.cleaner.discover.return_value[0]['实例']='gitea-gitea-6f16d664c7'
        with self.assertRaises(RuntimeError):self.execute([])
        self.cleaner.remove_kind.assert_not_called()

    def test_external_network_member_refuses(self):
        c=Mock();c.project='gitea-manager-a7a134635e'
        c.inventory.return_value={'container':[],'volume':[],'network':['网']}
        c.docker.return_value='{"外部容器":{}}'
        with self.assertRaises(RuntimeError):module.inventory(c)

    def test_external_volume_refuses(self):
        c=Mock();c.project='gitea-manager-a7a134635e'
        c.inventory.return_value={'container':['abc'],'volume':[],'network':[]}
        c.docker.return_value=json.dumps({module.LABEL:c.project,'com.docker.compose.service':'backend'})+'\n'+json.dumps([{'Type':'volume','Name':'别人的卷','Destination':'/app/review'}])
        with self.assertRaises(RuntimeError):module.inventory(c)

    def test_inventory_accepts_all_installer_services(self):
        project = 'gitea-manager-a7a134635e'
        state = {'product': 'manager', 'project': project, 'host': '127.0.0.1', 'port': 5480,
                 'secret_key': '测试会话密钥', 'admin_password': '测试管理员密码'}
        images = {name: {'tag': name + ':test'} for name in ('backend', 'frontend', 'executor', 'svn', 'git')}
        services = installer.configuration(state, {'images': images}, Path('/isolated-manager'))['services']
        self.assertIn('git-migration-worker', services)
        cleaner = Mock(project=project)
        cleaner.inventory.return_value = {'container': sorted(services), 'volume': [], 'network': []}

        def inspect(*args):
            service = args[-1]
            labels = {module.LABEL: project, 'com.docker.compose.service': service}
            return json.dumps(labels) + '\n' + json.dumps([])

        cleaner.docker.side_effect = inspect
        self.assertEqual(module.inventory(cleaner)['container'], sorted(services))

    def test_unknown_service_still_refuses(self):
        cleaner = Mock(project='gitea-manager-a7a134635e')
        cleaner.inventory.return_value = {'container': ['other'], 'volume': [], 'network': []}
        labels = {module.LABEL: cleaner.project, 'com.docker.compose.service': 'unknown-service'}
        cleaner.docker.return_value = json.dumps(labels) + '\n' + json.dumps([])
        with self.assertRaisesRegex(RuntimeError, '发现非预期服务'):
            module.inventory(cleaner)

class UpgradeVolumeTests(unittest.TestCase):
    def setUp(self):
        self.project = 'gitea-manager'
        self.volume = self.project + '-review-data'
        self.labels = {module.UPGRADE_REVIEW_LABEL: self.project}
        self.destination = '/app/review'
        self.users = ['own']
        self.removals = []
        self.exists = True
        self.cleaner = Cleaner(Mock(), 'manager', self.project)
        self.cleaner.docker = self.docker

    def docker(self, *args):
        if args[:2] == ('ps', '-aq'):
            return ' '.join(self.users) if args[-1].startswith('volume=') else 'own'
        if args[:2] == ('volume', 'ls'):
            if args[2] == '-q':
                return self.volume if self.labels.get(module.LABEL) == self.project and self.exists else ''
            return self.volume if self.exists else ''
        if args[:2] == ('network', 'ls'):
            return ''
        if args[:2] == ('container', 'inspect'):
            labels = {module.LABEL: self.project, 'com.docker.compose.service': 'backend'}
            mounts = [{'Type': 'volume', 'Name': self.volume, 'Destination': self.destination}]
            return json.dumps(labels) + '\n' + json.dumps(mounts)
        if args[:2] == ('volume', 'inspect'):
            return '\n'.join(json.dumps(value) for value in
                             (self.volume, 'local', '2026-11-17T00:00:00Z', self.labels))
        if args[:2] == ('volume', 'rm'):
            self.removals.append(args[-1])
            self.exists = False
            return args[-1]
        raise AssertionError(args)

    def test_custom_upgrade_label_without_compose_label(self):
        plan = module.inventory(self.cleaner)
        self.assertEqual(plan['volume'], [self.volume])
        self.assertEqual(plan['volume_identities'][self.volume]['labels'], self.labels)

    def test_all_upgrade_volume_names_and_targets(self):
        for suffix, destination in module.UPGRADE_VOLUMES.items():
            with self.subTest(suffix=suffix):
                self.volume = self.project + '-' + suffix
                self.destination = destination
                self.labels = {}
                self.assertEqual(module.inventory(self.cleaner)['volume'], [self.volume])

    def test_shared_upgrade_volume_is_rejected(self):
        self.users.append('foreign')
        with self.assertRaisesRegex(RuntimeError, '其他实例引用'):
            module.inventory(self.cleaner)

    def test_conflicting_owner_is_rejected(self):
        for key in (module.LABEL, module.UPGRADE_REVIEW_LABEL):
            with self.subTest(key=key):
                self.labels = {key: 'gitea-manager-other'}
                with self.assertRaisesRegex(RuntimeError, '属于其他实例'):
                    module.inventory(self.cleaner)

    def test_wrong_mount_target_is_rejected(self):
        self.destination = '/foreign-data'
        with self.assertRaisesRegex(RuntimeError, '挂载位置不一致'):
            module.inventory(self.cleaner)

    def test_arbitrary_external_volume_is_rejected(self):
        self.volume = 'other-review-data'
        with self.assertRaisesRegex(RuntimeError, '非本项目管理'):
            module.inventory(self.cleaner)

    def test_delete_rechecks_upgrade_volume_identity(self):
        plan = module.inventory(self.cleaner)
        self.labels = {module.UPGRADE_REVIEW_LABEL: 'gitea-manager-other'}
        with self.assertRaisesRegex(RuntimeError, '身份或归属改变'):
            self.cleaner.remove_kind('volume', plan)
        self.assertEqual(self.removals, [])

    def test_upgrade_volume_can_be_deleted(self):
        plan = module.inventory(self.cleaner)
        self.cleaner.remove_kind('volume', plan)
        self.assertEqual(self.removals, [self.volume])

    def test_orphan_upgrade_volume_is_included(self):
        with patch.object(self.cleaner, 'inventory', return_value={'container': [], 'volume': [], 'network': []}):
            self.users = []
            self.assertEqual(module.inventory(self.cleaner)['volume'], [self.volume])


if __name__=='__main__':unittest.main()
