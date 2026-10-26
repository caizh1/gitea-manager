"""灾备清单、准入边界和独立导入的确定性回归。"""
import io
import json
from pathlib import Path
import tempfile
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from services.backup_manifest import append_manifest, load_manifest, admit, storage_profile


class PhaseTwoTests(unittest.TestCase):
    def test_empty_rollback_health_does_not_relax_normal_auth(self):
        from services.commit_service import _require_target_health
        target = SimpleNamespace()
        with patch('services.commit_service._api_get_json') as request:
            _require_target_health(target, require_auth=False)
            self.assertEqual([call.args[1] for call in request.call_args_list], ['/api/v1/version'])
            request.reset_mock()
            _require_target_health(target)
            self.assertEqual([call.args[1] for call in request.call_args_list], ['/api/v1/version', '/api/v1/user'])

    def test_admission_rejects_each_mismatch(self):
        expected = {'镜像': 'sha256:固定构建', '数据库主版本': 17, '存储': {}, '功能配置': {'lfs_enabled': False}}
        for key, value in [('镜像', '另一个构建'), ('数据库主版本', 16), ('存储', {'lfs': {}}), ('功能配置', {'lfs_enabled': True})]:
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, '尚未修改'):
                admit({'准入': expected}, dict(expected, **{key: value}))
        admit({'准入': expected}, expected)

    def test_rejects_external_storage_and_nonpersistent_path(self):
        for raw in ('[storage]\nSTORAGE_TYPE=minio', '[lfs]\nPATH=/tmp/lfs',
                    '[session]\nPROVIDER=redis', '[storage.actions_log]\nSTORAGE_TYPE=azureblob'):
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                storage_profile(raw.encode())
        self.assertEqual(storage_profile(b'[lfs]\nPATH=/data/git/lfs')['lfs']['path'], '/data/git/lfs')

    def test_manifest_detects_missing_added_and_changed_files(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'backup.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('gitea-db.sql', '固定数据库样本')
            append_manifest(path, {}, {'governance_namespace': '摘要'}, SimpleNamespace(name='来源', api_token='令牌'))
            value = load_manifest(path)
            self.assertEqual(value['来源名称'], '来源')
            with zipfile.ZipFile(path) as archive:
                entries = {name: archive.read(name) for name in archive.namelist()}
            for changes in ({'gitea-db.sql': b'changed'}, {'extra': b'extra'}, {'gitea-db.sql': None}):
                candidate = dict(entries, **changes)
                with zipfile.ZipFile(path, 'w') as archive:
                    for name, data in candidate.items():
                        if data is not None:
                            archive.writestr(name, data)
                with self.assertRaises(RuntimeError):
                    load_manifest(path)

    def test_live_worker_cannot_be_taken_over(self):
        from services.restore_recovery import lease
        from flask import Flask
        from models import db
        app = Flask('实际数据库锁隔离验证')
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        db.init_app(app)
        target = SimpleNamespace(host='隔离宿主', gitea_container='固定实例', pg_container='固定数据库', pg_dbname='gitea')
        client = MagicMock()
        client.containers.get.return_value.id = '固定数据库容器ID'
        with app.app_context(), tempfile.TemporaryDirectory() as root, patch('config.BACKUP_DIR', root + '/backups'), patch('services.docker_service.managed_client', return_value=client):
            db.create_all()
            first = lease(target)
            try:
                target.host = '同一数据库的另一个连接别名'
                with self.assertRaisesRegex(RuntimeError, '活跃恢复'):
                    lease(target)
            finally:
                first.close()
            lease(target).close()
            db.session.remove()

    def test_changed_hook_permission_cannot_pass_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'backup.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                entry = zipfile.ZipInfo('repos/test.git/hooks/reference-transaction')
                entry.create_system = 3
                entry.external_attr = 0o100755 << 16
                archive.writestr(entry, '#!/bin/sh\nexit 42\n')
            append_manifest(path, {}, {}, SimpleNamespace(name='来源', api_token='令牌'))
            with zipfile.ZipFile(path) as archive:
                values = [(info, archive.read(info)) for info in archive.infolist()]
            with zipfile.ZipFile(path, 'w') as archive:
                for info, content in values:
                    if info.filename.endswith('reference-transaction'):
                        info.external_attr = 0o100644 << 16
                    archive.writestr(info, content)
            with self.assertRaisesRegex(RuntimeError, '权限不一致'):
                load_manifest(path)

    def test_network_cleanup_waits_for_endpoint_view(self):
        from services.restore_isolation import retire_network
        database = MagicMock()
        database.attrs = {'NetworkSettings': {'Networks': {'isolated': {}}}}
        network = MagicMock()
        network.name = 'isolated'
        states = iter([{'database': {}}, {}])
        network.reload.side_effect = lambda: setattr(network, 'attrs', {'Containers': next(states)})
        with patch('services.restore_isolation.time.sleep'):
            self.assertTrue(retire_network(network, database))
        network.disconnect.assert_called_once_with(database)
        network.remove.assert_called_once()

    def test_network_cleanup_failure_does_not_keep_writer_alive(self):
        from services.restore_isolation import IsolatedGitea
        isolated = IsolatedGitea(None, None, MagicMock(), None, 'isolated')
        container = isolated.container = MagicMock()
        isolated.network = MagicMock()
        isolated.network.name = 'isolated'
        isolated.network.reload.side_effect = RuntimeError('验收注入网络视图失败')
        isolated.close()
        self.assertIsNone(isolated.container)
        container.stop.assert_called_once()
        container.remove.assert_called_once()

    def test_ssh_host_archive_rejects_escape_and_public_private_key(self):
        from services.restore_archive import validate_ssh_archive
        for name, mode in [('../escape', 0o600), ('ssh/ssh_host_ed25519_key', 0o644)]:
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode='w') as archive:
                entry = tarfile.TarInfo(name)
                entry.mode = mode
                entry.size = 1
                archive.addfile(entry, io.BytesIO(b'x'))
            with self.assertRaises(RuntimeError):
                validate_ssh_archive(raw.getvalue())


if __name__ == '__main__':
    unittest.main()
