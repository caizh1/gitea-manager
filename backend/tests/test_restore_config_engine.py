"""用交付镜像的真实Go配置工具验证启动覆盖，不依赖Python解析器自证。"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from services.restore_config import RestoreConfig, values, parse
from services.restore_secrets import SECRET_URI


@unittest.skipUnless(os.environ.get('GITEA_ENVIRONMENT_TO_INI'), '未提供交付镜像的Go配置工具')
class ActualConfigurationEngineTests(unittest.TestCase):
    def test_real_engine_keeps_source_login_flags_after_target_environment_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'root'; root.mkdir()
            (root / 'app.ini').write_text('APP_NAME = 合成站点\n[service]\nREQUIRE_SIGNIN_VIEW = true\nDISABLE_REGISTRATION = true\n'
                '[repository]\nDISABLE_HTTP_GIT = false\n[mailer]\nENABLED = true\nPASSWD = `合成#分号;密码`\n'
                '[security]\nINSTALL_LOCK = true\n[database]\nHOST = 源数据库:5432\n')
            config = RestoreConfig(root, folder, b'[database]\nHOST = target:5432\n', SECRET_URI)
            environment = config.environment({'GITEA__service__REQUIRE_SIGNIN_VIEW': 'false',
                'GITEA__service__DISABLE_REGISTRATION__FILE': '/ignored', 'GITEA__mailer__PASSWD': '目标值'})
            config.write()
            # 临时目录在此单元容器承担/data挂载，实际文件正文仍由产品生成。
            for key, value in tuple(environment.items()):
                if key.endswith('__FILE'):
                    environment[key] = str(config.directory / Path(value).name)
            result = subprocess.run([os.environ['GITEA_ENVIRONMENT_TO_INI'], '--config', str(config.directory / 'app.ini')],
                                    env=dict(os.environ, **environment), capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            config.verify((config.directory / 'app.ini').read_bytes())
            actual = values(parse((config.directory / 'app.ini').read_bytes()))
            self.assertEqual(actual['service', 'require_signin_view'], 'true')
            self.assertEqual(actual['mailer', 'passwd'], '合成#分号;密码')


if __name__ == '__main__':
    unittest.main()
