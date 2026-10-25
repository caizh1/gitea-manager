"""在停写点记录恢复准入与独立导入所需元数据；清单不是数字签名。"""
import configparser
import hashlib
import json
from pathlib import PurePosixPath
import shlex
import zipfile

from services.restore_secrets import read_file

MANIFEST = 'manager-manifest.json'


def storage_profile(raw):
    config = configparser.ConfigParser(interpolation=None)
    config.read_string('[DEFAULT]\n' + raw.decode())
    from services.backup_actions import actions_path
    artifacts = actions_path(raw)
    result = {}
    for section in config.sections():
        name = section.lower()
        if name == 'storage' or name.startswith('storage.') or name in ('attachment', 'lfs', 'packages', 'avatar', 'repo-avatar', 'actions', 'actions.artifacts'):
            values = dict(config.items(section))
            if values.get('storage_type', 'local').lower() != 'local':
                raise RuntimeError('灾备暂不支持外部对象存储：' + section)
            for key in ('path', 'avatar_upload_path', 'repository_avatar_upload_path'):
                if key in values:
                    path = PurePosixPath(values[key])
                    if '..' in path.parts or (path.is_absolute() and not path.is_relative_to('/data/gitea') and not (name in ('actions.artifacts', 'storage.actions_artifacts') and str(path) == artifacts) and not (name == 'lfs' and str(path) == '/data/git/lfs')):
                        raise RuntimeError('存储路径不在持久数据目录：' + section + '.' + key)
            canonical = {'storage': '/data/gitea', 'attachment': '/data/gitea/attachments', 'storage.attachments': '/data/gitea/attachments', 'packages': '/data/gitea/packages', 'storage.packages': '/data/gitea/packages', 'lfs': '/data/gitea/lfs', 'storage.lfs': '/data/gitea/lfs'}
            if name in canonical and values.get('path'):
                actual_path = str(PurePosixPath('/data/gitea') / values['path'])
                allowed = {canonical[name]}
                if name == 'lfs':
                    allowed.add('/data/git/lfs')
                if actual_path not in allowed:
                    raise RuntimeError('该存储路径无法与 dump 的固定归档目录对应：' + section)
            result[name] = {key: value for key, value in values.items() if key in ('path', 'storage_type')}
        if name == 'picture':
            for key in ('AVATAR_STORAGE_TYPE', 'REPOSITORY_AVATAR_STORAGE_TYPE', 'AVATAR_UPLOAD_PATH', 'REPOSITORY_AVATAR_UPLOAD_PATH'):
                value = config.get(section, key, fallback='')
                expected = 'local' if key.endswith('STORAGE_TYPE') else '/data/gitea/' + ('repo-avatars' if key.startswith('REPOSITORY') else 'avatars')
                if value and value != expected:
                    raise RuntimeError('头像存储不在支持的本地标准目录：' + key)
            result[name] = {key: config.get(section, key, fallback='') for key in ('AVATAR_STORAGE_TYPE', 'REPOSITORY_AVATAR_STORAGE_TYPE', 'AVATAR_UPLOAD_PATH', 'REPOSITORY_AVATAR_UPLOAD_PATH')}
        if name == 'server' and config.get(section, 'LFS_CONTENT_PATH', fallback=''):
            raise RuntimeError('请先将旧 LFS_CONTENT_PATH 配置迁移至 lfs.PATH，再执行灾备')
        if name in ('session', 'cache', 'queue'):
            key = 'provider' if name != 'queue' else 'type'
            value = config.get(section, key, fallback='').lower()
            if value and value not in ('memory', 'file', 'level', 'channel'):
                raise RuntimeError('灾备暂不支持外部会话、缓存或队列：' + section)
    return result


def identity(gitea, database, server):
    from services.backup_consistency import database_sql
    raw = read_file(gitea, '/data/gitea/conf/app.ini')[0]
    config = configparser.ConfigParser(interpolation=None)
    config.read_string('[DEFAULT]\n' + raw.decode())
    behavior = {}
    for section in config.sections():
        name = section.lower()
        if name.startswith('governance'):
            behavior[name] = {key: value for key, value in config.items(section) if key not in config.defaults() and key != 'root'}
    behavior['lfs_enabled'] = config.getboolean('server', 'LFS_START_SERVER', fallback=False)
    return {'镜像': gitea.attrs['Image'],
            '数据库主版本': int(database_sql(database, server, 'SHOW server_version_num')) // 10000,
            '存储': storage_profile(raw), '功能配置': behavior}


def table_snapshot(database, server):
    """在启动恢复服务之前逐表流式哈希，包含增强业务、权限、审批和审计数据。"""
    from services.backup_consistency import database_sql, execute
    names = json.loads(database_sql(database, server,
        "SELECT coalesce(json_agg(tablename ORDER BY tablename),'[]') FROM pg_tables WHERE schemaname='public'"))
    commands = []
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        query = f'COPY (SELECT to_jsonb(t)::text AS row FROM public.{quoted} t ORDER BY to_jsonb(t)::text COLLATE "C") TO STDOUT'
        command = ['psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', server.pg_user, '-d', server.pg_dbname, '-c', query]
        commands.append(shlex.join(command) + ' | sha256sum')
    output = execute(database, ['bash', '-e', '-o', 'pipefail', '-c', '\n'.join(commands)], user='postgres')
    hashes = [line.split()[0] for line in output.splitlines()]
    if len(hashes) != len(names):
        raise RuntimeError('数据表校验结果数量异常')
    return dict(zip(names, hashes))


def append_manifest(path, requirements, tables, server):
    with zipfile.ZipFile(path, 'a') as archive:
        files = {}
        for entry in archive.infolist():
            with archive.open(entry) as stream:
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            files[entry.filename] = {'摘要': digest.hexdigest(), '权限': entry.external_attr >> 16, '系统': entry.create_system}
        payload = json.dumps({'版本': 2, '准入': requirements, '数据表': tables,
            '文件': files, '来源名称': server.name, 'API令牌': server.api_token,
            '边界': '同镜像、同 PostgreSQL 主版本、本地持久存储；不包含镜像及 Manager 的其他业务状态'}, ensure_ascii=False).encode()
        if len(payload) > 64 * 1024 * 1024:
            raise RuntimeError('灾备清单超出当前恢复器的支持上限，拒绝发布成功备份')
        archive.writestr(MANIFEST, payload)


def load_manifest(path):
    from services.restore_archive import validate_members
    with zipfile.ZipFile(path) as archive:
        validate_members(archive.infolist())
        if MANIFEST not in archive.namelist():
            return None
        if archive.getinfo(MANIFEST).file_size > 64 * 1024 * 1024:
            raise RuntimeError('灾备清单过大')
        value = json.loads(archive.read(MANIFEST))
        if value.get('版本') != 2 or not all(isinstance(value.get(key), dict) for key in ('准入', '数据表', '文件')):
            raise RuntimeError('灾备清单结构不受支持')
        files = set(archive.namelist()) - {MANIFEST}
        if files != set(value['文件']):
            raise RuntimeError('灾备清单文件集合不一致')
        for name, expected in value['文件'].items():
            with archive.open(name) as stream:
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            entry = archive.getinfo(name)
            actual = {'摘要': digest.hexdigest(), '权限': entry.external_attr >> 16, '系统': entry.create_system}
            if actual != expected:
                raise RuntimeError('灾备文件哈希或权限不一致：' + name)
        return value


def admit(manifest, actual):
    if manifest is not None and manifest['准入'] != actual:
        differences = [key for key in ('镜像', '数据库主版本', '存储', '功能配置') if manifest['准入'].get(key) != actual.get(key)]
        raise RuntimeError('恢复准入不匹配，尚未修改目标数据：' + '、'.join(differences))
