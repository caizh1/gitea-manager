"""回归真实 Git 的可执行权限，以及归档路径和全部引用校验。"""
import io
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.restore_archive import validate_members, restore_modes


class PhaseOneTests(unittest.TestCase):
    def test_hook_still_blocks_real_git_after_zip_restore(self):
        if not shutil.which('git'):
            self.skipTest('执行环境没有 Git')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / 'repo.git'
            def git(*args, check=True):
                return subprocess.run(['git', '--git-dir', str(repo), *args], check=check, capture_output=True)
            git('init', '--bare')
            blob = subprocess.run(['git', '--git-dir', str(repo), 'hash-object', '-w', '--stdin'],
                input='阶段一验收'.encode(), check=True, capture_output=True).stdout.decode().strip()
            raw = io.BytesIO()
            with zipfile.ZipFile(raw, 'w') as archive:
                info = zipfile.ZipInfo('repo.git/hooks/reference-transaction')
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o751) << 16
                archive.writestr(info, '#!/bin/sh\nif [ "$1" = prepared ]; then exit 42; fi\n')
            raw.seek(0)
            with zipfile.ZipFile(raw) as archive:
                members = archive.infolist()
                validate_members(members)
                archive.extractall(root)
                self.assertEqual(git('update-ref', 'refs/tags/before-fix', blob, check=False).returncode, 0)
                restore_modes(root, members)
                self.assertNotEqual(git('update-ref', 'refs/tags/after-fix', blob, check=False).returncode, 0)
                self.assertEqual((repo/'hooks/reference-transaction').stat().st_mode & 0o777, 0o751)

    def test_unsafe_archive_rejected(self):
        for name, mode in [('../escape', stat.S_IFREG), ('/absolute', stat.S_IFREG),
                           ('link', stat.S_IFLNK), ('device', stat.S_IFCHR)]:
            with self.subTest(name=name):
                entry = zipfile.ZipInfo(name)
                entry.external_attr = (mode | 0o777) << 16
                with self.assertRaises(RuntimeError): validate_members([entry])
        with self.assertRaises(RuntimeError):
            validate_members([zipfile.ZipInfo('file'), zipfile.ZipInfo('file')])

    def test_non_default_ref_difference_cannot_pass(self):
        from services.backup_consistency import verify_snapshot
        expected = {'数据库仓库': [], 'Git仓库': {'owner/repo.git': {'引用': ['refs/tags/release old']}}}
        actual = {'数据库仓库': [], 'Git仓库': {'owner/repo.git': {'引用': ['refs/tags/release new']}}}
        with self.assertRaisesRegex(RuntimeError, 'Git 引用'):
            verify_snapshot(expected, actual, [])

    def test_nonpersistent_data_and_wrong_database_rejected_before_stop(self):
        from services.backup_consistency import check_layout
        gitea = MagicMock()
        gitea.attrs = {'Config': {'Env': ['GITEA_CUSTOM=/data/gitea']}, 'HostConfig': {}, 'Mounts': [{'Destination': '/data', 'RW': True}],
                       'NetworkSettings': {'Networks': {'default': {}}}}
        database = SimpleNamespace(name='db', id='db-id', attrs={'HostConfig': {},
            'NetworkSettings': {'Networks': {'default': {'Aliases': ['db'], 'IPAddress': '10.0.0.2'}}}})
        server = SimpleNamespace(pg_dbname='gitea', pg_user='gitea')
        base = '[database]\nDB_TYPE=postgres\nHOST=db:5432\nNAME=gitea\nUSER=gitea\n'
        for config, message in [(base, '持久化'), (base.replace('NAME=gitea', 'NAME=other'), '不一致')]:
            with patch('services.backup_consistency.read_file', return_value=(config.encode(), None)):
                with self.assertRaisesRegex(RuntimeError, message):
                    check_layout(gitea, database, server)
        gitea.stop.assert_not_called()
        with patch('services.backup_consistency.read_file', return_value=((base + '[server]\nAPP_DATA_PATH=/data/gitea\n').encode(), None)):
            check_layout(gitea, database, server)
            with self.assertRaisesRegex(RuntimeError, '仓库根目录'):
                check_layout(gitea, database, server, restoring=True)
            gitea.attrs['Config']['Env'] = ['GITEA_CUSTOM=/data/custom']
            with self.assertRaisesRegex(RuntimeError, 'custom'):
                check_layout(gitea, database, server)
        gitea.attrs['Config']['Env'] = []
        for extra in ('SSH_ROOT_PATH=/data/other-ssh', 'SSH_AUTHORIZED_PRINCIPALS_ALLOW=username'):
            with patch('services.backup_consistency.read_file', return_value=((base + '[server]\nAPP_DATA_PATH=/data/gitea\n' + extra).encode(), None)):
                with self.assertRaisesRegex(RuntimeError, 'SSH'):
                    check_layout(gitea, database, server)
        gitea.stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
