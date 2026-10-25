"""在交付后端镜像内核对升级日志原子性、配置转义及原密钥校验。"""
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest.mock import Mock, patch
from cryptography.fernet import Fernet

spec=importlib.util.spec_from_file_location('runtime',Path(__file__).with_name('upgrade_runtime.py'))
runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)


class RuntimeChecks(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup);self.root=Path(self.folder.name)

    def test_git_migration_requires_original_decryptable_key(self):
        database = self.root / 'manager.db'
        key = Fernet.generate_key()
        key_file = self.root / 'git-migration.key'
        key_file.write_bytes(key)
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TABLE git_migration_sources(secret_ciphertext TEXT)')
            connection.execute('INSERT INTO git_migration_sources VALUES (?)',
                               (Fernet(key).encrypt('来源凭据'.encode()).decode(),))
        def check():
            return subprocess.run([sys.executable, '-c', runtime.CHECK_ACCESS_KEYS, str(database), ''], capture_output=True)
        self.assertEqual(check().returncode, 0)
        key_file.write_bytes(Fernet.generate_key())
        self.assertNotEqual(check().returncode, 0)
        key_file.unlink()
        self.assertNotEqual(check().returncode, 0)

    def test_snapshot_includes_writes_committed_during_old_service_shutdown(self):
        path = self.root/'manager.db'
        with sqlite3.connect(path) as connection:
            connection.executescript('CREATE TABLE access_sessions(last_seen_at INTEGER); INSERT INTO access_sessions VALUES (1);')
        old = Mock(); old.attrs = {'State': {'Running': True}}
        def stop(**_):
            with sqlite3.connect(path) as connection:
                connection.execute('UPDATE access_sessions SET last_seen_at=2')
            old.attrs['State']['Running'] = False
        old.stop.side_effect = stop
        tool = runtime.Upgrade.__new__(runtime.Upgrade)
        tool.info = {'phase': 'prepared', 'paused': ['old-container'],
                     'manager_mount': {'source': 'manager-data'}, 'manager_parameters': {},
                     'original': {'backend': {'Image': 'old-image'}}}
        tool.client = Mock(); tool.client.containers.get.return_value = old
        tool.client.images.get.return_value.save.return_value = iter([b'old-image'])
        tool.containers = Mock(return_value={'backend': old})
        phases, snapshots = [], []
        tool.save = lambda: phases.append(tool.info['phase'])
        def volume(action, *_):
            if action == 'inspect': return {'database': {'busy': []}}
            self.assertEqual(action, 'backup')
            self.assertFalse(old.attrs['State']['Running'])
            with sqlite3.connect(path) as connection:
                snapshots.append(connection.execute('SELECT last_seen_at FROM access_sessions').fetchone()[0])
        tool.volume = volume; tool.maintenance = Mock()
        with patch.object(runtime, 'STATE', self.root):
            tool.stop_old(); tool.backup(); tool.activate()
        self.assertEqual(snapshots, [2])
        self.assertEqual(phases, ['stopping', 'stopped', 'backed_up', 'replacing'])
        tool.maintenance.assert_called_once_with(True)

    def test_incomplete_offline_backup_recovers_original_without_restoring_partial_archive(self):
        for phase in ('stopping', 'stopped', 'backed_up'):
            tool = runtime.Upgrade.__new__(runtime.Upgrade)
            tool.info = {'phase': phase}
            tool.unpause = Mock(); tool.volume = Mock()
            tool.rollback()
            tool.unpause.assert_called_once()
            tool.volume.assert_not_called()
        tool.info['phase'] = 'prepared'
        with self.assertRaisesRegex(RuntimeError, '完全停止'):
            tool.backup()
        with self.assertRaisesRegex(RuntimeError, '快照未完成'):
            tool.activate()

    def test_shutdown_discovered_task_cannot_be_migrated_as_an_idle_snapshot(self):
        tool = runtime.Upgrade.__new__(runtime.Upgrade)
        tool.info = {'phase': 'stopped', 'manager_mount': {'source': 'manager-data'}, 'manager_parameters': {}}
        tool.containers = Mock(return_value={})
        tool.volume = Mock(return_value={'database': {'busy': [{'table': 'jobs', 'count': 1}]}})
        with self.assertRaisesRegex(RuntimeError, '新进入的任务'):
            tool.backup()
        tool.volume.assert_called_once_with('inspect', 'manager', tool.info['manager_mount'], {})
        self.assertEqual(tool.info['phase'], 'stopped')

    def test_secret_read_keeps_bytes_and_follows_container_symlink_without_exec(self):
        content = '原执行器令牌\n'.encode()
        def archive(name, value=None, target=None):
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode='w') as bundle:
                member = tarfile.TarInfo(name)
                if target:
                    member.type = tarfile.SYMTYPE; member.linkname = target
                    bundle.addfile(member)
                else:
                    member.size = len(value); bundle.addfile(member, io.BytesIO(value))
            return iter([raw.getvalue()]), {}
        container = Mock()
        container.get_archive.side_effect = [archive('token-link', target='executor.token'),
                                             archive('executor.token', value=content)]
        self.assertEqual(runtime.read_secret_file(container, '/run/secrets/token-link'), content)
        self.assertEqual([call.args for call in container.get_archive.call_args_list],
                         [('/run/secrets/token-link',), ('/run/secrets/executor.token',)])
        container.exec_run.assert_not_called()

    def test_missing_or_directory_secret_cannot_be_silently_replaced(self):
        container = Mock()
        container.get_archive.side_effect = runtime.docker.errors.NotFound('原文件不存在')
        self.assertEqual(runtime.read_secret_file(container, '/run/secrets/missing'), b'')
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w') as bundle:
            member = tarfile.TarInfo('secret-dir'); member.type = tarfile.DIRTYPE
            bundle.addfile(member)
        container.get_archive.side_effect = None
        container.get_archive.return_value = iter([raw.getvalue()]), {}
        with self.assertRaisesRegex(RuntimeError, '普通小文件'):
            runtime.read_secret_file(container, '/run/secrets/secret-dir')

    def test_failed_atomic_replace_preserves_previous_transaction(self):
        path=self.root/'状态.json';runtime.write(path,{'阶段':'已备份'})
        with patch.object(runtime.os,'replace',side_effect=OSError('模拟替换失败')):
            with self.assertRaises(OSError):runtime.write(path,{'阶段':'开始替换'})
        self.assertEqual(json.loads(path.read_text()),{'阶段':'已备份'})
        self.assertEqual(list(self.root.iterdir()),[path])

    def test_private_transaction_and_secret_modes(self):
        path=self.root/'状态.json';runtime.write(path,{'阶段':'完成'})
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        token=self.root/'令牌';runtime.atomic_text(token,'仅测试令牌',0o444)
        self.assertEqual(token.stat().st_mode&0o777,0o444)

    def test_compose_does_not_interpolate_literal_secret(self):
        source={'environment':{'SECRET':'原有$KEY及$$符号'},'port':5000}
        self.assertEqual(runtime.escaped(source),{'environment':{'SECRET':'原有$$KEY及$$$$符号'},'port':5000})
        self.assertEqual(source['environment']['SECRET'],'原有$KEY及$$符号')

    def test_approval_view_preserves_custom_mounts_without_review_secrets(self):
        backend={'environment':{'DATABASE_URL':'sqlite:////custom/state/manager.db',
            'SSH_KEY_PATH':'/custom/key','SSH_KEY_PASSPHRASE':'原密钥口令',
            'GM_UPGRADE_MAINTENANCE_FILE':'/review/.upgrade-maintenance','SECRET_KEY':'管理会话密钥'},
            'volumes':[{'type':'volume','source':'原管理卷','target':'/custom/state'},
                       {'type':'bind','source':'/host/key','target':'/custom/key','read_only':True},
                       {'type':'bind','source':'/var/run/docker.sock','target':'/var/run/docker.sock'},
                       {'type':'volume','source':'原审查卷','target':'/review'},
                       {'type':'bind','source':'/host/master.key','target':'/run/secrets/review_master'}]}
        original=json.loads(json.dumps(backend))
        networks={'原业务网络':None}
        spec=runtime.approval_view_spec(backend,'sha256:固定后端',networks)
        self.assertEqual(spec['image'],'sha256:固定后端')
        self.assertEqual(spec['environment']['DATABASE_URL'],backend['environment']['DATABASE_URL'])
        self.assertEqual(spec['environment']['SSH_KEY_PASSPHRASE'],'原密钥口令')
        self.assertNotIn('SECRET_KEY',spec['environment'])
        self.assertEqual(spec['volumes'],backend['volumes'][:4])
        self.assertNotIn('ports',spec)
        self.assertEqual(spec['networks'],networks)
        spec['volumes'][0]['source']='不能修改原卷';spec['networks'].clear()
        self.assertEqual(backend,original);self.assertEqual(networks,{'原业务网络':None})

    def test_approval_view_chooses_longest_mount_and_deduplicates(self):
        backend={'environment':{'DATABASE_URL':'sqlite:////state/sub/manager.db',
            'SSH_KEY_PATH':'/state/sub/key','GM_UPGRADE_MAINTENANCE_FILE':'/state/sub/.maintenance'},
            'volumes':[{'type':'volume','source':'父卷','target':'/state'},
                       {'type':'volume','source':'实际子卷','target':'/state/sub'}]}
        spec=runtime.approval_view_spec(backend,'固定镜像',{'default':None})
        self.assertEqual(spec['volumes'],[backend['volumes'][1]])
        self.assertIn('approval-view',runtime.WRITERS)
        self.assertIn('approval-view',runtime.SERVICES)

    def test_existing_encrypted_data_requires_original_key(self):
        key=Fernet.generate_key();path=self.root/'主密钥';path.write_bytes(key)
        database=self.root/'review.db'
        with sqlite3.connect(database) as c:
            c.execute('CREATE TABLE settings(value TEXT)')
            c.execute('INSERT INTO settings VALUES (?)',(json.dumps({'api_key':Fernet(key).encrypt('原有模型凭据'.encode()).decode()}),))
        def check():return subprocess.run([sys.executable,'-c',runtime.CHECK_KEY,str(path),str(database)],capture_output=True)
        result=check();self.assertEqual(result.returncode,0);self.assertEqual(result.stdout,key)
        path.write_bytes(Fernet.generate_key());result=check()
        self.assertNotEqual(result.returncode,0);self.assertEqual(result.stdout,b'')
        path.unlink();result=check();self.assertNotEqual(result.returncode,0)

    def test_directory_key_decryptability_and_fingerprints_are_verified(self):
        import hashlib
        key=Fernet.generate_key(); key_file=self.root/'access.key'; key_file.write_bytes(key)
        session_file=self.root/'.session-key'; session_file.write_text('原持久会话密钥')
        database=self.root/'manager.db'
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TABLE access_directory_sources(password_ciphertext TEXT)')
            connection.execute('INSERT INTO access_directory_sources VALUES (?)',
                (Fernet(key).encrypt('目录查询密码'.encode()).decode(),))
        def check():
            return subprocess.run([sys.executable,'-c',runtime.CHECK_ACCESS_KEYS,str(database),''],capture_output=True)
        result=check();self.assertEqual(result.returncode,0,result.stderr)
        actual=json.loads(result.stdout)
        self.assertEqual(actual[str(key_file)],hashlib.sha256(key).hexdigest())
        self.assertEqual(actual[str(session_file)],hashlib.sha256(session_file.read_bytes()).hexdigest())
        self.assertNotIn('目录查询密码',result.stdout.decode())
        key_file.write_bytes(Fernet.generate_key())
        self.assertNotEqual(check().returncode,0)
        key_file.unlink()
        self.assertNotEqual(check().returncode,0)



if __name__=='__main__':unittest.main()
