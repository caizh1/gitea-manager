"""快速恢复的第八份轮换及正在恢复的副本保护。"""
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from models import Backup, BackupReplica, db
from services import backup_replica


class BackupReplicaRetentionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        app = Flask(__name__)
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        db.init_app(app)
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        db.create_all()
        for number in range(1, 9):
            file = Path(self.directory.name) / str(number)
            file.write_text('完整备份占位')
            db.session.add(Backup(id=number, source_server_id=1, filename=file.name, file_path=str(file),
                                  source_export_id=number, status='success', commit_snapshot_status='success'))
            db.session.add(BackupReplica(backup_id=number, target_id=2, source_id=1,
                                         key=f'{number:032x}', state='ready' if number < 8 else 'receiving',
                                         binding='重复使用的备机'))
        db.session.commit()

    def test_prune_only_when_latest_ready_and_in_use_copy_is_preserved(self):
        with patch.object(backup_replica, '_file_lease', side_effect=BlockingIOError), \
                patch.object(backup_replica, 'execute') as execute:
            backup_replica._prune(None, 1, 2, '重复使用的备机')
            self.assertEqual(BackupReplica.query.count(), 8)
            execute.assert_not_called()
            newest = db.session.get(BackupReplica, (8, 2))
            newest.state = 'ready'
            db.session.commit()
            backup_replica._prune(None, 1, 2, '重复使用的备机')
            self.assertEqual(BackupReplica.query.count(), 8)
            execute.assert_not_called()
        from services.restore_recovery import _file_lease
        with patch('config.BACKUP_DIR', str(Path(self.directory.name) / 'backups')), \
                patch.object(backup_replica, 'execute') as execute:
            with _file_lease('backup-cache-read:' + f'{1:032x}'):
                backup_replica._prune(None, 1, 2, '重复使用的备机')
                execute.assert_not_called()
            backup_replica._prune(None, 1, 2, '重复使用的备机')
            self.assertEqual(BackupReplica.query.count(), 7)
            self.assertIsNone(db.session.get(BackupReplica, (1, 2)))
            execute.assert_called_once()
            self.assertTrue((Path(self.directory.name) / '1').is_file())

    def test_duplicate_export_uses_one_slot(self):
        newest = db.session.get(Backup, 8)
        newest.source_export_id = 7
        db.session.commit()
        self.assertEqual(backup_replica._keep_latest(1), [8, 6, 5, 4, 3, 2, 1])

    def test_old_snapshot_retry_does_not_evict_newer_backup_point(self):
        now = datetime.utcnow()
        for number in range(1, 9):
            backup = db.session.get(Backup, number)
            backup.source_stopped_at = now - timedelta(days=9 - number)
        oldest = db.session.get(Backup, 1)
        oldest.started_at = now + timedelta(minutes=1)
        db.session.commit()
        self.assertEqual(backup_replica._keep_latest(1), [8, 7, 6, 5, 4, 3, 2])

    def test_active_writer_extends_wait_without_duplicate_full_transfer(self):
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds

        def lease(_):
            if clock[0] < 8:
                raise BlockingIOError
            return nullcontext()

        with patch.object(backup_replica, '_file_lease', side_effect=lease), \
                patch('time.monotonic', side_effect=lambda: clock[0]), patch('time.sleep', side_effect=sleep):
            with backup_replica.wait_for_writer(2, 3, lambda: (clock[0] // 2) * 1024):
                self.assertEqual(clock[0], 8)

    def test_stalled_writer_exits_after_bounded_wait(self):
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds

        with patch.object(backup_replica, '_file_lease', side_effect=BlockingIOError), \
                patch('time.monotonic', side_effect=lambda: clock[0]), patch('time.sleep', side_effect=sleep):
            with self.assertRaises(BlockingIOError):
                with backup_replica.wait_for_writer(2, 3, lambda: 1024):
                    self.fail('持续停滞的备机不应阻塞普通恢复')
        self.assertEqual(clock[0], 3)

    def test_disk_pressure_preserves_existing_ready_archives(self):
        backup = db.session.get(Backup, 8)
        backup.archive_sha256 = 'f' * 64
        backup.file_size = 4096
        row = db.session.get(BackupReplica, (8, 2))
        chunks = [{'name': '00000000-' + 'a' * 64, 'size': backup.file_size}]
        with patch.object(backup_replica, 'make_parts'), \
                patch.object(backup_replica, 'put_file'), \
                patch.object(backup_replica, '_matching_parts', return_value=set()), \
                patch.object(backup_replica, '_space', return_value=1024), \
                patch('services.artifact_io.artifact_tool', return_value=nullcontext('/临时工具')), \
                patch.object(backup_replica, 'send_chunk') as send:
            with self.assertRaisesRegex(RuntimeError, '空间不足'):
                backup_replica.seal(None, None, row, backup, chunks)
            send.assert_not_called()
            self.assertEqual(BackupReplica.query.filter_by(state='ready').count(), 7)
