"""固定副本传输、摘要、权限和并发清理门禁回归。"""
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask
from models import db, Backup, BackupExport, GiteaServer
import git_migration.models
from routes.backup_routes import backup_bp
from services import backup_staging as staging
from services import backup_recovery as recovery


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('config.BACKUP_DIR', str(self.root / 'backups')))
        self.app = Flask('固定导出测试')
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + str(self.root / 'db'), LOGIN_DISABLED=True, TESTING=True)
        db.init_app(self.app)
        self.app.register_blueprint(backup_bp, url_prefix='/api')
        self.stack.enter_context(self.app.app_context())
        self.addCleanup(db.session.remove)
        db.create_all()
        self.server = GiteaServer(name='测试源', host='source', ssh_user='root', gitea_port=3000, gitea_url='http://source', api_token='测试令牌')
        db.session.add(self.server)
        db.session.flush()
        from datetime import datetime
        self.export = BackupExport(source_server_id=self.server.id, marker='manager-backup-' + 'a' * 32,
            volume='gitea-manager-spool-' + 'b' * 24, context_json='{}', state='ready', exported_at=datetime.utcnow())
        from services.backup_recovery import SERVER_FIELDS
        self.export.context_json = json.dumps({'服务器': {key: getattr(self.server, key) for key in SERVER_FIELDS}})
        db.session.add(self.export)
        db.session.flush()
        self.backup = Backup(source_server_id=self.server.id, source_export_id=self.export.id,
            filename='sample.zip', file_path='', status='failed', recovery_pending=False)
        db.session.add(self.backup)
        db.session.commit()

    def test_stream_digest_size_and_close(self):
        data = '固定备份内容'.encode()
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w') as archive:
            item = tarfile.TarInfo('gitea.zip')
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
        for valid in (True, False):
            stream = MagicMock()
            stream.__iter__.return_value = iter([raw.getvalue()])
            reader = MagicMock()
            reader.get_archive.return_value = stream, {}
            target = self.root / ('valid.zip' if valid else 'invalid.zip')
            expected = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest() if valid else '0' * 64}
            if valid:
                staging.receive_file(reader, '/export/gitea.zip', target, expected=expected)
                self.assertEqual(target.read_bytes(), data)
            else:
                with self.assertRaisesRegex(RuntimeError, '摘要'):
                    staging.receive_file(reader, '/export/gitea.zip', target, expected=expected)
            stream.close.assert_called_once()

    def test_retries_share_export_and_preserve_failure(self):
        with patch('routes.backup_routes.task_manager.run_backup') as dispatch:
            response = self.app.test_client().post(f'/api/backups/{self.backup.id}/retry-transfer')
        self.assertEqual(response.status_code, 201)
        new = db.session.get(Backup, response.json['id'])
        self.assertEqual(new.source_export_id, self.export.id)
        self.assertEqual(new.transfer_origin_id, self.backup.id)
        self.assertEqual(self.backup.status, 'failed')
        dispatch.assert_called_once_with(new.id)
        again = self.app.test_client().post(f'/api/backups/{self.backup.id}/retry-transfer')
        self.assertEqual(again.status_code, 409)
        with patch('services.backup_staging.cleanup_export') as cleanup:
            response = self.app.test_client().delete(f'/api/backups/{self.backup.id}/source-export', json={'confirm_discard': True})
        self.assertEqual(response.status_code, 409)
        cleanup.assert_not_called()

    def test_export_lock_blocks_other_operation(self):
        with staging.export_lease(self.export.id):
            response = self.app.test_client().post(f'/api/backups/{self.backup.id}/retry-transfer')
        self.assertEqual(response.status_code, 409)

    def test_cleanup_needs_explicit_discard_and_keeps_record(self):
        with self.assertRaisesRegex(RuntimeError, '明确确认丢弃'):
            staging.cleanup_export(self.export, self.server)
        response = self.app.test_client().delete(f'/api/backups/{self.backup.id}')
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(db.session.get(Backup, self.backup.id))

    def test_restored_source_does_not_receive_lifecycle_calls(self):
        client = MagicMock()
        journal = MagicMock(value={'源收尾已完成': True})
        recovery.recover_source(client, self.server, journal)
        client.containers.get.assert_not_called()
        journal.save.assert_not_called()

    def test_wrong_volume_owner_is_rejected(self):
        self.export.context_json = json.dumps({'来源容器': 'source'})
        client = MagicMock()
        client.volumes.get.return_value.attrs = {'Driver': 'local', 'Options': None, 'Labels': {'用途': '其他卷'}}
        with self.assertRaisesRegex(RuntimeError, '归属不匹配'):
            staging.verify_volume(client, self.export)

    def test_cleanup_error_keeps_success(self):
        self.backup.status = 'success'
        db.session.commit()
        with patch.object(staging, 'cleanup_export', side_effect=OSError('合成网络中断')):
            staging.cleanup_success(self.backup)
        self.assertEqual(self.backup.status, 'success')
        self.assertIn('清理未完成', self.export.error_msg)

    def test_legacy_row_remains_without_export(self):
        self.backup.source_export_id = None
        db.session.commit()
        from routes.backup_routes import backup_to_dict
        value = backup_to_dict(self.backup)
        self.assertEqual(value['source_export_state'], '')
        self.assertFalse(value['can_retry_transfer'])
        self.assertFalse(value['can_cleanup_export'])

    def test_frozen_receipt_recovers_database_commit_gap(self):
        self.export.exported_at = None
        self.export.state = 'building'
        journal = MagicMock(value={'站点导出': {'摘要': 'a' * 64, '大小': 123, '时间UTC': '2026-11-17T01:00:00'}})
        staging.restore_export_index(self.backup, journal)
        self.assertEqual(self.export.state, 'ready')
        self.assertEqual(self.export.size, 123)
        self.assertEqual(self.export.digest, 'a' * 64)

    def test_insufficient_space_is_detected_before_export_directory(self):
        helper = MagicMock()
        with patch('services.backup_consistency.execute', side_effect=['1024', '0']) as execute, \
                patch('services.backup_consistency.database_sql', return_value='1024'):
            with self.assertRaisesRegex(RuntimeError, '尚未停止源站'):
                staging.prepare_export(helper, MagicMock(), self.server, self.export.marker)
        self.assertEqual(execute.call_count, 2)
