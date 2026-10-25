"""停写期间导出数据库、仓库和校验基准；本地与 SSH 共用同一实现。"""
import hashlib
import configparser
from contextlib import closing
import logging
import sys
import json
import os
from pathlib import PurePosixPath
import tarfile
import tempfile
import uuid
import zipfile

from services.restore_secrets import read_file, put_file, security_values, uri_path

SNAPSHOT_FILE = 'manager-snapshot.json'
INSTANCE_LOCK = '/data/.manager-restore-lock'


def execute(container, args, user=None):
    code, raw = container.exec_run(args, user=user)
    if code:
        from services.diagnostics import redact
        raise RuntimeError('隔离数据操作失败：' + redact(raw.decode(errors='replace')[-2000:]))
    return raw.decode('utf-8').strip()


def database_sql(database, server, statement):
    return execute(database, ['psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', server.pg_user,
                             '-d', server.pg_dbname, '-Atc', statement], user='postgres')


def repository_root(container):
    config = configparser.ConfigParser(interpolation=None)
    config.read_string('[DEFAULT]\n' + read_file(container, '/data/gitea/conf/app.ini')[0].decode())
    data = config.get('server', 'APP_DATA_PATH', fallback='/app/gitea/data').rstrip('/')
    return config.get('repository', 'ROOT', fallback=data + '/gitea-repositories').rstrip('/')


def check_layout(gitea, database, server, restoring=False):
    """第一批只处理独占数据库及标准持久目录，拒绝隐式覆盖其他挂载。"""
    host = gitea.attrs['HostConfig']
    dbhost = database.attrs['HostConfig']
    if host.get('AutoRemove') or host.get('NetworkMode') in ('host', 'none') or str(host.get('NetworkMode', '')).startswith('container:'):
        raise RuntimeError('不支持自动删除或共享主机网络的 Gitea，尚未修改数据')
    if dbhost.get('PortBindings') or dbhost.get('NetworkMode') == 'host':
        raise RuntimeError('第一批仅支持未发布数据库端口的专用 PostgreSQL 容器')
    mounts = gitea.attrs['Mounts']
    if not any(m['Destination'] == '/data' and m['RW'] for m in mounts):
        raise RuntimeError('Gitea 必须使用可写持久 /data 卷')
    if any(m['Destination'].startswith('/data/') for m in mounts):
        raise RuntimeError('暂不支持 /data 内的嵌套挂载，尚未修改数据')
    if not set(gitea.attrs['NetworkSettings']['Networks']) & set(database.attrs['NetworkSettings']['Networks']):
        raise RuntimeError('Gitea 与 PostgreSQL 没有共同网络')
    config = configparser.ConfigParser(interpolation=None)
    config.read_string('[DEFAULT]\n' + read_file(gitea, '/data/gitea/conf/app.ini')[0].decode())
    hosts = {database.name, database.id, database.id[:12]}
    for network in database.attrs['NetworkSettings']['Networks'].values():
        hosts.update(network.get('Aliases') or [])
        hosts.add(network.get('IPAddress'))
    host, _, port = config.get('database', 'HOST', fallback='').strip().rpartition(':')
    if (config.get('database', 'DB_TYPE', fallback='') != 'postgres' or port != '5432' or host not in hosts
            or config.get('database', 'NAME', fallback='') != server.pg_dbname
            or config.get('database', 'USER', fallback='') != server.pg_user):
        raise RuntimeError('Manager 登记的 PostgreSQL 与 Gitea 实际配置不一致，尚未修改数据')
    if config.get('server', 'APP_DATA_PATH', fallback='/app/gitea/data').rstrip('/') != '/data/gitea':
        raise RuntimeError('应用数据必须持久化在 /data/gitea，拒绝遗漏容器可写层中的数据')
    environment = dict(item.split('=', 1) for item in gitea.attrs['Config'].get('Env', []) if '=' in item)
    if environment.get('GITEA_CUSTOM', '/data/gitea').rstrip('/') != '/data/gitea':
        raise RuntimeError('灾备暂只支持 /data/gitea 内的标准自定义目录，拒绝遗漏独立 custom 目录')
    if config.get('server', 'SSH_ROOT_PATH', fallback='/data/git/.ssh').rstrip('/') != '/data/git/.ssh':
        raise RuntimeError('灾备暂只支持标准 SSH 授权目录 /data/git/.ssh')
    if config.get('server', 'SSH_AUTHORIZED_PRINCIPALS_ALLOW', fallback='off') not in ('', 'off') or config.get('server', 'SSH_TRUSTED_USER_CA_KEYS', fallback=''):
        raise RuntimeError('灾备暂不支持 SSH 证书授权，拒绝遗漏授权主体或外部 CA 配置')
    if config.get('database', 'SCHEMA', fallback='public') not in ('', 'public'):
        raise RuntimeError('灾备暂只支持 PostgreSQL public 模式')
    supported = {'/data/git/repositories'} if restoring else {'/data/git/repositories', '/data/gitea/gitea-repositories'}
    if repository_root(gitea) not in supported:
        raise RuntimeError('仓库根目录不在本批支持范围，恢复目标须使用 /data/git/repositories')


def require_quiet(database, server):
    count = database_sql(database, server, "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND backend_type='client backend'")
    if count != '0':
        raise RuntimeError('停止 Gitea 后数据库仍有其他客户端，拒绝生成或覆盖不一致数据')


def inventory(database, server):
    query = """SELECT coalesce(json_agg(t ORDER BY t.id),'[]') FROM (
      SELECT id, owner_name, name, default_branch, is_empty,
      coalesce(nullif(to_jsonb(r)->>'governance_storage_owner',''),owner_name) AS storage_owner,
      coalesce(nullif(to_jsonb(r)->>'governance_storage_name',''),name) AS storage_name
      FROM repository r) t"""
    return json.loads(database_sql(database, server, query))


def repository_snapshot(helper, database, server):
    rows = inventory(database, server)
    root = repository_root(helper)
    paths = execute(helper, ['find', root, '-type', 'd', '-name', '*.git', '-prune']).splitlines()
    repositories = {}
    for path in sorted(paths):
        relative = str(PurePosixPath(path).relative_to(root))
        if '\n' in relative or '..' in PurePosixPath(relative).parts:
            raise RuntimeError('仓库路径无法安全记录')
        command = ['git', '--git-dir', path]
        execute(helper, command + ['fsck', '--full', '--no-reflogs'], user='git')
        refs = execute(helper, command + ['for-each-ref', '--format=%(refname) %(objectname)'], user='git').splitlines()
        head = execute(helper, ['cat', path + '/HEAD'])
        commits = execute(helper, command + ['log', '-100', '--format=%H', 'HEAD'], user='git').splitlines() if refs else []
        repositories[relative] = {'引用': refs, 'HEAD': head, '提交': commits}
    for row in rows:
        path = (row['storage_owner'] + '/' + row['storage_name'] + '.git').lower()
        if path not in repositories:
            raise RuntimeError('数据库仓库缺少 Git 正文：' + row['owner_name'] + '/' + row['name'])
    return {'版本': 1, '说明': '源实例停写期间采集，不能用恢复后的在线源状态替换', '数据库仓库': rows, 'Git仓库': repositories}


def load_snapshot(path):
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if PurePosixPath(name).name == SNAPSHOT_FILE]
        if not names:
            return None
        if len(names) != 1 or archive.getinfo(names[0]).file_size > 64 * 1024 * 1024:
            raise RuntimeError('备份校验快照结构不受支持')
        value = json.loads(archive.read(names[0]))
        if value.get('版本') != 1 or not isinstance(value.get('数据库仓库'), list) or not isinstance(value.get('Git仓库'), dict):
            raise RuntimeError('备份校验快照版本或结构不受支持')
        return value


def commit_rows(snapshot):
    for row in snapshot['数据库仓库']:
        path = (row['storage_owner'] + '/' + row['storage_name'] + '.git').lower()
        commits = snapshot['Git仓库'][path]['提交']
        yield row['owner_name'] + '/' + row['name'], commits


def verify_snapshot(expected, actual, saved_rows):
    if expected != actual:
        raise RuntimeError('停写快照与恢复后的数据库仓库、Git 引用或提交不一致')
    saved = {row.repo_name: row for row in saved_rows}
    pairs = dict(commit_rows(expected))
    if set(saved) != set(pairs):
        raise RuntimeError('Manager 校验记录与备份快照仓库集合不一致')
    for name, commits in pairs.items():
        digest = hashlib.sha256(','.join(sorted(commits)).encode()).hexdigest()
        if saved[name].commit_ids_hash != digest or saved[name].commit_count != len(commits):
            raise RuntimeError('Manager 校验记录与备份快照提交不一致：' + name)


def consistent_backup(server, backup):
    from config import BACKUP_DIR
    from services.docker_service import managed_client
    from services.operation_progress import update_backup_progress
    from services.restore_secrets import capture_backup_secret
    client = managed_client(server)
    helper = None
    locked = stopped = was_running = False
    marker = 'manager-backup-' + uuid.uuid4().hex
    try:
        gitea = client.containers.get(server.gitea_container)
        database = client.containers.get(server.pg_container)
        check_layout(gitea, database, server)
        from services.backup_manifest import identity, table_snapshot, append_manifest
        requirements = identity(gitea, database, server)
        config = configparser.ConfigParser(interpolation=None)
        config.read_string('[DEFAULT]\n' + read_file(gitea, '/data/gitea/conf/app.ini')[0].decode())
        for enabled, table, label in ((config.getboolean('server', 'LFS_START_SERVER', fallback=False), 'lfs_meta_object', 'LFS'),
                (config.getboolean('packages', 'ENABLED', fallback=True), 'package_blob', '软件包')):
            if not enabled and database_sql(database, server, 'SELECT EXISTS(SELECT 1 FROM ' + table + ')') == 't':
                raise RuntimeError(label + ' 已禁用但仍有对象记录；dump 会跳过正文，拒绝生成不完整备份')
        was_running = gitea.status == 'running'
        network = sorted(set(gitea.attrs['NetworkSettings']['Networks']) & set(database.attrs['NetworkSettings']['Networks']))[0]
        helper = client.containers.run(gitea.attrs['Image'], entrypoint=['sleep'], command=['infinity'],
            environment=gitea.attrs['Config'].get('Env'), volumes_from=[gitea.id], network=network,
            detach=True, labels={'gitea.manager.operation': marker})
        execute(helper, ['mkdir', INSTANCE_LOCK]); locked = True
        update_backup_progress(backup, 'stop_source', '正在停止源 Gitea，建立一致性备份点', 5, server.name)
        if was_running:
            gitea.stop(timeout=60)
        stopped = True
        require_quiet(database, server)
        from services.backup_dependencies import capture_startup_files, append_startup_files
        from services.backup_actions import append_artifacts
        source_config = read_file(gitea, '/data/gitea/conf/app.ini')[0]
        startup_files = capture_startup_files(gitea, helper, source_config)
        update_backup_progress(backup, 'snapshot', '正在记录停写状态下的仓库与引用', 10, server.name)
        snapshot = repository_snapshot(helper, database, server)
        tables = table_snapshot(database, server)
        path = '/tmp/' + marker + '.zip'
        update_backup_progress(backup, 'dump', '正在导出同一停写点的数据库和文件', 20, server.name)
        execute(helper, ['/usr/local/bin/gitea', 'dump', '-c', '/data/gitea/conf/app.ini', '--tempdir', '/tmp', '-f', path], user='git')
        require_quiet(database, server)
        if repository_snapshot(helper, database, server) != snapshot:
            raise RuntimeError('停写期间仓库状态发生变化，拒绝发布备份')
        os.makedirs(BACKUP_DIR, exist_ok=True)
        local = os.path.join(BACKUP_DIR, backup.filename)
        update_backup_progress(backup, 'transfer', '正在保存一致性备份', 50, backup.filename)
        with tempfile.TemporaryFile() as stream:
            with closing(helper.get_archive(path)[0]) as blocks:
                for block in blocks: stream.write(block)
            stream.seek(0)
            with tarfile.open(fileobj=stream) as archive:
                members = archive.getmembers()
                if len(members) != 1 or not members[0].isfile():
                    raise RuntimeError('Docker 备份归档结构异常')
                with archive.extractfile(members[0]) as source, open(local + '.partial', 'wb') as output:
                    os.chmod(local + '.partial', 0o600)
                    import shutil
                    shutil.copyfileobj(source, output)
        os.replace(local + '.partial', local)
        backup.file_path = local
        # ORM 读取过期的服务器配置会自动刷写备份记录；先提交，避免在耗时 I/O 期间占用写锁。
        from models import db
        db.session.commit()
        capture_backup_secret(server, backup)
        # 密钥快照更新 file_size；后续制品读取和清单哈希同样不能携带未提交写入。
        db.session.commit()
        append_startup_files(local, startup_files)
        append_artifacts(local, helper, database, server, source_config)
        from services.restore_archive import validate_ssh_archive
        blocks, size = [], 0
        with closing(gitea.get_archive('/data/ssh')[0]) as stream:
            for block in stream:
                size += len(block)
                if size > 2 * 1024 * 1024:
                    raise RuntimeError('SSH 主机身份归档超出支持上限')
                blocks.append(block)
        host_keys = b''.join(blocks)
        validate_ssh_archive(host_keys)
        with zipfile.ZipFile(local, 'a') as archive:
            archive.writestr(SNAPSHOT_FILE, json.dumps(snapshot, ensure_ascii=False))
            archive.writestr('manager-ssh-hostkeys.tar', host_keys)
        append_manifest(local, requirements, tables, server)
        backup.file_size = os.path.getsize(local)
    finally:
        original_error = sys.exc_info()[1]
        cleanup_error = None
        try:
            if stopped and was_running:
                gitea.start()
            if locked:
                execute(helper, ['rmdir', INSTANCE_LOCK])
        except Exception as exc:
            cleanup_error = exc
            logging.error('备份源启动或解锁失败，保留现场锁；异常类型：%s', type(exc).__name__)
        finally:
            try:
                if helper is not None: helper.remove(force=True)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
                logging.error('备份辅助容器回收失败；异常类型：%s', type(exc).__name__)
            finally:
                try:
                    client.close()
                except Exception as exc:
                    cleanup_error = cleanup_error or exc
        if cleanup_error:
            if original_error is not None:
                original_error.add_note('备份清理未完整完成，请检查源服务、实例锁及辅助容器；异常类型：' + type(cleanup_error).__name__)
            else:
                raise cleanup_error
