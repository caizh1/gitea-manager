import logging
import docker
from services.diagnostics import CURRENT

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def local_exec(container_name, cmd, user=None, workdir=None):
    client = _get_client()
    container = client.containers.get(container_name)
    exit_code, output = container.exec_run(cmd, user=user, workdir=workdir)
    decoded = output.decode('utf-8', errors='replace')
    report = CURRENT.get()
    if report:
        report.event('完成' if exit_code == 0 else '失败', 'Docker 容器命令', 容器=container.id[:12], 退出码=exit_code, 输出=decoded[-12000:])
    return exit_code, decoded


def local_cp_from(container_name, container_path, host_path, callback=None):
    import tarfile
    import tempfile
    import shutil
    import os
    client = _get_client()
    container = client.containers.get(container_name)
    bits, stat = container.get_archive(container_path)
    # Docker 返回 TAR 流，不能直接冒充业务 ZIP；先完整验证再原子替换。
    with tempfile.TemporaryFile() as raw:
        for chunk in bits:
            raw.write(chunk)
            # TAR 包含额外头部，不能用 ZIP 大小作为传输总量。
            if callback: callback(raw.tell(), None)
        raw.seek(0)
        with tarfile.open(fileobj=raw) as archive:
            members = archive.getmembers()
            if len(members) != 1 or not members[0].isfile() or members[0].name != os.path.basename(container_path):
                raise RuntimeError('Docker 返回的备份文件结构不匹配')
            temp = host_path + '.partial'
            try:
                with archive.extractfile(members[0]) as source, open(temp, 'wb') as target:
                    count = 0
                    for chunk in iter(lambda: source.read(1024*1024), b''):
                        target.write(chunk)
                        count += len(chunk)
                        if callback: callback(count, members[0].size)
                os.replace(temp, host_path)
            finally:
                if os.path.exists(temp): os.unlink(temp)


def local_cp_to(host_path, container_name, container_path):
    import tarfile
    import io
    import os

    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode='w') as tar:
        tar.add(host_path, arcname=os.path.basename(host_path))
    tar_stream.seek(0)

    client = _get_client()
    container = client.containers.get(container_name)
    container.put_archive(os.path.dirname(container_path), tar_stream)


def managed_client(server):
    """远端 Docker 复用页面确认的 SSH 信任与私钥，不开放 Docker TCP 端口。"""
    if server.connection_mode == 'local':
        return docker.from_env()
    from docker.transport.sshconn import SSHHTTPAdapter
    from services.ssh_service import SSHService

    class PinnedAdapter(SSHHTTPAdapter):
        def _create_paramiko_client(self, base_url):
            self.ssh_client = SSHService(server.host, server.ssh_port, server.ssh_user)._get_client()

        def get_connection(self, url, proxies=None):
            # 所有 Docker API 路径复用同一 SSH 池，避免耗尽 sshd MaxSessions。
            return super().get_connection('http+docker://ssh', proxies)

        def _connect(self):
            transport = self.ssh_client.get_transport()
            if transport is None or not transport.is_active() or not transport.is_authenticated():
                raise ConnectionError('SSH 管理连接已中断，不自动重放恢复操作')

    # 固定版本避免构造时访问占位地址；挂载受控通道后再协商实际 API。
    client = docker.DockerClient(base_url='http://127.0.0.1:2375', version='1.45', timeout=120)
    try:
        adapter = PinnedAdapter('ssh://managed-host', timeout=120)
        client.api._unmount('http://', 'https://')
        client.api._custom_adapter = adapter
        client.api.mount('http+docker://ssh', adapter)
        client.api.base_url = 'http+docker://ssh'
        client.api.trust_env = False
        from services.docker_ssh_stream import install
        install(client.api)
        client.api._version = client.api._retrieve_server_version()
        return client
    except Exception:
        client.close()
        raise
