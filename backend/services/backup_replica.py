"""把可独立恢复的全量 ZIP 提前送到固定备机，并保留最近七个成功备份点。"""
from contextlib import closing, contextmanager
from datetime import datetime
import hashlib
import json
import logging
import os
from queue import Empty, Full, Queue
import re
import tarfile
import threading
import uuid

from docker.errors import NotFound
from docker.types import Mount

from sqlalchemy import func

from models import Backup, BackupExport, BackupReplica, GiteaServer, ScheduledTask, db
from services.backup_consistency import execute
from services.docker_service import managed_client
from services.large_docker_transfer import PART
from services.restore_recovery import _file_lease
from services.restore_secrets import put_file

CACHE = '/manager-restore-cache'
LIMIT = 7
PRETRANSFER_WAIT_SECONDS = 12
_OWNER = 'gitea-manager-restore-cache-v1'
_SHA = re.compile('[a-f0-9]{64}')
_KEY = re.compile('[a-f0-9]{32}')


class InvalidReplica(RuntimeError):
    pass


def targets(source_id):
    return sorted({row.fast_restore_target_id for row in ScheduledTask.query.filter_by(source_server_id=source_id)
                   if row.fast_restore_target_id is not None})


def binding(client, target, gitea):
    mounts = sorted((item.get('Type'), item.get('Source'), item.get('Destination'))
                    for item in gitea.attrs['Mounts'] if item.get('Destination') == '/data')
    if len(mounts) != 1:
        raise RuntimeError('备机需要独立持久数据卷')
    identity = [client.info().get('ID'), mounts, target.gitea_container, target.pg_container, target.pg_dbname]
    if not identity[0]:
        raise RuntimeError('备机 Docker 身份不可用')
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def volume_for(client, identity, create=False):
    name = 'gitea-manager-restore-cache-' + identity[:32]
    labels = {'gitea.manager.owner': _OWNER, 'gitea.manager.target': identity}
    try:
        volume = client.volumes.get(name)
    except NotFound:
        if not create:
            raise
        volume = client.volumes.create(name=name, driver='local', labels=labels)
    volume.reload()
    if (volume.attrs['Driver'] != 'local' or volume.attrs.get('Options')
            or volume.attrs.get('Labels') != labels):
        raise RuntimeError('备机缓存卷归属不匹配')
    return Mount(target=CACHE, source=volume.name, type='volume', read_only=not create)


@contextmanager
def receiver(client, target, current):
    with _file_lease('backup-cache-volume:' + current):
        with _receiver(client, target, current) as helper:
            yield helper


@contextmanager
def _receiver(client, target, current):
    mount = volume_for(client, current, create=True)
    gitea = client.containers.get(target.gitea_container)
    name = 'manager-backup-cache-' + current[:32]
    labels = {'gitea.manager.owner': _OWNER, 'gitea.manager.target': current}
    try:
        helper = client.containers.get(name)
    except NotFound:
        helper = client.containers.create(gitea.attrs['Image'], name=name, entrypoint=['sleep'],
            command=['infinity'], network_mode='none', mounts=[mount], labels=labels)
    helper.reload()
    if (helper.labels != labels or helper.attrs['HostConfig']['NetworkMode'] != 'none'
            or [(row['Name'], row['Destination'], row['RW']) for row in helper.attrs['Mounts']
                if row['Type'] == 'volume' and row['Destination'] == CACHE] != [(mount['Source'], CACHE, True)]):
        raise RuntimeError('备机缓存助手身份不符')
    try:
        if helper.status != 'running':
            helper.start()
        execute(helper, ['chmod', '700', CACHE])
        yield helper
    finally:
        try:
            helper.remove(force=True, v=True)
        except NotFound:
            pass
        except Exception:
            logging.warning('备机缓存助手稍后复用或清理')


def save(row, state, done=None, total=None, error=''):
    row.state, row.error, row.updated_at = state, error, datetime.utcnow()
    if done is not None:
        row.done = done
    if total is not None:
        row.total = total
    db.session.commit()


def replica(backup, target_id):
    row = db.session.get(BackupReplica, (backup.id, target_id))
    if row is None:
        row = BackupReplica(backup_id=backup.id, target_id=target_id, source_id=backup.source_server_id,
                            key=uuid.uuid4().hex)
        db.session.add(row)
        db.session.commit()
    return row


def cache_dir(row):
    if not _KEY.fullmatch(row.key):
        raise RuntimeError('备机缓存任务标识无效')
    return CACHE + '/' + row.key


def archive_path(row, digest):
    if not _SHA.fullmatch(digest):
        raise RuntimeError('备份归档摘要无效')
    return cache_dir(row) + '/archive-' + digest + '.zip'


def make_parts(helper, directory):
    execute(helper, ['sh', '-ec', 'umask 077; mkdir -p "$1/parts"; test ! -L "$1"; test ! -L "$1/parts"', 'sh', directory])
    # 上次中断的候选归档既未发布，也不参与后续合并。
    execute(helper, ['sh', '-ec', 'for candidate in "$1"/.assembling-*; do rm -f -- "$candidate"; done', 'sh', directory])


def describe_chunk(source, number, size):
    offset = source.tell()
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        body = source.read(min(1024 * 1024, remaining))
        if not body:
            raise RuntimeError('待预传分块长度不符')
        digest.update(body)
        remaining -= len(body)
    source.seek(offset)
    return f'{number:08d}-' + digest.hexdigest()


def send_chunk(target, helper, directory, name, source, size):
    # 分段传输使用新 SSH 连接，避免多小时会话重协商。
    import tempfile
    with tempfile.TemporaryFile() as wire:
        with tarfile.open(fileobj=wire, mode='w') as archive:
            member = tarfile.TarInfo(name + '.partial')
            member.size, member.mode = size, 0o600
            archive.addfile(member, source)
        wire.seek(0)
        with closing(managed_client(target)) as client:
            if not client.api.put_archive(helper.id, directory, wire):
                raise RuntimeError('备机分块接收失败')
            remote = client.containers.get(helper.id)
            execute(remote, ['sh', '-ec', 'test ! -L "$1.partial"; mv -f "$1.partial" "$1"', 'sh', directory + '/' + name])


def _space(helper):
    return int(execute(helper, ['sh', '-ec', 'df -Pk "$1" | tail -1 | awk \'{print $4}\'', 'sh', CACHE])) * 1024


def _matching_parts(helper, directory, plan, tool):
    from services.artifact_io import run_artifact_tool
    present = set()

    def report(item, final):
        if final and item.get('complete') is not True:
            raise RuntimeError('备机分块检查没有确认完成')
        if 'part' in item:
            present.add(item['part'])

    run_artifact_tool(helper, [tool, 'cache-inspect', directory + '/parts', plan, ''], report)
    return present


def _stop_requested():
    from services.upgrade_maintenance import active
    if active():
        raise RuntimeError('Manager 正在维护，备机预传将在维护结束后继续')


def seal(target, helper, row, backup, chunks=None, report=None):
    from services.artifact_io import artifact_tool
    directory = cache_dir(row)
    make_parts(helper, directory)
    filename = archive_path(row, backup.archive_sha256)
    # 只有此前已核验的成品允许复用。恢复前仍会再次读取并比对整包。
    if row.state == 'ready' and execute(helper, ['sh', '-ec', 'test ! -L "$1"; if test -f "$1"; then echo yes; fi',
                                                     'sh', filename]).strip() == 'yes':
        return
    if chunks is None:
        chunks = backup.archive_parts or describe(backup.file_path)
    plan = directory + '/index.json'
    put_file(helper, plan, json.dumps({'sha256': backup.archive_sha256, 'size': backup.file_size, 'parts': chunks}).encode(), 0o600)
    with artifact_tool(helper) as remote:
        tool = remote + '/tool'
        present = _matching_parts(helper, directory, plan, tool)
        missing = sum(item['size'] for item in chunks if item['name'] not in present)
        reserve = max(1024 ** 3, backup.file_size)
        if _space(helper) < backup.file_size + missing + reserve:
            raise RuntimeError('备机缓存可用空间不足，已有恢复副本已保留')
        done = backup.file_size - missing
        save(row, 'receiving', done, backup.file_size)
        if report:
            report(done, backup.file_size)
        with open(backup.file_path, 'rb') as source:
            for number, item in enumerate(chunks):
                _stop_requested()
                if item['name'] in present:
                    source.seek(item['size'], os.SEEK_CUR)
                    continue
                size = item['size']
                if describe_chunk(source, number, size) != item['name']:
                    raise RuntimeError('Manager 全量备份与已校验分块清单不一致')
                if _space(helper) < backup.file_size + size + reserve:
                    raise RuntimeError('备机空间不足，已暂停预传并保留现有副本')
                send_chunk(target, helper, directory + '/parts', item['name'], source, size)
                done += size
                save(row, 'receiving', done)
                if report:
                    report(done, backup.file_size)
        from services.artifact_io import run_artifact_tool
        run_artifact_tool(helper, [tool, 'cache-seal', directory + '/parts', filename, plan], None)
    save(row, 'ready', backup.file_size, backup.file_size)
    execute(helper, ['rm', '-rf', '--', directory + '/parts', plan])


def describe(filename):
    chunks = []
    with open(filename, 'rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        for index, offset in enumerate(range(0, size, PART)):
            length = min(PART, size - offset)
            chunks.append({'name': describe_chunk(stream, index, length), 'size': length})
            stream.seek(length, os.SEEK_CUR)
    return chunks


def _keep_latest(source_id):
    point_time = func.coalesce(BackupExport.exported_at, Backup.source_stopped_at, Backup.started_at)
    rows = (Backup.query.outerjoin(BackupExport, BackupExport.id == Backup.source_export_id)
            .filter(Backup.source_server_id == source_id, Backup.status == 'success',
                    Backup.commit_snapshot_status == 'success')
            .order_by(point_time.desc(), Backup.started_at.desc(), Backup.id.desc()).yield_per(100))
    exports = set()
    latest = []
    for backup in rows:
        point = ('export', backup.source_export_id) if backup.source_export_id else ('backup', backup.id)
        if point not in exports:
            exports.add(point)
            latest.append(backup.id)
        if len(latest) == LIMIT:
            break
    return latest


def _error(row, exc):
    from services.diagnostics import redact

    db.session.rollback()
    row = db.session.get(BackupReplica, (row.backup_id, row.target_id))
    if row is not None:
        save(row, 'retry', error=redact(str(exc))[:500])


@contextmanager
def wait_for_writer(target_id, wait, advancing=None):
    import time
    start = time.monotonic()
    deadline = start + wait
    latest_bytes = -1
    while True:
        try:
            lock = _file_lease('backup-cache-write:' + str(target_id))
            break
        except BlockingIOError:
            now = time.monotonic()
            if advancing and now - start < 600:
                done = advancing()
                if done is not None and done > latest_bytes:
                    latest_bytes = done
                    deadline = min(start + 600, now + wait)
            if now >= deadline:
                raise
            time.sleep(1)
    with lock:
        yield


def prepare(backup, target, report=None):
    """前台恢复优先补齐同一归档。失败时由调用方走普通恢复。"""
    from sqlalchemy import select

    if target.id not in targets(backup.source_server_id) or backup.id not in _keep_latest(backup.source_server_id):
        return
    def advancing():
        other = db.session.get(BackupReplica, (backup.id, target.id), populate_existing=True)
        if other and other.state == 'receiving':
            report(other.done, other.total)
            return other.done

    with wait_for_writer(target.id, 45 if report else 0, advancing if report else None):
        db.session.execute(select(Backup.id).where(Backup.id == backup.id)).scalar_one()
        row = replica(backup, target.id)
        if row.state == 'unsupported':
            return
        try:
            import zipfile
            from services.backup_manifest import read_manifest
            with zipfile.ZipFile(backup.file_path) as archive:
                if read_manifest(archive) is None:
                    save(row, 'unsupported', error='历史归档使用普通恢复')
                    return
            if not backup.archive_sha256:
                with open(backup.file_path, 'rb') as stream:
                    backup.archive_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
                db.session.commit()
            with closing(managed_client(target)) as client:
                current = binding(client, target, client.containers.get(target.gitea_container))
                with receiver(client, target, current) as helper:
                    if row.binding != current:
                        row.binding = current
                        save(row, 'pending', 0, backup.file_size)
                    seal(target, helper, row, backup, report=report)
                    try:
                        _prune(helper, backup.source_server_id, target.id, current)
                    except Exception:
                        db.session.rollback()
                        logging.warning('已保留备机副本，等待下次回收旧版本')
        except Exception as exc:
            _error(row, exc)
            raise


def _prune(helper, source_id, target_id, current):
    wanted = _keep_latest(source_id)
    ready = BackupReplica.query.filter(BackupReplica.target_id == target_id, BackupReplica.source_id == source_id,
        BackupReplica.backup_id.in_(wanted), BackupReplica.state == 'ready', BackupReplica.binding == current).count()
    if ready != len(wanted):
        return
    for row in BackupReplica.query.filter_by(source_id=source_id, target_id=target_id, binding=current).all():
        if row.backup_id in wanted:
            continue
        backup = db.session.get(Backup, row.backup_id)
        if backup is not None and backup.status in ('running', 'pending'):
            continue
        try:
            with _file_lease('backup-cache-read:' + row.key):
                execute(helper, ['rm', '-rf', '--', cache_dir(row)])
                db.session.delete(row)
                db.session.commit()
        except BlockingIOError:
            continue


@contextmanager
def read_ready(client, target, gitea, backup):
    row = db.session.get(BackupReplica, (backup.id, target.id))
    if row is None or row.state != 'ready' or not backup.archive_sha256:
        yield None
        return
    try:
        lock = _file_lease('backup-cache-read:' + row.key)
    except BlockingIOError:
        yield None
        return
    with lock:
        try:
            db.session.refresh(row)
            current = binding(client, target, gitea)
            if row.state != 'ready' or row.binding != current:
                candidate = None
            else:
                candidate = (row.key, volume_for(client, current), archive_path(row, backup.archive_sha256))
        except Exception:
            logging.warning('备机缓存不可用，本次可使用 Manager 全量备份')
            candidate = None
        yield candidate


def invalidate(backup, target, candidate, reason):
    with _file_lease('backup-cache-write:' + str(target.id)):
        row = db.session.get(BackupReplica, (backup.id, target.id), populate_existing=True)
        if row is None or row.key != candidate[0]:
            return
        _error(row, reason)
        with closing(managed_client(target)) as client, receiver(client, target, row.binding) as helper:
            execute(helper, ['rm', '-f', '--', candidate[2]])


@contextmanager
def pretransfer(backup):
    """备机可持续接收时跟上源副本，失联或持续滞后时恢复独立备份。"""
    from flask import current_app, has_app_context

    selected = targets(backup.source_server_id) if has_app_context() else []
    if not selected:
        yield lambda path, count, total: None
        return
    app = current_app._get_current_object()
    queues = []
    key = backup.id

    def upload(target_id, parts, finished):
        with app.app_context():
            backup = db.session.get(Backup, key)
            target = db.session.get(GiteaServer, target_id)
            if target is None:
                return
            try:
                with _file_lease('backup-cache-write:' + str(target.id)):
                    row = replica(backup, target.id)
                    with closing(managed_client(target)) as client:
                        identity = binding(client, target, client.containers.get(target.gitea_container))
                        with receiver(client, target, identity) as helper:
                            if row.binding != identity:
                                row.binding = identity
                                save(row, 'pending', 0, 0)
                            directory = cache_dir(row)
                            make_parts(helper, directory)
                            while not finished.is_set() or not parts.empty():
                                try:
                                    file, number, length, total = parts.get(timeout=1)
                                except Empty:
                                    continue
                                with file:
                                    _stop_requested()
                                    if _space(helper) < length + total + max(1024 ** 3, total):
                                        raise RuntimeError('备机预传空间不足，保留已就绪副本')
                                    file.seek(number * PART)
                                    name = describe_chunk(file, number, length)
                                    send_chunk(target, helper, directory + '/parts', name, file, length)
                                save(row, 'receiving', min(total, row.done + length), total)
            except BlockingIOError:
                pass
            except Exception as exc:
                if 'row' in locals():
                    _error(row, exc)
            finally:
                finished.set()
                while True:
                    try:
                        file, _, _, _ = parts.get_nowait()
                    except Empty:
                        break
                    file.close()

    for target in selected:
        queue, event = Queue(maxsize=3), threading.Event()
        thread = threading.Thread(target=upload, args=(target, queue, event), daemon=True)
        queues.append((queue, event))
        thread.start()
    last = 0

    def received(path, count, total):
        nonlocal last
        while count - last >= PART or (count == total and count > last):
            length = min(PART, count - last)
            for queue, event in queues:
                if event.is_set():
                    continue
                file = open(path, 'rb')
                try:
                    queue.put((file, last // PART, length, total), timeout=PRETRANSFER_WAIT_SECONDS)
                except Full:
                    file.close()
                    # 限制单台长期无进展的备机拖住本次独立备份的时间。
                    event.set()
            last += length
    try:
        yield received
    finally:
        for _, event in queues:
            event.set()


def reconcile():
    """持久后台补齐最近七份，由文件锁协调两个 Gunicorn 进程。"""
    for source, target_id in sorted({(row.source_server_id, row.fast_restore_target_id)
                                  for row in ScheduledTask.query if row.fast_restore_target_id is not None}):
        target = db.session.get(GiteaServer, target_id)
        if target is None:
            continue
        latest = _keep_latest(source)
        for backup_id in latest:
            backup = db.session.get(Backup, backup_id)
            row = db.session.get(BackupReplica, (backup_id, target.id))
            if row and row.state in ('ready', 'unsupported'):
                continue
            try:
                prepare(backup, target)
            except BlockingIOError:
                break
            except Exception:
                db.session.rollback()
                break
        else:
            retired = BackupReplica.query.filter(BackupReplica.source_id == source, BackupReplica.target_id == target.id,
                                                 BackupReplica.backup_id.not_in(latest)).all()
            pending = [db.session.get(Backup, row.backup_id) for row in retired]
            if latest and any(backup is None or backup.status not in ('running', 'pending') for backup in pending):
                try:
                    with wait_for_writer(target.id, 0), closing(managed_client(target)) as client:
                        current = binding(client, target, client.containers.get(target.gitea_container))
                        with receiver(client, target, current) as helper:
                            _prune(helper, source, target.id, current)
                except BlockingIOError:
                    continue


def start(app):
    from services.upgrade_maintenance import active

    def loop():
        import time
        while True:
            if not active():
                try:
                    with app.app_context():
                        reconcile()
                except Exception:
                    logging.warning('备机缓存后台检查暂时失败', exc_info=True)
            time.sleep(30)
    threading.Thread(target=loop, name='backup-replica', daemon=True).start()


def status_for(backup):
    rows = BackupReplica.query.filter_by(backup_id=backup.id).all()
    return {str(row.target_id): dict(state=row.state, done=row.done, total=row.total,
            updated_at=row.updated_at.isoformat() + 'Z', error=row.error) for row in rows}
