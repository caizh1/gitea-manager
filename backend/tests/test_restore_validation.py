import hashlib
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from flask import Flask
from models import db, Backup, BackupRepoCommit, GiteaServer, RestoreTask, RestoreVerification
from services.commit_service import verify_restore


class FakeResponse:
    def __init__(self, status_code, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def commit_hash(commits):
    return hashlib.sha256(','.join(sorted(commits)).encode()).hexdigest()


class RestoreValidationTestCase(unittest.TestCase):
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
            role='main',
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
            file_path='/tmp/dump.zip',
            status='success',
            started_at=datetime.utcnow(),
        )
        db.session.add(self.backup)
        db.session.commit()

        self.task = RestoreTask(
            backup_id=self.backup.id,
            target_server_id=self.target.id,
            target_server_name=self.target.name,
            status='running',
            started_at=datetime.utcnow(),
        )
        db.session.add(self.task)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def add_backup_commits(self, repo='org/repo', commits=None):
        commits = commits or ['a', 'b']
        db.session.add(BackupRepoCommit(
            backup_id=self.backup.id,
            repo_name=repo,
            commit_count=len(commits),
            latest_commit_sha=commits[0],
            commit_ids_hash=commit_hash(commits),
            commit_ids=json.dumps(sorted(commits)),
            collected_at=datetime.utcnow(),
        ))
        db.session.flush()
        self.backup.commit_snapshot_status = 'success'
        self.backup.commit_snapshot_repo_count = BackupRepoCommit.query.filter_by(
            backup_id=self.backup.id
        ).count()
        self.backup.commit_snapshot_error = ''
        self.backup.commit_snapshot_collected_at = datetime.utcnow()
        db.session.commit()

    def mark_empty_snapshot_success(self):
        self.backup.commit_snapshot_status = 'success'
        self.backup.commit_snapshot_repo_count = 0
        self.backup.commit_snapshot_error = ''
        self.backup.commit_snapshot_collected_at = datetime.utcnow()
        db.session.commit()

    def fake_get(self, version_status=200, user_status=200, repos_status=200,
                 repos=None, commits=None):
        repos = repos if repos is not None else [{'full_name': 'org/repo'}]
        commits = commits if commits is not None else ['a', 'b']

        def _get(url, headers=None, params=None, timeout=None):
            if url.endswith('/api/v1/version'):
                return FakeResponse(version_status, {'version': '1.0'}, 'version failed')
            if url.endswith('/api/v1/user'):
                return FakeResponse(user_status, {'login': 'admin'}, 'token failed')
            if url.endswith('/api/v1/repos/search'):
                return FakeResponse(repos_status, {'data': repos}, 'repos failed')
            if url.endswith('/api/v1/repos/org/repo/commits'):
                return FakeResponse(200, [{'sha': sha} for sha in commits])
            return FakeResponse(404, {}, 'not found')

        return _get

    def refresh_results(self):
        task = RestoreTask.query.get(self.task.id)
        verification = RestoreVerification.query.filter_by(restore_task_id=self.task.id).first()
        return task, verification, json.loads(verification.mismatch_details)

    def run_verify(self, fake_get):
        with patch('services.commit_service.RESTORE_HEALTH_CHECK_ATTEMPTS', 1), \
             patch('services.commit_service.RESTORE_HEALTH_CHECK_INTERVAL_SECONDS', 0), \
             patch('services.commit_service.requests.get', side_effect=fake_get):
            verify_restore(self.task.id)

    def run_verify_with_health_retries(self, fake_get, attempts=3):
        with patch('services.commit_service.RESTORE_HEALTH_CHECK_ATTEMPTS', attempts), \
             patch('services.commit_service.RESTORE_HEALTH_CHECK_INTERVAL_SECONDS', 0), \
             patch('services.commit_service.requests.get', side_effect=fake_get):
            verify_restore(self.task.id)

    def test_version_must_be_reachable(self):
        self.add_backup_commits()
        self.run_verify(self.fake_get(version_status=500))

        task, verification, details = self.refresh_results()
        self.assertEqual(task.status, 'failed')
        self.assertEqual(verification.status, 'failed')
        self.assertIn('/api/v1/version', task.error_msg)
        self.assertEqual(details[0]['path'], '/api/v1/version')
        self.assertEqual(details[0]['url'], 'http://backup.local/api/v1/version')

    def test_legacy_backup_without_snapshot_fails_before_api_checks(self):
        calls = {'count': 0}

        def _get(url, headers=None, params=None, timeout=None):
            calls['count'] += 1
            return FakeResponse(200, {})

        self.run_verify(_get)

        task, verification, details = self.refresh_results()
        self.assertEqual(task.status, 'failed')
        self.assertEqual(verification.status, 'failed')
        self.assertIn('备份缺少 Commit ID 快照', task.error_msg)
        self.assertEqual(details[0]['type'], 'missing_backup_commit_snapshot')
        self.assertEqual(calls['count'], 0)

    def test_empty_snapshot_success_allows_empty_target(self):
        self.mark_empty_snapshot_success()
        self.run_verify(self.fake_get(repos=[]))

        task, verification, details = self.refresh_results()
        self.assertEqual(task.status, 'success')
        self.assertEqual(verification.status, 'success')
        self.assertEqual(verification.total_repos, 0)
        self.assertEqual(details, [])

    def test_empty_snapshot_fails_when_target_has_extra_repo(self):
        self.mark_empty_snapshot_success()
        self.run_verify(self.fake_get(repos=[{'full_name': 'org/repo'}]))

        task, verification, details = self.refresh_results()
        self.assertEqual(task.status, 'failed')
        self.assertEqual(verification.status, 'failed')
        self.assertEqual(details[0]['type'], 'extra_repo')
        self.assertEqual(details[0]['repo'], 'org/repo')

    def test_transient_version_connection_reset_is_retried(self):
        self.add_backup_commits()
        calls = {'version': 0}

        def _get(url, headers=None, params=None, timeout=None):
            if url.endswith('/api/v1/version'):
                calls['version'] += 1
                if calls['version'] == 1:
                    raise ConnectionResetError(104, 'Connection reset by peer')
                return FakeResponse(200, {'version': '1.0'})
            if url.endswith('/api/v1/user'):
                return FakeResponse(200, {'login': 'admin'})
            if url.endswith('/api/v1/repos/search'):
                return FakeResponse(200, {'data': [{'full_name': 'org/repo'}]})
            if url.endswith('/api/v1/repos/org/repo/commits'):
                return FakeResponse(200, [{'sha': 'a'}, {'sha': 'b'}])
            return FakeResponse(404, {}, 'not found')

        self.run_verify_with_health_retries(_get, attempts=3)

        task, verification, details = self.refresh_results()
        self.assertEqual(task.status, 'success')
        self.assertEqual(verification.status, 'success')
        self.assertEqual(calls['version'], 2)
        self.assertEqual(details, [])

    def test_token_must_be_valid(self):
        self.add_backup_commits()
        self.run_verify(self.fake_get(user_status=401))

        task, verification, details = self.refresh_results()
        self.assertEqual(task.status, 'failed')
        self.assertEqual(verification.status, 'failed')
        self.assertIn('/api/v1/user', task.error_msg)
        self.assertEqual(details[0]['path'], '/api/v1/user')

    def test_repo_list_api_failure_is_not_treated_as_empty_success(self):
        self.add_backup_commits()
        self.run_verify(self.fake_get(repos_status=500))

        task, verification, details = self.refresh_results()
        self.assertEqual(task.status, 'failed')
        self.assertEqual(verification.status, 'failed')
        self.assertIn('/api/v1/repos/search', task.error_msg)
        self.assertEqual(details[0]['path'], '/api/v1/repos/search')

    def test_missing_target_repo_fails_validation(self):
        self.add_backup_commits(repo='org/repo', commits=['a', 'b'])
        self.run_verify(self.fake_get(repos=[]))

        task, verification, details = self.refresh_results()
        self.assertEqual(task.status, 'failed')
        self.assertEqual(verification.status, 'failed')
        self.assertEqual(details[0]['type'], 'missing_repo')
        self.assertEqual(details[0]['repo'], 'org/repo')

    def test_commit_mismatch_fails_validation(self):
        self.add_backup_commits(repo='org/repo', commits=['a', 'b'])
        self.run_verify(self.fake_get(commits=['a', 'c']))

        task, verification, details = self.refresh_results()
        self.assertEqual(task.status, 'failed')
        self.assertEqual(verification.status, 'failed')
        self.assertEqual(details[0]['type'], 'commit_mismatch')
        self.assertEqual(details[0]['missing_samples'], ['b'])
        self.assertEqual(details[0]['extra_samples'], ['c'])

    def test_matching_commits_mark_restore_success(self):
        self.add_backup_commits(repo='org/repo', commits=['a', 'b'])
        self.run_verify(self.fake_get(commits=['b', 'a']))

        task, verification, details = self.refresh_results()
        self.assertEqual(task.status, 'success')
        self.assertEqual(task.error_msg, '')
        self.assertEqual(verification.status, 'success')
        self.assertEqual(verification.matched_repos, 1)
        self.assertEqual(details, [])


if __name__ == '__main__':
    unittest.main()
