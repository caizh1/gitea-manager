"""初始化专用容器通道；不记录命令输出，兼容现有本地 Docker 与 SSH。"""
import json
import shlex
import time

from .common import ReviewError


class ContainerAccess:
    def __init__(self, server):
        self.server = server
        self.docker = None
        if server.is_local:
            import docker
            self.docker = docker.from_env(timeout=120)

    def close(self):
        if self.docker:
            self.docker.close()

    def run(self, args, binary=False, input=None, check=True):
        try:
            if self.docker:
                code, output = self._local(args, input)
            else:
                code, output = self._remote(args, input)
            if check and code:
                raise ReviewError('Gitea 容器操作未完成，请检查已有 SSH/Docker 权限或初始化待核对步骤')
            if len(output) > 4 * 1024 * 1024:
                raise ReviewError('容器返回超过初始化读取上限')
            return output if binary else output.decode('utf-8')
        except ReviewError:
            raise
        except Exception:
            raise ReviewError('无法操作已配置的 Gitea 容器，请检查服务器连接与 Docker 权限') from None

    def _local(self, args, data):
        if args[:2] == ['docker', 'inspect'] and args[2:] == [self.server.gitea_container]:
            return 0, json.dumps([self.docker.containers.get(args[2]).attrs]).encode()
        if args[:3] == ['docker', 'exec', '--user'] and args[4] == self.server.gitea_container:
            result = self.docker.containers.get(args[4]).exec_run(args[5:], user=args[3], demux=True)
            out, err = result.output
            return result.exit_code, (out or b'') if result.exit_code == 0 else (err or b'')
        if args[:4] == ['docker', 'cp', '-a', '-'] and args[4].startswith(self.server.gitea_container + ':'):
            target = args[4].split(':', 1)[1]
            ok = self.docker.containers.get(self.server.gitea_container).put_archive(target, data)
            return (0 if ok else 1), b''
        raise ReviewError('不支持的初始化容器操作')

    def _remote(self, args, data):
        from services.ssh_service import SSHService
        ssh = SSHService(self.server.host, self.server.ssh_port, self.server.ssh_user)
        client = ssh._get_client()
        try:
            channel = client.get_transport().open_session(timeout=20)
            channel.settimeout(20)
            channel.exec_command(shlex.join(args))
            if data:
                channel.sendall(data)
            channel.shutdown_write()
            output = bytearray(); started = time.monotonic()
            while True:
                if channel.recv_ready(): output.extend(channel.recv(65536))
                if channel.recv_stderr_ready(): channel.recv_stderr(65536)
                if len(output) > 4 * 1024 * 1024 or time.monotonic() - started > 120:
                    raise ReviewError('远程初始化操作超时或输出过大，结果需要回读核对')
                if channel.exit_status_ready() and not channel.recv_ready():
                    return channel.recv_exit_status(), bytes(output)
                time.sleep(0.02)
        finally:
            client.close()
