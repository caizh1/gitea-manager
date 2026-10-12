"""实际 SQLite 会话验证进度、事务隔离、错误及传输计量。"""
import io
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from sqlalchemy import text
from sqlalchemy.orm import Session
from models import db, GiteaServer, Backup, BackupRepoCommit, BackupStepLog, RestoreTask, RestoreStepLog
from services.operation_progress import update_backup_progress, progress_for_response, ProgressReader
from services.restore_progress import update_restore_progress
from services.commit_service import collect_backup_commits


class OperationProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + str(Path(self.directory.name) / 'progress.db')
        db.init_app(self.app)
        self.context = self.app.app_context(); self.context.push()
        self.addCleanup(self.context.pop); self.addCleanup(db.session.remove)
        db.create_all()
        self.server = GiteaServer(name='测试源', host='测试主机', ssh_user='root', gitea_port=3000, gitea_url='http://test.invalid', api_token='秘密测试值')
        db.session.add(self.server); db.session.commit()
        self.backup = Backup(source_server_id=self.server.id, filename='sample.zip', file_path='', status='running')
        db.session.add(self.backup); db.session.commit()

    def test_independent_reader_sees_progress_and_utc(self):
        update_backup_progress(self.backup, 'collect', '正在采集', 70, current=3, total=60, unit='repos', item='团队/仓库')
        with Session(db.engine) as reader:
            row = reader.get(Backup, self.backup.id)
            self.assertEqual((row.progress_current, row.progress_total, row.progress_item), (3, 60, '团队/仓库'))
            self.assertTrue(progress_for_response(row)['progress_updated_at'].endswith('Z'))
            self.assertEqual(row.status, 'running')

    def test_saving_rollback_point_is_not_rollback_failure(self):
        update_backup_progress(self.backup, 'stop', '停止服务', 50)
        update_backup_progress(self.backup, 'save_rollback', '保存回退点', 50)
        self.assertEqual(BackupStepLog.query.filter_by(step_key='stop').one().status, 'success')

    def test_throttle_and_final_count(self):
        update_backup_progress(self.backup, 'transfer', '正在传输', 50, current=1, total=100)
        first = self.backup.progress_updated_at
        update_backup_progress(self.backup, 'transfer', '正在传输', 50, current=2, total=100)
        self.assertEqual(self.backup.progress_current, 1)
        update_backup_progress(self.backup, 'transfer', '正在传输', 50, current=100, total=100)
        self.assertEqual(self.backup.progress_current, 100)
        self.assertGreaterEqual(self.backup.progress_updated_at, first)
        self.assertEqual(BackupStepLog.query.count(), 1)

    def test_failure_retains_step_and_quantity(self):
        update_backup_progress(self.backup, 'transfer', '正在传输', 50, current=20, total=100)
        self.backup.status = 'failed'
        update_backup_progress(self.backup, 'failed', '备份失败', 50, '连接中断')
        step = BackupStepLog.query.filter_by(step_key='transfer').one()
        self.assertEqual(step.status, 'failed')
        self.assertEqual(step.detail, '连接中断')
        self.assertEqual(self.backup.progress_current, 20)

    def test_progress_failure_cannot_rollback_business_status(self):
        db.session.execute(text('DROP TABLE backup_step_logs')); db.session.commit()
        self.backup.status = 'success'
        with self.assertLogs(level='WARNING'):
            update_backup_progress(self.backup, 'completed', '备份完成', 100)
        with Session(db.engine) as reader:
            self.assertEqual(reader.get(Backup, self.backup.id).status, 'success')

    def test_snapshot_network_phase_has_no_partial_writes_or_lock(self):
        repos = [{'full_name': f'团队/仓库{i}'} for i in range(60)]
        calls = []
        def commits(*args, **kwargs):
            with Session(db.engine) as other:
                self.assertEqual(other.query(BackupRepoCommit).count(), 0)
                # 第二连接可以写入，证明没有跨网络读取持有写锁。
                other.execute(text('UPDATE gitea_servers SET repo_count = repo_count + 1 WHERE id = :id'), {'id': self.server.id})
                other.commit()
            calls.append(args)
            return ['a' * 40]
        with patch('services.commit_service._get_all_repos', return_value=repos), patch('services.commit_service._get_repo_commits', side_effect=commits):
            self.assertEqual(collect_backup_commits(self.backup.id), 60)
        self.assertEqual(len(calls), 60)
        self.assertEqual(BackupRepoCommit.query.count(), 60)

    def test_collection_failure_preserves_existing_snapshot(self):
        row = BackupRepoCommit(backup_id=self.backup.id, repo_name='旧/仓库', commit_count=1)
        db.session.add(row); db.session.commit()
        with patch('services.commit_service._get_all_repos', return_value=[{'full_name': '新/仓库'}]), patch('services.commit_service._get_repo_commits', side_effect=RuntimeError('采集失败')):
            with self.assertRaises(RuntimeError): collect_backup_commits(self.backup.id)
        self.assertEqual(BackupRepoCommit.query.one().repo_name, '旧/仓库')

    def test_empty_collection(self):
        with patch('services.commit_service._get_all_repos', return_value=[]):
            self.assertEqual(collect_backup_commits(self.backup.id), 0)
        self.assertEqual(self.backup.progress_total, 0)

    def test_progress_reader_preserves_bytes_and_seek(self):
        data = b'\x00\xff' * 100000
        counts = []
        reader = ProgressReader(io.BytesIO(data), lambda n, total: counts.append((n, total)))
        self.assertEqual(reader.read(25) + reader.read(), data)
        self.assertEqual(counts[-1], (len(data), len(data)))
        reader.seek(0)
        self.assertEqual(reader.read(), data)

    def test_restore_steps_and_rollback_remain_running_until_terminal(self):
        task = RestoreTask(backup_id=self.backup.id, target_server_id=self.server.id, status='running')
        db.session.add(task); db.session.commit()
        update_restore_progress(task, 'import_database', '正在导入数据库', 55)
        update_restore_progress(task, 'rollback', '正在回退', 55, '导入失败')
        self.assertEqual(task.status, 'running')
        self.assertEqual(RestoreStepLog.query.filter_by(step_key='import_database').one().status, 'failed')
        task.status = 'failed'
        update_restore_progress(task, 'failed', '恢复失败', 55, '已回退')
        self.assertEqual(RestoreStepLog.query.filter_by(status='running').count(), 0)

    def test_legacy_migrations_are_repeatable_and_preserve_rows(self):
        import ast
        import sqlite3
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text())
        statements = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
                      and node.value.startswith(('ALTER TABLE backups ADD COLUMN progress_', 'ALTER TABLE restore_tasks ADD COLUMN progress_'))]
        connection = sqlite3.connect(':memory:')
        for table in ('backups', 'restore_tasks'):
            connection.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY, status TEXT)')
            connection.execute(f"INSERT INTO {table} VALUES (1, 'success')")
        for _ in range(2):
            for statement in statements:
                try: connection.execute(statement)
                except sqlite3.OperationalError as exc:
                    if 'duplicate column' not in str(exc): raise
        self.assertEqual(connection.execute('SELECT status, progress_current, progress_total FROM backups').fetchone(), ('success', None, None))
        self.assertIsNone(connection.execute('SELECT progress_step_started_at FROM restore_tasks').fetchone()[0])
        connection.close()

    def test_secret_redaction(self):
        from services.diagnostics import register_secret
        register_secret('秘密测试值')
        update_backup_progress(self.backup, 'prepare', '正在准备', 0, '凭据：秘密测试值')
        self.assertNotIn('秘密测试值', self.backup.progress_detail)
        self.assertNotIn('秘密测试值', BackupStepLog.query.one().detail)


if __name__ == '__main__': unittest.main()
