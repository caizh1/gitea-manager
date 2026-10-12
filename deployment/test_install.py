"""安装器确定性边界回归；不创建 Docker 资源。"""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import socket
import subprocess
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('installer', Path(__file__).with_name('install.py'))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.manifest = {'product': 'manager', 'images': {key: {'tag': key} for key in ('backend', 'frontend', 'executor')}}
        self.args = argparse.Namespace(host='127.0.0.1', port=35480, ssh_port=None)

    def fresh(self):
        installer = MODULE.Installer(self.root, self.manifest)
        installer.initialize_state(self.args)
        MODULE.private_write(installer.state_file, installer.state)
        installer.prepare()
        return installer

    def test_invalid_address_and_ports(self):
        for value in ('http://host', 'host:123', 'host/path', 'a b', '1.2.3.999', '0.0.0.0', '224.0.0.1', '127.0.0.2', '::1', 'a$B'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MODULE.validate_host(value)
        for value in ('0', '65536', '-1', '1.0', 'abc'):
            with self.assertRaises(ValueError):
                MODULE.validate_port(value)
        self.assertEqual(MODULE.validate_host('git.example.com'), 'git.example.com')

    def test_rerun_preserves_all_secrets_and_project(self):
        first = self.fresh()
        before = first.state_file.read_bytes()
        second = MODULE.Installer(self.root, self.manifest)
        second.initialize_state(self.args)
        second.prepare()
        self.assertEqual(before, second.state_file.read_bytes())
        self.assertEqual(first.state['project'], second.state['project'])
        self.assertEqual((self.root / 'state/master.key').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / 'state/executor.token').stat().st_mode & 0o777, 0o444)
        self.assertEqual((self.root / 'state').stat().st_mode & 0o777, 0o700)

    def test_missing_or_modified_key_does_not_regenerate(self):
        installer = self.fresh()
        key = self.root / 'state/master.key'
        key.write_text('不能替换原密钥')
        with self.assertRaises(RuntimeError):
            installer.prepare()
        self.assertEqual(key.read_text(), '不能替换原密钥')

    def test_changed_config_or_directory_is_rejected(self):
        installer = self.fresh()
        (self.root / 'compose.install.json').write_text('{}')
        with self.assertRaises(RuntimeError):
            installer.prepare()
        installer.state['directory'] = '/another-directory'
        with self.assertRaises(RuntimeError):
            installer.initialize_state(self.args)

    def test_first_install_entry_cannot_silently_upgrade(self):
        self.fresh()
        self.manifest['images']['backend']['tag'] = '不同版本'
        installer = MODULE.Installer(self.root, self.manifest)
        with self.assertRaisesRegex(RuntimeError, '升级流程'):
            installer.initialize_state(self.args)

    def test_manager_network_secrets_and_external_integration(self):
        installer = self.fresh()
        config = json.loads((self.root / 'compose.install.json').read_text())
        services = config['services']
        self.assertEqual(len(services), 7)
        self.assertEqual(services['backend']['environment']['INTEGRATION_ENABLED'], 'false')
        self.assertNotIn('INTEGRATION_SERVICE_TOKEN', services['backend']['environment'])
        self.assertNotIn('ports', services['backend'])
        for name in ('review-worker', 'review-progress', 'review-feedback', 'review-executor'):
            self.assertNotIn('/var/run/docker.sock:/var/run/docker.sock', services[name].get('volumes', []))
        self.assertTrue(config['networks']['review-execution']['internal'])
        self.assertEqual(services['review-executor']['networks'], ['review-execution'])
        authorization = 'access_authorization'
        self.assertNotEqual(installer.state['access_authorization_token'], installer.state['executor_token'])
        for name in ('backend', 'review-worker', 'review-progress', 'review-feedback'):
            self.assertIn(authorization, services[name]['secrets'])
            self.assertEqual(services[name]['environment']['AI_REVIEW_AUTHORIZATION_URL'],
                             'http://backend:5000/api/internal/authorization/check')
        self.assertNotIn(authorization, services['review-executor']['secrets'])
        self.assertNotIn('AI_REVIEW_AUTHORIZATION_TOKEN_FILE', services['review-executor']['environment'])
        self.assertEqual((self.root / 'state/access-authorization.token').stat().st_mode & 0o777, 0o600)

        self.assertEqual(services['review-executor']['volumes'][0]['target'], '/app/review-native')
        for service in services.values():
            for mount in service.get('volumes', []):
                if isinstance(mount, dict):
                    self.assertTrue(mount['volume']['nocopy'])

    def test_gitea_database_private_and_install_locked(self):
        self.manifest = {'product': 'gitea', 'images': {key: {'tag': key} for key in ('gitea', 'database')}}
        self.args.ssh_port = 32222
        installer = self.fresh()
        config = MODULE.configuration(installer.state, self.manifest, self.root)
        self.assertNotIn('ports', config['services']['database'])
        self.assertEqual(config['services']['gitea']['environment']['GITEA__security__INSTALL_LOCK'], 'true')
        self.assertNotEqual(installer.state['admin_password'], installer.state['database_password'])

    def test_https_certificate_pair_and_mounts_are_preserved(self):
        certificate, private_key = self.root/'fixture-cert.pem', self.root/'fixture-key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-subj','/CN=localhost','-keyout',str(private_key),'-out',str(certificate)],
            check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.args.tls_cert, self.args.tls_key, self.args.tls_port = str(certificate), str(private_key), 35443
        installer = self.fresh()
        config = json.loads((self.root/'compose.install.json').read_text())
        self.assertEqual(installer.state['url'], 'https://127.0.0.1:35443')
        self.assertIn('127.0.0.1:35443:443', config['services']['frontend']['ports'])
        self.assertEqual(config['services']['backend']['environment']['TRUSTED_PROXY_HOSTS'], 'frontend')
        self.assertEqual((self.root/'state/tls/key.pem').read_bytes(), private_key.read_bytes())
        installer.prepare()
        (self.root/'state/tls/key.pem').unlink()
        with self.assertRaisesRegex(RuntimeError, '丢失'):
            installer.prepare()
        self.assertFalse((self.root/'state/tls/key.pem').exists())

    def test_https_requires_matching_certificate_and_key(self):
        self.args.tls_cert = str(self.root/'missing-cert.pem')
        with self.assertRaisesRegex(RuntimeError, '同时指定'):
            MODULE.Installer(self.root,self.manifest).initialize_state(self.args)

    def test_failed_atomic_write_preserves_original(self):
        target = self.root / 'sample.json'
        MODULE.private_write(target, {'结果': '原始状态'})
        with patch.object(MODULE.os, 'replace', side_effect=OSError('模拟断电')):
            with self.assertRaises(OSError):
                MODULE.private_write(target, {'结果': '不应生效'})
        self.assertEqual(json.loads(target.read_text()), {'结果': '原始状态'})

    def test_occupied_port_is_rejected_before_persisting_credentials(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            self.args.port = listener.getsockname()[1]
            installer = MODULE.Installer(self.root, self.manifest)
            installer.initialize_state(self.args)
            with patch.object(MODULE, 'run', return_value=''), self.assertRaisesRegex(RuntimeError, '已占用'):
                installer.check_ports()
            self.assertFalse(installer.state_file.exists())

    def test_corrupt_package_stops_before_any_installation(self):
        import hashlib
        import shutil
        shutil.copy2(Path(__file__).resolve().parents[1] / 'install.sh', self.root / 'install.sh')
        (self.root / 'installer-manifest.json').write_text('{}')
        (self.root / 'sample.txt').write_text('校验前的内容')
        digest = hashlib.sha256((self.root / 'sample.txt').read_bytes()).hexdigest()
        (self.root / 'SHA256SUMS').write_text(digest + '  sample.txt\n')
        (self.root / 'sample.txt').write_text('校验后被篡改的内容')
        result = subprocess.run(['bash', str(self.root / 'install.sh'), '--desktop-test'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'FAILED', result.stdout + result.stderr)
        self.assertFalse((self.root / 'state/installation.json').exists())
        self.assertTrue(list((self.root / 'state/diagnostics').glob('diagnostics-*.tar.gz')))

    def test_reuse_requires_exact_image_identity(self):
        self.manifest['images'] = {'backend': {'tag': 'sample:fixed', 'ids': ['sha256:expected']}}
        installer = MODULE.Installer(self.root, self.manifest)
        correct = json.dumps([{'Id': 'sha256:expected', 'Architecture': 'amd64', 'Os': 'linux'}])
        with patch.object(MODULE, 'run', side_effect=['sample:fixed\n', correct]) as command:
            installer.load_images()
        self.assertFalse(any('load' in call.args[0] for call in command.call_args_list))
        wrong = json.dumps([{'Id': 'sha256:unexpected', 'Architecture': 'amd64', 'Os': 'linux'}])
        with patch.object(MODULE, 'run', side_effect=['sample:fixed\n', wrong, '', wrong]), self.assertRaisesRegex(RuntimeError, '镜像身份'):
            installer.load_images()


if __name__ == '__main__':
    unittest.main()
