"""密钥来源、环境覆盖和容器回退的确定性回归。"""
import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.restore_secrets import archive_secret, security_values, uri_path, normalized_environment, SecretTransition, SECRET_URI


class RestoreSecretTests(unittest.TestCase):
    def test_ini_and_file_and_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'app.ini').write_text('[security]\nSECRET_KEY = `旧实例的密钥`\n')
            self.assertEqual(archive_secret(root), '旧实例的密钥')
            (root/'app.ini').write_text('[security]\nSECRET_KEY =\n')
            self.assertEqual(archive_secret(root), '!#@FDEWREWR&*(')
            (root/'app.ini').write_text('[security]\nSECRET_KEY_URI=file:/data/gitea/custom-key\n')
            (root/'data').mkdir()
            (root/'data/custom-key').write_text('文件密钥\n')
            self.assertEqual(archive_secret(root), '文件密钥')
            (root/'app.ini').write_text('[security]\nSECRET_KEY_URI=file:/run/secrets/external\n')
            with self.assertRaisesRegex(RuntimeError, '外置'):
                archive_secret(root)
            (root/'manager-secrets.json').write_text(json.dumps({'配置摘要': hashlib.sha256((root/'app.ini').read_bytes()).hexdigest(), '数据加密密钥': '外置密钥'}))
            self.assertEqual(archive_secret(root), '外置密钥')
            (root/'app.ini').write_text('[security]\nSECRET_KEY=其他\n')
            with self.assertRaisesRegex(RuntimeError, '不一致'):
                archive_secret(root)

    def test_reject_ambiguous_config_and_unsafe_uri(self):
        with self.assertRaises(RuntimeError):
            security_values(b'[security]\nSECRET_KEY=x\nSECRET_KEY_URI=file:/x\n')
        for uri in ('https://example.test/key', 'file:relative', 'file:/data/../key', 'file://host/key'):
            with self.assertRaises(RuntimeError): uri_path(uri)
        self.assertEqual(uri_path('file:///run/secrets/key'), '/run/secrets/key')
        self.assertEqual(uri_path('file:/run/secrets/key%20name'), '/run/secrets/key%20name')
        self.assertEqual(security_values(b'[security]\nSECRET_KEY=abc;comment\n'), ('abc', ''))
        self.assertEqual(security_values(b'[security]\nSECRET_KEY=`abc;#def` ;comment\n'), ('abc;#def', ''))
        self.assertEqual(security_values(b"[security]\nSECRET_KEY='abc'\n"), ('abc', ''))
        with self.assertRaises(RuntimeError):
            security_values(b'[security]\nSECRET_KEY=%(other)s\n')

    def test_remove_all_override_forms_preserve_other_env(self):
        env = {'GITEA__SECURITY__SECRET_KEY': '旧密钥', 'GITEA__security__SECRET_KEY__FILE': '/old',
               'GITEA__security__SECRET_KEY_URI__FILE': '/old-uri', 'GITEA__database__HOST': 'db:5432'}
        normalized = normalized_environment(env)
        self.assertEqual(normalized, {'GITEA__security__SECRET_KEY': '', 'GITEA__security__SECRET_KEY_URI': SECRET_URI,
                                      'GITEA__database__HOST': 'db:5432'})

    def test_no_recreate_for_ini_or_existing_managed_uri(self):
        for env in ([], ['GITEA__security__SECRET_KEY_URI='+SECRET_URI]):
            container = MagicMock()
            container.attrs = {'Config': {'Env': env}}
            transition = SecretTransition(MagicMock(), container, '隔离')
            self.assertFalse(transition.needed)
            self.assertIs(transition.switch(), container)

    def test_container_recreation_preserves_volume_and_published_port(self):
        client, original = MagicMock(), MagicMock()
        original.name, original.id, original.short_id = 'gitea', '原容器', '原短标识'
        original.attrs = {'Image': 'sha256:固定镜像', 'Config': {'Env': ['GITEA__security__SECRET_KEY=旧密钥']},
            'HostConfig': {'Binds': None, 'PortBindings': {'3000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '34567'}]}},
            'Mounts': [{'Type': 'volume', 'Name': '已有匿名卷', 'Destination': '/data', 'RW': True}],
            'NetworkSettings': {'Networks': {}, 'Ports': {'3000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '34567'}]}}}
        client.api._result.return_value = {'Id': '新容器'}
        transition = SecretTransition(client, original, '隔离')
        transition.switch()
        config = client.api._post_json.call_args.kwargs['data']
        self.assertEqual(config['HostConfig']['Binds'], ['已有匿名卷:/data:rw'])
        self.assertEqual(config['HostConfig']['PortBindings']['3000/tcp'][0]['HostPort'], '34567')
        self.assertNotIn('GITEA__security__SECRET_KEY=旧密钥', config['Env'])
        transition.rollback()
        original.rename.assert_called_with('gitea')

    def test_dynamic_ports_fail_before_stopping_target(self):
        original = MagicMock()
        original.attrs = {'HostConfig': {'PortBindings': {'3000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': ''}]}}}
        with self.assertRaisesRegex(RuntimeError, '随机宿主端口'):
            SecretTransition(MagicMock(), original, '隔离')
        original.stop.assert_not_called()

    def test_compose_shared_anchor_does_not_change_other_service(self):
        original = MagicMock()
        original.attrs = {'Image': '固定镜像', 'HostConfig': {}, 'Config': {
            'Env': ['GITEA__security__SECRET_KEY=旧密钥'], 'Labels': {
                'com.docker.compose.project': '隔离', 'com.docker.compose.service': 'gitea',
                'com.docker.compose.project.config_files': '/srv/compose.yaml'}}}
        raw = b'services:\n  other: &shared\n    environment:\n      GITEA__security__SECRET_KEY: old\n  gitea: *shared\n'
        transition = SecretTransition(MagicMock(), original, '隔离')
        with patch('services.restore_secrets.read_file', return_value=(raw, MagicMock())):
            transition.prepare()
        parsed = yaml.safe_load(transition.compose_new)
        self.assertEqual(parsed['services']['other']['environment']['GITEA__security__SECRET_KEY'], 'old')
        self.assertEqual(parsed['services']['gitea']['environment']['GITEA__security__SECRET_KEY'], '')


if __name__ == '__main__': unittest.main()
