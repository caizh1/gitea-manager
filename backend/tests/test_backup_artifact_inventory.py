"""制品清单优化后仍按路径和实际正文验证完整备份。"""
import hashlib
from io import BytesIO
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import zipfile

from services.backup_actions import append_indexed_artifacts
from services.backup_manifest import append_manifest, load_manifest
from services.backup_metadata import read_zip_json


class ArtifactInventoryTests(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.zip = self.root / 'backup.zip'
        self.body = b'package-123'
        self.inventory = {'workflow/package': {'size': len(self.body), 'sha256': hashlib.sha256(self.body).hexdigest()}}
        self.staged = {'directory': '/data/gitea/actions_artifacts', 'records': ['workflow/package']}
        self.member = 'data/actions_artifacts/workflow/package'

    def write_zip(self, members):
        with zipfile.ZipFile(self.zip, 'w') as archive:
            archive.writestr('gitea-db.sql', '数据库快照')
            for name, body in members.items():
                archive.writestr(name, body)

    def finalize(self, checks, progress=None):
        append_manifest(self.zip, {}, {}, SimpleNamespace(name='验收源', api_token='验收令牌'),
                        progress=progress, artifact_checks=checks)

    def test_valid_files_and_original_missing_notice_persist_in_archive(self):
        self.write_zip({self.member: self.body})
        self.staged['records'] += ['workflow/missing', 'workflow/missing']
        checks, warning = append_indexed_artifacts(self.zip, self.staged, self.inventory)
        self.assertEqual(warning, {'count': 1, 'examples': ['workflow/missing']})
        progress = Mock()
        captured = []
        def record(*args, **kwargs):
            captured.append(dict(kwargs['artifacts']))
        progress.show.side_effect = record
        self.finalize(checks, progress)
        self.assertEqual(captured[-1], {'files_done': 1, 'files_total': 1, 'bytes_done': len(self.body),
                                        'bytes_total': len(self.body)})
        self.assertEqual(load_manifest(self.zip)['文件'][self.member]['摘要'], self.inventory['workflow/package']['sha256'])
        with zipfile.ZipFile(self.zip) as archive:
            snapshot = read_zip_json(archive, 'manager-actions.json')
            self.assertEqual(snapshot['源端缺失'], ['workflow/missing'])
            self.assertEqual(archive.read(self.member), self.body)

    def test_archive_missing_or_unexpected_member_fails(self):
        for members in ({}, {self.member: self.body, 'data/actions_artifacts/unexpected': b''}):
            with self.subTest(文件数=len(members)):
                self.write_zip(members)
                with self.assertRaisesRegex(RuntimeError, '文件集合'):
                    append_indexed_artifacts(self.zip, self.staged, self.inventory)

    def test_same_size_corruption_is_rejected_in_manifest_pass(self):
        self.write_zip({self.member: b'wrong------'})
        checks, _ = append_indexed_artifacts(self.zip, self.staged, self.inventory)
        with self.assertRaisesRegex(RuntimeError, '制品摘要'):
            self.finalize(checks)

    def test_external_archive_is_appended_and_verified(self):
        self.write_zip({})
        path = self.root / 'actions.tar'
        with tarfile.open(path, mode='w') as archive:
            member = tarfile.TarInfo('external/workflow/package')
            member.size = len(self.body)
            member.mode = 0o640
            archive.addfile(member, BytesIO(self.body))
        self.staged.update(directory='/data/external', archive_path=path)
        checks, warning = append_indexed_artifacts(self.zip, self.staged, self.inventory)
        self.assertIsNone(warning)
        self.finalize(checks)
        with zipfile.ZipFile(self.zip) as archive:
            member = archive.getinfo('manager-actions-artifacts/workflow/package')
            self.assertEqual(member.external_attr >> 16 & 0o777, 0o640)
            self.assertEqual(archive.read(member), self.body)

    def test_missing_source_directory_has_empty_inventory_and_warning(self):
        self.write_zip({})
        checks, warning = append_indexed_artifacts(self.zip, self.staged, {})
        self.assertEqual(checks, {})
        self.assertEqual(warning['count'], 1)
        self.finalize(checks)
        self.assertIsNotNone(load_manifest(self.zip))
