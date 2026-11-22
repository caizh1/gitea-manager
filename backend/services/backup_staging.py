"""源端持久化固定副本；源站恢复后只读接收，失败副本由管理员保留或丢弃。"""
import base64
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import uuid
from types import SimpleNamespace

from docker.errors import NotFound
from docker.types import Mount

from services.backup_metadata import metadata_io, write_json, write_zip_json
from services.restore_secrets import read_file, security_values, uri_path, LEGACY_SECRET

MOUNT = '/manager-backup-spool'
OWNER = 'gitea-manager-site-backup-v1'
METADATA = 'export.json'


def export_path(marker):
    if not isinstance(marker, str) or not re.fullmatch(r'manager-backup-[0-9a-f]{32}', marker):
        raise RuntimeError('源端导出标识无效')
    return MOUNT + '/' + marker


@contextmanager
def export_lease(identifier):
    from config import BACKUP_DIR
    root = Path(BACKUP_DIR).parent / 'export-locks'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(root / str(int(identifier)), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('该固定副本正在接收或清理，请稍后重试') from None
        yield


def verify_volume(client, export):
    context = json.loads(export.context_json)
    volume = client.volumes.get(export.volume)
    volume.reload()
    if (volume.attrs.get('Driver') != 'local' or volume.attrs.get('Options')
            or volume.attrs.get('Labels') != {'gitea.manager.owner': OWNER, 'gitea.manager.source': context['来源容器']}):
        raise RuntimeError('源端备份卷归属不匹配')


def ensure_spool(client, source):
    name = 'gitea-manager-spool-' + hashlib.sha256(source.id.encode()).hexdigest()[:24]
    labels = {'gitea.manager.owner': OWNER, 'gitea.manager.source': source.id}
    try:
        volume = client.volumes.get(name)
    except NotFound:
        volume = client.volumes.create(name=name, driver='local', labels=labels)
    volume.reload()
    if volume.attrs.get('Driver') != 'local' or volume.attrs.get('Options') or volume.attrs.get('Labels') != labels:
        raise RuntimeError('源端备份卷归属不匹配')
    return name


def spool_mount(name, readonly=False):
    if not re.fullmatch(r'gitea-manager-spool-[0-9a-f]{24}', name):
        raise RuntimeError('源端备份卷名称无效')
    return Mount(target=MOUNT, source=name, type='volume', read_only=readonly)


def create_export(client, source, server, backup, journal):
    from models import BackupExport, db
    from services.backup_recovery import SERVER_FIELDS
    context = {'服务器': {key: getattr(server, key) for key in SERVER_FIELDS},
               '来源容器': source.id, '镜像': source.attrs['Image']}
    export = BackupExport(source_server_id=server.id, marker=journal.value['操作标识'],
                          volume=ensure_spool(client, source), context_json=json.dumps(context, ensure_ascii=False))
    db.session.add(export)
    db.session.flush()
    backup.source_export_id = export.id
    journal.value['固定导出ID'] = export.id
    journal.value['站点备份卷'] = export.volume
    journal.save('准备源端固定导出')
    db.session.commit()
    return export


def prepare_export(helper, database, server, marker):
    from config import BACKUP_DIR
    from services.artifact_io import artifact_binary
    from services.backup_consistency import execute, database_sql
    root = export_path(marker)
    # 主数据可能同时存在原始临时副本、ZIP及独立制品副本，数据库还需要SQL临时空间。
    data_bytes = int(execute(helper, ['sh', '-ec', 'du -sk /data | cut -f 1'])) * 1024
    database_bytes = int(database_sql(database, server, 'SELECT pg_database_size(current_database())'))
    required = (data_bytes + database_bytes) * 3 + 1024 ** 3
    available = int(execute(helper, ['sh', '-ec', 'df -Pk "$1" | tail -n 1 | awk \'{print $4}\'', 'sh', MOUNT])) * 1024
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if available < required or shutil.disk_usage(BACKUP_DIR).free < required:
        raise RuntimeError(f'备份空间不足，源端和接收端各需至少 {required} 字节可用空间，尚未停止源站')
    artifact_binary(helper.client)
    execute(helper, ['sh', '-ec', 'umask 077; test ! -e "$1"; mkdir "$1"; chown git:git "$1"; chmod 700 "$1"', 'sh', root])
    return root


def freeze_metadata(helper, source, database, server, export, snapshot, tables, requirements, startup, raw, journal,
                    progress=None):
    from models import db
    from services.backup_consistency import execute, database_sql
    from services.backup_actions import actions_path
    from services.artifact_io import collect_artifacts
    from services.diagnostics import register_secret
    root = export_path(export.marker)
    directory = actions_path(raw)
    records = json.loads(database_sql(database, server,
        "SELECT coalesce(json_agg(storage_path),'[]') FROM action_artifact WHERE status=2"))
    def report(sample, final):
        progress.show('snapshot_actions', '扫描并计算源端制品摘要', item=sample['item'],
                      artifacts={k: v for k, v in sample.items() if k != 'item'}, heartbeat=True, force=final)

    files = ['gitea.zip', 'ssh.tar'] + collect_artifacts(helper, directory, root, report if progress else None)
    if progress:
        progress.show('freeze_export', '固化密钥、SSH 身份与备份校验基准')
    execute(helper, ['sh', '-ec', 'umask 077; tar -cf "$1" -C /data ssh', 'sh', root + '/ssh.tar'])
    secret, uri = security_values(raw)
    if uri:
        secret = read_file(source, uri_path(uri))[0].decode().strip()
    elif not secret:
        secret = LEGACY_SECRET
    if not secret:
        raise RuntimeError('源端加密密钥为空')
    register_secret(secret)
    components = {}
    for name in files:
        digest = execute(helper, ['sha256sum', root + '/' + name]).split()[0]
        size = int(execute(helper, ['stat', '-c', '%s', root + '/' + name]))
        if not re.fullmatch(r'[0-9a-f]{64}', digest) or size <= 0:
            raise RuntimeError('固定副本大小或摘要无效')
        components[name] = {'sha256': digest, 'size': size}
    value = {'版本': 2, '导出标识': export.marker, '来源容器': source.id, '来源名称': server.name,
             '来源令牌': server.api_token, '配置': base64.b64encode(raw).decode(), '仓库快照': snapshot,
             '数据表': tables, '准入': requirements, '启动依赖': startup, '文件': components,
             '密钥': {'配置摘要': hashlib.sha256(raw).hexdigest(), '数据加密密钥': secret},
             '制品': {'directory': directory, 'records': records}}
    digest = upload_metadata(helper, root, value)
    execute(helper, ['sh', '-ec', 'sync; mv "$1" "$2"; chmod -R a-w "$3"; sync',
                     'sh', root + '/' + METADATA + '.partial', root + '/' + METADATA, root])
    from services.backup_consistency import require_quiet
    require_quiet(database, server)
    total = sum(item['size'] for item in components.values())
    exported_at = datetime.utcnow()
    journal.value['站点导出'] = {'摘要': digest, '大小': total, '时间UTC': exported_at.isoformat()}
    journal.save('固定导出已持久化')
    export.digest, export.size, export.exported_at, export.state = digest, total, exported_at, 'ready'
    db.session.commit()


def upload_metadata(helper, root, value):
    from config import BACKUP_DIR
    size = None
    with metadata_io('固化导出元数据', lambda: size):
        with tempfile.TemporaryFile(dir=BACKUP_DIR) as payload, tempfile.TemporaryFile(dir=BACKUP_DIR) as wire:
            write_json(payload, value)
            size = payload.tell()
            payload.seek(0)
            digest = hashlib.file_digest(payload, 'sha256').hexdigest()
            payload.seek(0)
            entry = tarfile.TarInfo(METADATA + '.partial')
            entry.size, entry.mode = size, 0o600
            with tarfile.open(fileobj=wire, mode='w') as archive:
                archive.addfile(entry, payload)
            wire.seek(0)
            if not helper.put_archive(root, wire):
                raise RuntimeError(f'固化导出元数据（{size} 字节）失败：源端拒绝写入')
    return digest


def restore_export_index(backup, journal):
    export = db_export(backup)
    receipt = journal.value.get('站点导出')
    if not backup.source_stopped_at and journal.value.get('停止请求已持久化'):
        stopped = journal.value.get('实际停止源', {}).get('State', {})
        try:
            finished = datetime.fromisoformat(stopped.get('FinishedAt', '').replace('Z', '+00:00'))
            backup.source_stopped_at = finished.astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, AttributeError):
            pass
    if backup.source_stopped_at and not backup.source_restored_at:
        checked = journal.value.get('源健康核实时间UTC')
        if checked or journal.value.get('源收尾已完成'):
            backup.source_restored_at = datetime.fromisoformat(checked or journal.value['记录时间UTC'])
    if export and receipt and not export.exported_at:
        export.digest, export.size = receipt['摘要'], receipt['大小']
        export.exported_at = datetime.fromisoformat(receipt['时间UTC'])
        export.state = 'ready'


def db_export(backup):
    from models import BackupExport, db
    return db.session.get(BackupExport, backup.source_export_id) if backup.source_export_id else None


class BlockReader(io.RawIOBase):
    def __init__(self, blocks):
        self.blocks, self.pending = iter(blocks), b''

    def readable(self):
        return True

    def readinto(self, buffer):
        if not self.pending:
            self.pending = next(self.blocks, b'')
        size = min(len(buffer), len(self.pending))
        buffer[:size] = self.pending[:size]
        self.pending = self.pending[size:]
        return size


def receive_file(reader, remote, target, expected=None, callback=None):
    blocks, details = reader.get_archive(remote, chunk_size=1024 * 1024)
    with closing(blocks), io.BufferedReader(BlockReader(blocks)) as stream, tarfile.open(fileobj=stream, mode='r|') as archive:
        member = archive.next()
        if not member or not member.isfile() or member.name != Path(remote).name:
            raise RuntimeError('固定副本归档条目无效')
        if member.size != details.get('size', member.size) or (expected and member.size != expected['size']):
            raise RuntimeError('固定副本大小不符')
        digest = hashlib.sha256()
        with metadata_io(f'接收固定副本 {member.name}', member.size), archive.extractfile(member) as body, \
                os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
            count = 0
            for block in iter(lambda: body.read(1024 * 1024), b''):
                output.write(block)
                digest.update(block)
                count += len(block)
                if callback:
                    callback(count)
            output.flush()
            os.fsync(output.fileno())
        if count != member.size or archive.next() is not None or (expected and digest.hexdigest() != expected['sha256']):
            raise RuntimeError('固定副本摘要或文件集合不符')
        return digest.hexdigest()


def reader_container(client, export, journal, writable=False, tool_scratch=False):
    verify_volume(client, export)
    context = json.loads(export.context_json)
    name = 'manager-spool-reader-' + uuid.uuid4().hex
    spec = {'名称': name, '镜像': context['镜像'], '卷': export.volume, '导出': export.marker, '可写': writable}
    mounts = [spool_mount(export.volume, not writable),
              Mount(target='/data', source='', type='tmpfs', read_only=True, tmpfs_size=1048576)]
    if tool_scratch:
        # 仅工具助手的容器写层可写。没有业务卷，固定副本始终以只读方式挂载。
        spec['临时工具写层'] = True
    journal.value['导出读取助手'] = spec
    journal.value['传输收尾完成'] = False
    journal.save('准备导出读取助手')
    reader = client.containers.create(context['镜像'], name=name, entrypoint=['sleep'], command=['infinity'],
        mounts=mounts,
        read_only=not tool_scratch, network_mode='none', labels={'gitea.manager.owner': OWNER, 'gitea.manager.export': export.marker})
    spec['ID'] = reader.id
    spec['创建时间'] = reader.attrs['Created']
    journal.save('导出读取助手已创建')
    reader.start()
    return reader


def cleanup_reader(client, journal):
    spec = journal.value.get('导出读取助手')
    if not spec:
        return
    try:
        reader = client.containers.get(spec.get('ID') or spec['名称'])
    except NotFound:
        journal.value['传输收尾完成'] = True
        journal.save('导出读取助手已清理')
        return
    reader.reload()
    attrs = reader.attrs
    mounts = attrs.get('Mounts') or []
    spool = [item for item in mounts if item.get('Destination') == MOUNT]
    scratch = [item for item in mounts if item.get('Destination') == '/data']
    tool = [item for item in mounts if item.get('Destination') == '/tmp']
    tool_expected = bool(spec.get('临时工具目录'))  # 兼容上一次中断的临时目录助手。
    writable_layer = bool(spec.get('临时工具写层'))
    if (reader.name != spec['名称'] or attrs['Image'] != spec['镜像']
            or (spec.get('ID') and reader.id != spec['ID'])
            or (spec.get('创建时间') and attrs['Created'] != spec['创建时间'])
            or attrs['HostConfig'].get('NetworkMode') != 'none'
            or bool(attrs['HostConfig'].get('ReadonlyRootfs')) == writable_layer
            or attrs['Config'].get('Entrypoint') != ['sleep'] or attrs['Config'].get('Cmd') != ['infinity']
            or attrs['Config'].get('Labels') != {'gitea.manager.owner': OWNER, 'gitea.manager.export': spec['导出']}
            or len(mounts) != 2 + tool_expected or len(spool) != 1 or spool[0].get('Name') != spec['卷']
            or len(tool) != tool_expected or (tool_expected and (tool[0].get('Type') != 'tmpfs' or not tool[0].get('RW')))
            or spool[0].get('RW') != spec.get('可写', False)
            or len(scratch) != 1 or scratch[0].get('Type') != 'tmpfs' or scratch[0].get('RW')):
        raise RuntimeError('读取助手与持久化创建意图不符，拒绝清理')
    reader.remove(force=True)
    journal.value['传输收尾完成'] = True
    journal.save('导出读取助手已清理')


def receive_export(client, export, destination, backup, journal, progress, server=None):
    from services import backup_chunks
    from services.artifact_io import INVENTORY
    from services.large_docker_transfer import PART, receive_in_parts
    root = export_path(export.marker)
    reuse = (server is not None and server.connection_mode == 'ssh' and backup_chunks.enabled(export.source_server_id))
    reader = reader_container(client, export, journal, tool_scratch=reuse) if reuse else reader_container(client, export, journal)
    os.makedirs(Path(destination).parent, exist_ok=True)
    from services.backup_replica import pretransfer
    with tempfile.TemporaryDirectory(prefix='receive-', dir=str(Path(destination).parent)) as temp, \
            pretransfer(backup) as enqueue:
        folder = Path(temp)
        progress.show('transfer', '接收固定导出元数据', item=METADATA, force=True)
        metadata = folder / METADATA
        digest = receive_file(reader, root + '/' + METADATA, metadata)
        if digest != export.digest:
            raise RuntimeError('导出元数据摘要不符')
        with metadata_io('解析固定导出元数据', metadata.stat().st_size), metadata.open('rb') as stream:
            value = json.load(stream)
        context = json.loads(export.context_json)
        version = value.get('版本')
        if version not in (1, 2) or value.get('导出标识') != export.marker or value.get('来源容器') != context['来源容器']:
            raise RuntimeError('导出身份不符')
        files = value['文件']
        core = {'gitea.zip', 'ssh.tar'}
        if version == 2:
            core.add(INVENTORY)
        if version == 2 and not value['制品']['directory'].startswith('/data/gitea/'):
            allowed = (core | {'actions.tar'},)
        elif version == 2:
            allowed = (core,)
        else:
            allowed = (core, core | {'actions.tar'})
        if set(files) not in allowed:
            raise RuntimeError('固定导出文件集合不符')
        if sum(item['size'] for item in files.values()) != export.size:
            raise RuntimeError('固定导出总大小不符')
        base = 0
        for name, expected in files.items():
            def report(count):
                if name == 'gitea.zip' and 0 < count < expected['size']:
                    enqueue(folder / name, count, expected['size'])
                done = base + count
                progress.show('transfer', '接收源端固定副本', current=done, total=export.size,
                              unit='bytes', item=name, heartbeat=True)
            report(0)
            reused = False
            if reuse and expected['size'] > PART:
                try:
                    reused = backup_chunks.receive(
                        server, reader, export, name, root + '/' + name, folder / name, expected, progress, report)
                except Exception as error:
                    from services.diagnostics import CURRENT, redact
                    (folder / name).unlink(missing_ok=True)
                    if CURRENT.get():
                        CURRENT.get().event('跳过', '内容复用暂不可用，重新接收固定副本', 原因=redact(str(error))[:500])
                    progress.show('transfer', '内容复用暂不可用，回退完整接收', item=name, force=True)
            if not reused:
                if server is not None and server.connection_mode == 'ssh' and expected['size'] > PART:
                    receive_in_parts(server, reader, root + '/' + name, folder / name, expected, report)
                else:
                    receive_file(reader, root + '/' + name, folder / name, expected=expected, callback=report)
            if name == 'gitea.zip':
                enqueue(folder / name, expected['size'], expected['size'])
            base += expected['size']
        progress.show('transfer', '固定副本已接收并校验', current=base, total=export.size,
                      unit='bytes', heartbeat=True, force=True)
        warning = assemble_export(folder, value, progress)
        os.replace(folder / 'gitea.zip', destination)
        descriptor = os.open(Path(destination).parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if warning:
            backup.actions_warning = warning


def assemble_export(folder, value, progress):
    from services.restore_secrets import capture_backup_secret
    from services.backup_dependencies import append_startup_files
    from services.backup_actions import append_artifacts, append_indexed_artifacts
    from services.backup_manifest import append_manifest
    from services.restore_archive import validate_ssh_archive
    from services.backup_consistency import SNAPSHOT_FILE
    import zipfile
    path = str(folder / 'gitea.zip')
    progress.show('assemble', '从固定副本整理备份内容')
    capture_backup_secret(None, SimpleNamespace(file_path=path), snapshot=value['密钥'])
    append_startup_files(path, value['启动依赖'])
    artifact = dict(value['制品'], archive_path=str(folder / 'actions.tar') if 'actions.tar' in value['文件'] else None)
    artifact_checks = None
    warning = None
    if value.get('版本') == 2:
        from services.artifact_io import INVENTORY
        with (folder / INVENTORY).open('rb') as stream:
            inventory = json.load(stream)
        artifact_checks, warning = append_indexed_artifacts(path, artifact, inventory, progress)
    else:
        append_artifacts(path, None, None, None, base64.b64decode(value['配置'], validate=True), progress=progress, staged=artifact)
    if (folder / 'ssh.tar').stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError('SSH 主机身份归档过大')
    keys = (folder / 'ssh.tar').read_bytes()
    validate_ssh_archive(keys)
    with zipfile.ZipFile(path, 'a') as archive:
        write_zip_json(archive, SNAPSHOT_FILE, value['仓库快照'])
        archive.writestr('manager-ssh-hostkeys.tar', keys)
    append_manifest(path, value['准入'], value['数据表'], SimpleNamespace(name=value['来源名称'], api_token=value['来源令牌']),
                    progress=progress, artifact_checks=artifact_checks)
    with open(path, 'rb') as stream:
        os.fsync(stream.fileno())
    return warning


def retry_export(server, backup, journal):
    from models import db
    from config import BACKUP_DIR
    from services.backup_recovery import SERVER_FIELDS
    from services.docker_service import managed_client
    from services.backup_activity import BackupActivity
    from services.backup_outcome import RecoveryProgress, record_failure
    export = db_export(backup)
    context = json.loads(export.context_json)
    if context['服务器'] != {key: getattr(server, key) for key in SERVER_FIELDS}:
        raise RuntimeError('源端连接登记已变化，不能将旧副本重定向到其他主机')
    journal.value['源收尾已完成'] = True
    journal.save('仅接收固定副本')
    progress = BackupActivity(backup)
    progress.source = 'available' if backup.source_restored_at else None
    with export_lease(export.id), closing(managed_client(server)) as client:
        db.session.refresh(export)
        if export.state != 'ready':
            raise RuntimeError('固定副本已不可重新接收')
        try:
            receive_export(client, export, os.path.join(BACKUP_DIR, backup.filename), backup, journal, progress,
                           server=server)
            backup.file_path = os.path.join(BACKUP_DIR, backup.filename)
            backup.file_size = os.path.getsize(backup.file_path)
            db.session.commit()
        except Exception as error:
            try:
                db.session.rollback()
                record_failure(backup, error)
            except Exception:
                db.session.rollback()
            raise
        finally:
            import sys
            original = sys.exc_info()[1]
            try:
                if journal.value.get('导出读取助手') and not journal.value.get('传输收尾完成'):
                    with RecoveryProgress(backup).run('清理固定副本读取助手') as recovery:
                        if backup.source_restored_at:
                            recovery.source('available', backup.source_restored_at)
                        cleanup_reader(client, journal)
            except Exception as error:
                if original is None:
                    raise
                original.add_note('读取助手清理失败：' + type(error).__name__)


def cleanup_export(export, server, discard=False):
    """调用方持有导出锁；状态和清理意图持久化，允许中断后安全续清理。"""
    from models import Backup, db
    from services.backup_recovery import Journal, SERVER_FIELDS, directory
    from services.docker_service import managed_client
    from services.backup_consistency import execute
    db.session.refresh(export)
    if export.state in ('cleaned', 'discarded'):
        return
    if Backup.query.filter(Backup.source_export_id == export.id,
            db.or_(Backup.status.in_(('running', 'pending')), Backup.recovery_pending.is_(True))).first():
        raise RuntimeError('固定副本仍有接收或收尾任务')
    if json.loads(export.context_json)['服务器'] != {key: getattr(server, key) for key in SERVER_FIELDS}:
        raise RuntimeError('源端连接登记已变化，拒绝清理')
    success = Backup.query.filter_by(source_export_id=export.id, status='success').first()
    if not discard and not success:
        raise RuntimeError('固定副本尚未成功接收，请明确确认丢弃')
    discard = discard or export.state == 'discard_pending'
    export.state = 'discard_pending' if discard else 'cleanup_pending'
    db.session.commit()
    # 独立清理日志可在删除回执丢失后再次核对同一个路径和助手。
    path = directory() / ('export-cleanup-' + str(export.id) + '.json')
    journal = Journal(path, json.loads(path.read_text()) if path.exists() else {})
    with closing(managed_client(server)) as client:
        try:
            cleanup_reader(client, journal)
            reader = reader_container(client, export, journal, writable=True)
            root = export_path(export.marker)
            script = ('test ! -L "$1"; if test -e "$1"; then '
                      'if test -n "$2"; then test "$(sha256sum "$1/export.json" | cut -d " " -f 1)" = "$2"; fi; '
                      'chmod u+w "$1"; rm -rf -- "$1"; fi; sync')
            execute(reader, ['sh', '-ec', script, 'sh', root, export.digest or ''])
            cleanup_reader(client, journal)
            export.state = 'discarded' if discard else 'cleaned'
            export.cleaned_at, export.error_msg = datetime.utcnow(), ''
            db.session.commit()
        except Exception:
            export.error_msg = '源端副本清理未完成，请重试清理'
            db.session.commit()
            raise


def cleanup_success(backup):
    from models import db
    export = db_export(backup)
    if export and export.state not in ('cleaned', 'discarded'):
        try:
            with export_lease(export.id):
                cleanup_export(export, backup.source_server)
        except Exception:
            # 副本清理失败不能覆盖已经校验并持久化的成功备份。
            export.error_msg = '源端副本清理未完成，请重试清理'
            db.session.commit()


def recover_exports():
    """成功回执已提交后发生中断时，巡检只继续副本清理。"""
    from models import BackupExport, Backup, db
    rows = BackupExport.query.filter(BackupExport.state.in_(('ready', 'cleanup_pending'))).all()
    for export in rows:
        if Backup.query.filter(Backup.source_export_id == export.id,
                db.or_(Backup.status.in_(('running', 'pending')), Backup.recovery_pending.is_(True))).first():
            continue
        success = Backup.query.filter_by(source_export_id=export.id, status='success').first()
        if success:
            cleanup_success(success)
