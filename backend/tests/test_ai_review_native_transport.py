"""原生模型流的任意分片边界测试。"""
import json
import os
from pathlib import Path
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from ai_review.model_transport import (ProcessModelStream, ModelTransportError,
                                       safe_exception_metrics)


class _Process:
    def __init__(self, output):
        self.stdout = output


def _wire(tag, value):
    return tag + struct.pack('!I', len(value)) + value


class NativeTransportTests(unittest.TestCase):
    def test_tls_diagnostic_keeps_only_safe_numeric_details(self):
        class VerifyFailure(Exception):
            errno = 71
            verify_code = 20

        inner = VerifyFailure('certificate detail must not be stored')
        error = requests.exceptions.SSLError(inner)
        metrics = safe_exception_metrics(error, 'SSLError', {
            'transfer_phase': 'connecting', 'received_bytes': 0,
            'first_data_elapsed_seconds': None, 'last_data_elapsed_seconds': None,
            'target_host': '127.0.0.1', 'tls_phase': None,
        })
        self.assertEqual(metrics['tls_phase'], 'connecting')
        self.assertEqual(metrics['tls_exception_type'], 'VerifyFailure')
        self.assertEqual(metrics['tls_errno'], 71)
        self.assertEqual(metrics['tls_verify_code'], 20)
        self.assertNotIn('certificate detail', json.dumps(metrics))

    def _stream(self, chunks):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, 'rb', buffering=0)
        row = ProcessModelStream.__new__(ProcessModelStream)
        row.started = time.monotonic()
        row.metrics = {}
        row.buffer = bytearray()
        row.deadline = time.monotonic() + 5
        row.last_authorization_check = time.monotonic() - 1
        row.process = _Process(reader)

        def write():
            try:
                for chunk in chunks:
                    os.write(write_fd, chunk)
            finally:
                os.close(write_fd)

        thread = threading.Thread(target=write)
        thread.start()
        return row, reader, thread

    def test_multiple_frames_in_one_read_keep_next_partial_body(self):
        first, second = b'a', b'b' * 9001
        wire = _wire(b'D', first) + _wire(b'D', second)
        row, reader, thread = self._stream([wire])
        try:
            self.assertEqual(row._frame(), (b'D', first))
            self.assertEqual(row._frame(), (b'D', second))
        finally:
            thread.join()
            reader.close()

    def test_every_byte_boundary_including_large_frame(self):
        frames = [(b'H', b'{}'), (b'D', bytes(range(256)) * 40), (b'X', b'{}')]
        wire = b''.join(_wire(*frame) for frame in frames)
        row, reader, thread = self._stream([wire[index:index + 1] for index in range(len(wire))])
        try:
            self.assertEqual([row._frame() for _ in frames], frames)
        finally:
            thread.join()
            reader.close()

    def test_authorization_and_deadline_are_checked_even_with_buffered_data(self):
        frame = _wire(b'D', b'fast')
        row, reader, thread = self._stream([frame])
        try:
            calls = []
            self.assertEqual(row._frame(lambda: calls.append('checked')), (b'D', b'fast'))
            self.assertEqual(calls, ['checked'])
            row.buffer.extend(frame)
            row.deadline = time.monotonic() - .01
            with self.assertRaises(ModelTransportError):
                row._frame()
        finally:
            thread.join()
            reader.close()

    def test_authorization_is_revoked_during_no_data_wait(self):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, 'rb', buffering=0)
        row = ProcessModelStream.__new__(ProcessModelStream)
        row.started = time.monotonic()
        row.metrics = {}
        row.buffer = bytearray()
        row.deadline = time.monotonic() + 5
        row.last_authorization_check = time.monotonic() - 1
        row.heartbeat = None
        row.process = _Process(reader)
        checks = []

        def revoke():
            checks.append(time.monotonic())
            if len(checks) > 1:
                raise RuntimeError('授权已撤销')

        started = time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError, '授权已撤销'):
                row._frame(revoke)
            self.assertLess(time.monotonic() - started, .8)
            self.assertGreaterEqual(len(checks), 2)
        finally:
            os.close(write_fd)
            reader.close()

    def test_authorization_is_revoked_during_continuous_fast_frames(self):
        frame = _wire(b'D', b'x' * 1024)
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, 'rb', buffering=0)
        row = ProcessModelStream.__new__(ProcessModelStream)
        row.started = time.monotonic()
        row.metrics = {}
        row.buffer = bytearray()
        row.deadline = time.monotonic() + 5
        row.last_authorization_check = time.monotonic() - 1
        row.heartbeat = None
        row.process = _Process(reader)
        stopped = threading.Event()

        def write():
            try:
                while not stopped.is_set():
                    os.write(write_fd, frame)
            except (BrokenPipeError, OSError):
                pass

        writer = threading.Thread(target=write)
        writer.start()
        checks = []

        def revoke():
            checks.append(time.monotonic())
            if len(checks) > 1:
                raise RuntimeError('授权已撤销')

        started = time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError, '授权已撤销'):
                for _ in row.chunks(revoke):
                    pass
            self.assertLess(time.monotonic() - started, .8)
            self.assertGreaterEqual(len(checks), 2)
        finally:
            stopped.set()
            reader.close()
            try:
                os.close(write_fd)
            except OSError:
                pass
            writer.join(timeout=1)
            self.assertFalse(writer.is_alive())


if __name__ == '__main__':
    unittest.main()
