"""从只读固定副本补传缺块，生成与源端逐字节相同的完整文件。"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess

from config import BACKUP_DIR
from services.artifact_io import artifact_binary_for_machine, artifact_tool, run_artifact_tool
from services.backup_consistency import command_output
from services.diagnostics import CURRENT
from services.docker_service import managed_client

SHA256 = re.compile('[0-9a-f]{64}')
MAX_CHUNK = 32 * 1024 * 1024


def enabled(source_id):
    from models import ScheduledTask
    return ScheduledTask.query.filter_by(source_server_id=source_id, reuse_source_chunks=True).first() is not None


def _get_index(reader, tool, remote, expected):
    parts = []
    end = 0

    def report(item, final):
        nonlocal end
        if 'complete' in item:
            if item['complete'] != expected or end != expected:
                raise RuntimeError('源端分块清单总大小不符')
            return
        if (final or item.get('offset') != end or type(item.get('size')) is not int
                or not 0 < item['size'] <= min(MAX_CHUNK, expected - end)
                or not isinstance(item.get('sha256'), str) or not SHA256.fullmatch(item['sha256'])):
            raise RuntimeError('源端分块清单无效')
        end += item['size']
        parts.append(item)

    run_artifact_tool(reader, [tool, 'source-index', remote, '', ''], report)
    if end != expected:
        raise RuntimeError('源端没有返回完整分块清单')
    return parts


def _read_source(server, reader, tool, name, item, temporary):
    digest = hashlib.sha256()
    count = 0
    with closing(managed_client(server)) as client, temporary.open('xb') as output:
        args = [tool, 'source-range', name, str(item['offset']), str(item['size'])]
        job = client.api.exec_create(reader.id, args, user='root')['Id']
        errors = b''
        with closing(client.api.exec_start(job, stream=True, demux=True)) as frames:
            for data, error in frames:
                if error:
                    errors = (errors + error)[-4096:]
                if data:
                    count += len(data)
                    if count > item['size']:
                        raise RuntimeError('源端分块超过约定大小')
                    output.write(data)
                    digest.update(data)
        command_output(client.api.exec_inspect(job)['ExitCode'], errors)
        if count != item['size'] or digest.hexdigest() != item['sha256']:
            raise RuntimeError('源端分块长度或摘要不符')
        output.flush()
        os.fsync(output.fileno())


def _body(path, item):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != item['size']:
            return None
        data = stream.read(item['size'])
    if hashlib.sha256(data).hexdigest() != item['sha256']:
        return None
    return data


def _existing_zip(source_id, name):
    """首次启用时，现有 Manager 全量 ZIP 也可作为分块来源。"""
    if name != 'gitea.zip':
        return None, {}
    from models import Backup
    row = (Backup.query.filter_by(source_server_id=source_id, status='success')
           .order_by(Backup.started_at.desc(), Backup.id.desc()).first())
    if not row or not row.file_path or not row.file_size:
        return None, {}
    filename = Path(row.file_path).resolve()
    try:
        tool = artifact_binary_for_machine(platform.machine())
        output = subprocess.run([str(tool), 'source-index', str(filename), '', ''],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=600, check=True)
        parts = [json.loads(line) for line in output.stdout.splitlines()]
        if not parts or parts[-1].get('complete') != row.file_size:
            return None, {}
        return filename, {item['sha256']: item for item in parts[:-1]}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, RuntimeError):
        return None, {}


def _zip_chunk(filename, index, item):
    previous = index.get(item['sha256'])
    if previous is None or previous['size'] != item['size']:
        return None
    try:
        with filename.open('rb') as stream:
            stream.seek(previous['offset'])
            body = stream.read(item['size'])
    except OSError:
        return None
    if len(body) == item['size'] and hashlib.sha256(body).hexdigest() == item['sha256']:
        return body
    return None


def receive(server, reader, export, name, remote, destination, expected, progress, account):
    # 保留上一份组件及本次未完成组件，避免成功前清掉可用块。
    store = Path(BACKUP_DIR).parent / 'backup-chunks' / str(export.source_server_id) / name
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    pointer = store / 'latest'
    latest_name = pointer.read_text().strip() if pointer.is_file() else ''
    if latest_name and not re.fullmatch('manager-backup-[0-9a-f]{32}', latest_name):
        raise RuntimeError('本地分块缓存索引无效')
    staging = store / export.marker
    for directory in store.glob('manager-backup-*'):
        if directory.is_dir() and directory.name not in (latest_name, export.marker):
            shutil.rmtree(directory)
    # 两份均可暂存才启用内容复用，空间不足走原固定副本接收路径。
    if shutil.disk_usage(store).free < 2 * expected['size'] + 128 * 1024 * 1024:
        return False
    staging.mkdir(mode=0o700, exist_ok=True)
    previous_zip, previous_index = _existing_zip(export.source_server_id, name) if not latest_name else (None, {})
    with artifact_tool(reader) as remote_tool:
        tool = remote_tool + '/tool'
        progress.show('transfer', '正在源端固定副本中计算可复用分块', item=name, force=True)
        parts = _get_index(reader, tool, remote, expected['size'])
        total = transmitted = 0
        digest = hashlib.sha256()
        with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
            for item in parts:
                key = item['sha256']
                current, previous = staging / key, store / latest_name / key
                body = _body(current, item)
                if body is None and latest_name and previous != current:
                    body = _body(previous, item)
                    if body is not None:
                        current.unlink(missing_ok=True)
                        os.link(previous, current)
                if body is None:
                    temporary = current.with_suffix('.partial')
                    temporary.unlink(missing_ok=True)
                    body = _zip_chunk(previous_zip, previous_index, item)
                    if body is None:
                        _read_source(server, reader, tool, remote, item, temporary)
                        transmitted += item['size']
                    else:
                        with temporary.open('xb') as stream:
                            stream.write(body)
                            stream.flush()
                            os.fsync(stream.fileno())
                    os.replace(temporary, current)
                    if body is None:
                        body = _body(current, item)
                        if body is None:
                            raise RuntimeError('已接收分块无法通过本地摘要校验')
                output.write(body)
                digest.update(body)
                total += len(body)
                account(total)
            output.flush()
            os.fsync(output.fileno())
        if total != expected['size'] or digest.hexdigest() != expected['sha256']:
            raise RuntimeError('复用分块组成的固定副本完整摘要不符')
    # 完整固定副本验证结束后发布新索引。删除旧分块不影响任何完整 ZIP。
    with pointer.with_suffix('.partial').open('w') as stream:
        stream.write(export.marker + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pointer.with_suffix('.partial'), pointer)
    descriptor = os.open(store, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if latest_name and latest_name != export.marker:
        shutil.rmtree(store / latest_name, ignore_errors=True)
    message = f'{name} 实际跨网 {transmitted:,} 字节，复用 {total - transmitted:,} 字节'
    progress.show('transfer', message, item=name, force=True)
    if CURRENT.get():
        CURRENT.get().event('完成', '固定副本按内容复用', 文件=name, 跨网字节=transmitted, 复用字节=total - transmitted)
    return True
