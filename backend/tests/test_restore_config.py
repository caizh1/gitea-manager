"""源业务策略、目标部署参数和启动覆盖的真实配置合并回归。"""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import yaml

from services.restore_config import RestoreConfig, parse, values
from services.restore_secrets import SECRET_URI, SecretTransition


SOURCE = b'''APP_NAME = source
[server]
DOMAIN = source.test
HTTP_PORT = 3000
ROOT_URL = http://source.test/
[database]
HOST = source-db:5432
NAME = source
USER = source
PASSWD = source-password
[service]
REQUIRE_SIGNIN_VIEW = true
DISABLE_REGISTRATION = false
[mailer]
ENABLED = true
SMTP_ADDR = mail.source.test
PASSWD = `source;#password`
[webhook]
ALLOWED_HOST_LIST = private
[migrations]
ALLOW_LOCALNETWORKS = true
'''
TARGET = b'''APP_NAME = target
[server]
DOMAIN = target.test
HTTP_PORT = 4443
ROOT_URL = https://target.test/
PROTOCOL = https
[database]
HOST = target-db:5432
NAME = target
USER = target
PASSWD = target-password
[service]
REQUIRE_SIGNIN_VIEW = false
DISABLE_REGISTRATION = true
EXTRA_TARGET_ONLY = true
[mailer]
ENABLED = false
'''


class RestoreConfigurationTests(unittest.TestCase):
    def test_quoted_business_values_keep_comment_characters(self):
        self.assertEqual(values(parse(b'[mailer]\nPASSWD="a#b;c"\n'))['mailer', 'passwd'], 'a#b;c')

    def test_go_default_section_environment_uses_source_global_value(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'root'; root.mkdir()
            (root / 'app.ini').write_text('APP_NAME=源站点\n')
            config = RestoreConfig(root, folder, b'', SECRET_URI)
            self.assertEqual(config.environment({'GITEA____APP_NAME': '目标站点'})['GITEA____APP_NAME'], '源站点')
    def test_go_ini_keys_are_canonical_uppercase(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'root'; root.mkdir()
            (root / 'app.ini').write_text('[service]\nREQUIRE_SIGNIN_VIEW=true\n')
            config = RestoreConfig(root, folder, b'', SECRET_URI)
            config.write()
            raw = (root / 'data/conf/app.ini').read_text()
            self.assertIn('REQUIRE_SIGNIN_VIEW = true', raw)
            self.assertNotIn('require_signin_view =', raw)
    def make(self, folder, source=SOURCE):
        root = Path(folder)
        (root / 'app.ini').write_bytes(source)
        return RestoreConfig(root, root, TARGET, SECRET_URI)

    def test_preserve_source_policy_and_target_connection_only(self):
        with tempfile.TemporaryDirectory() as folder:
            restored = self.make(folder)
            restored.write()
            raw = (Path(folder) / 'data/conf/app.ini').read_bytes()
            data = values(parse(raw))
            self.assertEqual(data[('service', 'require_signin_view')], 'true')
            self.assertEqual(data[('service', 'disable_registration')], 'false')
            self.assertEqual(data[('mailer', 'smtp_addr')], 'mail.source.test')
            self.assertEqual(data[('mailer', 'passwd')], 'source;#password')
            self.assertEqual(data[('webhook', 'allowed_host_list')], 'private')
            self.assertEqual(data[('migrations', 'allow_localnetworks')], 'true')
            self.assertEqual(data[('database', 'passwd')], 'target-password')
            self.assertEqual(data[('server', 'root_url')], 'https://target.test/')
            self.assertEqual(data[('server', 'protocol')], 'https')
            self.assertEqual(data[('default', 'app_name')], 'source')
            self.assertNotIn(('service', 'app_name'), data)
            self.assertNotIn(('service', 'extra_target_only'), data)
            restored.verify(raw)
            changed = raw.replace(b'REQUIRE_SIGNIN_VIEW = true', b'REQUIRE_SIGNIN_VIEW = false')
            self.assertNotEqual(changed, raw, '反例必须实际修改登录策略')
            with self.assertRaisesRegex(RuntimeError, '拒绝发布'):
                restored.verify(changed)

    def test_target_absent_deployment_value_does_not_inherit_source_endpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            source = SOURCE + b'\n[SERVER_EXTRA]\nVALUE = yes\n'
            restored = self.make(folder, source.replace(b'HTTP_PORT = 3000', b'HTTP_PORT = 3000\nSSH_PORT = 2000'))
            self.assertNotIn(('server', 'ssh_port'), restored.expected)

    def test_environment_and_file_forms_cannot_restore_target_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            restored = self.make(folder)
            env = {'GITEA__service__REQUIRE_SIGNIN_VIEW': 'false', 'GITEA__MAILER__PASSWD__FILE': '/old',
                   'GITEA__database__HOST': 'target-db:5432', 'USER_UID': '1000'}
            changed = restored.environment(env)
            self.assertEqual(changed['GITEA__service__REQUIRE_SIGNIN_VIEW'], 'true')
            name = Path(changed['GITEA__MAILER__PASSWD__FILE']).name
            self.assertEqual((Path(folder) / 'data/conf' / name).read_text(), 'source;#password')
            self.assertEqual(changed['GITEA__database__HOST'], env['GITEA__database__HOST'])
            self.assertEqual(changed['USER_UID'], '1000')

    def test_compose_anchor_and_inherited_file_override_are_migrated(self):
        with tempfile.TemporaryDirectory() as folder:
            restored = self.make(folder)
            container = MagicMock()
            container.attrs = {'Image': '固定镜像', 'HostConfig': {}, 'Config': {'Env': [
                'GITEA__service__REQUIRE_SIGNIN_VIEW=false'], 'Labels': {
                'com.docker.compose.project': '隔离', 'com.docker.compose.service': 'gitea',
                'com.docker.compose.project.config_files': '/srv/compose.yaml'}}}
            transition = SecretTransition(MagicMock(), container, '隔离')
            transition.configure_business(restored)
            raw = b'''services:
  other: &shared
    environment:
      GITEA__service__REQUIRE_SIGNIN_VIEW: 'false'
      GITEA__mailer__PASSWD__FILE: /old
  gitea: *shared
'''
            with patch('services.restore_secrets.read_file', return_value=(raw, MagicMock())):
                transition.prepare()
            parsed = yaml.safe_load(transition.compose_new)
            self.assertEqual(parsed['services']['other']['environment']['GITEA__service__REQUIRE_SIGNIN_VIEW'], 'false')
            current = parsed['services']['gitea']['environment']
            self.assertEqual(current['GITEA__service__REQUIRE_SIGNIN_VIEW'], 'true')
            self.assertTrue(current['GITEA__mailer__PASSWD__FILE'].startswith('/data/gitea/conf/manager-restored-env-'))
            self.assertTrue(transition.needed)

    def test_startup_uri_uses_matching_archived_body(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data/conf').mkdir(parents=True)
            body = b'source-signing-key'
            (root / 'data/conf/manager-startup-oauth2-jwt_secret').write_bytes(body)
            (root / 'manager-startup-files.json').write_text(json.dumps({'版本': 1, '文件': [
                {'配置节': 'oauth2', '配置项': 'JWT_SECRET', '内容': base64.b64encode(body).decode()}]}))
            restored = self.make(folder, SOURCE + b'\n[oauth2]\nJWT_SECRET_URI=file:/external/key\n')
            self.assertEqual(restored.expected[('oauth2', 'jwt_secret_uri')], 'file:/data/gitea/conf/manager-startup-oauth2-jwt_secret')


if __name__ == '__main__':
    unittest.main()
