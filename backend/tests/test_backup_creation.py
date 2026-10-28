"""真实 SQLite 与实例文件锁验证备份入口去重；后台执行替身不会操作实际服务。"""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from flask import Flask
from models import Backup, GiteaServer, db
from routes.backup_routes import backup_bp
from svn_migration.guards import install, target_guard


class BackupCreationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='backup-creation-')
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / 'manager.db'
        self.app = Flask('隔离备份入口')
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + str(self.database),
                               TESTING=True, LOGIN_DISABLED=True)
        db.init_app(self.app)
        self.app.register_blueprint(backup_bp, url_prefix='/api')
        install(self.app)
        with self.app.app_context():
            db.create_all()
            for name in ('测试源甲', '测试源乙'):
                db.session.add(GiteaServer(name=name, host='127.0.0.1', ssh_user='root', role='primary',
                                          gitea_port=3000, gitea_url='http://127.0.0.1:3000', api_token='测试凭据'))
            db.session.commit()
            self.sources = [server.id for server in GiteaServer.query.order_by(GiteaServer.id)]
        self.addCleanup(self.close_database)
        dispatch = patch('routes.backup_routes.task_manager.run_backup')
        self.dispatch = dispatch.start()
        self.addCleanup(dispatch.stop)

    def close_database(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def create(self, source=None):
        with self.app.test_client() as client:
            return client.post('/api/backups', json={'source_server_id': source or self.sources[0]})

    def test_repeated_request_before_worker_start_creates_one_backup(self):
        first, second = self.create(), self.create()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json['backup_id'], first.json['id'])
        self.dispatch.assert_called_once_with(first.json['id'])
        with self.app.app_context():
            self.assertEqual(Backup.query.count(), 1)

    def test_running_pending_and_unrecovered_failure_block_new_backup(self):
        first = self.create()
        for status, pending in (('running', False), ('pending', False), ('failed', True)):
            with self.subTest(状态=status, 等待恢复=pending):
                with self.app.app_context():
                    backup = db.session.get(Backup, first.json['id'])
                    backup.status, backup.recovery_pending = status, pending
                    db.session.commit()
                self.assertEqual(self.create().status_code, 409)
        self.assertEqual(self.dispatch.call_count, 1)

    def test_finished_and_recovered_backup_does_not_block_next_request(self):
        for status in ('success', 'failed'):
            first = self.create()
            self.assertEqual(first.status_code, 201)
            with self.app.app_context():
                backup = db.session.get(Backup, first.json['id'])
                backup.status, backup.recovery_pending = status, False
                db.session.commit()
            second = self.create()
            self.assertEqual(second.status_code, 201)
            with self.app.app_context():
                backup = db.session.get(Backup, second.json['id'])
                backup.status, backup.recovery_pending = 'success', False
                db.session.commit()

    def test_other_source_can_create_its_own_backup(self):
        self.assertEqual(self.create(self.sources[0]).status_code, 201)
        self.assertEqual(self.create(self.sources[1]).status_code, 201)
        self.assertEqual(self.dispatch.call_count, 2)

    def test_existing_worker_lock_rejects_request_without_inserting(self):
        with self.app.app_context(), target_guard(self.sources[0]):
            self.assertEqual(self.create().status_code, 409)
            self.assertEqual(Backup.query.count(), 0)
        self.dispatch.assert_not_called()

    def test_parallel_requests_create_one_record(self):
        barrier = threading.Barrier(8)
        def create(_):
            barrier.wait(timeout=5)
            return self.create().status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(create, range(8)))
        self.assertEqual(sorted(statuses), [201] + [409] * 7)
        with self.app.app_context():
            self.assertEqual(Backup.query.count(), 1)
        self.assertEqual(self.dispatch.call_count, 1)

    def test_two_processes_share_the_same_reservation(self):
        script = '''
import json, sys
from unittest.mock import patch
from flask import Flask
from models import db
from routes.backup_routes import backup_bp
from svn_migration.guards import install
app = Flask('双进程备份核验')
app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + sys.argv[1], LOGIN_DISABLED=True, TESTING=True)
db.init_app(app)
app.register_blueprint(backup_bp, url_prefix='/api')
install(app)
print('就绪', flush=True)
sys.stdin.readline()
with patch('routes.backup_routes.task_manager.run_backup'):
    response = app.test_client().post('/api/backups', json={'source_server_id': int(sys.argv[2])})
    print(json.dumps({'状态码': response.status_code}), flush=True)
'''
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        processes = [subprocess.Popen([sys.executable, '-c', script, str(self.database), str(self.sources[0])],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, env=environment) for _ in range(2)]
        try:
            for process in processes:
                self.assertEqual(process.stdout.readline().strip(), '就绪')
            for process in processes:
                process.stdin.write('开始\n'); process.stdin.flush()
            statuses = []
            for process in processes:
                output, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, error)
                statuses.append(json.loads(output)['状态码'])
            self.assertEqual(sorted(statuses), [201, 409])
            with self.app.app_context():
                self.assertEqual(Backup.query.count(), 1)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill(); process.communicate()
