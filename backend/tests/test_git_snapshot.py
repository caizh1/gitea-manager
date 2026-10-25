"""代码快照的凭据边界、引用一致性与失败回执测试。"""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from git_migration.git_snapshot import GitSnapshot, parse_refs
from svn_migration.common import MigrationError


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.provider = SimpleNamespace(kind='gitea', auth_method='password', secret='不可泄露的密码',
            actor={'username': 'source-admin'}, credentials=Mock(return_value={}),
            get=Mock(return_value={'id': 1, 'full_name': 'team/repository'}),
            http=SimpleNamespace(base='https://source.test/subpath', verify=True))
        self.snapshot = GitSnapshot(self.provider, {'id': 1, 'path': 'team/repository'}, Path(self.folder.name) / 'snapshot')

    def test_authentication_is_url_scoped_and_proxy_is_disabled(self):
        env = self.snapshot.environment()
        config = {env['GIT_CONFIG_KEY_' + str(i)]: env['GIT_CONFIG_VALUE_' + str(i)] for i in range(int(env['GIT_CONFIG_COUNT']))}
        self.assertNotIn('http.extraHeader', config)
        self.assertIn('http.https://source.test/subpath/team/repository.git.extraHeader', config)
        self.assertEqual(config['http.followRedirects'], 'false')
        self.assertEqual(config['protocol.allow'], 'never')
        self.assertEqual(env['HTTPS_PROXY'], '')
        self.assertEqual(config['lfs.url'], 'https://source.test/subpath/team/repository.git/info/lfs')

    def test_command_error_never_echoes_source_output(self):
        self.snapshot.process.run = Mock(side_effect=MigrationError('错误包含不可泄露的密码', 'command'))
        with self.assertRaises(MigrationError) as result:
            self.snapshot.git('ls-remote', self.snapshot.url)
        self.assertNotIn(self.provider.secret, str(result.exception))
        self.assertEqual(result.exception.code, 'git_read')

    def test_source_change_between_reads_fails_snapshot(self):
        first = b'a' * 40 + b'\trefs/heads/main\n'
        second = b'b' * 40 + b'\trefs/heads/main\n'
        def git(*args, **kwargs):
            if args[0] == 'ls-remote':
                self.reads += 1
                return first if self.reads == 1 else second
            if args[0] == 'for-each-ref':
                return first
            return b''
        self.reads = 0
        self.snapshot.git = git
        with self.assertRaisesRegex(MigrationError, '源引用已变化'):
            self.snapshot.capture()

    def test_annotated_tag_peel_does_not_replace_tag_object(self):
        raw = b'a' * 40 + b'\trefs/tags/v1\n' + b'b' * 40 + b'\trefs/tags/v1^{}\n'
        self.assertEqual(parse_refs(raw), {'refs/tags/v1': 'a' * 40})

    def test_existing_workspace_is_never_reused_by_name(self):
        with self.assertRaisesRegex(MigrationError, '不能覆盖'):
            GitSnapshot(self.provider, {'id': 1, 'path': 'team/repository'}, self.snapshot.folder)


if __name__ == '__main__':
    unittest.main()
