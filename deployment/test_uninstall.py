"""卸载的双确认、身份与资源隔离回归。"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('uninstall', Path(__file__).with_name('uninstall.py'))
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class UninstallTests(unittest.TestCase):
    def test_missing_identity_never_reports_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'installer-manifest.json').write_text(json.dumps({'product': 'manager'}))
            with patch.object(u, 'ROOT', root), patch.object(u.os, 'geteuid', return_value=0), \
                    patch.object(u, 'confirm', return_value=True), patch.object(u.Cleaner, 'docker', return_value=''):
                self.assertEqual(u.main(), 1)

    def test_empty_project_never_reports_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'installer-manifest.json').write_text(json.dumps({'product': 'manager'}))
            (root / 'state').mkdir()
            (root / 'state/installation.json').write_text(json.dumps({'product': 'manager', 'directory': directory, 'project': 'gitea-manager-0123456789'}))
            with patch.object(u, 'ROOT', root), patch.object(u.os, 'geteuid', return_value=0), \
                    patch.object(u, 'confirm', return_value=True), patch.object(u.Cleaner, 'docker', return_value=''):
                self.assertEqual(u.main(), 1)
                self.assertTrue((root / 'state/installation.json').exists())

    def test_discovery_does_not_select_unrelated_product(self):
        cleaner = u.Cleaner(None, 'manager', None)
        rows = [
            ['a', 'gitea-manager-old-frontend-1', '5480', 'Up', 'gitea-manager-old', '/opt/old-manager'],
            ['b', 'gitea-gitea-old-gitea-1', '3000', 'Up', 'gitea-gitea-old', '/opt/gitea'],
        ]
        cleaner.docker = lambda *args: '\n'.join(json.dumps(row) for row in rows)
        self.assertEqual([row['容器'] for row in cleaner.discover()], ['a'])
        self.assertIsNone(cleaner.project)

    def test_automatic_target_and_multiple_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory).resolve()
            rows = []
            targets = []
            for token in ('0123456789', 'abcdef0123'):
                target = package / token; target.mkdir(); (target / 'state').mkdir()
                project = 'gitea-manager-' + token
                (target / 'installer-manifest.json').write_text(json.dumps({'product': 'manager'}))
                (target / 'state/installation.json').write_text(json.dumps({'product': 'manager', 'directory': str(target), 'project': project}))
                rows.append({'实例': project, '原安装目录': str(target), '端口': '5480'})
                targets.append((target, project))
            cleaner = u.Cleaner(None, 'manager', None)
            self.assertEqual(u.choose_target(package, 'manager', None, cleaner, rows[:1], lambda _: self.fail('唯一实例无需选择')), targets[0])
            self.assertEqual(u.choose_target(package, 'manager', None, cleaner, rows, lambda _: '2'), targets[1])
            with self.assertRaisesRegex(RuntimeError, '取消'):
                u.choose_target(package, 'manager', None, cleaner, rows, lambda _: '')
            rows[0]['原安装目录'] = str(targets[1][0])
            with self.assertRaisesRegex(RuntimeError, '不一致'):
                u.choose_target(package, 'manager', None, cleaner, rows[:1])

    def test_confirmation(self):
        for answers, expected in [(['no'], False), (['UNINSTALL', 'no'], False),
                                  (['UNINSTALL', 'DELETE gitea-manager-0123456789'], True)]:
            iterator = iter(answers)
            self.assertEqual(u.confirm('manager', 'gitea-manager-0123456789', lambda _: next(iterator)), expected)

    def test_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'installer-manifest.json').write_text(json.dumps({'product': 'manager'}))
            self.assertEqual(u.identity(root), ('manager', None))
            (root / 'compose.install.json').write_text('{}')
            with self.assertRaises(RuntimeError): u.identity(root)
            (root / 'state').mkdir()
            state = {'product': 'manager', 'directory': directory, 'project': 'gitea-manager-0123456789'}
            path = root / 'state/installation.json'
            path.write_text(json.dumps(state))
            self.assertEqual(u.identity(root), ('manager', state['project']))
            state['project'] = 'gitea-gitea-0123456789'
            path.write_text(json.dumps(state))
            with self.assertRaises(RuntimeError): u.identity(root)

    def cleaner(self, foreign=False, unknown=False):
        c = u.Cleaner(None, 'manager', 'gitea-manager-0123456789')
        volume = c.project + ('_unknown' if unknown else '_manager-data')
        def docker(*args):
            if args[:2] == ('ps', '-aq'):
                return 'foreign' if foreign and args[-1].startswith('volume=') else 'own'
            if args[:2] == ('volume', 'ls'): return volume
            if args[:2] == ('network', 'ls'): return 'network'
            raise AssertionError(args)
        c.docker = docker
        return c

    def test_foreign_volume_user(self):
        with self.assertRaisesRegex(RuntimeError, '其他实例'): self.cleaner(foreign=True).inventory()

    def test_unknown_volume(self):
        with self.assertRaisesRegex(RuntimeError, '未知数据卷'): self.cleaner(unknown=True).inventory()

    def test_changed_plan(self):
        with self.assertRaisesRegex(RuntimeError, '资源发生变化'): self.cleaner().remove({})

    def test_manager_database_guard(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            c = u.Cleaner(None, 'manager', 'gitea-manager-0123456789')
            c.docker = lambda *args: directory
            plan = {'volume': [c.project + '_manager-data']}
            with c.fresh_manager_guard(plan):
                other = sqlite3.connect(str(Path(directory) / 'gitea-manager.db'), timeout=0.01)
                with self.assertRaises(sqlite3.OperationalError): other.execute('CREATE TABLE gitea_servers(id INTEGER)')
                other.close()
            db = sqlite3.connect(str(Path(directory) / 'gitea-manager.db'))
            db.execute('CREATE TABLE gitea_servers(id INTEGER)')
            db.execute('INSERT INTO gitea_servers VALUES (1)'); db.commit(); db.close()
            with self.assertRaisesRegex(RuntimeError, '已有管理'):
                with c.fresh_manager_guard(plan): self.fail('不得进入删除阶段')

    def test_symlink_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'state').symlink_to('/tmp')
            with self.assertRaisesRegex(RuntimeError, '符号链接'): u.identity(root)


if __name__ == '__main__': unittest.main()
