"""归档必须在容器命令前关闭，清理失败不得覆盖首次错误。"""
from contextlib import ExitStack
import io
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from services.backup_actions import append_artifacts
from services.backup_consistency import consistent_backup


class BackupStreamTests(unittest.TestCase):
    def test_archive_closed_before_exec_and_on_disk_error(self):
        for disk_error in (False, True):
            with self.subTest(写盘失败=disk_error), tempfile.TemporaryDirectory() as root:
                path = Path(root) / 'backup.zip'
                with zipfile.ZipFile(path, 'w') as archive:
                    archive.writestr('gitea-db.sql', '数据库样本')
                raw = io.BytesIO()
                with tarfile.open(fileobj=raw, mode='w') as archive:
                    item = tarfile.TarInfo('actions_artifacts')
                    item.type = tarfile.DIRTYPE
                    archive.addfile(item)
                stream = MagicMock()
                stream.__iter__.return_value = iter([raw.getvalue()])
                source = MagicMock()
                source.get_archive.return_value = (stream, {})
                def execute(*args):
                    stream.close.assert_called_once()
                    return '/data/gitea/actions_artifacts'
                with ExitStack() as stack:
                    stack.enter_context(patch('services.backup_consistency.database_sql', return_value='[]'))
                    command = stack.enter_context(patch('services.backup_consistency.execute', side_effect=execute))
                    if disk_error:
                        temporary = stack.enter_context(patch('services.backup_actions.tempfile.TemporaryFile'))
                        temporary.return_value.__enter__.return_value.write.side_effect = OSError('验收磁盘已满')
                        with self.assertRaisesRegex(OSError, '验收磁盘已满'):
                            append_artifacts(path, source, None, None, b'')
                        command.assert_not_called()
                    else:
                        append_artifacts(path, source, None, None, b'')
                        command.assert_called_once()
                    stream.close.assert_called_once()

    def test_cleanup_preserves_original_error_and_closes_client(self):
        client, original, helper = MagicMock(), MagicMock(), MagicMock()
        original.status = 'running'
        original.attrs = {'Image': '隔离镜像', 'Config': {}, 'NetworkSettings': {'Networks': {'network': {}}}}
        database = MagicMock()
        database.attrs = {'NetworkSettings': {'Networks': {'network': {}}}}
        client.containers.get.side_effect = [original, database]
        client.containers.create.return_value = helper
        original.start.side_effect = RuntimeError('验收启动失败')
        helper.remove.side_effect = RuntimeError('验收回收失败')
        primary = RuntimeError('验收首次错误')
        with ExitStack() as stack:
            stack.enter_context(patch('services.docker_service.managed_client', return_value=client))
            for name in ('check_layout', 'require_quiet', 'execute', 'database_sql'):
                stack.enter_context(patch('services.backup_consistency.' + name))
            stack.enter_context(patch('services.backup_manifest.identity', return_value={}))
            stack.enter_context(patch('services.backup_consistency.read_file', return_value=(b'', None)))
            stack.enter_context(patch('services.backup_dependencies.capture_startup_files', return_value={}))
            stack.enter_context(patch('services.operation_progress.update_backup_progress'))
            stack.enter_context(patch('services.backup_consistency.repository_snapshot', side_effect=primary))
            stack.enter_context(patch('services.backup_recovery.recover_source', side_effect=lambda *args: helper.remove(force=True)))
            with self.assertRaises(RuntimeError) as error:
                consistent_backup(SimpleNamespace(gitea_container='源', pg_container='数据库', name='隔离实例'), MagicMock(),
                                  journal=MagicMock(value={'原源': {'说明': '此用例验证清理错误不得遮蔽归档首败'}}))
        self.assertIs(error.exception, primary)
        self.assertIn('清理未完整完成', primary.__notes__[0])
        helper.remove.assert_called_once_with(force=True)
        client.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
