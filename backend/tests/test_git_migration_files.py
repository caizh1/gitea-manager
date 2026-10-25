"""二进制下载的来源限制、完整性与失败回执。"""
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock
from git_migration.files import SourceFiles
from svn_migration.common import MigrationError


class FileTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        response = Mock(status_code=200)
        response.iter_content.return_value = [b'fixed', b'-content']
        self.response = response
        self.session = Mock()
        self.session.get.return_value = response
        self.provider = SimpleNamespace(http=SimpleNamespace(base='https://source.test/git', session=self.session, verify=True),
                                        credentials=lambda: {'headers': {'Authorization': '不可回显'}})
        self.files = SourceFiles(self.provider, self.folder.name)

    def test_streamed_content_matches_manifest(self):
        result = self.files.download('https://source.test/git/files/1', expected_size=13)
        self.assertEqual(result['sha256'], hashlib.sha256(b'fixed-content').hexdigest())
        self.assertEqual(Path(result['file']).read_bytes(), b'fixed-content')
        self.assertFalse(self.session.get.call_args.kwargs['allow_redirects'])

    def test_external_origin_and_application_path_never_receive_credentials(self):
        for url in ('https://other.test/files/1', 'http://source.test/git/files/1', 'https://source.test/other/files/1'):
            with self.assertRaises(MigrationError):
                self.files.download(url)
        self.session.get.assert_not_called()

    def test_truncated_content_never_becomes_complete(self):
        with self.assertRaises(MigrationError):
            self.files.download('https://source.test/git/files/1', expected_size=14)
        self.assertEqual(len(list(Path(self.folder.name).glob('*.partial'))), 1)

    def test_redirect_is_not_followed(self):
        self.response.status_code = 302
        with self.assertRaises(MigrationError):
            self.files.download('https://source.test/git/files/1')
        self.response.iter_content.assert_not_called()


if __name__ == '__main__':
    unittest.main()


class RedirectTests(unittest.TestCase):
    setUp = FileTests.setUp

    def test_redirect_outside_source_is_blocked_before_following(self):
        self.response.status_code = 302
        self.response.headers = {'Location': 'https://other.test/download'}
        with self.assertRaises(MigrationError):
            self.files.download('https://source.test/git/files/1', follow_same_origin=True)
        self.assertEqual(self.session.get.call_count, 1)
