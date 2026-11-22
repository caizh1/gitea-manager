"""大文件传输逐段使用独立连接，并核对完整内容。"""
from contextlib import nullcontext
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from services import large_docker_transfer as transfer


class LargeDockerTransferTests(unittest.TestCase):
    def test_receive_reconnects_and_checks_complete_digest(self):
        data = b'0123456789ABCDEFG'
        clients = []
        for start in range(0, len(data), 8):
            client = MagicMock()
            client.api.exec_start.return_value = (frame for frame in [(data[start:start + 8], None)])
            client.api.exec_inspect.return_value = {'ExitCode': 0}
            clients.append(client)
        with tempfile.TemporaryDirectory() as root, patch.object(transfer, 'BLOCK', 4), \
                patch.object(transfer, 'PART', 8), patch.object(transfer, 'managed_client', side_effect=clients), \
                patch('services.backup_consistency.execute', return_value=str(len(data))):
            destination = Path(root) / 'copy'
            totals = []
            transfer.receive_in_parts(None, SimpleNamespace(id='验收容器'), '/copy', destination,
                                      {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}, totals.append)
            self.assertEqual(destination.read_bytes(), data)
            self.assertEqual(totals, [8, 16, len(data)])
            for client in clients:
                client.close.assert_called_once()

    def test_receive_rejects_mismatch(self):
        client = MagicMock()
        client.api.exec_start.return_value = (frame for frame in [('损坏'.encode(), None)])
        client.api.exec_inspect.return_value = {'ExitCode': 0}
        with tempfile.TemporaryDirectory() as root, patch.object(transfer, 'managed_client', return_value=client), \
                patch('services.backup_consistency.execute', return_value='6'):
            with self.assertRaisesRegex(RuntimeError, '摘要不符'):
                transfer.receive_in_parts(None, SimpleNamespace(id='验收容器'), '/copy', Path(root) / 'copy',
                                          {'size': 6, 'sha256': '0' * 64}, lambda _: None)

    def test_target_checks_parts_before_extraction(self):
        for corrupt in (False, True):
            with self.subTest(注入损坏=corrupt), tempfile.TemporaryDirectory() as root:
                body = b'archive input' * 1024
                source = io.BytesIO()
                with tarfile.open(fileobj=source, mode='w') as archive:
                    entry = tarfile.TarInfo('restored')
                    entry.size = len(body)
                    archive.addfile(entry, io.BytesIO(body))
                source.seek(0)
                def upload(path, wire):
                    if isinstance(wire, bytes):
                        wire = io.BytesIO(wire)
                    with tarfile.open(fileobj=wire) as archive:
                        for entry in archive:
                            data = archive.extractfile(entry).read()
                            (Path(path) / entry.name).write_bytes(
                                data[::-1] if corrupt and entry.name == 'part-00000000' else data)
                    return True

                container = SimpleNamespace(id='验收容器', put_archive=upload)
                server = SimpleNamespace(connection_mode='ssh')
                tool = Path(root) / 'tool'
                tool.write_text('#!/bin/sh\nexec tar -xpf - -C "$2"\n')
                tool.chmod(0o700)
                def client(_):
                    return SimpleNamespace(close=lambda: None, api=SimpleNamespace(
                        put_archive=lambda identifier, path, wire: upload(path, wire)))
                def execute(_, args, label):
                    env = os.environ.copy()
                    if sys.platform == 'darwin':
                        executable = Path(root) / 'sha256sum'
                        executable.write_text('#!/bin/sh\nexec shasum -a 256 "$@"\n')
                        executable.chmod(0o700)
                        env['PATH'] = root + os.pathsep + env['PATH']
                    try:
                        subprocess.run(args, check=True, capture_output=True, env=env)
                    except subprocess.CalledProcessError as error:
                        error.add_note(error.stderr.decode(errors='replace'))
                        raise
                with patch.object(transfer, 'PART', 4096), patch.object(transfer, 'managed_client', side_effect=client), \
                        patch('services.artifact_io.artifact_tool', side_effect=lambda _: nullcontext(root)):
                    if corrupt:
                        with self.assertRaises(subprocess.CalledProcessError):
                            transfer.put_archive(server, container, root, source, lambda *_: None, execute)
                        self.assertFalse((Path(root) / 'restored').exists())
                    else:
                        transfer.put_archive(server, container, root, source, lambda *_: None, execute)
                        self.assertEqual((Path(root) / 'restored').read_bytes(), body)
                        self.assertFalse((Path(root) / 'manager-transfer-parts').exists())


if __name__ == '__main__':
    unittest.main()
