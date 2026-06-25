import json
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

try:
    import paramiko  # noqa: F401
except ModuleNotFoundError:
    sys.modules['paramiko'] = types.SimpleNamespace(
        RSAKey=types.SimpleNamespace(from_private_key_file=lambda *args, **kwargs: None),
        SSHClient=lambda: None,
        AutoAddPolicy=lambda: None,
    )
try:
    import docker  # noqa: F401
except ModuleNotFoundError:
    sys.modules['docker'] = types.SimpleNamespace(from_env=lambda: None)

from flask import Flask

from models import db, Backup, GiteaServer, ScheduleLog, ScheduledTask
from routes.restore_routes import execute_restore as execute_restore_route
from services import commit_service
from services.gitea_service import do_backup
from services.schedule_runner import run_schedule_task


class BackupCommitSnapshotTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.source = GiteaServer(
            name='source',
            role='primary',
            host='source.local',
            ssh_user='root',
            gitea_port=3000,
            gitea_url='http://source.local',
            api_token='source-token',
        )
        self.target = GiteaServer(
            name='backup',
            role='backup',
            host='backup.local',
            ssh_user='root',
            gitea_port=3000,
            gitea_url='http://backup.local',
            api_token='target-token',
        )
        db.session.add_all([self.source, self.target])
        db.session.commit()

        self.backup = Backup(
            source_server_id=self.source.id,
            filename='dump.zip',
            file_path='',
            status='running',
            commit_snapshot_status='pending',
            started_at=datetime.utcnow(),
        )
        db.session.add(self.backup)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def fake_dump(self, server, backup):
        backup.file_path = '/tmp/dump.zip'
        backup.file_size = 123

    def test_do_backup_marks_success_only_after_commit_snapshot(self):
        with patch('services.gitea_service._backup_remote', side_effect=self.fake_dump), \
             patch('services.commit_service.collect_backup_commits', return_value=2):
            do_backup(self.backup.id)

        backup = Backup.query.get(self.backup.id)
        self.assertEqual(backup.status, 'success')
        self.assertEqual(backup.error_msg, '')
        self.assertEqual(backup.commit_snapshot_status, 'success')
        self.assertEqual(backup.commit_snapshot_repo_count, 2)
        self.assertEqual(backup.commit_snapshot_error, '')
        self.assertIsNotNone(backup.commit_snapshot_collected_at)

    def test_do_backup_fails_when_commit_snapshot_collection_fails(self):
        with patch('services.gitea_service._backup_remote', side_effect=self.fake_dump), \
             patch('services.commit_service.collect_backup_commits', side_effect=Exception('repos API failed')):
            do_backup(self.backup.id)

        backup = Backup.query.get(self.backup.id)
        self.assertEqual(backup.status, 'failed')
        self.assertIn('Commit ID 快照采集失败', backup.error_msg)
        self.assertEqual(backup.commit_snapshot_status, 'failed')
        self.assertIn('repos API failed', backup.commit_snapshot_error)

    def test_get_repo_commits_only_collects_recent_limit(self):
        calls = []

        def fake_api_get_json(server, path, params=None, timeout=30, repo='Gitea API', page=None):
            page = (params or {}).get('page', 1)
            calls.append((page, (params or {}).get('limit'), repo, page))

            if page == 1:
                return [{'sha': f'c{idx}'} for idx in range(50)]
            if page == 2:
                return [{'sha': f'c{idx + 50}'} for idx in range(50)]

            self.fail(f'Unexpected page request: {page}')

        with patch('services.commit_service._api_get_json', side_effect=fake_api_get_json):
            commits = commit_service._get_repo_commits(self.source, 'UFS', 'qemu', max_commits=100)

        self.assertEqual(len(commits), 100)
        self.assertEqual(calls, [
            (1, 50, 'UFS/qemu', 1),
            (2, 50, 'UFS/qemu', 2),
        ])

    def test_restore_route_rejects_legacy_success_backup_without_snapshot(self):
        self.backup.status = 'success'
        self.backup.commit_snapshot_status = ''
        db.session.commit()

        with self.app.test_request_context('/restore', method='POST', json={
            'backup_id': self.backup.id,
            'target_server_id': self.target.id,
        }):
            response, status = execute_restore_route.__wrapped__()

        self.assertEqual(status, 400)
        self.assertIn('备份缺少 Commit ID 快照', response.get_json()['error'])

    def test_schedule_stops_before_restore_when_backup_snapshot_fails(self):
        task = ScheduledTask(
            name='nightly',
            source_server_id=self.source.id,
            target_ids=json.dumps([self.target.id]),
            schedule_hour=2,
            schedule_minute=0,
            last_status='running',
            created_at=datetime.utcnow(),
        )
        db.session.add(task)
        db.session.commit()

        def fake_failed_backup(backup_id):
            backup = Backup.query.get(backup_id)
            backup.status = 'failed'
            backup.error_msg = 'Commit ID 快照采集失败: repos API failed'
            backup.commit_snapshot_status = 'failed'
            backup.commit_snapshot_error = 'repos API failed'
            db.session.commit()

        with patch('services.schedule_runner.do_backup', side_effect=fake_failed_backup), \
             patch('services.schedule_runner.do_restore') as do_restore_mock:
            run_schedule_task(task.id, started_at=datetime.utcnow())

        task = ScheduledTask.query.get(task.id)
        log = ScheduleLog.query.filter_by(schedule_task_id=task.id).first()
        self.assertEqual(task.last_status, 'failed')
        self.assertIn('Commit ID 快照采集失败', task.last_log)
        self.assertEqual(log.backup_status, 'failed')
        self.assertFalse(do_restore_mock.called)


if __name__ == '__main__':
    unittest.main()
