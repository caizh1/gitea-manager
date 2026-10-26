"""Docker SSH 劫持响应沿用 HTTP 文件缓冲，严格消费原始多路复用帧。"""
import struct
import sys
import types


class BufferedSSHSocket:
    """响应头可能已预读正文；先消费同一文件缓冲，再读取其专属 SSH 通道。"""
    def __init__(self, response):
        self.response = response
        self.reader = response.raw._fp.fp
        self.channel = self.reader.channel
        if not isinstance(getattr(self.reader, '_rbuffer', None), bytes):
            raise RuntimeError('Docker SSH 响应不是受支持的 Paramiko 缓冲文件')
        self.closed = False

    def recv(self, size, flags=0):
        if flags:
            raise ValueError('Docker SSH 缓冲读取不支持额外标志')
        if size <= 0:
            return b''
        buffered = len(self.reader._rbuffer)
        if buffered:
            return self.reader.read(min(size, buffered))
        # 缓冲耗尽后沿用SDK的等待语义，不能把无输出的长导出缩短为SSH通道超时。
        from docker.utils.socket import read
        return read(self.channel, size)

    def send(self, data, flags=0):
        return self.channel.send(data, flags)

    def sendall(self, data, flags=0):
        return self.channel.sendall(data, flags)

    def fileno(self):
        return self.channel.fileno()

    def gettimeout(self):
        return self.channel.gettimeout()

    def settimeout(self, value):
        return self.channel.settimeout(value)

    def close(self):
        if self.closed:
            return
        self.closed = True
        original = sys.exc_info()[1]
        first = None
        # 文件和通道各自关闭；一个异常不能跳过另一个，也不能掩盖原解析失败。
        for close in (self.response.close, self.channel.close):
            try:
                close()
            except BaseException as exc:
                if first is None:
                    first = exc
        if first is not None:
            if original is None:
                raise first
            if hasattr(original, 'add_note'):
                try:
                    original.add_note('Docker SSH 响应关闭未完整完成：' + type(first).__name__)
                except BaseException:
                    pass


class ResponseIterator:
    """即使调用者尚未读取第一帧，close也必须释放响应和自己的通道。"""
    def __init__(self, iterator, socket):
        self.iterator, self.socket = iterator, socket

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.iterator)

    def close(self):
        try:
            self.iterator.close()
        finally:
            self.socket.close()


def _exactly(socket, count, *, clean_eof=False):
    chunks = []
    remaining = count
    while remaining:
        block = socket.recv(min(remaining, 1024 * 1024))
        if not block:
            if clean_eof and remaining == count:
                return None
            raise RuntimeError('Docker SSH 原始响应帧未完整结束，拒绝使用不完整输出')
        if len(block) > remaining:
            raise RuntimeError('Docker SSH 原始响应超过当前帧边界')
        chunks.append(block)
        remaining -= len(block)
    return b''.join(chunks)


def _frames(socket, tty, demux):
    try:
        if tty:
            while True:
                block = socket.recv(4096)
                if not block:
                    return
                yield (block, None) if demux else block
        else:
            while True:
                header = _exactly(socket, 8, clean_eof=True)
                if header is None:
                    return
                stream, reserved, size = struct.unpack('>B3sI', header)
                if stream not in (1, 2) or reserved != b'\0\0\0':
                    raise RuntimeError('Docker SSH 多路复用帧头无效')
                remaining = size
                while remaining:
                    block = _exactly(socket, min(remaining, 1024 * 1024))
                    remaining -= len(block)
                    yield ((block, None) if stream == 1 else (None, block)) if demux else block
    finally:
        socket.close()


def install(api):
    """仅修改本次 managed_client，不修改 Docker SDK、全局对象或远端返回。"""
    if getattr(api, '_manager_buffered_ssh_stream', False):
        return
    original_socket = api._get_raw_response_socket
    original_read = api._read_from_socket

    def raw_socket(instance, response):
        if not instance.base_url.startswith('http+docker://ssh'):
            return original_socket(response)
        instance._raise_for_status(response)
        try:
            return BufferedSSHSocket(response)
        except BaseException:
            for close in (response.close, getattr(getattr(getattr(response.raw, '_fp', None), 'fp', None), 'channel', None)):
                try:
                    if callable(close):
                        close()
                    elif close is not None:
                        close.close()
                except BaseException:
                    pass
            raise

    def read(instance, response, stream, tty=True, demux=False):
        if not instance.base_url.startswith('http+docker://ssh'):
            return original_read(response, stream, tty=tty, demux=demux)
        socket = raw_socket(instance, response)
        iterator = _frames(socket, tty, demux)
        if stream:
            return ResponseIterator(iterator, socket)
        if demux:
            output = [None, None]
            for frame in iterator:
                for index, block in enumerate(frame):
                    if block is not None:
                        output[index] = (output[index] or b'') + block
            return tuple(output)
        return b''.join(iterator)

    api._get_raw_response_socket = types.MethodType(raw_socket, api)
    api._read_from_socket = types.MethodType(read, api)
    api._manager_buffered_ssh_stream = True
