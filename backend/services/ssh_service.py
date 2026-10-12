import paramiko
import os
import logging
import time
import hashlib
import base64
from services.diagnostics import CURRENT
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

    def _get_client(self, timeout=10):
        report = CURRENT.get()
        if report: report.event('开始', 'SSH 连接检查', 地址=self.host, 端口=self.port, 账号=self.user, 密钥可读=os.access(self.key_path,os.R_OK))
        client = paramiko.SSHClient()
        known_hosts = os.environ.get('SSH_KNOWN_HOSTS')
        if known_hosts:
            if os.path.isfile(known_hosts): client.load_host_keys(known_hosts)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.load_system_host_keys()
        try:
            key = paramiko.RSAKey.from_private_key_file(self.key_path, password=self.passphrase)
            client.connect(self.host, self.port, self.user, pkey=key, timeout=timeout,
                           banner_timeout=timeout, auth_timeout=timeout, allow_agent=False, look_for_keys=False)
        except FileNotFoundError:
            client.close()
            raise FileNotFoundError(f'SSH key not found: {self.key_path}') from None
        except Exception:
            client.close()
            raise
        if report:
            fingerprint = 'SHA256:'+base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
            report.event('完成','SSH 主机校验与公钥认证', 公钥指纹=fingerprint)
        return client

    @staticmethod
    def _bounded_append(buffer, data, limit):
        buffer.extend(data)
        if len(buffer) > limit:
            del buffer[:len(buffer) - limit]

    def exec(self, command, timeout=600, output_limit=2 * 1024 * 1024):
        logging.info('[SSH] %s:%s 执行远程操作', self.host, self.port)
        client = self._get_client()
        try:
            transport = client.get_transport()
            channel = transport.open_session(timeout=30)
            channel.settimeout(1)
            channel.exec_command(command)
            stdout_data = bytearray()
            stderr_data = bytearray()
            last_activity = time.monotonic()
            deadline = last_activity + timeout if timeout else float('inf')

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

                if time.monotonic() >= deadline:
                    raise SSHCommandTimeout(
                        f'SSH 命令超过 {timeout} 秒执行上限'
                    )
                time.sleep(0.05)

            exit_code = channel.recv_exit_status()
            out = stdout_data.decode('utf-8', errors='replace').strip()
            err = stderr_data.decode('utf-8', errors='replace').strip()
            report = CURRENT.get()
            if report: report.event('完成' if exit_code == 0 else '失败','SSH 远程命令',退出码=exit_code,标准错误=err[-12000:])
            return exit_code, out, err
        finally:
            client.close()

    def get_file(self, remote_path, local_path, callback=None):
        client = self._get_client()
        try:
            sftp = client.open_sftp()
            sftp.get(remote_path, local_path, callback=callback)
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
