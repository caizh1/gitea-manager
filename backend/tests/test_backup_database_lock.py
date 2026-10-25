"""真实 SQLite 双连接验证：备份耗时处理不得阻塞登录写入。"""
from contextlib import ExitStack
import io
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from flask import Flask
from models import db, Backup, GiteaServer, Setting
import access.models
from services.backup_consistency import consistent_backup
from auth import init_auth
from routes.auth_routes import auth_bp
from access.authentication import initialize_access
from werkzeug.security import generate_password_hash


class BackupDatabaseLockTests(unittest.TestCase):
    def test_login_can_write_during_secret_and_artifact_io(self):
        for external_secret in (False, True):
            with self.subTest(外置密钥=external_secret), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                app = Flask('备份事务验收')
                app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + str(root/'manager.db')
                app.config.update(TESTING=True, SECRET_KEY='隔离认证会话测试密钥',
                    ACCESS_KEY_FILE=str(root/'access.key'),
                    SQLALCHEMY_ENGINE_OPTIONS={'connect_args': {'timeout': .2}})
                db.init_app(app)
                init_auth(app)
                app.register_blueprint(auth_bp, url_prefix='/api')
                with app.app_context():
                    db.create_all()
                    db.session.add(Setting(key='admin_password', value=generate_password_hash('隔离登录密码-123')))
                    db.session.commit()
                    initialize_access()
                    server = GiteaServer(name='隔离源', host='127.0.0.1', ssh_user='root',
                        gitea_port=3000, gitea_url='http://127.0.0.1', api_token='隔离值')
                    db.session.add(server); db.session.flush()
                    backup = Backup(source_server_id=server.id, filename='sample.zip', file_path='', status='running')
                    db.session.add(backup); db.session.commit()
                    body = io.BytesIO()
                    with zipfile.ZipFile(body, 'w') as archive:
                        archive.writestr('gitea-db.sql', '隔离数据库样本')
                    raw = io.BytesIO()
                    with tarfile.open(fileobj=raw, mode='w') as archive:
                        item = tarfile.TarInfo('sample.zip'); item.size = len(body.getvalue())
                        archive.addfile(item, io.BytesIO(body.getvalue()))
                    client, source, database, helper = (MagicMock() for _ in range(4))
                    source.status = 'running'
                    source.attrs = {'Image':'隔离镜像', 'Config':{}, 'NetworkSettings':{'Networks':{'network':{}}}}
                    database.attrs = {'NetworkSettings':{'Networks':{'network':{}}}}
                    client.containers.get.side_effect = [source, database]
                    client.containers.run.return_value = helper
                    helper.get_archive.return_value = ((block for block in [raw.getvalue()]), {})
                    source.get_archive.return_value = ((block for block in [b'keys']), {})
                    phases = []
                    def login_write(phase):
                        # 与登录请求使用独立连接；短超时使真实写锁竞争立即暴露。
                        with sqlite3.connect(str(root/'manager.db'), timeout=.1) as connection:
                            connection.execute('INSERT INTO access_login_throttle(key,window_at,attempts) VALUES (?,0,1)', (phase,))
                        results = []
                        def login():
                            try:
                                with app.test_client() as client:
                                    response = client.post('/api/login', json={'password':'隔离登录密码-123'}, base_url='https://localhost')
                                    results.append(response.status_code)
                                    results.append(client.get('/api/session', base_url='https://localhost').json['authenticated'])
                            except Exception as exc:
                                results.append(type(exc).__name__)
                        thread = threading.Thread(target=login)
                        thread.start(); thread.join(timeout=5)
                        self.assertFalse(thread.is_alive(), '备份阶段阻塞真实登录请求')
                        self.assertEqual(results, [200, True])
                        phases.append(phase)
                    def secret_stage(*args):
                        if external_secret:
                            _ = server.host  # 与外置密钥的 managed_client 配置读取一致。
                        login_write('密钥文件处理期间')
                        backup.file_size = 123
                    def artifact_stage(*args):
                        _ = server.pg_user  # 与制品数据库采集读取配置一致，可能触发自动刷写。
                        login_write('制品传输与校验期间')
                    def progress(*args, **kwargs):
                        db.session.commit()  # 真实进度更新同样会使 ORM 实例过期。
                    with ExitStack() as stack:
                        stack.enter_context(patch('config.BACKUP_DIR', str(root/'backups')))
                        stack.enter_context(patch('services.docker_service.managed_client', return_value=client))
                        for name in ('check_layout','require_quiet','execute'):
                            stack.enter_context(patch('services.backup_consistency.'+name))
                        stack.enter_context(patch('services.backup_consistency.read_file', return_value=(b'',None)))
                        stack.enter_context(patch('services.backup_consistency.repository_snapshot', return_value={}))
                        for name in ('identity','table_snapshot','append_manifest'):
                            stack.enter_context(patch('services.backup_manifest.'+name, return_value={}))
                        stack.enter_context(patch('services.backup_dependencies.capture_startup_files', return_value={}))
                        stack.enter_context(patch('services.backup_dependencies.append_startup_files'))
                        stack.enter_context(patch('services.restore_secrets.capture_backup_secret', side_effect=secret_stage))
                        stack.enter_context(patch('services.restore_archive.validate_ssh_archive'))
                        stack.enter_context(patch('services.backup_actions.append_artifacts', side_effect=artifact_stage))
                        stack.enter_context(patch('services.operation_progress.update_backup_progress', side_effect=progress))
                        consistent_backup(server, backup)
                    self.assertEqual(phases, ['密钥文件处理期间','制品传输与校验期间'])
                    source.start.assert_called_once()
                    db.session.remove()


if __name__ == '__main__':
    unittest.main()
