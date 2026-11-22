"""仅在目标端解包正文。Manager 继续负责准入、配置和事务回退。"""
import hashlib
from pathlib import PurePosixPath
import shlex
import shutil
import stat

from services.backup_manifest import check_member
from services.backup_replica import InvalidReplica
from services.restore_archive import restore_modes
from services.restore_secrets import put_file


def stage_metadata(archive, manifest, workspace):
    if manifest is None:
        raise InvalidReplica('该 ZIP 没有快速恢复所需的灾备清单')
    sql = [entry.filename for entry in archive.infolist()
           if not entry.is_dir() and PurePosixPath(entry.filename).name == 'gitea-db.sql']
    if len(sql) != 1:
        raise InvalidReplica('备份必须包含唯一数据库 SQL')
    prefix = sql[0][:-len('gitea-db.sql')]
    metadata = {'manager-actions.json', 'manager-startup-files.json', prefix + 'app.ini',
                prefix + 'manager-secrets.json'}
    for entry in archive.infolist():
        path = entry.filename
        if entry.is_dir() or not (path in metadata or path.startswith(prefix + 'data/conf/manager-')):
            continue
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with archive.open(entry) as stream, target.open('xb') as output:
            for body in iter(lambda: stream.read(1024 * 1024), b''):
                output.write(body)
                digest.update(body)
        check_member(entry, digest.hexdigest(), manifest)
        restore_modes(workspace, [entry])
    return workspace / prefix, sql[0]


def expected_hooks(manifest, root_prefix):
    generated = {'pre-receive', 'update', 'post-receive', 'proc-receive'}
    hooks = {}
    for name, item in manifest['文件'].items():
        if not name.startswith(root_prefix) or name.endswith('/'):
            continue
        relative = name[len(root_prefix):]
        _, delimiter, basename = relative.partition('.git/hooks/')
        if not delimiter or basename in generated:
            continue
        path = PurePosixPath(basename)
        if len(path.parts) == 2 and path.parts[0] in {part + '.d' for part in generated} and path.name == 'gitea':
            continue
        mode = item['权限'] & 0o777 if item['系统'] == 3 else 0
        hooks[relative] = (item['摘要'], mode or 0o644)
    return hooks


def unpack(helper, candidate, backup, stage, report):
    from services.artifact_io import artifact_tool, run_artifact_tool
    try:
        with artifact_tool(helper) as remote:
            run_artifact_tool(helper, [remote + '/tool', 'cache-prepare', stage, candidate[2],
                                       backup.archive_sha256], report)
    except Exception as exc:
        raise InvalidReplica('备机缓存缺失、损坏或无法在目标盘解压') from exc


def put_configuration(helper, configuration, stage, environment):
    names = {'app.ini', 'manager-secret-key', 'manager-secret-uri', 'manager-empty-secret'}
    for key, value in environment.items():
        if key.upper().startswith('GITEA__') and value.startswith('/data/gitea/conf/manager-restored-env-'):
            names.add(PurePosixPath(value).name)
    for name in sorted(names):
        path = configuration.directory / name
        put_file(helper, stage + '/new-gitea/conf/' + name, path.read_bytes(), stat.S_IMODE(path.stat().st_mode))


def stage_sql(helper, target, database, stage, pg_temp, expected, execute):
    source = stage + '/incoming.sql'
    destination = pg_temp + '/incoming.sql'
    if target.connection_mode == 'local':
        from services.restore_recovery import copy_archive
        copy_archive(helper, source, database, pg_temp)
    else:
        from services.ssh_service import SSHService
        read = shlex.join(['docker', 'exec', helper.id, 'cat', source])
        write = shlex.join(['docker', 'exec', '-i', '-u', 'postgres', database.id,
                            'sh', '-ec', 'umask 077; cat > "$1"', 'sh', destination])
        command = '\n'.join((
            'set -eu',
            'scratch=$(mktemp -d)',
            "trap 'rm -rf -- \"$scratch\"' EXIT",
            'mkfifo "$scratch/sql"',
            read + ' > "$scratch/sql" &',
            'sender=$!',
            'if ' + write + ' < "$scratch/sql"; then wait "$sender";',
            'else kill "$sender" 2>/dev/null || :; wait "$sender" 2>/dev/null || :; exit 1; fi',
        ))
        code, _, _ = SSHService(target.host, target.ssh_port, target.ssh_user).exec(
            shlex.join(['sh', '-c', command]), timeout=1800)
        if code:
            raise RuntimeError('在备机本地准备数据库 SQL 失败')
    value = execute(database, ['sha256sum', destination], '校验目标暂存数据库 SQL', user='postgres')
    if value.split()[0] != expected:
        raise RuntimeError('目标暂存数据库 SQL 摘要不符')
