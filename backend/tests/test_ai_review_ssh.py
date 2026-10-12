"""验证任务连接复用、失败关闭、无重放，以及页面诊断不泄露敏感命令。"""
import errno
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import paramiko

from ai_review.setup_transport import AccessError, ConnectionPool, ContainerAccess, RemoteSession


def server(host='ssh.internal', port=22, user='root'):
    return SimpleNamespace(host=host, ssh_port=port, ssh_user=user, is_local=False, gitea_container='gitea')


def connection():
    client = MagicMock(); transport = client.get_transport.return_value
    transport.is_active.return_value = transport.is_authenticated.return_value = True
    channel = transport.open_session.return_value
    channel.closed = False
    channel.recv_ready.return_value = channel.recv_stderr_ready.return_value = False
    channel.exit_status_ready.return_value = True
    channel.recv_exit_status.return_value = 0
    return client, transport, channel


class SSHTests(unittest.TestCase):
    def setUp(self):
        self.key = patch('paramiko.RSAKey.from_private_key_file', return_value=object()); self.key.start(); self.addCleanup(self.key.stop)
        self.client, self.transport, self.channel = connection()
        self.factory = patch('paramiko.SSHClient', return_value=self.client).start(); self.addCleanup(patch.stopall)

    def test_multiple_repositories_share_one_connection_and_close_each_channel(self):
        with ConnectionPool() as pool:
            for _ in range(3):
                access = ContainerAccess(server(), pool)
                for _ in range(12): access.run(['docker', 'exec', 'gitea', 'true'])
                access.close()
                self.client.close.assert_not_called()
        self.client.connect.assert_called_once(); self.client.close.assert_called_once()
        self.transport.set_keepalive.assert_called_once_with(30)
        self.assertEqual(self.channel.close.call_count, 36)

    def test_pool_does_not_share_different_connection_identity(self):
        with ConnectionPool() as pool:
            a = pool.remote(server())
            self.assertIs(a, pool.remote(server()))
            self.assertIsNot(a, pool.remote(server(port=2222)))
            self.assertIsNot(a, pool.remote(server(user='operator')))
            self.assertIsNot(a, pool.remote(server(host='other.internal')))

    def test_failed_handshake_closes_partial_client_and_does_not_retry(self):
        self.client.connect.side_effect = paramiko.SSHException('模拟协商中断，秘密不得输出')
        access = ContainerAccess(server())
        with self.assertRaises(AccessError) as caught: access.run(['秘密命令'])
        self.assertEqual(caught.exception.code, 'SSH_HANDSHAKE')
        self.assertNotIn('秘密', str(caught.exception))
        self.client.close.assert_called_once()
        with self.assertRaises(AccessError): access.run(['不能再次执行'])
        self.client.connect.assert_called_once(); self.channel.exec_command.assert_not_called()

    def test_connection_failures_are_classified(self):
        cases = [(paramiko.AuthenticationException('秘密'), 'SSH_AUTH'),
                 (paramiko.ssh_exception.NoValidConnectionsError({('ssh.internal', 22): ConnectionRefusedError(errno.ECONNREFUSED, '秘密')}), 'SSH_REFUSED'),
                 (ConnectionResetError(errno.ECONNRESET, '秘密'), 'SSH_RESET'),
                 (TimeoutError('秘密'), 'SSH_TIMEOUT')]
        for exc, code in cases:
            with self.subTest(错误=code):
                self.client.connect.side_effect = exc
                with ConnectionPool() as pool:
                    with self.assertRaises(AccessError) as caught: ContainerAccess(server(), pool).run(['秘密命令'])
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(caught.exception.unknown)
                self.assertNotIn('秘密', str(caught.exception))

    def test_command_submission_reset_is_unknown_and_not_replayed(self):
        self.channel.exec_command.side_effect = ConnectionResetError(errno.ECONNRESET, '秘密命令')
        with ConnectionPool() as pool:
            access = ContainerAccess(server(), pool)
            with self.assertRaises(AccessError) as caught: access.run(['秘密命令'])
            self.assertTrue(caught.exception.unknown)
            self.assertEqual(caught.exception.code, 'SSH_RESET')
            with self.assertRaises(AccessError): access.run(['秘密命令'])
        self.channel.exec_command.assert_called_once(); self.client.connect.assert_called_once()
        self.channel.close.assert_called_once()

    def test_channel_rejection_is_not_reported_as_key_error(self):
        self.transport.open_session.side_effect = paramiko.ChannelException(1, '秘密')
        with ConnectionPool() as pool:
            with self.assertRaises(AccessError) as caught: ContainerAccess(server(), pool).run(['秘密'])
        self.assertEqual(caught.exception.code, 'SSH_CHANNEL'); self.assertFalse(caught.exception.unknown)

    def test_command_timeout_stops_and_closes(self):
        with ConnectionPool() as pool:
            pool.remote(server()).command_timeout = -1
            with self.assertRaises(AccessError) as caught: ContainerAccess(server(), pool).run(['秘密'])
        self.assertEqual(caught.exception.code, 'SSH_TIMEOUT'); self.assertTrue(caught.exception.unknown)
        self.channel.close.assert_called_once(); self.client.close.assert_called_once()

    def test_lost_exit_status_is_unknown(self):
        self.channel.recv_exit_status.return_value = -1
        with ConnectionPool() as pool:
            with self.assertRaises(AccessError) as caught: ContainerAccess(server(), pool).run(['秘密'])
        self.assertEqual(caught.exception.code, 'SSH_RESET'); self.assertTrue(caught.exception.unknown)

    def test_missing_exec_acknowledgement_is_bounded(self):
        import threading
        import time
        released = threading.Event()
        self.client.close.side_effect = released.set
        def waiting(command):
            released.wait(1)
            raise paramiko.SSHException('确认等待被关闭')
        self.channel.exec_command.side_effect = waiting
        start = time.monotonic()
        with ConnectionPool() as pool:
            pool.remote(server()).command_timeout = .05
            with self.assertRaises(AccessError) as caught:
                ContainerAccess(server(), pool).run(['不得重放的命令'])
        self.assertLess(time.monotonic()-start, .5)
        self.assertEqual(caught.exception.code, 'SSH_TIMEOUT')
        self.assertTrue(caught.exception.unknown)
        self.channel.exec_command.assert_called_once()
        self.client.close.assert_called_once()

    def test_dead_connection_is_not_silently_reopened(self):
        with ConnectionPool() as pool:
            access = ContainerAccess(server(), pool); access.run(['true'])
            self.transport.is_active.return_value = False
            with self.assertRaises(AccessError) as caught: access.run(['不应发送'])
            self.assertEqual(caught.exception.code, 'SSH_LOST')
        self.client.connect.assert_called_once(); self.channel.exec_command.assert_called_once()

    def test_drains_stderr_without_returning_it(self):
        self.channel.recv_stderr_ready.side_effect = [True, False]
        self.channel.recv_stderr.return_value = b'private-output'
        with ConnectionPool() as pool:
            self.assertEqual(ContainerAccess(server(), pool).run(['true']), '')
        self.channel.recv_stderr.assert_called_once()

    def test_known_command_failure_does_not_expose_stderr(self):
        self.channel.recv_exit_status.return_value = 74
        with ConnectionPool() as pool:
            with self.assertRaises(AccessError) as caught: ContainerAccess(server(), pool).run(['秘密'])
        self.assertEqual(caught.exception.code, 'REMOTE_COMMAND_FAILED'); self.assertFalse(caught.exception.unknown)
        self.assertIn('74', str(caught.exception)); self.assertNotIn('秘密', str(caught.exception))

    def test_channel_closes_and_connection_survives_between_commands(self):
        with ConnectionPool() as pool:
            ContainerAccess(server(), pool).run(['true'])
            self.channel.close.assert_called_once()
            self.client.close.assert_not_called()


if __name__ == '__main__': unittest.main()
