import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from flask import Flask

from models import RestoreStepLog, db
from services.gitea_service import (
    _checked_remote_name,
    _exception_message,
    _restore_remote,
    _verify_remote_archive,
)
from services.remote_job_service import (
    RemoteJobStatus,
    RemoteJobTimeout,
    _parse_status,
    _wrapper_script,
    run_remote_job,
)
from services.restore_step_service import finish_restore_step, restore_step_to_dict, start_restore_step
from services.ssh_service import SSHService


class _双流通道:
    def __init__(self, stdout_chunks, stderr_chunks):
        self.stdout_chunks = list(stdout_chunks)
        self.stderr_chunks = list(stderr_chunks)

    def settimeout(self, _timeout):
        return None

    def exec_command(self, _command):
        return None

    def recv_ready(self):
        return bool(self.stdout_chunks)

    def recv(self, _size):
        return self.stdout_chunks.pop(0)

    def recv_stderr_ready(self):
        return bool(self.stderr_chunks)

    def recv_stderr(self, _size):
        return self.stderr_chunks.pop(0)

    def exit_status_ready(self):
        return not self.stdout_chunks and not self.stderr_chunks

    def recv_exit_status(self):
        return 0


class _伪客户端:
    def __init__(self, channel):
        self.channel = channel
        self.closed = False

    def get_transport(self):
        return self

    def open_session(self, timeout=None):
        return self.channel

    def close(self):
        self.closed = True


class SSH命令测试(unittest.TestCase):
    def test_大量_stdout_stderr_会被同时读取(self):
        stdout = [b'o' * 65536 for _ in range(8)]
        stderr = [b'e' * 65536 for _ in range(8)]
        channel = _双流通道(stdout, stderr)
        client = _伪客户端(channel)
        service = SSHService('example.invalid')

        with patch.object(service, '_get_client', return_value=client):
            code, out, err = service.exec('large-output', timeout=2)

        self.assertEqual(code, 0)
        self.assertEqual(len(out), 8 * 65536)
        self.assertEqual(len(err), 8 * 65536)
        self.assertTrue(client.closed)


class 远端作业测试(unittest.TestCase):
    def test_包装器使用日志重定向和原子退出码(self):
        script = _wrapper_script('/tmp/gitea-manager/restore-7/steps/sql', 'false')
        self.assertIn('> stdout.log 2> stderr.log', script)
        self.assertIn('mv "$exit_tmp" exit_code', script)
        self.assertIn('child_pid', script)

    def test_状态解析读取原子状态文件字段(self):
        status = _parse_status(
            'state=done\nexit_code=3\npid=10\nchild_pid=11\n'
            'started_at=100\nfinished_at=120\nstdout_size=99\nstderr_size=8\n'
        )
        self.assertEqual(status.state, 'done')
        self.assertEqual(status.exit_code, 3)
        self.assertEqual(status.started_epoch, 100)
        self.assertEqual(status.finished_epoch, 120)

    @patch('services.remote_job_service.time.sleep', return_value=None)
    @patch('services.remote_job_service.read_remote_job_tails', return_value=('完成', ''))
    @patch('services.remote_job_service.start_remote_job')
    def test_SSH瞬断后轮询同一远端作业而不重复启动(self, start, _tails, _sleep):
        states = [
            OSError('连接重置'),
            RemoteJobStatus(state='running', stdout_size=10),
            RemoteJobStatus(state='done', exit_code=0, stdout_size=20),
        ]
        with patch('services.remote_job_service.get_remote_job_status', side_effect=states):
            result = run_remote_job(
                object(),
                '/tmp/job',
                'long-command',
                timeout_seconds=60,
                poll_seconds=0,
                reconnect_grace_seconds=300,
                diagnostic_interval_seconds=999,
            )

        self.assertEqual(result.exit_code, 0)
        start.assert_called_once()

    @patch('services.remote_job_service.time.sleep', return_value=None)
    @patch('services.remote_job_service.read_remote_job_tails', return_value=('完成', ''))
    @patch('services.remote_job_service.start_remote_job')
    def test_超过600秒的大仓库作业不会触发旧超时(self, start, _tails, _sleep):
        states = [
            RemoteJobStatus(state='running'),
            RemoteJobStatus(state='running'),
            RemoteJobStatus(state='done', exit_code=0),
        ]
        clock = [0, 301, 301, 601, 601, 602, 602]
        with patch('services.remote_job_service.get_remote_job_status', side_effect=states), \
             patch('services.remote_job_service.time.monotonic', side_effect=clock):
            result = run_remote_job(
                object(),
                '/tmp/job',
                'copy-qemu-repository',
                timeout_seconds=7200,
                poll_seconds=0,
                diagnostic_interval_seconds=9999,
            )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.elapsed_seconds, 602)
        start.assert_called_once()

    @patch('services.remote_job_service.read_remote_job_tails', return_value=('out', 'err'))
    @patch('services.remote_job_service.terminate_remote_job')
    @patch('services.remote_job_service.start_remote_job')
    def test_超时会终止远端作业并保留日志尾部(self, _start, terminate, _tails):
        with patch('services.remote_job_service.time.monotonic', side_effect=[0, 11]):
            with self.assertRaises(RemoteJobTimeout) as raised:
                run_remote_job(object(), '/tmp/job', 'sleep 99', timeout_seconds=10)

        terminate.assert_called_once()
        self.assertEqual(raised.exception.result.stdout_tail, 'out')
        self.assertEqual(raised.exception.result.stderr_tail, 'err')


class 恢复步骤日志测试(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_日志尾部会截断且指标保持结构化(self):
        # SQLite 默认不检查外键，测试只关注步骤日志的持久化契约。
        step = start_restore_step(123, 'sql', '导入数据库', remote_job_dir='/tmp/job')
        finish_restore_step(
            step,
            status='failed',
            exit_code=2,
            stdout_tail='o' * 7000,
            stderr_tail='e' * 7000,
            metrics={'已运行秒数': 301},
        )
        saved = RestoreStepLog.query.get(step.id)
        payload = restore_step_to_dict(saved)
        self.assertEqual(len(payload['stdout_tail']), 6000)
        self.assertEqual(len(payload['stderr_tail']), 6000)
        self.assertEqual(payload['metrics']['已运行秒数'], 301)
        self.assertEqual(payload['remote_job_dir'], '/tmp/job')


class 远程恢复编排测试(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.write(b'zip-content')
        handle.close()
        self.backup_path = handle.name
        self.target = SimpleNamespace(
            host='10.10.5.22',
            ssh_port=22,
            ssh_user='root',
            gitea_container='gitea',
            pg_container='gitea-postgres',
            pg_user='gitea',
            pg_dbname='gitea',
            name='Gitea 备 22',
        )
        self.backup = SimpleNamespace(file_path=self.backup_path, filename='dump.zip')
        self.task = SimpleNamespace(id=88)

    def tearDown(self):
        os.unlink(self.backup_path)

    def _运行编排(self, import_fails=False, fail_long_key=None, fail_simple_key=None):
        long_steps = []
        simple_steps = []
        ssh_commands = []
        self.last_long_steps = long_steps
        self.last_simple_steps = simple_steps
        self.last_ssh_commands = ssh_commands

        class 假SSH:
            def __init__(self, *_args, **_kwargs):
                pass

            def put_file(self, *_args, **_kwargs):
                return None

            def exec(self, command, timeout=None, output_limit=None):
                ssh_commands.append(command)
                if 'docker inspect -f' in command:
                    return 0, 'gitea/image:latest', ''
                return 0, '', ''

        def simple(_task, key, _label, _percent, _detail, _action):
            simple_steps.append(key)
            if key == fail_simple_key:
                raise RuntimeError(f'{key} 失败')
            if key == 'verify_upload':
                return {'SHA-256': 'ok'}
            if key in {'copy_config', 'start_gitea'}:
                return _action()
            return None

        def long(_task, _ssh, _root, key, _label, _percent, _detail, command, **_kwargs):
            long_steps.append((key, command))
            if (key == 'import_database' and import_fails) or key == fail_long_key:
                message = '非法 SQL' if key == 'import_database' else f'{key} 失败'
                raise RuntimeError(message)
            return SimpleNamespace(exit_code=0, elapsed_seconds=1, stdout_tail='', stderr_tail='', metrics={})

        fake_step = SimpleNamespace(id=1)
        patches = (
            patch('services.gitea_service.SSHService', 假SSH),
            patch('services.gitea_service._run_simple_restore_step', side_effect=simple),
            patch('services.gitea_service._run_long_restore_step', side_effect=long),
            patch('services.gitea_service.start_restore_step', return_value=fake_step),
            patch('services.gitea_service.finish_restore_step'),
            patch('services.gitea_service.update_restore_step'),
            patch('services.gitea_service.update_restore_progress'),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            result = _restore_remote(self.target, self.backup, self.task)
        return result, simple_steps, long_steps

    def test_非法SQL停止后续复制和启动(self):
        with self.assertRaisesRegex(RuntimeError, '非法 SQL'):
            self._运行编排(import_fails=True)
        self.assertEqual([key for key, _command in self.last_long_steps], ['extract', 'import_database'])
        self.assertNotIn('copy_config', self.last_simple_steps)
        self.assertNotIn('start_gitea', self.last_simple_steps)

    def test_数据库重建失败不会开始SQL导入(self):
        with self.assertRaisesRegex(RuntimeError, 'recreate_database 失败'):
            self._运行编排(fail_simple_key='recreate_database')
        self.assertEqual([key for key, _command in self.last_long_steps], ['extract'])

    def test_仓库复制失败不会继续数据复制或启动(self):
        with self.assertRaisesRegex(RuntimeError, 'copy_repos 失败'):
            self._运行编排(fail_long_key='copy_repos')
        keys = [key for key, _command in self.last_long_steps]
        self.assertNotIn('copy_data', keys)
        self.assertNotIn('start_gitea', self.last_simple_steps)

    def test_hooks失败会作为恢复失败向上抛出(self):
        with self.assertRaisesRegex(RuntimeError, 'regenerate_hooks 失败'):
            self._运行编排(fail_long_key='regenerate_hooks')

    def test_目标路径_SQL选项和app_ini覆盖符合恢复契约(self):
        result, simple_steps, long_steps = self._运行编排()
        commands = dict(long_steps)
        self.assertEqual(result, '/tmp/gitea-manager/restore-88')
        self.assertIn('psql -X -v ON_ERROR_STOP=1 -v VERBOSITY=terse', commands['import_database'])
        self.assertIn('< /tmp/gitea-manager/restore-88/extracted/gitea-db.sql', commands['import_database'])
        self.assertIn('gitea:/data/git/repositories/', commands['copy_repos'])
        self.assertIn('find /data/git/repositories -mindepth 1 -delete', commands['copy_repos'])
        self.assertIn('gitea:/data/gitea/', commands['copy_data'])
        self.assertIn('find /data/gitea -mindepth 1 -delete', commands['copy_data'])
        self.assertIn('copy_config', simple_steps)
        self.assertIn('start_gitea', simple_steps)
        self.assertTrue(any('gitea:/data/gitea/conf/app.ini' in command for command in self.last_ssh_commands))

    def test_容器或数据库名不允许命令注入字符(self):
        self.assertEqual(_checked_remote_name('gitea-postgres_1', '容器'), 'gitea-postgres_1')
        with self.assertRaises(ValueError):
            _checked_remote_name('gitea; reboot', '容器')

    def test_空文本异常也会生成非空错误(self):
        message = _exception_message(TimeoutError())
        self.assertIn('TimeoutError', message)
        self.assertTrue(message.strip())

    def test_上传哈希不一致会阻断恢复(self):
        ssh = SimpleNamespace(exec=lambda *_args, **_kwargs: (0, '10\nwrong-hash\n100\n1000', ''))
        with self.assertRaisesRegex(RuntimeError, '上传校验不一致'):
            _verify_remote_archive(ssh, '/tmp/backup.zip', 10, 'expected-hash')

    def test_解压空间不足会阻断恢复(self):
        ssh = SimpleNamespace(exec=lambda *_args, **_kwargs: (0, '10\nexpected-hash\n1001\n1000', ''))
        with self.assertRaisesRegex(RuntimeError, '/tmp 空间不足'):
            _verify_remote_archive(ssh, '/tmp/backup.zip', 10, 'expected-hash')


if __name__ == '__main__':
    unittest.main()
