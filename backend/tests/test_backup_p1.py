"""验证三项高优先级修复的路径准入、启动依赖与授权回退边界。"""
import base64
import configparser
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from services.backup_actions import actions_path, validate_external, verify_artifacts
from services.backup_manifest import storage_profile
from services.backup_dependencies import DEPENDENCIES, append_startup_files, install_startup_files


class BackupP1Tests(unittest.TestCase):
    def test_actions_legacy_and_named_override(self):
        for section in ('actions.artifacts', 'storage.actions_artifacts'):
            raw = ('[' + section + ']\nPATH=/data/actions-external').encode()
            self.assertEqual(actions_path(raw), '/data/actions-external')
            self.assertIn(section, storage_profile(raw))
        self.assertEqual(actions_path(b'[storage]\nPATH=/data/gitea\n[actions.artifacts]\nPATH=custom'), '/data/gitea/custom')
        self.assertEqual(actions_path(b'[storage.actions_artifacts]\nPATH=/data/other\n[actions.artifacts]\nPATH=/data/actions-external'), '/data/actions-external')

    def test_actions_rejects_external_backend_and_overlapping_path(self):
        for raw in (b'[actions.artifacts]\nSTORAGE_TYPE=minio', b'[actions.artifacts]\nPATH=/tmp/body',
                    b'[actions.artifacts]\nPATH=/data/git', b'[storage.local]\nPATH=/data/gitea'):
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                storage_profile(raw)
        for path in ('/data/ssh', '/data/gitea', '/data/git', '/data/manager-restore-abc', '/data/x/../git'):
            with self.assertRaises(RuntimeError):
                validate_external(path)

    def test_actions_body_mismatch_and_escape_are_rejected(self):
        with patch('services.backup_consistency.execute', return_value='b' * 64):
            with self.assertRaisesRegex(RuntimeError, '不一致'):
                verify_artifacts(None, {'路径': '/data/actions-external', '文件': {'body': 'a' * 64}})
            with self.assertRaisesRegex(RuntimeError, '不安全'):
                verify_artifacts(None, {'路径': '/data/actions-external', '文件': {'../escape': 'a' * 64}})

    def test_startup_secret_installed_before_start_and_survives_restore(self):
        body = '固定外置启动秘密'.encode()
        value = {'版本': 1, '文件': [{'配置节': 'oauth2', '配置项': 'JWT_SECRET', '内容': base64.b64encode(body).decode()}]}
        config = configparser.ConfigParser(interpolation=None)
        config.optionxform = str
        config.read_string('[oauth2]\nJWT_SECRET_URI=file:/external/oauth\nJWT_SECRET=')
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'backup.zip')
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('gitea-db.sql', '')
            append_startup_files(path, value)
            with zipfile.ZipFile(path) as archive, patch('services.backup_dependencies.put_file') as put:
                install_startup_files(archive, config, None)
                put.assert_called_once_with(None, '/data/gitea/conf/manager-startup-oauth2-jwt_secret', body, 0o600, 1000, 1000)
                self.assertEqual(archive.read('data/conf/manager-startup-oauth2-jwt_secret'), body)
                self.assertEqual(config.get('oauth2', 'JWT_SECRET_URI'), 'file:/data/gitea/conf/manager-startup-oauth2-jwt_secret')

    def test_startup_dependency_cannot_choose_arbitrary_destination(self):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            archive.writestr(DEPENDENCIES, json.dumps({'版本': 1, '文件': [{'配置节': '../escape', '配置项': 'JWT_SECRET', '内容': 'eA=='}]}))
        with zipfile.ZipFile(raw) as archive, self.assertRaises(RuntimeError):
            install_startup_files(archive, configparser.ConfigParser(), None)

