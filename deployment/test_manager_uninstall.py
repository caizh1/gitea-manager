"""不依赖旧安装目录、双重确认和共享资源保护的回归。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import install as installer
import manager_uninstall as module


class Tests(unittest.TestCase):
    def setUp(self):
        self.cleaner = Mock()
        self.cleaner.docker.return_value = ''
        self.cleaner.discover.return_value = [{'实例':'gitea-manager-a7a134635e','名称':'测试 Manager','状态':'运行','端口':'5480','原安装目录':'/tmp/已删除目录'}]
        self.plan = {'container':['abc'], 'volume':['卷'], 'network':['网'], 'mounts':[]}

    def execute(self, answers, changed=False):
        with tempfile.TemporaryDirectory() as folder, patch.object(module, 'inventory', side_effect=[self.plan, {} if changed else self.plan]):
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
        c.docker.return_value=json.dumps({module.LABEL:c.project,'com.docker.compose.service':'backend'})+'\n'+json.dumps([{'Type':'volume','Name':'别人的卷'}])
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

if __name__=='__main__':unittest.main()
