import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services import commit_gate_service as service


class FakeReader:
    capabilities = None

    def __init__(self, server, seconds):
        pass

    def governance_capabilities(self):
        return self.capabilities

    def close(self):
        pass


class CommitGateStorageTests(unittest.TestCase):
    def setUp(self):
        self.server = SimpleNamespace(version='1.27.3', is_local=True, gitea_container='gitea')

    def test_only_confirmed_legacy_uses_logical_path(self):
        FakeReader.capabilities = None
        with patch.object(service, 'Reader', FakeReader), patch.object(service, '_run_in_gitea', return_value=(0, '/data/git/repositories/o/r.git', '')):
            self.assertEqual(service._resolve_repo_dir(self.server, 'o/r', 7), '/data/git/repositories/o/r.git')

    def test_declared_storage_identity_uses_stable_repository_id(self):
        FakeReader.capabilities = {'version': 1, 'repository_storage_identity': True}
        with patch.object(service, 'Reader', FakeReader), patch.object(service, '_resolve_native_repo_dir', return_value='/stable/old/r.git') as resolve:
            self.assertEqual(service._resolve_repo_dir(self.server, 'new/r', 7), '/stable/old/r.git')
            resolve.assert_called_once_with(self.server, 7)

    def test_native_without_storage_identity_never_guesses_display_path(self):
        FakeReader.capabilities = {'version': 1, 'hierarchical_groups': True}
        with patch.object(service, 'Reader', FakeReader), patch.object(service, '_run_in_gitea') as legacy:
            with self.assertRaisesRegex(RuntimeError, '稳定项目存储身份能力'):
                service._resolve_repo_dir(self.server, 'display/path', 7)
            legacy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
