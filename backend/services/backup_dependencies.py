"""显式携带 dump 不会收集的启动秘密；恢复时使用包内私有副本。"""
import base64
import configparser
import json
from pathlib import PurePosixPath
import zipfile

from services.restore_secrets import read_file, put_file, uri_path

DEPENDENCIES = 'manager-startup-files.json'
SECRET_FIELDS = (('security', 'SECRET_KEY'), ('security', 'INTERNAL_TOKEN'),
                 ('server', 'LFS_JWT_SECRET'), ('oauth2', 'JWT_SECRET'))


def capture_startup_files(source, helper, raw):
    from services.backup_consistency import execute
    from services.restore_secrets import security_values
    from docker.errors import NotFound
    config = configparser.ConfigParser(interpolation=None)
    config.read_string('[DEFAULT]\n' + raw.decode())
    files = []
    for section, key in SECRET_FIELDS:
        # 使用与既有 SECRET_KEY 相同的 Go INI 引号及注释规则。
        secret, uri = security_values(('[security]\nSECRET_KEY=' + config.get(section, key, fallback='')
            + '\nSECRET_KEY_URI=' + config.get(section, key + '_URI', fallback='')).encode())
        if not uri:
            continue
        path = uri_path(uri)
        content, member = read_file(source, path)
        if not content.decode().strip():
            raise RuntimeError('启动秘密文件为空：' + section + '.' + key)
        try:
            other, _ = read_file(helper, path)
        except NotFound:
            execute(helper, ['mkdir', '-p', str(PurePosixPath(path).parent)])
            put_file(helper, path, content, member.mode, member.uid, member.gid)
        else:
            if other != content:
                raise RuntimeError('备份助手的启动秘密与源实例不一致')
        files.append({'配置节': section, '配置项': key, '内容': base64.b64encode(content).decode()})
    # 非对称 OAuth 签名私钥不能在离线启动时另造。默认文件也必须收集。
    algorithm = config.get('oauth2', 'JWT_SIGNING_ALGORITHM', fallback='RS256').strip()
    if algorithm not in ('HS256', 'HS384', 'HS512'):
        path = str(PurePosixPath('/data/gitea') / config.get('oauth2', 'JWT_SIGNING_PRIVATE_KEY_FILE', fallback='jwt/private.pem'))
        content, _ = read_file(source, path)
        files.append({'配置节': 'oauth2', '配置项': 'JWT_SIGNING_PRIVATE_KEY_FILE', '内容': base64.b64encode(content).decode()})
    return {'版本': 1, '文件': files}


def install_startup_files(archive, config, container):
    """在第一次启动前安装，URI 指向 data 内副本，正常恢复后仍然有效。"""
    if DEPENDENCIES not in archive.namelist():
        if any(config.get(section, key + '_URI', fallback='') for section, key in SECRET_FIELDS if key != 'SECRET_KEY'):
            raise RuntimeError('旧备份缺少启动依赖快照，请重新备份后导出离线包')
        return
    if archive.getinfo(DEPENDENCIES).file_size > 16 * 1024 * 1024:
        raise RuntimeError('启动依赖快照过大')
    value = json.loads(archive.read(DEPENDENCIES))
    if value.get('版本') != 1:
        raise RuntimeError('启动依赖快照版本不受支持')
    allowed = set(SECRET_FIELDS) | {('oauth2', 'JWT_SIGNING_PRIVATE_KEY_FILE')}
    seen = set()
    for entry in value['文件']:
        section, key = entry['配置节'], entry['配置项']
        if (section, key) not in allowed or (section, key) in seen:
            raise RuntimeError('启动依赖配置不受支持或重复')
        seen.add((section, key))
        content = base64.b64decode(entry['内容'], validate=True)
        if not content or len(content) > 2 * 1024 * 1024:
            raise RuntimeError('启动依赖文件为空或过大')
        if key == 'SECRET_KEY':
            continue  # 加密密钥由既有 SecretTransition 迁移。
        path = '/data/gitea/conf/manager-startup-' + section + '-' + key.lower()
        put_file(container, path, content, 0o600, 1000, 1000)
        if not config.has_section(section):
            config.add_section(section)
        config.set(section, key, path if key.endswith('_FILE') else '')
        if not key.endswith('_FILE'):
            config.set(section, key + '_URI', 'file:' + path)


def append_startup_files(path, value):
    with zipfile.ZipFile(path, 'a') as archive:
        archive.writestr(DEPENDENCIES, json.dumps(value, ensure_ascii=False))
        for entry in value['文件']:
            if entry['配置项'] == 'SECRET_KEY':
                continue
            # 保证之后的目录切换也保留第一次启动所需的同一份内容。
            sql = next(name for name in archive.namelist() if name.endswith('gitea-db.sql'))
            name = str(PurePosixPath(sql).parent / 'data/conf' / ('manager-startup-' + entry['配置节'] + '-' + entry['配置项'].lower()))
            if name in archive.namelist():
                if archive.read(name) != base64.b64decode(entry['内容'], validate=True):
                    raise RuntimeError('源目录占用 Manager 启动依赖保留名称')
                continue
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            archive.writestr(info, base64.b64decode(entry['内容'], validate=True))
