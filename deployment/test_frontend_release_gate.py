"""验证发版门禁拒绝错误镜像、过期源码与失败的静态检查。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import frontend_release_gate as gate


class FrontendReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.report = {'镜像': 'frontend:fixed', '镜像ID': 'sha256:fixed',
                       '源码与测试SHA256': {'页面': '固定摘要'}}

    def test_archive_checks_the_tested_image_identity(self):
        with patch.object(gate, 'archive_image_ids') as identity, \
                patch.object(gate, 'source_hashes', return_value={'页面': '固定摘要'}):
            gate.verify_archive(self.root / 'images.tar', self.report)
        identity.assert_called_once_with(self.root / 'images.tar',
                                         {'frontend': {'tag': 'frontend:fixed', 'ids': ['sha256:fixed']}})

    def test_changed_source_cannot_reuse_a_passing_result(self):
        with patch.object(gate, 'archive_image_ids'), \
                patch.object(gate, 'source_hashes', return_value={'页面': '新的摘要'}):
            with self.assertRaisesRegex(RuntimeError, '发生变化'):
                gate.verify_archive(self.root / 'images.tar', self.report)

    def test_archive_identity_failure_stops_release(self):
        with patch.object(gate, 'archive_image_ids', side_effect=RuntimeError('镜像身份不一致')):
            with self.assertRaisesRegex(RuntimeError, '镜像身份不一致'):
                gate.verify_archive(self.root / 'images.tar', self.report)

    def test_static_failure_stops_before_starting_any_container(self):
        failure = gate.subprocess.CompletedProcess(['npm'], 1, '根节点检查失败', '')
        with patch.object(gate, 'ROOT', self.root), patch.object(gate, 'source_hashes', return_value={}), \
                patch.object(gate.subprocess, 'run', return_value=failure) as run:
            with self.assertRaisesRegex(RuntimeError, '未通过'):
                gate.verify_frontend('frontend:fixed')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ['npm', 'run', 'test:route-roots'])
        records = list((self.root / 'work').glob('frontend-release-*/report.json'))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text())['状态'], '失败')

    def test_browser_failure_stops_release_and_removes_its_container(self):
        def run(args, **kwargs):
            if args[:3] == ['npm', 'run', 'test:route-roots']:
                return gate.subprocess.CompletedProcess(args, 0, '', '')
            if args[:3] == ['docker', 'image', 'inspect']:
                value = [{'Architecture': 'amd64', 'Os': 'linux', 'Id': 'sha256:fixed'}]
            elif args[:2] == ['docker', 'run']:
                return gate.subprocess.CompletedProcess(args, 0, 'isolated-container', '')
            elif args[:2] == ['docker', 'inspect']:
                value = [{'NetworkSettings': {'Ports': {'80/tcp': [{'HostPort': '12345'}]}}}]
            elif args[0] == 'node':
                return gate.subprocess.CompletedProcess(args, 1, '导航页面空白', '')
            else:
                return gate.subprocess.CompletedProcess(args, 0, '', '')
            return gate.subprocess.CompletedProcess(args, 0, json.dumps(value), '')

        with patch.object(gate, 'ROOT', self.root), patch.object(gate, 'source_hashes', return_value={}), \
                patch.object(gate.subprocess, 'run', side_effect=run) as commands, \
                patch.object(gate, 'build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.status = 200
            with self.assertRaisesRegex(RuntimeError, '未通过'):
                gate.verify_frontend('frontend:fixed')
        self.assertIn(['docker', 'rm', '-f', 'isolated-container'],
                      [call.args[0] for call in commands.call_args_list])
        report = next((self.root / 'work').glob('frontend-release-*/report.json'))
        self.assertEqual(json.loads(report.read_text())['状态'], '失败')


if __name__ == '__main__':
    unittest.main()
