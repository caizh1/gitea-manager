"""使用真实套接字和Paramiko文件预读复现SDK丢失输出，不请求Docker业务接口。"""
import socket
import struct
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

import docker
from paramiko.channel import ChannelFile
from requests import Response
from services.docker_ssh_stream import install


class Channel:
    def __init__(self, socket):
        self.socket = socket
        self.closed = False
    def recv(self, size): return self.socket.recv(size)
    def send(self, data, flags=0): return self.socket.send(data, flags)
    def sendall(self, data, flags=0): return self.socket.sendall(data, flags)
    def fileno(self): return self.socket.fileno()
    def gettimeout(self): return self.socket.gettimeout()
    def settimeout(self, value): self.socket.settimeout(value)
    def close(self): self.closed = True; self.socket.close()


def frame(stream, body):
    return struct.pack('>BxxxI', stream, len(body)) + body


class SSHBufferedResponseTests(unittest.TestCase):
    def response(self, body):
        incoming, outgoing = socket.socketpair()
        incoming.settimeout(2)
        outgoing.sendall(b'HTTP/1.1 101 UPGRADED\r\n\r\n' + body)
        outgoing.shutdown(socket.SHUT_WR)
        outgoing.close()
        channel = Channel(incoming)
        reader = ChannelFile(channel, 'rb', 32768)
        self.assertTrue(reader.readline().startswith(b'HTTP/1.1 101'))
        self.assertEqual(reader.readline(), b'\r\n')
        self.assertEqual(reader._rbuffer, body)
        response = Response()
        response.raw = SimpleNamespace(_fp=SimpleNamespace(fp=reader))
        response.status_code = 101
        response.close = MagicMock(side_effect=reader.close)
        self.addCleanup(channel.close)
        return response, channel

    def api(self, fixed=True):
        api = docker.APIClient(base_url='http://127.0.0.1:1', version='1.45')
        api.base_url = 'http+docker://ssh'
        if fixed:
            install(api)
        self.addCleanup(api.close)
        return api

    def test_original_sdk_loses_prefetched_output_and_fixed_api_preserves_it(self):
        body = frame(1, b'correct-sha256\n')
        response, _ = self.response(body)
        self.assertEqual(self.api(False)._read_from_socket(response, False, tty=False), b'')
        response, channel = self.response(body)
        self.assertEqual(self.api()._read_from_socket(response, False, tty=False), b'correct-sha256\n')
        response.close.assert_called_once()
        self.assertTrue(channel.closed)

    def test_stdout_stderr_multiplex_and_demux(self):
        response, _ = self.response(frame(1, b'out') + frame(2, b'error') + frame(1, b'more'))
        self.assertEqual(self.api()._read_from_socket(response, False, tty=False, demux=True), (b'outmore', b'error'))

    def test_partial_frame_is_rejected_and_closed(self):
        response, channel = self.response(frame(1, b'expected')[:-2])
        with self.assertRaisesRegex(RuntimeError, '未完整结束'):
            self.api()._read_from_socket(response, False, tty=False)
        self.assertTrue(channel.closed)
        response.close.assert_called_once()

    def test_stream_can_close_without_reading_first_frame(self):
        response, channel = self.response(frame(1, b'value'))
        iterator = self.api()._read_from_socket(response, True, tty=False)
        iterator.close()
        self.assertTrue(channel.closed)
        response.close.assert_called_once()

    def test_tty_preserves_buffered_bytes(self):
        response, _ = self.response(b'plain-output')
        self.assertEqual(self.api()._read_from_socket(response, False, tty=True), b'plain-output')

    def test_quiet_gap_keeps_original_sdk_waiting_semantics_after_buffer(self):
        incoming, outgoing = socket.socketpair()
        incoming.settimeout(.01)
        payload = frame(1, '完整导出正文'.encode())
        outgoing.sendall(b'HTTP/1.1 101 UPGRADED\r\n\r\n' + payload[:9])
        channel = Channel(incoming); reader = ChannelFile(channel, 'rb', 32768)
        reader.readline(); reader.readline()
        response = Response(); response.status_code = 101
        response.raw = SimpleNamespace(_fp=SimpleNamespace(fp=reader)); response.close = reader.close
        def later():
            time.sleep(.05); outgoing.sendall(payload[9:]); outgoing.shutdown(socket.SHUT_WR); outgoing.close()
        sender = threading.Thread(target=later); sender.start()
        try:
            self.assertEqual(self.api()._read_from_socket(response, False, tty=False), '完整导出正文'.encode())
        finally:
            channel.close(); sender.join(timeout=1)
        self.assertFalse(sender.is_alive())


if __name__ == '__main__':
    unittest.main()
