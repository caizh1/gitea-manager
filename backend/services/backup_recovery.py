"""未来备份的持久收尾事务；跨工作进程互斥，只恢复本任务原先运行的同一源。"""
from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from types import SimpleNamespace

OWNER = 'gitea-manager-backup-recovery-v1'
LOCK = '/data/.manager-restore-lock'
OWNER_FILE = LOCK + '/backup-owner.json'
SERVER_FIELDS = ('id', 'host', 'ssh_port', 'ssh_user', 'connection_mode', 'gitea_container',
                 'pg_container', 'pg_user', 'pg_dbname', 'gitea_url', 'is_local')
_threads = {}
_thread_lock = threading.Lock()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def directory():
    from config import BACKUP_DIR, DATABASE_URL
    from sqlalchemy.engine import make_url
    url = make_url(DATABASE_URL)
    parent = Path(url.database).absolute().parent if url.drivername.startswith('sqlite') and url.database not in (None, ':memory:') else Path(BACKUP_DIR).absolute().parent
    root = parent / 'backup-recovery'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = root.lstat()
    require(stat.S_ISDIR(metadata.st_mode) and not root.is_symlink() and metadata.st_uid == os.geteuid()
            and metadata.st_mode & 0o077 == 0, '备份恢复事务目录归属或权限不受支持')
    return root


def paths(root, identifier, token):
    require(type(identifier) is int and identifier > 0 and isinstance(token, str) and re.fullmatch('[a-f0-9]{32}', token),
            '备份没有本版本独立事务标识，不自动接管旧备份')
    prefix = str(identifier) + '-' + token
    return Path(root) / (prefix + '.json'), Path(root) / (prefix + '.lock')


@contextmanager
def lease(root, identifier, token):
    path, lock = paths(root, identifier, token)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    stream = os.fdopen(descriptor, 'r+b')
    try:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0,
                '备份进程锁归属或权限不符')
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield path
    finally:
        stream.close()


def identity(container):
    container.reload()
    data = container.attrs
    result = {key: data[key] for key in ('Id', 'Image', 'Created', 'Mounts', 'State', 'RestartCount')}
    result['Name'] = data.get('Name', '/' + container.name)
    result['Mounts'] = sorted(result['Mounts'], key=lambda value: json.dumps(value, sort_keys=True))
    return json.loads(json.dumps(result))


def same_instance(before, after):
    return all(before[key] == after[key] for key in ('Id', 'Image', 'Created', 'Mounts', 'Name', 'RestartCount'))


def same_lifecycle(before, after):
    # 健康检查日志会自然轮转；仍保存完整原文，但不把采样日志当成进程身份。
    return same_instance(before, after) and {k: v for k, v in before['State'].items() if k != 'Health'} == {
        k: v for k, v in after['State'].items() if k != 'Health'}


def same_recovered_source(value, actual):
    before = value['原源']
    if same_instance(before, actual):
        return True
    # Docker的显式start可清零历史RestartCount；只在本任务启动意图已持久化后认可。
    return (value['源启动请求已持久化'] and actual['RestartCount'] == 0 and all(
        before[key] == actual[key] for key in ('Id', 'Image', 'Created', 'Mounts', 'Name')))


class Journal:
    def __init__(self, path, value):
        self.path, self.value = Path(path), value
        self.instance_lock = None

    def save(self, phase=None):
        if phase is not None:
            self.value['阶段'] = phase
        self.value['记录时间UTC'] = datetime.utcnow().isoformat()
        raw = (json.dumps(self.value, ensure_ascii=False, sort_keys=True) + '\n').encode()
        require(len(raw) <= 128 * 1024, '备份恢复事务元数据超过上界')
        temporary = self.path.with_suffix('.pending')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def create(cls, path, backup):
        require(not path.exists() and not path.is_symlink(), '备份事务已存在，禁止重复执行或覆盖原始记录')
        value = {'所有者': OWNER, '版本': 1, '任务ID': backup.id, '源ID': backup.source_server_id,
                 '随机校验': backup.recovery_token, '诊断编号': backup.diagnostic_id,
                 '停止请求已持久化': False, '源启动请求已持久化': False, '源收尾已完成': False,
                 '助手删除请求已持久化': False, '解锁请求已持久化': False, '事务完成': False}
        journal = cls(path, value); journal.save('准备'); return journal

    @classmethod
    def load(cls, path, identifier, token, source_id):
        metadata = path.lstat()
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid() and metadata.st_mode & 0o077 == 0
                and metadata.st_size <= 128 * 1024, '备份事务文件归属、权限或大小不符')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            value = json.load(stream)
        require(value['所有者'] == OWNER and value['版本'] == 1 and value['任务ID'] == identifier
                and value['随机校验'] == token and value['源ID'] == source_id, '备份事务与本轮数据库记录不匹配')
        return cls(path, value)

    def attach(self, server, source, database, marker):
        from services.restore_recovery import database_lease
        self.instance_lock = database_lease(database.id, server.pg_dbname)
        ensure_database_ready(database.id, server.pg_dbname, exclude=self.value['任务ID'])
        self.value.update({'服务器': {key: getattr(server, key) for key in SERVER_FIELDS},
                           '原源': identity(source), '原PG': identity(database), '操作标识': marker})
        self.save('原实例已核实')

    def before_helper(self, source, network):
        self.value['助手网络'] = network
        self.value['助手环境SHA256'] = hashlib.sha256(json.dumps(source.attrs['Config'].get('Env'), sort_keys=True).encode()).hexdigest()
        self.save('创建本次助手')

    def validate_helper(self, helper):
        helper.reload()
        attrs = helper.attrs
        require(helper.name == self.value['操作标识'] and attrs['Image'] == self.value['原源']['Image']
                and (attrs['Config'].get('Labels') or {}).get('gitea.manager.operation') == self.value['操作标识']
                and attrs['Config']['Entrypoint'] == ['sleep'] and attrs['Config']['Cmd'] == ['infinity']
                and attrs['HostConfig']['VolumesFrom'] == [self.value['原源']['Id']]
                and attrs['HostConfig']['NetworkMode'] == self.value['助手网络']
                and hashlib.sha256(json.dumps(attrs['Config'].get('Env'), sort_keys=True).encode()).hexdigest() == self.value['助手环境SHA256'],
                '本次助手完整创建意图不符，不认领其他容器')

    def before_lock(self):
        raw = json.dumps({'所有者': OWNER, '任务ID': self.value['任务ID'], '随机校验': self.value['随机校验']}).encode()
        self.value['锁所有者SHA256'] = hashlib.sha256(raw).hexdigest()
        self.value['实例锁请求已持久化'] = True
        self.save('获取实例锁')

    def helper(self, helper):
        self.validate_helper(helper)
        current = identity(helper)
        attrs = helper.attrs
        require(attrs['Config']['Labels'].get('gitea.manager.operation') == self.value['操作标识'], '本次助手标识不符')
        self.value['助手'] = current
        self.save('助手已核实')

    def lock(self, helper):
        from services.restore_secrets import put_file
        from services.backup_consistency import execute
        raw = json.dumps({'所有者': OWNER, '任务ID': self.value['任务ID'], '随机校验': self.value['随机校验']}).encode()
        execute(helper, ['mkdir', self.staged_lock()])
        put_file(helper, self.staged_lock() + '/backup-owner.json', raw, 0o600, 0, 0)
        execute(helper, ['sync'])
        execute(helper, ['sh', '-ec', 'mv -T -n -- "$1" "$2"; ! test -e "$1"; sync',
                         'sh', self.staged_lock(), LOCK])
        self.value['锁已取得'] = True
        self.save('实例锁已核实')

    def before_stop(self, source=None):
        if source is not None:
            require(same_lifecycle(self.value['原源'], identity(source)), '停源前原实例生命周期已变化，拒绝操作')
        self.value['停止请求已持久化'] = True
        self.value['原运行源恢复授权已持久化'] = self.value['原源']['State']['Running']
        self.save('停止源')

    def stopped(self, source):
        actual = identity(source)
        require(same_instance(self.value['原源'], actual) and not actual['State']['Running'], '本次原源停止状态未确认')
        self.value['实际停止源'] = actual
        self.save('原源已停止')

    def before_start(self):
        self.value['源启动请求已持久化'] = True
        self.value.setdefault('源启动请求时间UTC', datetime.utcnow().isoformat())
        self.save('恢复源')

    def recovered(self, source):
        actual = identity(source)
        require(same_recovered_source(self.value, actual) and actual['State']['Running'] == self.value['原源']['State']['Running'],
                '备份后原源身份或运行状态未恢复')
        self.value['恢复后源'] = actual
        self.save('源状态已恢复')

    def resume(self, source):
        # 恢复授权在停源之前落盘。后续元数据写盘失败仍须尝试恢复已确认停止的原源。
        require(self.value.get('原运行源恢复授权已持久化'), '没有本任务原运行源恢复授权')
        failure = None
        try:
            self.before_start()
        except Exception as exc:
            failure = exc
        source.start()
        if failure is not None:
            raise failure

    def before_remove_helper(self):
        self.value['助手删除请求已持久化'] = True
        self.save('回收本次助手')

    def retired_lock(self):
        return '/data/.manager-backup-unlock-' + self.value['随机校验']

    def staged_lock(self):
        return '/data/.manager-backup-lock-' + self.value['随机校验']

    def complete_source(self):
        self.value['源收尾已完成'] = True
        self.save('源收尾完成')


@contextmanager
def execution(backup):
    from models import db, Backup
    root = directory()
    with lease(root, backup.id, backup.recovery_token) as path:
        current = db.session.get(Backup, backup.id, populate_existing=True)
        require(current is not None and current.recovery_pending and current.status in ('running', 'pending'),
                '备份已被终结或接管，不重复执行')
        try:
            ensure_ready(current.source_server_id, exclude=current.id)
        except RuntimeError:
            current.status = 'failed'; current.completed_at = datetime.utcnow()
            current.error_msg = '同源备份尚未收尾，本任务未执行'
            current.recovery_pending = False; db.session.commit()
            raise
        journal = Journal.create(path, current)
        db.session.commit()
        try:
            yield journal
        finally:
            primary = sys.exc_info()[1]
            failures = []
            try:
                current = db.session.get(Backup, backup.id)
                if current is not None and current.status in ('success', 'failed') and (
                        journal.value['源收尾已完成'] or not journal.value.get('原源')):
                    current.recovery_pending = False
                    journal.value['事务完成'] = True; journal.save('完成')
                    db.session.commit()
            except BaseException as exc:
                failures.append(exc)
            finally:
                if journal.instance_lock is not None:
                    try:
                        journal.instance_lock.close()
                    except BaseException as exc:
                        failures.append(exc)
            if failures and primary is None:
                raise failures[0]


def ensure_ready(source_id, exclude=None):
    from models import Backup
    query = Backup.query.filter(Backup.source_server_id == source_id, Backup.recovery_pending.is_(True))
    if exclude is not None:
        query = query.filter(Backup.id != exclude)
    require(query.first() is None, '该实例有未确认收尾的备份，请先完成原源恢复，禁止覆盖或删除恢复凭据')


def ensure_database_ready(identifier, database_name, exclude=None):
    from models import Backup
    query = Backup.query.filter(Backup.recovery_pending.is_(True))
    if exclude is not None:
        query = query.filter(Backup.id != exclude)
    for task in query.all():
        if not task.recovery_token:
            continue
        path, _ = paths(directory(), task.id, task.recovery_token)
        if path.exists():
            other = Journal.load(path, task.id, task.recovery_token, task.source_server_id).value
            require(not (other.get('原PG', {}).get('Id') == identifier
                         and other.get('服务器', {}).get('pg_dbname') == database_name),
                    '同一实际数据库还有未确认收尾的备份，拒绝别名绕过互斥')


def install_guards(app):
    from flask import request, jsonify
    @app.before_request
    def protect_pending_source():
        if request.endpoint in ('servers.update_server', 'servers.delete_server'):
            try:
                ensure_ready(request.view_args['sid'])
            except RuntimeError as exc:
                return jsonify(error=str(exc)), 409


def _unlock_script():
    # 先原子移走本次锁目录，再清理唯一退休目录；中断后不删除后来其他任务的新锁。
    return ('if test -e "$4"; then test ! -L "$4"; '
            'if test -e "$4/backup-owner.json"; then '
            'test "$(sha256sum "$4/backup-owner.json" | cut -d " " -f 1)" = "$3"; rm -- "$4/backup-owner.json"; fi; rmdir "$4"; fi; '
            'if ! test -e "$2"; then '
            'if ! test -e "$1"; then exit 0; fi; '
            'test ! -L "$1"; '
            'if test "$5" = no && ! test -e "$1/backup-owner.json"; then exit 0; fi; '
            'if test "$5" = no && test "$(sha256sum "$1/backup-owner.json" | cut -d " " -f 1)" != "$3"; then exit 0; fi; '
            'test "$(sha256sum "$1/backup-owner.json" | cut -d " " -f 1)" = "$3"; '
            'mv -T -- "$1" "$2"; fi; '
            'test ! -L "$2"; '
            'if test -e "$2/backup-owner.json"; then '
            'test "$(sha256sum "$2/backup-owner.json" | cut -d " " -f 1)" = "$3"; '
            'rm -- "$2/backup-owner.json"; fi; rmdir "$2"')


def _clear_lock(client, source, journal):
    """专属无网络助手只检查本次锁令牌并rmdir，不接触业务正文或数据库。"""
    token = journal.value['随机校验']
    journal.value['解锁请求已持久化'] = True; journal.save('中断后解锁')
    from docker.errors import NotFound
    name = 'manager-backup-unlock-' + token
    command = [_unlock_script(), 'sh', LOCK, journal.retired_lock(), journal.value['锁所有者SHA256'],
               journal.staged_lock(), 'yes' if journal.value.get('停止请求已持久化') else 'no']
    try:
        helper = client.containers.get(name)
    except NotFound:
        if journal.value.get('解锁已完成'):
            return
        # 确切名称和完整意图先持久化；丢失create回执后仍只能识别这一个无网络助手。
        journal.value['解锁助手名称'] = name; journal.save('创建本次解锁助手')
        helper = client.containers.create(source.attrs['Image'], name=name, entrypoint=['sh', '-ec'],
            command=command,
            volumes_from=[source.id], network_mode='none', labels={'gitea.manager.backup-cleanup': token})
    helper.reload()
    attrs = helper.attrs
    require(journal.value.get('解锁助手名称') == name and attrs['Image'] == source.attrs['Image']
            and (attrs['Config'].get('Labels') or {}).get('gitea.manager.backup-cleanup') == token
            and attrs['HostConfig']['NetworkMode'] == 'none'
            and attrs['HostConfig']['VolumesFrom'] == [source.id]
            and attrs['Config']['Entrypoint'] == ['sh', '-ec']
            and attrs['Config']['Cmd'] == command,
            '解锁助手完整意图不符，不认领其他容器')
    if journal.value.get('解锁已完成'):
        require(not attrs['State']['Running'], '已结束解锁助手出现未知运行状态')
        helper.remove()
        return
    if attrs['State']['Status'] == 'created':
        helper.start()
    result = helper.wait(timeout=120)
    helper.reload()
    require(result['StatusCode'] == 0 and not helper.attrs['State']['Running']
            and not helper.attrs['State']['OOMKilled'], '本次锁未完成安全释放，保留精确助手')
    journal.value['解锁已完成'] = True; journal.save('本次锁已释放')
    helper.remove()


def recover_source(client, server, journal):
    from docker.errors import NotFound
    value = journal.value
    require({key: getattr(server, key) for key in SERVER_FIELDS} == value['服务器'], '服务器登记信息已变化，不自动接管')
    source = client.containers.get(value['原源']['Id'])
    database = client.containers.get(value['原PG']['Id'])
    actual, pg = identity(source), identity(database)
    require(same_recovered_source(value, actual) and same_lifecycle(pg, value['原PG']), '原源身份或原PG生命周期已变化，不擅自启停')
    before = value['原源']['State']; now = actual['State']
    if value['停止请求已持久化']:
        require(now['Status'] in ('running', 'exited') and not any(now[k] for k in ('Paused', 'Restarting', 'Dead', 'OOMKilled')),
                '源状态不属于本次受控停止或启动，不自动修改')
        if now['Running'] and now['StartedAt'] != before['StartedAt']:
            require(value['源启动请求已持久化'] and ('恢复后源' not in value or actual == value['恢复后源']),
                    '源存在未知的新启动，拒绝再次启停')
        elif now['Running']:
            require('实际停止源' in value or value.get('源收尾已完成'),
                    '停源请求结果未知，不能以当前仍在运行认定异步停源已结束')
        elif not now['Running']:
            require(now['StartedAt'] == before['StartedAt'], '停止源生命周期不是本次原实例')
            if '实际停止源' in value:
                require(actual == value['实际停止源'], '已采样的完整停止State发生变化')
    else:
        require(same_lifecycle(actual, value['原源']), '没有本次停源意图却发生源生命周期变化')
    helper = None
    if '助手' in value:
        try:
            helper = client.containers.get(value['助手']['Id'])
        except NotFound:
            # 原完整ID确已不存在，已不能继续写源；不因删除回执落盘丢失而跳过恢复源。
            # 这不认可归档成功，整条中断任务仍只能终结为failed。
            helper = None
        if helper is not None:
            journal.validate_helper(helper)
            require(same_instance(value['助手'], identity(helper)),
                    '助手不是本次完整原实例，拒绝清理')
            # 已绑定的完整助手身份即为有限删除授权；记录失败不能跳过原源恢复。
            try:
                journal.before_remove_helper()
            except Exception:
                pass
            helper.remove(force=True)
            require(not client.containers.list(all=True, filters={'id': helper.id}), '本次助手尚未实际退出删除')
    else:
        matches = client.containers.list(all=True, filters={'label': 'gitea.manager.operation=' + value['操作标识']})
        require(len(matches) <= 1, '本次助手不唯一，不自动接管')
        if matches:
            helper = matches[0]
            journal.helper(helper)  # 只认完整持久创建意图同一的唯一助手，不能仅按标签删除。
            journal.before_remove_helper(); helper.remove(force=True)
    if before['Running'] and not now['Running']:
        require(value['停止请求已持久化'], '没有本任务停源授权')
        journal.resume(source)
    journal.recovered(source)
    if before['Running']:
        from services.commit_service import _require_target_health
        _require_target_health(server)
    if value.get('实例锁请求已持久化'):
        _clear_lock(client, source, journal)
    journal.complete_source()


def recover_pending():
    from models import db, Backup, GiteaServer
    from services.docker_service import managed_client
    root = directory()
    rows = db.session.query(Backup.id, Backup.recovery_token).filter_by(recovery_pending=True).all()
    db.session.rollback()
    for identifier, token in rows:
        if not token:
            continue  # 旧版本及旧验收现场没有新事务标识，永不自动接管。
        try:
            with lease(root, identifier, token) as path:
                task = db.session.get(Backup, identifier, populate_existing=True)
                if task is None or not task.recovery_pending or task.recovery_token != token:
                    db.session.rollback(); continue
                if not path.exists():
                    if task.started_at > datetime.utcnow() - timedelta(minutes=5):
                        db.session.rollback(); continue
                    journal = Journal.create(path, task)
                else:
                    journal = Journal.load(path, identifier, token, task.source_server_id)
                if journal.value['事务完成']:
                    task.recovery_pending = False; db.session.commit(); continue
                if journal.value['源收尾已完成'] and task.status == 'success':
                    journal.value['事务完成'] = True; journal.save('完成')
                    task.recovery_pending = False; db.session.commit(); continue
                task.status = 'failed'; task.completed_at = datetime.utcnow()
                task.error_msg = '备份执行进程已中断，正在核实原源并恢复运行状态；归档未验证，不可用于恢复'
                task.progress_stage = 'recovery_source'; task.progress_label = '正在收尾中断备份'
                task.progress_updated_at = datetime.utcnow()
                source = db.session.get(GiteaServer, task.source_server_id)
                server = SimpleNamespace(**{key: getattr(source, key) for key in (*SERVER_FIELDS, 'api_token', 'name')}) if source else None
                db.session.commit()
                if journal.value.get('原源'):
                    require(server is not None, '原源登记已不存在，不自动操作')
                    client = managed_client(server)
                    instance_lock = None
                    try:
                        from services.restore_recovery import database_lease
                        instance_lock = database_lease(journal.value['原PG']['Id'], server.pg_dbname)
                        recover_source(client, server, journal)
                    finally:
                        try:
                            client.close()
                        finally:
                            if instance_lock is not None:
                                instance_lock.close()
                journal.value['事务完成'] = True; journal.save('完成')
                task = db.session.get(Backup, identifier)
                task.status = 'failed'; task.recovery_pending = False
                task.error_msg = '中断备份已完成受控收尾，原源运行状态已核实；归档未验证，请重新发起备份'
                task.progress_stage = 'failed'; task.progress_label = '备份中断，已完成收尾'
                task.progress_percent = 100; task.progress_updated_at = datetime.utcnow()
                db.session.commit()
                from services.alert_service import on_backup_completed
                on_backup_completed(task.source_server_id, False, task.error_msg, backup_id=task.id)
        except BlockingIOError:
            db.session.rollback()  # 活跃工作线程持锁，不按超时猜测它已经退出。
        except Exception as exc:
            db.session.rollback()
            logging.error('中断备份%s收尾未确认，保留原事务且不重放备份；异常类型：%s', identifier, type(exc).__name__)


def start(app):
    key = os.getpid(), app.config['SQLALCHEMY_DATABASE_URI']
    with _thread_lock:
        if key in _threads and _threads[key].is_alive():
            return
        def loop():
            while True:
                try:
                    with app.app_context():
                        from services.upgrade_maintenance import active
                        if not active():
                            recover_pending()
                except Exception as exc:
                    logging.error('备份恢复巡检未完成：%s', type(exc).__name__)
                time.sleep(10)
        thread = threading.Thread(target=loop, daemon=True, name='backup-recovery')
        _threads[key] = thread
        thread.start()
