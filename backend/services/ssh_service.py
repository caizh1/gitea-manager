import paramiko
import os
import logging
from config import SSH_KEY_PATH, SSH_KEY_PASSPHRASE


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

    def exec(self, command):
        logging.info('[SSH] %s:%s $ %s', self.host, self.port, command[:120])
        client = self._get_client()
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=600)
            out = stdout.read().decode('utf-8', errors='replace').strip()
            err = stderr.read().decode('utf-8', errors='replace').strip()
            exit_code = stdout.channel.recv_exit_status()
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

    def put_file(self, local_path, remote_path):
        client = self._get_client()
        try:
            sftp = client.open_sftp()
            sftp.put(local_path, remote_path)
            sftp.close()
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
