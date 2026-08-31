import json
import sys
import unittest
from datetime import datetime
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from flask import Flask

from models import Backup, GiteaServer, IntegrationAuditLog, IntegrationOutbox, IntegrationServiceToken, db
from routes.integration_routes import integration_bp
from services.integration_outbox_service import emit_event, synchronize_bootstrap_token, token_hash


class IntegrationApiTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.app.register_blueprint(integration_bp, url_prefix='/api/integrations/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        db.session.add(IntegrationServiceToken(name='sentinel-test', token_hash=token_hash('service-token'), active=True))
        server = GiteaServer(name='source', role='primary', host='source.local', ssh_user='root',
                             gitea_port=3000, gitea_url='http://source.local', api_token='绝不能返回的令牌')
        db.session.add(server)
        db.session.flush()
        backup = Backup(source_server_id=server.id, filename='dump.zip', file_path='/backup/dump.zip',
                        file_size=123, status='failed', error_msg='磁盘已满', source_api_token='绝不能返回的源令牌',
                        commit_snapshot_status='failed', started_at=datetime.utcnow(), completed_at=datetime.utcnow())
        db.session.add(backup)
        db.session.flush()
        self.backup_id = backup.id
        emit_event('backup.failed', 'backup', backup.id, 'failed', {'error': '磁盘已满', 'api_token': 'secret'})
        db.session.commit()
        self.client = self.app.test_client()
        self.headers = {'Authorization': 'Bearer service-token'}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_requires_service_token_and_audits_failure(self):
        response = self.client.get('/api/integrations/v1/health')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(IntegrationAuditLog.query.filter_by(status='failed').count(), 1)

    def test_incremental_events_are_monotonic_and_redacted(self):
        response = self.client.get('/api/integrations/v1/events?after_seq=0&limit=10', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([item['seq'] for item in payload['events']], sorted(item['seq'] for item in payload['events']))
        self.assertEqual(payload['events'][0]['payload']['api_token'], '[已脱敏]')
        self.assertGreaterEqual(IntegrationAuditLog.query.filter_by(action='integration.cursor.advance').count(), 1)

    def test_task_detail_never_returns_credentials_or_paths(self):
        response = self.client.get(f'/api/integrations/v1/tasks/backup/{self.backup_id}', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        encoded = json.dumps(response.get_json(), ensure_ascii=False)
        self.assertNotIn('绝不能返回', encoded)
        self.assertNotIn('/backup/dump.zip', encoded)
        self.assertNotIn('api_token', encoded)

    def test_task_detail_转义html并脱敏高熵内容(self):
        backup = Backup.query.get(self.backup_id)
        backup.error_msg = '<img onerror=alert(1)> ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn123456'
        db.session.commit()
        response = self.client.get(f'/api/integrations/v1/tasks/backup/{self.backup_id}', headers=self.headers)
        encoded = json.dumps(response.get_json(), ensure_ascii=False)
        self.assertNotIn('<img', encoded)
        self.assertIn('[疑似凭据，已脱敏]', encoded)

    def test_unknown_task_type_is_rejected(self):
        response = self.client.get('/api/integrations/v1/tasks/unknown/1', headers=self.headers)
        self.assertEqual(response.status_code, 404)

    def test_terminal_backup_state_and_outbox_share_commit(self):
        backup = Backup.query.get(self.backup_id)
        before = IntegrationOutbox.query.count()
        backup.status = 'success'
        backup.error_msg = ''
        db.session.commit()
        event = IntegrationOutbox.query.filter_by(task_type='backup', task_id=backup.id, event_type='backup.completed').one()
        self.assertEqual(event.status, 'success')
        self.assertGreater(IntegrationOutbox.query.count(), before)

    def test_bootstrap_token_is_hashed_and_idempotent(self):
        self.assertTrue(synchronize_bootstrap_token('rotated-service-token'))
        db.session.commit()
        self.assertFalse(synchronize_bootstrap_token('rotated-service-token'))
        self.assertNotEqual(IntegrationServiceToken.query.filter_by(active=True).one().token_hash, 'rotated-service-token')


if __name__ == '__main__':
    unittest.main()
