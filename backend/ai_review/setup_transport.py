"""自动接入专用连接池；任务内复用 SSH，不重连、不重放，不记录敏感输出。"""
import errno
import json
import shlex
import socket
import threading
import time

import paramiko

from .common import ReviewError


class AccessError(ReviewError):
    def __init__(self, code, phase, message, *, unknown=False, cause=None):
        super().__init__(message)
        self.code, self.phase, self.unknown = code, phase, unknown
        self.exception_type = type(cause).__name__ if cause else ''
        self.errno = getattr(cause, 'errno', None)


def connection_error(exc, phase, unknown=False):
    if isinstance(exc, AccessError):
        return exc
    if isinstance(exc, (paramiko.AuthenticationException, paramiko.PasswordRequiredException)):
        code, label = 'SSH_AUTH', 'SSH 密钥认证失败'
    elif phase == '读取 SSH 密钥' or isinstance(exc, FileNotFoundError):
        code, label = 'SSH_KEY', 'Manager 的 SSH 密钥无法读取'
    elif isinstance(exc, paramiko.BadHostKeyException):
        code,label='SSH_HOST_KEY_CHANGED','SSH 主机指纹变化，连接已阻止'
    elif isinstance(exc, paramiko.ssh_exception.NoValidConnectionsError):
        numbers = {getattr(e, 'errno', None) for e in exc.errors.values()}
        code = 'SSH_REFUSED' if errno.ECONNREFUSED in numbers else 'SSH_CONNECT'
        label = 'SSH 端口拒绝连接' if code == 'SSH_REFUSED' else 'SSH TCP 连接失败'
    elif isinstance(exc, ConnectionRefusedError):
        code, label = 'SSH_REFUSED', 'SSH 端口拒绝连接'
    elif isinstance(exc, (TimeoutError, socket.timeout)):
        code, label = 'SSH_TIMEOUT', 'SSH 操作超时'
    elif isinstance(exc, (ConnectionResetError, BrokenPipeError, EOFError)):
        code, label = 'SSH_RESET', 'SSH 连接被重置或中断'
    elif isinstance(exc, paramiko.ChannelException):
        code, label = 'SSH_CHANNEL', 'SSH 命令通道被拒绝'
    elif isinstance(exc, paramiko.SSHException):
        code, label = ('SSH_HANDSHAKE', 'SSH 协商失败') if phase == '建立 SSH 连接' else ('SSH_CHANNEL', 'SSH 通道中断')
    else:
        code, label = 'SSH_CONNECT' if phase == '建立 SSH 连接' else 'SSH_IO', 'SSH 通信未完成'
    tail = '；命令结果不确定，已停止后续操作，请核对后再继续' if unknown else '；已停止后续操作，未自动重连'
    return AccessError(code, phase, label + tail, unknown=unknown, cause=exc)


class RemoteSession:
    def __init__(self, server):
        self.server, self.client, self.failed = server, None, False
        self.command_timeout = 120

    def close(self):
        client, self.client = self.client, None
        if client:
            client.close()

    def connect(self):
        if self.failed:
            raise AccessError('SSH_STOPPED', '连接状态', '本次接入的 SSH 已停止，未重新连接')
        if self.client is not None:
            transport = self.client.get_transport()
            if not transport or not transport.is_active() or not transport.is_authenticated():
                self.failed = True
                self.close()
                raise AccessError('SSH_LOST', '连接状态', '复用的 SSH 连接已断开，已停止后续操作，未自动重连')
            return transport
        from services.ssh_service import SSHService
        settings = SSHService(self.server.host, self.server.ssh_port, self.server.ssh_user)
        phase = '建立 SSH 连接'
        try:
            client = settings._get_client(timeout=10)
            self.client = client
            transport = client.get_transport()
            if not transport or not transport.is_active() or not transport.is_authenticated():
                raise EOFError()
            transport.set_keepalive(30)
            return transport
        except Exception as exc:
            self.failed = True
            self.close()
            raise connection_error(exc, phase) from None

    def run(self, args, data):
        transport = self.connect()
        channel = None
        phase, submitted = '创建 SSH 命令通道', False
        started = time.monotonic()
        timed_out = threading.Event()
        def expire():
            # Paramiko 3.4 的 exec 请求确认不受 Channel.settimeout 控制。
            # 关闭整个本任务连接，才能唤醒确认等待及阻塞的输入发送。
            timed_out.set()
            self.failed = True
            self.close()
        watchdog = threading.Timer(max(.001, self.command_timeout), expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            channel = transport.open_session(timeout=20)
            channel.settimeout(20)
            phase, submitted = '执行远程命令', True
            # exec 请求可能已经到达服务端；从此处开始中断均视为结果不确定。
            channel.exec_command(shlex.join(args))
            if data:
                channel.sendall(data)
            channel.shutdown_write()
            output = bytearray(); size = 0
            while True:
                if time.monotonic() - started > self.command_timeout:
                    raise TimeoutError()
                if channel.recv_ready():
                    part = channel.recv(65536); output.extend(part); size += len(part)
                if channel.recv_stderr_ready():
                    size += len(channel.recv_stderr(65536))
                if size > 4 * 1024 * 1024:
                    raise AccessError('SSH_OUTPUT_LIMIT', phase, '远程输出超过限制；结果待核对，已停止后续操作', unknown=True)
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    code = channel.recv_exit_status()
                    if code < 0:
                        raise EOFError()
                    return code, bytes(output)
                if not transport.is_active() or channel.closed:
                    raise EOFError()
                time.sleep(0.02)
        except Exception as exc:
            self.failed = True
            self.close()
            if timed_out.is_set():
                exc = TimeoutError()
            raise connection_error(exc, phase, submitted) from None
        finally:
            watchdog.cancel()
            watchdog.join()
            if channel is not None:
                channel.close()


class ConnectionPool:
    """只在一次任务内共享；现有执行锁保证命令串行，不与备份业务共享。"""
    def __init__(self):
        self.sessions = {}

    def remote(self, server):
        key = (server.host, server.ssh_port, server.ssh_user)
        if key not in self.sessions:
            self.sessions[key] = RemoteSession(server)
        return self.sessions[key]

    def close(self):
        for session in self.sessions.values():
            session.close()
        self.sessions.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ContainerAccess:
    def __init__(self, server, pool=None):
        self.server = server
        self.docker = None
        self.pool = pool if pool is not None else ConnectionPool()
        self.owns_pool = pool is None
        if server.is_local:
            import docker
            self.docker = docker.from_env(timeout=120)

    def close(self):
        if self.docker:
            self.docker.close()
        if self.owns_pool:
            self.pool.close()

    def run(self, args, binary=False, input=None, check=True):
        try:
            if self.docker:
                code, output = self._local(args, input)
            else:
                code, output = self.pool.remote(self.server).run(args, input)
            if check and code:
                raise AccessError('REMOTE_COMMAND_FAILED', '执行容器命令',
                                  'Gitea 容器命令返回非零退出码 ' + str(code) + '；请核对当前步骤')
            if len(output) > 4 * 1024 * 1024:
                raise AccessError('CONTAINER_OUTPUT_LIMIT', '读取容器结果', '容器返回超过初始化读取上限', unknown=True)
            return output if binary else output.decode('utf-8')
        except ReviewError:
            raise
        except Exception as exc:
            raise AccessError('CONTAINER_IO', '容器通信', '容器通信未完成；已停止后续操作，请核对结果', unknown=True, cause=exc) from None

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
