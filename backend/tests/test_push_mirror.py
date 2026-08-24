import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from flask import Flask

from models import db, GiteaServer, MirrorAuditLog, MirrorConfig, MirrorRepoStatus
from routes.mirror_routes import create_mirror as create_mirror_route
from routes.mirror_routes import setup_mirror as setup_mirror_route
from services.mirror_service import PUSH_MIRROR_MODE, setup_mirror, sync_mirror


class FakeResponse:
    def __init__(self, status_code, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else ('{}' if payload is not None else '')

    def json(self):
        return self._payload


class PushMirrorTestCase(unittest.TestCase):
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

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def add_config(self, mode=PUSH_MIRROR_MODE, interval=45, sync_on_commit=True):
        config = MirrorConfig(
            source_server_id=self.source.id,
            target_server_id=self.target.id,
            sync_mode=mode,
            sync_interval=interval,
            sync_on_commit=sync_on_commit,
            created_at=datetime.utcnow(),
        )
        db.session.add(config)
        db.session.commit()
        return config

    def test_create_mirror_route_forces_push_mirror(self):
        with self.app.test_request_context('/mirrors', method='POST', json={
            'source_server_id': self.source.id,
            'target_server_id': self.target.id,
            'sync_mode': 'gitea_mirror',
            'sync_interval': 15,
            'sync_on_commit': False,
        }):
            response, status = create_mirror_route.__wrapped__()

        self.assertEqual(status, 201)
        data = response.get_json()
        self.assertEqual(data['sync_mode'], PUSH_MIRROR_MODE)
        self.assertEqual(data['sync_interval'], 15)
        self.assertFalse(data['sync_on_commit'])
        self.assertFalse(data['deprecated'])

    def test_setup_push_mirror_uses_source_push_mirror_api(self):
        config = self.add_config(interval=45, sync_on_commit=True)
        calls = []

        def fake_request(method, url, headers=None, timeout=None, **kwargs):
            calls.append({'method': method, 'url': url, 'json': kwargs.get('json')})
            path = urlparse(url).path
            host = urlparse(url).hostname
            if host == 'source.local' and path == '/api/v1/repos/search':
                return FakeResponse(200, {'data': [{
                    'id': 1,
                    'full_name': 'org/repo',
                    'private': True,
                    'description': 'demo',
                }]})
            if host == 'backup.local' and path == '/api/v1/repos/org/repo':
                return FakeResponse(404, {}, 'not found')
            if host == 'backup.local' and path == '/api/v1/user':
                return FakeResponse(200, {'login': 'mirrorbot'})
            if host == 'backup.local' and path == '/api/v1/orgs/org':
                return FakeResponse(404, {}, 'not found')
            if host == 'backup.local' and path == '/api/v1/orgs':
                return FakeResponse(201, {'username': 'org'})
            if host == 'backup.local' and path == '/api/v1/org/org/repos':
                return FakeResponse(201, {'id': 99})
            if host == 'source.local' and path == '/api/v1/repos/org/repo/push_mirrors':
                if method == 'GET':
                    return FakeResponse(200, [])
                return FakeResponse(200, {'remote_name': 'backup'})
            if host == 'source.local' and path == '/api/v1/repos/org/repo/push_mirrors-sync':
                return FakeResponse(200, {})
            return FakeResponse(500, {}, f'unexpected {method} {url}')

        with patch('services.mirror_service.requests.request', side_effect=fake_request):
            setup_mirror(config.id)

        config = MirrorConfig.query.get(config.id)
        repo = MirrorRepoStatus.query.filter_by(mirror_config_id=config.id, repo_name='org/repo').first()
        push_call = next(c for c in calls if c['method'] == 'POST' and c['url'].endswith('/push_mirrors'))
        self.assertEqual(config.status, 'success')
        self.assertEqual(config.progress_percent, 100)
        self.assertEqual(config.progress_stage, 'completed')
        self.assertEqual(repo.status, 'success')
        self.assertEqual(repo.remote_name, 'backup')
        self.assertEqual(push_call['json']['remote_address'], 'http://backup.local/org/repo.git')
        self.assertEqual(push_call['json']['remote_username'], 'mirrorbot')
        self.assertEqual(push_call['json']['remote_password'], 'target-token')
        self.assertEqual(push_call['json']['interval'], '45m')
        self.assertTrue(push_call['json']['sync_on_commit'])

    def test_setup_push_mirror_records_repo_failure_details(self):
        config = self.add_config()

        def fake_request(method, url, headers=None, timeout=None, **kwargs):
            path = urlparse(url).path
            host = urlparse(url).hostname
            if host == 'source.local' and path == '/api/v1/repos/search':
                return FakeResponse(200, {'data': [{
                    'id': 1,
                    'full_name': 'org/repo',
                    'private': True,
                    'description': 'demo',
                }]})
            if host == 'backup.local' and path == '/api/v1/repos/org/repo':
                return FakeResponse(403, {}, 'target token cannot read repo')
            return FakeResponse(500, {}, f'unexpected {method} {url}')

        with patch('services.mirror_service.requests.request', side_effect=fake_request):
            setup_mirror(config.id)

        config = MirrorConfig.query.get(config.id)
        repo = MirrorRepoStatus.query.filter_by(mirror_config_id=config.id, repo_name='org/repo').first()
        self.assertEqual(config.status, 'partial')
        self.assertEqual(config.failed_repos, 1)
        self.assertIn('target token cannot read repo', config.last_sync_log)
        self.assertIn('http://backup.local/api/v1/repos/org/repo', config.last_sync_log)
        self.assertEqual(repo.status, 'failed')
        self.assertIn('target token cannot read repo', repo.error_msg)

    def test_manual_sync_uses_source_push_mirror_sync_api(self):
        config = self.add_config()
        db.session.add(MirrorRepoStatus(
            mirror_config_id=config.id,
            repo_name='org/repo',
            status='success',
            sync_mode=PUSH_MIRROR_MODE,
            remote_name='backup',
            created_at=datetime.utcnow(),
        ))
        db.session.commit()
        calls = []

        def fake_request(method, url, headers=None, timeout=None, **kwargs):
            calls.append({'method': method, 'url': url})
            if url.endswith('/api/v1/repos/org/repo/push_mirrors-sync'):
                return FakeResponse(200, {})
            return FakeResponse(500, {}, f'unexpected {method} {url}')

        with patch('services.mirror_service.requests.request', side_effect=fake_request):
            sync_mirror(config.id)

        config = MirrorConfig.query.get(config.id)
        self.assertEqual(config.status, 'success')
        self.assertTrue(any(c['url'].endswith('/push_mirrors-sync') for c in calls))
        self.assertFalse(any(c['url'].endswith('/mirror-sync') for c in calls))

    def test_setup_marks_missing_source_and_audits_it(self):
        config = self.add_config()
        db.session.add(MirrorRepoStatus(
            mirror_config_id=config.id,
            repo_name='org/removed',
            source_repo_id=77,
            status='success',
            sync_mode=PUSH_MIRROR_MODE,
            remote_name='backup',
            created_at=datetime.utcnow(),
        ))
        db.session.commit()

        def fake_request(method, url, headers=None, timeout=None, **kwargs):
            if url.endswith('/api/v1/repos/search'):
                return FakeResponse(200, {'data': []})
            return FakeResponse(500, {}, f'unexpected {method} {url}')

        with patch('services.mirror_service.requests.request', side_effect=fake_request):
            setup_mirror(config.id)

        repo = MirrorRepoStatus.query.filter_by(mirror_config_id=config.id, repo_name='org/removed').first()
        audit = MirrorAuditLog.query.filter_by(mirror_config_id=config.id, action='mark_missing_source').first()
        config = MirrorConfig.query.get(config.id)
        self.assertEqual(repo.status, 'missing_source')
        self.assertEqual(config.progress_percent, 100)
        self.assertEqual(config.progress_stage, 'completed_partial')
        self.assertIsNotNone(audit)
        self.assertIn('未发现该仓库', audit.reason)

    def test_manual_sync_skips_missing_source_and_audits_it(self):
        config = self.add_config()
        db.session.add(MirrorRepoStatus(
            mirror_config_id=config.id,
            repo_name='org/removed',
            source_repo_id=77,
            status='missing_source',
            sync_mode=PUSH_MIRROR_MODE,
            remote_name='backup',
            created_at=datetime.utcnow(),
        ))
        db.session.commit()
        calls = []

        def fake_request(method, url, headers=None, timeout=None, **kwargs):
            calls.append({'method': method, 'url': url})
            return FakeResponse(500, {}, f'unexpected {method} {url}')

        with patch('services.mirror_service.requests.request', side_effect=fake_request):
            sync_mirror(config.id)

        repo = MirrorRepoStatus.query.filter_by(mirror_config_id=config.id, repo_name='org/removed').first()
        audit = MirrorAuditLog.query.filter_by(mirror_config_id=config.id, action='skip_missing_source').first()
        config = MirrorConfig.query.get(config.id)
        self.assertEqual(repo.status, 'missing_source')
        self.assertEqual(config.progress_percent, 100)
        self.assertIn('失败或跳过', config.progress_detail)
        self.assertEqual(calls, [])
        self.assertIsNotNone(audit)

    def test_setup_repairs_renamed_repo_by_source_repo_id(self):
        config = self.add_config()
        db.session.add(MirrorRepoStatus(
            mirror_config_id=config.id,
            repo_name='org/old-repo',
            source_repo_id=1,
            target_repo_id=88,
            status='success',
            sync_mode=PUSH_MIRROR_MODE,
            remote_name='backup',
            created_at=datetime.utcnow(),
        ))
        db.session.commit()
        calls = []

        def fake_request(method, url, headers=None, timeout=None, **kwargs):
            calls.append({'method': method, 'url': url, 'json': kwargs.get('json')})
            path = urlparse(url).path
            host = urlparse(url).hostname
            if host == 'source.local' and path == '/api/v1/repos/search':
                return FakeResponse(200, {'data': [{
                    'id': 1,
                    'full_name': 'org/new-repo',
                    'private': True,
                    'description': 'renamed',
                }]})
            if host == 'backup.local' and path == '/api/v1/repos/org/new-repo':
                return FakeResponse(404, {}, 'not found')
            if host == 'backup.local' and path == '/api/v1/repos/org/old-repo':
                if method == 'PATCH':
                    return FakeResponse(200, {'id': 88})
                return FakeResponse(200, {'id': 88})
            if host == 'source.local' and path == '/api/v1/repos/org/new-repo/push_mirrors':
                if method == 'GET':
                    return FakeResponse(200, [{
                        'remote_name': 'backup',
                        'remote_address': 'http://backup.local/org/old-repo.git',
                    }])
                return FakeResponse(200, {'remote_name': 'backup'})
            if host == 'source.local' and path == '/api/v1/repos/org/new-repo/push_mirrors/backup':
                return FakeResponse(204, {})
            if host == 'backup.local' and path == '/api/v1/user':
                return FakeResponse(200, {'login': 'mirrorbot'})
            if host == 'source.local' and path == '/api/v1/repos/org/new-repo/push_mirrors-sync':
                return FakeResponse(200, {})
            return FakeResponse(500, {}, f'unexpected {method} {url}')

        with patch('services.mirror_service.requests.request', side_effect=fake_request):
            setup_mirror(config.id)

        repo = MirrorRepoStatus.query.filter_by(mirror_config_id=config.id, repo_name='org/new-repo').first()
        rename_audit = MirrorAuditLog.query.filter_by(mirror_config_id=config.id, action='rename_target_repo').first()
        update_audit = MirrorAuditLog.query.filter_by(mirror_config_id=config.id, action='update_push_mirror').first()
        config = MirrorConfig.query.get(config.id)
        self.assertIsNotNone(repo)
        self.assertEqual(repo.status, 'success')
        self.assertEqual(config.progress_percent, 100)
        self.assertIsNotNone(rename_audit)
        self.assertIsNotNone(update_audit)
        self.assertTrue(any(c['method'] == 'PATCH' and c['url'].endswith('/api/v1/repos/org/old-repo') for c in calls))
        self.assertTrue(any(c['method'] == 'DELETE' and c['url'].endswith('/push_mirrors/backup') for c in calls))

    def test_deprecated_config_setup_is_rejected_by_route(self):
        config = self.add_config(mode='gitea_mirror')
        with self.app.test_request_context(f'/mirrors/{config.id}/setup', method='POST'):
            response, status = setup_mirror_route.__wrapped__(config.id)

        self.assertEqual(status, 400)
        self.assertIn('已弃用', response.get_json()['error'])


if __name__ == '__main__':
    unittest.main()
