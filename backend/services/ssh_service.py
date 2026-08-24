import paramiko
import os
import logging
import time
from config import SSH_KEY_PATH, SSH_KEY_PASSPHRASE


class SSHCommandTimeout(TimeoutError):
    pass


class SSHService:
    def __init__(self, host, port=22, user='root', key_path=None, passphrase=None):
        self.host = host
        self.port = port
        self.user = user
        self.key_path = key_path or SSH_KEY_PATH
        self.passphrase = passphrase or SSH_KEY_PASSPHRASE

    def _get_client(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        if os.path.exists(self.key_path):
            key = paramiko.RSAKey.from_private_key_file(self.key_path, password=self.passphrase)
            client.connect(self.host, self.port, self.user, pkey=key, timeout=30)
        else:
            raise FileNotFoundError(f'SSH key not found: {self.key_path}')
        return client

    @staticmethod
    def _bounded_append(buffer, data, limit):
        buffer.extend(data)
        if len(buffer) > limit:
            del buffer[:len(buffer) - limit]

    def exec(self, command, timeout=600, output_limit=2 * 1024 * 1024):
        logging.info('[SSH] %s:%s $ %s', self.host, self.port, command[:120])
        client = self._get_client()
        try:
            transport = client.get_transport()
            channel = transport.open_session(timeout=30)
            channel.settimeout(1)
            channel.exec_command(command)
            stdout_data = bytearray()
            stderr_data = bytearray()
            last_activity = time.monotonic()

            while True:
                progressed = False
                while channel.recv_ready():
                    self._bounded_append(stdout_data, channel.recv(65536), output_limit)
                    progressed = True
                while channel.recv_stderr_ready():
                    self._bounded_append(stderr_data, channel.recv_stderr(65536), output_limit)
                    progressed = True

                if progressed:
                    last_activity = time.monotonic()

                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break

                if timeout is not None and time.monotonic() - last_activity >= timeout:
                    raise SSHCommandTimeout(
                        f'SSH 命令连续 {timeout} 秒无输出或通道进展: {command[:120]}'
                    )
                time.sleep(0.05)

            exit_code = channel.recv_exit_status()
            out = stdout_data.decode('utf-8', errors='replace').strip()
            err = stderr_data.decode('utf-8', errors='replace').strip()
            return exit_code, out, err
        finally:
            client.close()

    def get_file(self, remote_path, local_path):
        client = self._get_client()
        try:
            sftp = client.open_sftp()
            sftp.get(remote_path, local_path)
            sftp.close()
        finally:
            client.close()

    def put_file(self, local_path, remote_path, callback=None):
        client = self._get_client()
        try:
            sftp = client.open_sftp()
            sftp.put(local_path, remote_path, callback=callback)
            sftp.close()
        finally:
            client.close()

    def put_text(self, remote_path, content, mode=0o600):
        client = self._get_client()
        try:
            sftp = client.open_sftp()
            with sftp.open(remote_path, 'w') as handle:
                handle.write(content)
            sftp.chmod(remote_path, mode)
            sftp.close()
        finally:
            client.close()

    def read_text(self, remote_path, max_bytes=65536):
        client = self._get_client()
        try:
            sftp = client.open_sftp()
            with sftp.open(remote_path, 'r') as handle:
                data = handle.read(max_bytes)
            sftp.close()
            if isinstance(data, bytes):
                return data.decode('utf-8', errors='replace')
            return str(data)
        finally:
            client.close()

    def file_exists(self, remote_path):
        client = self._get_client()
        try:
            sftp = client.open_sftp()
            sftp.stat(remote_path)
            sftp.close()
            return True
        except IOError:
            return False
        finally:
            client.close()

    def test_connection(self):
        try:
            exit_code, out, err = self.exec('echo ok')
            return exit_code == 0 and 'ok' in out
        except Exception as e:
            logging.error(f'SSH test_connection failed to {self.host}:{self.port}: {e}')
            return False
