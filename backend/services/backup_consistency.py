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
    return command_output(code, raw)


def command_output(code, raw):
    if code != 0:
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


def repository_snapshot(helper, database, server, progress=None, stage='snapshot'):
    if progress:
        progress.show(stage, '读取数据库仓库清单')
    rows = inventory(database, server)
    root = repository_root(helper)
    find = ['find', root, '-type', 'd', '-name', '*.git', '-prune']
    paths = (progress.execute(helper, find, stage=stage, action='扫描 Git 仓库')
             if progress else execute(helper, find)).splitlines()
    repositories = {}
    total = len(paths)
    for path in sorted(paths):
        relative = str(PurePosixPath(path).relative_to(root))
        if '\n' in relative or '..' in PurePosixPath(relative).parts:
            raise RuntimeError('仓库路径无法安全记录')
        command = ['git', '--git-dir', path]

        def read(container, args, action, user=None):
            if progress:
                return progress.execute(container, args, stage=stage, action=action, user=user,
                                        current=len(repositories), total=total, unit='repos', item=relative)
            return execute(container, args, user=user)

        read(helper, command + ['fsck', '--full', '--no-reflogs'], '检查 Git 对象完整性', user='git')
        refs = read(helper, command + ['for-each-ref', '--format=%(refname) %(objectname)'],
                    '读取分支和标签引用', user='git').splitlines()
        head = read(helper, ['cat', path + '/HEAD'], '读取仓库 HEAD')
        commits = (read(helper, command + ['log', '-100', '--format=%H', 'HEAD'],
                        '读取最近提交', user='git').splitlines() if refs else [])
        repositories[relative] = {'引用': refs, 'HEAD': head, '提交': commits}
    if progress:
        progress.show(stage, '核对数据库与磁盘仓库', current=total, total=total, unit='repos', force=True)
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
        if len(names) != 1:
            raise RuntimeError('备份校验快照结构不受支持')
        from services.backup_metadata import read_zip_json
        value = read_zip_json(archive, names[0])
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


def consistent_backup(server, backup, journal=None):
    if journal is None:
        from services.backup_recovery import execution
        with execution(backup) as current:
            return consistent_backup(server, backup, journal=current)
    from config import BACKUP_DIR
    from services.docker_service import managed_client
    from services.backup_activity import BackupActivity
    from services.operation_progress import finish_current_step
    from services.backup_outcome import RecoveryProgress, record_failure
    from services import backup_staging
    from models import db
    from datetime import datetime
    progress = BackupActivity(backup, server)
    progress.show('prepare', '连接源实例并检查备份条件')
    client = managed_client(server)
    helper = None
    was_running = False
    recovery_attempted = False
    marker = 'manager-backup-' + uuid.uuid4().hex
    try:
        gitea = client.containers.get(server.gitea_container)
        database = client.containers.get(server.pg_container)
        journal.attach(server, gitea, database, marker, daemon=client.info().get('ID'))
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
        progress.show('prepare', '准备备份辅助容器和实例锁', force=True)
        export = backup_staging.create_export(client, gitea, server, backup, journal)
        journal.before_helper(gitea, network, export.volume)
        helper = client.containers.create(gitea.attrs['Image'], name=marker, entrypoint=['sleep'], command=['infinity'],
            environment=gitea.attrs['Config'].get('Env'), volumes_from=[gitea.id], network=network,
            mounts=[backup_staging.spool_mount(export.volume)],
            labels={'gitea.manager.operation': marker})
        journal.helper(helper)
        helper.start()
        export_root = backup_staging.prepare_export(helper, database, server, marker)
        journal.before_lock()
        journal.lock(helper, client, gitea)
        progress.show('stop_source', '检查源容器与停写条件')
        if was_running:
            journal.before_stop(gitea)
            with progress.container_state(gitea, 'stop_source', '等待源 Gitea 容器停止'):
                gitea.stop(timeout=60)
        journal.stopped(gitea)
        if was_running:
            backup.source_stopped_at = datetime.utcnow()
            db.session.commit()
        progress.show('stop_source', '确认数据库已停写', force=True)
        require_quiet(database, server)
        progress.source = 'paused'
        progress.show('stop_source', '采集启动配置', force=True)
        from services.backup_dependencies import capture_startup_files, append_startup_files
        from services.backup_actions import append_artifacts
        source_config = read_file(gitea, '/data/gitea/conf/app.ini')[0]
        requirements = identity(gitea, database, server)
        startup_files = capture_startup_files(gitea, helper, source_config)
        snapshot = repository_snapshot(helper, database, server, progress)
        tables = table_snapshot(database, server, progress)
        path = export_root + '/gitea.zip'
        progress.execute(helper, ['/usr/local/bin/gitea', 'dump', '-c', '/data/gitea/conf/app.ini',
                                  '--tempdir', export_root, '-f', path], stage='dump',
                         action='生成备份归档', user='git', archive=path)
        progress.show('snapshot_verify', '确认导出期间数据库保持停写')
        require_quiet(database, server)
        if repository_snapshot(helper, database, server, progress, stage='snapshot_verify') != snapshot:
            raise RuntimeError('停写期间仓库状态发生变化，拒绝发布备份')
        backup_staging.freeze_metadata(helper, gitea, database, server, export,
            snapshot, tables, requirements, startup_files, source_config, journal, progress=progress)
        require_quiet(database, server)
        from services.backup_recovery import recover_source
        progress.source = 'recovering' if was_running else 'stopped'
        recovery_attempted = True
        try:
            with RecoveryProgress(backup).run('核对并恢复源站') as recovery:
                recover_source(client, server, journal, progress=progress, recovery=recovery)
        except Exception as error:
            record_failure(backup, error, stage='resume_source')
            raise
        if was_running:
            backup.source_restored_at = backup.source_restored_at or datetime.utcnow()
            db.session.commit()
        progress.source = 'available' if was_running else 'stopped'
        finish_current_step(backup, stage='resume_source')
        local = os.path.join(BACKUP_DIR, backup.filename)
        with backup_staging.export_lease(export.id):
            backup_staging.receive_export(client, export, local, backup, journal, progress, server=server)
        backup.file_path = local
        final_size = os.path.getsize(local)
        db.session.commit()
    except Exception as error:
        try:
            db.session.rollback()
            record_failure(backup, error)
        except Exception:
            db.session.rollback()
            logging.warning('备份首错暂时无法写入数据库', exc_info=True)
        raise
    finally:
        original_error = sys.exc_info()[1]
        cleanup_error = None
        try:
            if (journal.value.get('原源') and not journal.value.get('源收尾已完成')
                    and not recovery_attempted):
                from services.backup_recovery import recover_source
                if progress.source == 'paused' and was_running:
                    progress.source = 'recovering'
                progress.show('resume_source', '核对源容器并清理备份现场',
                              previous_failed=original_error is not None)
                with RecoveryProgress(backup).run('核对并恢复源站') as recovery:
                    try:
                        recover_source(client, server, journal, progress=progress, recovery=recovery)
                    except Exception as error:
                        from services.backup_outcome import failure_detail
                        finish_current_step(backup, failure_detail(error), stage='resume_source', status='failed')
                        raise
                progress.source = 'available' if journal.value['原源']['State']['Running'] else 'stopped'
                progress.show('resume_source', '源站收尾已完成', force=True)
                finish_current_step(backup, stage='resume_source')
            elif not journal.value.get('原源'):
                journal.complete_source()
            if journal.value.get('导出读取助手') and not journal.value.get('传输收尾完成'):
                with RecoveryProgress(backup).run('清理固定副本读取助手'):
                    backup_staging.cleanup_reader(client, journal)
        except BaseException as exc:
            cleanup_error = exc
            try:
                logging.error('备份源启动或解锁失败，保留现场锁；异常类型：%s', type(exc).__name__)
            except BaseException:
                pass
        finally:
            try:
                client.close()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error:
            if original_error is not None:
                try:
                    original_error.add_note('备份清理未完整完成，请检查源服务、实例锁及辅助容器；异常类型：' + type(cleanup_error).__name__)
                except BaseException:
                    pass
            else:
                raise cleanup_error
    backup.file_size = final_size
    db.session.commit()
