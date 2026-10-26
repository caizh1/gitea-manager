"""合成实例与真实SQLite/文件锁/SIGKILL验证；不接触任何真实备份或服务。"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask
from models import db, Backup, GiteaServer
from services import backup_recovery as recovery


def metadata(identifier, running):
    return {'Id': identifier, 'Name': '/' + identifier, 'Image': '固定合成镜像', 'Created': '固定创建时间',
            'Mounts': [{'Source': '合成卷', 'Destination': '/data', 'RW': True}], 'RestartCount': 0,
            'State': {'Running': running, 'Status': 'running' if running else 'exited',
                      'StartedAt': '固定原启动时间', 'FinishedAt': '' if running else '固定停止时间',
                      'Pid': 100 if running else 0, 'ExitCode': 0, 'OOMKilled': False,
                      'Paused': False, 'Restarting': False, 'Dead': False},
            'Config': {'Env': [], 'Labels': {}}, 'HostConfig': {}}


class Container:
    def __init__(self, path):
        self.path = path
        self.reload()
        self.starts = self.stops = 0

    def reload(self):
        self.attrs = json.loads(self.path.read_text())
        self.id = self.attrs['Id']; self.name = self.attrs['Name'].lstrip('/')

    def save(self):
        self.path.write_text(json.dumps(self.attrs))

    def stop(self):
        self.stops += 1; self.reload()
        self.attrs['State'].update(Running=False, Status='exited', Pid=0, FinishedAt='固定停止时间')
        self.save()

    def start(self):
        self.starts += 1; self.reload()
        self.attrs['State'].update(Running=True, Status='running', Pid=101, StartedAt='本任务恢复启动时间')
        self.attrs['RestartCount'] = 0
        self.save()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('config.BACKUP_DIR', str(self.root / 'backups')))
        self.stack.enter_context(patch('config.DATABASE_URL', 'sqlite:///' + str(self.root / 'manager.db')))
        self.app = Flask('合成备份中断恢复')
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + str(self.root / 'manager.db')
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.stack.enter_context(self.app.app_context())
        db.create_all()
        self.addCleanup(db.session.remove)
        self.server = GiteaServer(name='合成源', host='隔离宿主', ssh_user='root', connection_mode='ssh',
                                  gitea_container='source', pg_container='database', pg_dbname='合成库',
                                  pg_user='合成用户', gitea_port=3000, gitea_url='http://invalid', api_token='合成值')
        db.session.add(self.server); db.session.commit()
        self.task = Backup(source_server_id=self.server.id, filename='合成样本.zip', file_path='', status='running',
                           diagnostic_id='合成诊断', started_at=datetime.utcnow() - timedelta(minutes=10))
        db.session.add(self.task); db.session.commit()
        for name in ('source', 'database'):
            (self.root / (name + '.json')).write_text(json.dumps(metadata(name, True)))
        self.source = Container(self.root / 'source.json'); self.database = Container(self.root / 'database.json')
        self.client = MagicMock()
        self.client.containers.get.side_effect = lambda identifier: {'source': self.source, 'database': self.database}[identifier]
        self.client.containers.list.return_value = []
        self.managed = self.stack.enter_context(patch('services.docker_service.managed_client', return_value=self.client))
        self.health = self.stack.enter_context(patch('services.commit_service._require_target_health'))
        self.alert = self.stack.enter_context(patch('services.alert_service.on_backup_completed'))

    def journal(self):
        path, _ = recovery.paths(recovery.directory(), self.task.id, self.task.recovery_token)
        result = recovery.Journal.create(path, self.task)
        result.attach(self.server, self.source, self.database, 'manager-backup-' + 'a' * 32)
        self.addCleanup(lambda: result.instance_lock.close())
        return result

    def test_killed_process_releases_real_os_lock_and_recovers_exact_original_source(self):
        source_id = self.server.id
        for phase in ('停止回执前', '完整停止态之后', '导出过程中'):
            with self.subTest(阶段=phase):
                if phase != '停止回执前':
                    self.task = Backup(source_server_id=source_id, filename='合成样本.zip', file_path='', status='running')
                    db.session.add(self.task); db.session.commit()
                    self.source.attrs = metadata('source', True); self.source.save()
                identifier, token = self.task.id, self.task.recovery_token
                db.session.remove()
                read, write = os.pipe()
                pid = os.fork()
                if pid == 0:
                    os.close(read)
                    try:
                        task = db.session.get(Backup, identifier)
                        server = db.session.get(GiteaServer, source_id)
                        with recovery.execution(task) as journal:
                            journal.attach(server, self.source, self.database, 'manager-backup-' + 'b' * 32)
                            journal.before_stop(self.source)
                            self.source.stop()
                            if phase != '停止回执前':
                                journal.stopped(self.source)
                            os.write(write, b'1')
                            signal.pause()
                    except BaseException:
                        import traceback
                        traceback.print_exc()
                        os._exit(91)
                    os._exit(92)
                os.close(write)
                try:
                    self.assertTrue(select.select([read], [], [], 10)[0], '子进程没有到达固定中断点')
                    self.assertEqual(os.read(read, 1), b'1')
                    # 真正持锁的工作进程仍活跃，巡检必须只跳过，不能按running耗时接管。
                    recovery.recover_pending()
                    self.managed.assert_not_called()
                finally:
                    os.close(read); os.kill(pid, signal.SIGKILL)
                    _, status = os.waitpid(pid, 0)
                self.assertTrue(os.WIFSIGNALED(status)); self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
                recovery.recover_pending()
                current = db.session.get(Backup, identifier, populate_existing=True)
                self.assertEqual(current.status, 'failed'); self.assertFalse(current.recovery_pending)
                self.source.reload(); self.assertTrue(self.source.attrs['State']['Running'])
                self.assertEqual(self.database.starts + self.database.stops, 0)
                path, _ = recovery.paths(recovery.directory(), identifier, token)
                saved = recovery.Journal.load(path, identifier, token, source_id)
                self.assertTrue(saved.value['事务完成']); self.assertTrue(saved.value['源收尾已完成'])
                count = self.source.starts
                recovery.recover_pending()
                self.assertEqual(self.source.starts, count)
                self.managed.reset_mock()

    def test_stop_request_without_engine_ack_is_not_false_completion(self):
        journal = self.journal(); journal.before_stop(self.source)
        with self.assertRaisesRegex(RuntimeError, '结果未知'):
            recovery.recover_source(self.client, self.server, journal)
        self.assertFalse(journal.value['源收尾已完成']); self.assertEqual(self.source.starts, 0)
        self.source.stop()  # 模拟引擎随后完成原请求；没有重发stop。
        recovery.recover_source(self.client, self.server, journal)
        self.assertTrue(journal.value['源收尾已完成']); self.assertEqual(self.source.starts, 1)

    def test_disk_full_after_stop_does_not_skip_original_source_start(self):
        journal = self.journal(); journal.before_stop(self.source); self.source.stop(); journal.stopped(self.source)
        failure = OSError('合成ENOSPC')
        with patch.object(journal, 'save', side_effect=failure), self.assertRaises(OSError) as result:
            recovery.recover_source(self.client, self.server, journal)
        self.assertIs(result.exception, failure)
        self.source.reload(); self.assertTrue(self.source.attrs['State']['Running'])
        self.assertEqual(self.source.starts, 1)

    def test_unknown_source_or_pg_lifecycle_refuses_all_mutations(self):
        for name in ('source', 'database'):
            with self.subTest(对象=name):
                journal = self.journal(); journal.before_stop(self.source); self.source.stop(); journal.stopped(self.source)
                item = self.source if name == 'source' else self.database
                item.attrs['Image'] = '未知镜像'; item.save()
                with self.assertRaisesRegex(RuntimeError, '已变化'):
                    recovery.recover_source(self.client, self.server, journal)
                self.assertEqual(self.source.starts + self.database.starts, 0)
                journal.instance_lock.close(); journal.path.unlink()
                self.source.attrs = metadata('source', True); self.source.save()
                self.database.attrs = metadata('database', True); self.database.save()

    def test_originally_stopped_source_remains_stopped(self):
        self.source.stop(); journal = self.journal()
        recovery.recover_source(self.client, self.server, journal)
        self.assertEqual(self.source.starts, 0); self.health.assert_not_called()

    def test_docker_explicit_start_can_reset_prior_restart_counter(self):
        self.source.attrs['RestartCount'] = 3; self.source.save()
        journal = self.journal(); journal.before_stop(self.source); self.source.stop(); journal.stopped(self.source)
        recovery.recover_source(self.client, self.server, journal)
        self.assertTrue(journal.value['源收尾已完成']); self.assertEqual(self.source.attrs['RestartCount'], 0)

    def test_legacy_running_task_has_no_new_recovery_ownership(self):
        self.task.recovery_token = ''; self.task.recovery_pending = False; db.session.commit()
        recovery.recover_pending()
        self.managed.assert_not_called(); self.assertEqual(self.task.status, 'running')

    def test_unresolved_task_blocks_new_backup_restore_and_alias_database(self):
        journal = self.journal()
        with self.assertRaisesRegex(RuntimeError, '未确认收尾'):
            recovery.ensure_ready(self.server.id)
        from services.restore_recovery import database_lease
        with self.assertRaises(BlockingIOError):
            database_lease(self.database.id, self.server.pg_dbname)
        journal.instance_lock.close()
        with self.assertRaisesRegex(RuntimeError, '别名绕过'):
            recovery.ensure_database_ready(self.database.id, self.server.pg_dbname)
        from services.local_restore import restore
        with self.assertRaisesRegex(RuntimeError, '未确认收尾'):
            restore(self.server, None, None, None)
        self.managed.assert_not_called()

    def test_completed_task_cannot_be_replayed_or_overwrite_diagnostics(self):
        self.task.status = 'success'; self.task.recovery_pending = False
        self.task.diagnostic_id = '原已完成诊断'; db.session.commit()
        from services.gitea_service import do_backup
        # 直接测试实例锁以内入口；外部迁移互斥已有独立用例。
        with self.assertRaisesRegex(RuntimeError, '已被终结'):
            do_backup.__wrapped__(self.task.id)
        db.session.refresh(self.task)
        self.assertEqual(self.task.status, 'success'); self.assertEqual(self.task.diagnostic_id, '原已完成诊断')
        self.managed.assert_not_called()

    def test_legacy_schema_migration_does_not_adopt_old_rows(self):
        import ast, sqlite3
        tree = ast.parse((Path('/app') / 'app.py').read_text())
        statements = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                      and isinstance(node.value, str) and node.value.startswith('ALTER TABLE backups ADD COLUMN recovery_')]
        self.assertEqual(len(statements), 2)
        with sqlite3.connect(':memory:') as connection:
            connection.execute('CREATE TABLE backups(id INTEGER PRIMARY KEY,status TEXT)')
            connection.executemany('INSERT INTO backups VALUES (?,?)', [(20, 'running'), (21, 'running'), (31, 'running'), (39, 'running')])
            for statement in statements:
                connection.execute(statement)
            self.assertEqual(connection.execute('SELECT recovery_token,recovery_pending FROM backups').fetchall(), [('', 0)] * 4)

    def test_server_change_and_delete_cannot_destroy_pending_recovery_identity(self):
        recovery.install_guards(self.app)
        self.app.add_url_rule('/source/<int:sid>', endpoint='servers.update_server', view_func=lambda sid: '不应修改', methods=['PUT'])
        self.app.add_url_rule('/source/<int:sid>', endpoint='servers.delete_server', view_func=lambda sid: '不应删除', methods=['DELETE'])
        with self.app.test_client() as client:
            self.assertEqual(client.put('/source/' + str(self.server.id)).status_code, 409)
            self.assertEqual(client.delete('/source/' + str(self.server.id)).status_code, 409)

    def test_wrong_journal_token_and_symlink_are_rejected(self):
        journal = self.journal()
        with self.assertRaisesRegex(RuntimeError, '不匹配'):
            recovery.Journal.load(journal.path, self.task.id, 'c' * 32, self.server.id)
        link = self.root / 'link.json'; link.symlink_to(journal.path)
        with self.assertRaisesRegex(RuntimeError, '归属'):
            recovery.Journal.load(link, self.task.id, self.task.recovery_token, self.server.id)

    def test_atomic_lock_cleanup_never_deletes_later_foreign_lock(self):
        journal = self.journal(); journal.before_lock()
        lock, retired, staged = [self.root / name for name in ('lock', 'retired', 'staged')]
        owner = json.dumps({'所有者': recovery.OWNER, '任务ID': self.task.id, '随机校验': self.task.recovery_token}).encode()
        retired.mkdir(); (retired / 'backup-owner.json').write_bytes(owner)
        lock.mkdir(); (lock / 'foreign').write_text('其他任务')
        command = ['sh', '-ec', recovery._unlock_script(), 'sh', str(lock), str(retired),
                   journal.value['锁所有者SHA256'], str(staged), 'yes']
        subprocess.run(command, check=True, capture_output=True)
        self.assertEqual((lock / 'foreign').read_text(), '其他任务'); self.assertFalse(retired.exists())
        # 在原子移锁之后、删除owner之后再中断，同一退休目录也只能rmdir。
        retired.mkdir(); subprocess.run(command, check=True, capture_output=True)
        self.assertTrue(lock.exists()); self.assertFalse(retired.exists())
        result = subprocess.run(command, check=False, capture_output=True)
        self.assertNotEqual(result.returncode, 0); self.assertTrue((lock / 'foreign').exists())


if __name__ == '__main__':
    unittest.main()
