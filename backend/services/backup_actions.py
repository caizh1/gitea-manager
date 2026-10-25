"""补齐独立 Actions 制品目录，并与数据库、主数据目录一起切换和回退。"""
import configparser
from contextlib import closing
import hashlib
import json
from pathlib import PurePosixPath
import re
import shlex
import shutil
import stat
import tarfile
import tempfile
import zipfile

ARCHIVE_ROOT = 'manager-actions-artifacts'


def actions_path(raw):
    config = configparser.ConfigParser(interpolation=None)
    config.read_string('[DEFAULT]\n' + raw.decode())
    sections = {section.lower(): dict(config.items(section)) for section in config.sections()}
    # 有名称的自定义后端会改变继承语义，明确拒绝而不猜测实际路径。
    if 'storage.local' in sections:
        raise RuntimeError('暂不支持 storage.local 命名后端，请使用明确的本地存储配置')
    legacy = sections.get('actions.artifacts', {})
    named = sections.get('storage.actions_artifacts', {})
    default = sections.get('storage', {})
    for value in (legacy, named, default):
        if value.get('storage_type', '') not in ('', 'local'):
            raise RuntimeError('灾备暂不支持外部 Actions 制品存储')
    if legacy.get('storage_type') == 'local':
        target, global_default = legacy, False
    elif named.get('storage_type') or named.get('path'):
        target, global_default = named, False
    else:
        target, global_default = default, True
    base = target.get('path', '')
    path = str(PurePosixPath(base) / 'actions_artifacts') if base and global_default else base or '/data/gitea/actions_artifacts'
    path = str(PurePosixPath('/data/gitea') / path)
    if target is not legacy and legacy.get('path'):
        override = legacy['path']
        if not override.startswith('/') and base and not base.startswith('/'):
            raise RuntimeError('Actions 相对路径继承存在歧义，请使用绝对路径')
        path = str(PurePosixPath(base or '/data/gitea') / override)
    if '..' in PurePosixPath(path).parts:
        raise RuntimeError('Actions 存储路径不能包含上级目录')
    if path.startswith('/data/gitea/'):
        return path
    return validate_external(path)


def validate_external(path):
    if (not isinstance(path, str) or not re.fullmatch(r'/data/[A-Za-z0-9][A-Za-z0-9_.-]*', path)
            or path in ('/data/git', '/data/gitea', '/data/ssh') or path.startswith('/data/manager-')):
        raise RuntimeError('外置 Actions 制品目录必须是 /data 下独立普通目录')
    return path


def external_path(raw):
    path = actions_path(raw)
    return path if not path.startswith('/data/gitea/') else None


def append_artifacts(path, source, database, server, raw):
    from docker.errors import NotFound
    from services.backup_consistency import database_sql, execute
    directory = actions_path(raw)
    records = json.loads(database_sql(database, server,
        "SELECT coalesce(json_agg(storage_path),'[]') FROM action_artifact WHERE status=2"))
    inventory = {}
    with zipfile.ZipFile(path, 'a') as archive:
        sql = next(name for name in archive.namelist() if name.endswith('gitea-db.sql'))
        inside = directory.startswith('/data/gitea/')
        prefix = str(PurePosixPath(sql).parent / 'data' / PurePosixPath(directory).relative_to('/data/gitea')) if inside else ARCHIVE_ROOT
        try:
            blocks, _ = source.get_archive(directory)
        except NotFound:
            if records:
                raise RuntimeError('Actions 制品表存在有效记录，但正文目录不存在') from None
            if not inside:
                archive.writestr(prefix + '/', '')
        else:
            with tempfile.TemporaryFile() as stream:
                # Docker 归档尚未消费时，同一容器的 exec 创建会被阻塞。
                # 先结束传输；写入失败时也关闭响应，才能执行后续清理命令。
                with closing(blocks):
                    for block in blocks:
                        stream.write(block)
                if execute(source, ['readlink', '-f', directory]) != directory:
                    raise RuntimeError('Actions 制品目录不能通过符号链接指向其他目录')
                stream.seek(0)
                with tarfile.open(fileobj=stream) as contents:
                    seen = set()
                    for member in contents:
                        item = PurePosixPath(member.name)
                        if (item.is_absolute() or '..' in item.parts or not item.parts or item.parts[0] != PurePosixPath(directory).name
                                or not (member.isfile() or member.isdir()) or str(item) in seen):
                            raise RuntimeError('Actions 制品目录包含重复或非普通条目')
                        seen.add(str(item))
                        relative = str(PurePosixPath(*item.parts[1:]))
                        name = prefix + ('/' + relative if relative != '.' else '') + ('/' if member.isdir() else '')
                        if member.isfile():
                            with contents.extractfile(member) as body:
                                digest = hashlib.file_digest(body, 'sha256').hexdigest()
                            inventory[relative] = digest
                        if inside:
                            if member.isfile():
                                if name not in archive.namelist():
                                    raise RuntimeError('dump 遗漏 Actions 制品正文：' + relative)
                                with archive.open(name) as body:
                                    if hashlib.file_digest(body, 'sha256').hexdigest() != digest:
                                        raise RuntimeError('Actions 制品正文与停写点不一致')
                            continue
                        info = zipfile.ZipInfo(name)
                        info.create_system = 3
                        info.external_attr = ((stat.S_IFDIR if member.isdir() else stat.S_IFREG) | (member.mode & 0o777)) << 16
                        if member.isdir():
                            archive.writestr(info, '')
                        else:
                            with contents.extractfile(member) as body, archive.open(info, 'w', force_zip64=True) as output:
                                shutil.copyfileobj(body, output)
        for record in records:
            if record not in inventory:
                raise RuntimeError('有效 Actions 制品缺少正文：' + str(record))
        archive.writestr('manager-actions.json', json.dumps({'版本': 1, '路径': directory, '文件': inventory}, ensure_ascii=False))


def switch_command(stage, path):
    path = shlex.quote(validate_external(path))
    return f'mkdir -p {path}; mv {path} {stage}/old-actions; mv {stage}/new-actions {path}; chown -R git:git {path}'


def verify_artifacts(helper, snapshot):
    from services.backup_consistency import execute
    directory = snapshot['路径']
    if not directory.startswith('/data/gitea/'):
        validate_external(directory)
    for relative, digest in snapshot['文件'].items():
        path = PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts or str(path) == '.':
            raise RuntimeError('Actions 正文快照路径不安全')
        actual = execute(helper, ['sha256sum', '--', str(PurePosixPath(directory) / path)])[:64]
        if actual != digest:
            raise RuntimeError('恢复后 Actions 正文与停写点不一致：' + relative)


def rollback_command(stage, path):
    path = shlex.quote(validate_external(path))
    return f'if test -d {stage}/old-actions; then rm -rf {path}; mv {stage}/old-actions {path}; fi'
