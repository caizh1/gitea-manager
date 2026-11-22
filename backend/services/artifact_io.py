"""在数据所在的容器内批量处理制品并消费有界进度流。"""
from contextlib import closing, contextmanager
import json
import logging
import os
from pathlib import Path
import tempfile
import uuid

from services.backup_metadata import write_json
from services.restore_secrets import put_file

INVENTORY = 'actions-inventory.json'


def artifact_binary(client):
    return artifact_binary_for_machine(client.info()['Architecture'])


def artifact_binary_for_machine(machine):
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64', 'amd64': 'amd64', 'arm64': 'arm64'}.get(machine)
    if not arch:
        raise RuntimeError(f'制品批量工具暂不支持此 Docker 架构：{machine}')
    directory = Path(os.getenv('BACKUP_ARTIFACT_TOOL_DIR', '/usr/local/lib/gitea-manager/backup-artifacts'))
    binary = directory / ('linux-' + arch)
    if not binary.is_file():
        raise RuntimeError('当前 Manager 缺少制品批量工具，请核对后端镜像')
    return binary


@contextmanager
def artifact_tool(container):
    from services.backup_consistency import execute

    binary = artifact_binary(container.client).read_bytes()
    remote = '/tmp/manager-artifacts-' + uuid.uuid4().hex
    execute(container, ['mkdir', '-m', '700', remote])
    try:
        put_file(container, remote + '/tool', binary, mode=0o700)
        yield remote
    finally:
        try:
            execute(container, ['rm', '-rf', '--', remote])
        except Exception:
            logging.warning('制品工具临时目录清理失败', exc_info=True)


def run_artifact_tool(container, arguments, report):
    from services.backup_consistency import command_output

    api = container.client.api
    identifier = api.exec_create(container.id, arguments, user='root')['Id']
    errors = bytearray()
    pending = b''
    final = None
    with closing(api.exec_start(identifier, stream=True, demux=True)) as frames:
        for stdout, stderr in frames:
            if stderr:
                errors.extend(stderr[-4096:])
                del errors[:-4096]
            if stdout:
                parts = (pending + stdout).split(b'\n')
                pending = parts.pop()
                if len(pending) > 65536:
                    raise RuntimeError('制品工具返回了无效进度')
                for line in parts:
                    final = json.loads(line)
                    if report:
                        report(final, False)
    command_output(api.exec_inspect(identifier)['ExitCode'], bytes(errors))
    if pending or final is None:
        raise RuntimeError('制品工具没有返回完整的结束进度')
    if report:
        report(final, True)


def collect_artifacts(container, directory, export_root, report):
    archive = '' if directory.startswith('/data/gitea/') else export_root + '/actions.tar'
    with artifact_tool(container) as remote:
        run_artifact_tool(container, [remote + '/tool', 'inventory', directory,
                                     export_root + '/' + INVENTORY, archive], report)
    return [INVENTORY, 'actions.tar'] if archive else [INVENTORY]


def verify_artifact_files(container, directory, files, report):
    if not files:
        if report:
            report(dict(files_done=0, files_total=0, bytes_done=0, bytes_total=0, item=''), True)
        return
    import tarfile
    with artifact_tool(container) as remote, tempfile.TemporaryFile() as manifest, tempfile.TemporaryFile() as wire:
        write_json(manifest, files)
        size = manifest.tell()
        manifest.seek(0)
        with tarfile.open(fileobj=wire, mode='w') as archive:
            item = tarfile.TarInfo('inventory.json')
            item.size, item.mode = size, 0o600
            archive.addfile(item, manifest)
        wire.seek(0)
        if not container.put_archive(remote, wire):
            raise RuntimeError('无法写入恢复后的制品校验清单')
        run_artifact_tool(container, [remote + '/tool', 'verify', directory, remote + '/inventory.json', ''], report)
