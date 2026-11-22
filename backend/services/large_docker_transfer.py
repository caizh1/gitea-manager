"""远程大文件按独立 SSH 会话传输，避免单个会话承载数小时的数据。"""
from contextlib import closing
import hashlib
import io
import os
import tarfile
import tempfile

from services.docker_service import managed_client
from services.operation_progress import ProgressReader

BLOCK = 1024 * 1024
PART = 128 * BLOCK


def receive_in_parts(server, reader, remote, target, expected, callback):
    from services.backup_consistency import command_output, execute

    size = expected['size']
    if int(execute(reader, ['stat', '-c', '%s', remote])) != size:
        raise RuntimeError('固定副本大小不符')
    digest = hashlib.sha256()
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        for offset in range(0, size, PART):
            length = min(PART, size - offset)
            done = 0
            with closing(managed_client(server)) as client:
                args = ['dd', 'if=' + remote, 'bs=' + str(BLOCK), 'skip=' + str(offset // BLOCK),
                        'count=' + str((length + BLOCK - 1) // BLOCK)]
                identifier = client.api.exec_create(reader.id, args, user='root')['Id']
                errors = b''
                with closing(client.api.exec_start(identifier, stream=True, demux=True)) as frames:
                    for data, error in frames:
                        if error:
                            errors = (errors + error[-4096:])[-4096:]
                        if data:
                            done += len(data)
                            if done > length:
                                raise RuntimeError('源端固定副本长度超过约定的分段范围')
                            output.write(data)
                            digest.update(data)
                            callback(offset + done)
                command_output(client.api.exec_inspect(identifier)['ExitCode'], errors)
            if done != length:
                raise RuntimeError('源端固定副本分段读取不完整')
        output.flush()
        os.fsync(output.fileno())
    if digest.hexdigest() != expected['sha256']:
        raise RuntimeError('固定副本摘要不符')


class DigestReader:
    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size):
        block = self.source.read(size)
        self.digest.update(block)
        return block


def put_archive(server, container, destination, stream, report, execute):
    """现有 TAR 格式保持一致，分段临时文件只在目标暂存目录内使用。"""
    reader = ProgressReader(stream, report)
    if server.connection_mode != 'ssh' or reader.total <= PART:
        if not container.put_archive(destination, reader):
            raise RuntimeError('写入恢复暂存目录失败')
        return reader.total

    workspace = destination + '/manager-transfer-parts'
    execute(container, ['mkdir', '-m', '700', workspace], '准备接收恢复文件分段')
    entries = []
    for offset in range(0, reader.total, PART):
        length = min(PART, reader.total - offset)
        member = tarfile.TarInfo(f'part-{offset // PART:08d}')
        member.size, member.mode = length, 0o600
        part = DigestReader(stream)
        with tempfile.TemporaryFile() as wire:
            with tarfile.open(fileobj=wire, mode='w') as archive:
                archive.addfile(member, part)
            wire.seek(0)

            def account(sent, _):
                report(offset + min(length, max(0, sent - tarfile.BLOCKSIZE)), reader.total)

            with closing(managed_client(server)) as client:
                if not client.api.put_archive(container.id, workspace, ProgressReader(wire, account)):
                    raise RuntimeError('目标接收恢复文件分段失败')
        entries.append(f'{part.digest.hexdigest()}  {member.name}\n')

    checksums = ''.join(entries).encode('ascii')
    wire = io.BytesIO()
    with tarfile.open(fileobj=wire, mode='w') as archive:
        manifest = tarfile.TarInfo('SHA256SUMS')
        manifest.mode, manifest.size = 0o600, len(checksums)
        archive.addfile(manifest, io.BytesIO(checksums))
    if not container.put_archive(workspace, wire.getvalue()):
        raise RuntimeError('写入恢复文件分段摘要失败')
    from services.artifact_io import artifact_tool

    command = '''
cd "$1"
set -- "$1" "$2" "$3" "$4" part-*
test "$#" -eq "$(( $3 + 4 ))"
sha256sum -c SHA256SUMS >/dev/null
mkfifo joined
cat part-* > joined &
sender=$!
if ! ("$4" extract "$2" '' '' && cat >/dev/null) < joined; then
    kill "$sender" 2>/dev/null || :
    wait "$sender" 2>/dev/null || :
    exit 1
fi
wait "$sender"
rm -rf -- "$1"
'''
    with artifact_tool(container) as remote:
        execute(container, ['sh', '-ec', command, 'sh', workspace, destination, str(len(entries)), remote + '/tool'],
                '校验并解开目标暂存文件')
    return reader.total
