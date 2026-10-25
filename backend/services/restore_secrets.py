"""恢复密钥随数据迁移；启动环境及 Compose 一起变更、一起回退。"""
import configparser
import copy
import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import tarfile
import zipfile
from urllib.parse import quote, urlsplit

import yaml
from docker.types import Mount

SECRET_PATH = '/data/gitea/conf/manager-secret-key'
SECRET_URI = 'file:' + SECRET_PATH
LEGACY_SECRET = '!#@FDEWREWR&*('


def read_file(container, path):
    """不用命令输出读取秘密，避免进入诊断日志。"""
    stream, details = container.get_archive(path)
    if details.get('size', 0) > 2 * 1024 * 1024:
        raise RuntimeError('配置或密钥文件超过允许大小')
    with io.BytesIO(b''.join(stream)) as raw, tarfile.open(fileobj=raw) as archive:
        members = archive.getmembers()
        if len(members) != 1 or not members[0].isfile() or members[0].size > 2 * 1024 * 1024:
            raise RuntimeError('配置或密钥文件不是受支持的普通小文件')
        return archive.extractfile(members[0]).read(), members[0]


def put_file(container, path, content, mode=0o600, uid=0, gid=0):
    with io.BytesIO() as raw:
        with tarfile.open(fileobj=raw, mode='w') as archive:
            member = tarfile.TarInfo(PurePosixPath(path).name)
            member.size, member.mode, member.uid, member.gid = len(content), mode, uid, gid
            archive.addfile(member, io.BytesIO(content))
        if not container.put_archive(str(PurePosixPath(path).parent), raw.getvalue()):
            raise RuntimeError('配置或密钥文件写入失败')


def security_values(raw):
    try:
        config = configparser.ConfigParser(interpolation=None, strict=True)
        config.read_string('[DEFAULT]\n' + raw.decode('utf-8'))
        def value(name):
            text = config.get('security', name, fallback='').strip()
            for delimiter in ('"""', '`'):
                if text.startswith(delimiter):
                    end = text.rfind(delimiter)
                    if end < len(delimiter):
                        raise ValueError()
                    text = text[len(delimiter):end]
                    break
            else:
                # Gitea 的 Go INI 默认在普通值的任意 #/; 处开始注释，再剥除成对引号。
                import re
                text = re.split(r'[;#]', text, maxsplit=1)[0].strip()
                if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
                    text = text[1:-1]
            import re
            if re.search(r'%\([^)]+\)s', text) or text != text.strip():
                raise ValueError()
            return text
        secret, uri = value('SECRET_KEY'), value('SECRET_KEY_URI')
        if secret and uri:
            raise ValueError()
        return secret, uri
    except (ValueError, UnicodeError, configparser.Error):
        raise RuntimeError('无法无歧义读取 Gitea 的数据加密密钥配置') from None


def uri_path(uri):
    parsed = urlsplit(uri)
    if parsed.scheme != 'file' or parsed.netloc or parsed.query or parsed.fragment:
        raise RuntimeError('仅支持本地文件形式的 Gitea 密钥 URI')
    # 与 Gitea loadSecret 使用的 Go URL.RequestURI 一致，不能把 %xx 再解码成另一条路径。
    path = quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")
    if not path.startswith('/') or '..' in PurePosixPath(path).parts or '\x00' in path:
        raise RuntimeError('Gitea 密钥文件必须使用绝对路径')
    return path


def archive_secret(root):
    snapshot = root / 'manager-secrets.json'
    if snapshot.exists():
        try:
            data = json.loads(snapshot.read_text())
            if data['配置摘要'] != hashlib.sha256((root / 'app.ini').read_bytes()).hexdigest():
                raise ValueError()
            secret = data['数据加密密钥']
            if not isinstance(secret, str) or not secret:
                raise ValueError()
            return secret
        except (ValueError, KeyError, OSError):
            raise RuntimeError('备份密钥快照与配置不一致') from None
    secret, uri = security_values((root / 'app.ini').read_bytes())
    if uri:
        path = uri_path(uri)
        prefix = '/data/gitea/'
        if not path.startswith(prefix):
            raise RuntimeError('备份未包含外置加密密钥；请用新版 Manager 重新备份源 Gitea，无需重装目标')
        try:
            secret = (root / 'data' / path[len(prefix):]).read_text().strip()
        except OSError:
            raise RuntimeError('备份缺少其配置引用的加密密钥文件') from None
    if not secret:
        if uri:
            raise RuntimeError('备份的加密密钥文件为空')
        secret = LEGACY_SECRET
    return secret


def capture_backup_secret(server, backup):
    """外置 Secret 不一定在 dump 中，备份必须显式携带密钥而非只携带路径。"""
    from services.docker_service import managed_client
    from services.diagnostics import register_secret
    with zipfile.ZipFile(backup.file_path, 'a') as archive:
        configs = [p for p in archive.namelist() if PurePosixPath(p).name == 'app.ini'
                   and str(PurePosixPath(p).parent / 'gitea-db.sql') in archive.namelist()]
        if len(configs) != 1:
            raise RuntimeError('备份必须包含唯一的 Gitea 主配置')
        raw = archive.read(configs[0])
        secret, uri = security_values(raw)
        if uri:
            client = managed_client(server)
            try:
                secret = read_file(client.containers.get(server.gitea_container), uri_path(uri))[0].decode().strip()
            finally:
                client.close()
        # 官方 Gitea 对未配置密钥的历史实例使用此固定兼容值；显式记录避免恢复时另造密钥。
        elif not secret:
            secret = LEGACY_SECRET
        if not secret:
            raise RuntimeError('源 Gitea 加密密钥文件为空，备份不可用于可靠恢复')
        register_secret(secret)
        name = str(PurePosixPath(configs[0]).parent / 'manager-secrets.json')
        if name in archive.namelist():
            raise RuntimeError('备份包含重复密钥快照，拒绝覆盖')
        archive.writestr(name, json.dumps({'说明': '随备份保存的 Gitea 数据加密密钥，请按敏感备份保管',
            '配置摘要': hashlib.sha256(raw).hexdigest(), '数据加密密钥': secret}, ensure_ascii=False))
    backup.file_size = os.path.getsize(backup.file_path)


def secret_environment(environment):
    result = {}
    for key, value in environment.items():
        normalized = key.upper()
        if normalized in ('GITEA__SECURITY__SECRET_KEY', 'GITEA__SECURITY__SECRET_KEY_URI',
                          'GITEA__SECURITY__SECRET_KEY__FILE', 'GITEA__SECURITY__SECRET_KEY_URI__FILE'):
            result[key] = value
    return result


def normalized_environment(environment):
    result = dict(environment)
    for key in secret_environment(environment):
        del result[key]
    result['GITEA__security__SECRET_KEY'] = ''
    result['GITEA__security__SECRET_KEY_URI'] = SECRET_URI
    return result


class SecretTransition:
    """只有启动环境会覆盖新配置时才重建容器，保留旧容器直到验证完成。"""
    def __init__(self, client, container, marker):
        self.client, self.original, self.current = client, container, container
        self.marker = marker
        self.name = container.name
        self.attrs = copy.deepcopy(container.attrs)
        host = self.attrs.get('HostConfig') or {}
        if host.get('PublishAllPorts') or any(
            not binding.get('HostPort') or binding.get('HostPort') == '0'
            for bindings in (host.get('PortBindings') or {}).values() for binding in bindings or []):
            raise RuntimeError('目标使用 Docker 随机宿主端口，重启后地址可能变化；请在 Compose 中固定当前映射端口后恢复，无需重装')
        environment = dict(v.split('=', 1) for v in self.attrs['Config'].get('Env', []) if '=' in v)
        settings = secret_environment(environment)
        self.needed = bool(settings) and settings != {
            'GITEA__security__SECRET_KEY_URI': SECRET_URI}
        if settings == {'GITEA__security__SECRET_KEY': '', 'GITEA__security__SECRET_KEY_URI': SECRET_URI}:
            self.needed = False
        self.environment = normalized_environment(environment)
        self.compose_helper = None
        self.compose_changed = False
        self.renamed = False
        self.disconnected = []
        self.compose_original = self.compose_new = None

    def prepare(self):
        if not self.needed:
            return
        host = self.attrs['HostConfig']
        if host.get('AutoRemove') or str(host.get('NetworkMode', '')).startswith('container:'):
            raise RuntimeError('自动删除或共享其他容器网络的目标不能安全重建，尚未修改目标')
        labels = self.attrs['Config'].get('Labels') or {}
        if not labels.get('com.docker.compose.project'):
            return
        files = labels.get('com.docker.compose.project.config_files', '').split(',')
        service = labels.get('com.docker.compose.service')
        path = PurePosixPath(files[-1])
        if not path.is_absolute() or not service or len(files) == 0:
            raise RuntimeError('目标 Compose 文件位置不可确认，尚未修改目标；请保留部署配置文件')
        self.compose_name = path.name
        self.compose_helper = self.client.containers.run(
            self.attrs['Image'], entrypoint=['sleep'], command=['infinity'], detach=True,
            network_mode='none', mounts=[Mount('/compose', str(path.parent), type='bind')],
            labels={'gitea.manager.operation': self.marker})
        raw, self.compose_stat = read_file(self.compose_helper, '/compose/' + path.name)
        self.compose_original = raw
        try:
            value = yaml.safe_load(raw)
            services = value.setdefault('services', {})
            if value.get('include') and service not in services:
                raise ValueError()
            entry = copy.deepcopy(services.get(service) or {})
            services[service] = entry
            env = copy.deepcopy(entry.get('environment') or {})
            if isinstance(env, list):
                env = dict(v.split('=', 1) if '=' in v else (v, None) for v in env)
            if not isinstance(env, dict):
                raise ValueError()
            # 显式覆盖上游文件及 env_file 的每个已生效拼写；不修改 .env 或外部 Secret。
            for key in secret_environment(self.environment) | secret_environment(
                    dict(v.split('=', 1) for v in self.attrs['Config'].get('Env', []) if '=' in v)) | secret_environment(env):
                normalized = key.upper()
                env[key] = ('/data/gitea/conf/manager-secret-uri' if normalized.endswith('SECRET_KEY_URI__FILE') else
                            '/data/gitea/conf/manager-empty-secret' if normalized.endswith('SECRET_KEY__FILE') else
                            SECRET_URI if normalized.endswith('SECRET_KEY_URI') else '')
            entry['environment'] = env
            self.compose_new = yaml.safe_dump(value, allow_unicode=True, sort_keys=False).encode()
        except (ValueError, TypeError, AttributeError, yaml.YAMLError):
            raise RuntimeError('目标 Compose 含不支持的结构或自定义 YAML 标签，尚未修改目标') from None

    def compose_replace(self, expected, replacement):
        path = '/compose/' + self.compose_name
        temporary = '/compose/.' + self.marker
        member = self.compose_stat
        put_file(self.compose_helper, temporary, replacement, member.mode, member.uid, member.gid)
        digest = hashlib.sha256(expected).hexdigest()
        script = 'test "$(sha256sum -- "$1" | cut -d " " -f 1)" = "$2" && mv -f -- "$3" "$1"'
        code, _ = self.compose_helper.exec_run(['sh', '-ec', script, 'sh', path, digest, temporary])
        if code:
            raise RuntimeError('Compose 文件在恢复期间发生变化或不可写，拒绝覆盖；原容器与回退点保留')
        if read_file(self.compose_helper, path)[0] != replacement:
            raise RuntimeError('Compose 写入回读不一致，回退点保留')

    def endpoint(self, name, value):
        endpoint = {}
        aliases = [v for v in (value.get('Aliases') or []) if v not in (self.original.id, self.original.short_id)]
        if name not in ('bridge', 'host', 'none'):
            endpoint['Aliases'] = aliases
            endpoint['IPAMConfig'] = value.get('IPAMConfig') or {
                'IPv4Address': value.get('IPAddress', ''), 'IPv6Address': value.get('GlobalIPv6Address', '')}
        return endpoint

    def switch(self):
        if not self.needed:
            return self.current
        if self.compose_helper:
            # 先保存原文件，进程中断后仍有人工恢复依据；文件不含新增明文密钥。
            member = self.compose_stat
            put_file(self.compose_helper, '/compose/.' + self.marker + '.bak', self.compose_original,
                     0o600, member.uid, member.gid)
            self.compose_changed = True
            self.compose_replace(self.compose_original, self.compose_new)
        self.original.rename(self.name + '-' + self.marker)
        self.renamed = True
        networks = self.attrs['NetworkSettings']['Networks']
        for name in networks:
            self.client.networks.get(name).disconnect(self.original)
            self.disconnected.append(name)
        config = copy.deepcopy(self.attrs['Config'])
        config['Image'] = self.attrs['Image']
        config['Labels'] = dict(config.get('Labels') or {}, **{'gitea.manager.restore': self.marker})
        config['Env'] = [key + '=' + value for key, value in self.environment.items()]
        host = copy.deepcopy(self.attrs['HostConfig'])
        # 匿名卷必须复用实际卷，不能让 Docker 为重建容器分配空卷。
        bound = {v.split(':')[1] for v in host.get('Binds') or [] if ':' in v}
        mounted = {m['Target'] for m in host.get('Mounts') or []}
        for inherited in host.get('VolumesFrom') or []:
            owner = self.client.containers.get(inherited.split(':', 1)[0])
            mounted.update(m['Destination'] for m in owner.attrs['Mounts'])
        for mount in self.attrs['Mounts']:
            if mount['Type'] == 'volume' and mount['Destination'] not in bound | mounted:
                host.setdefault('Binds', [])
                if host['Binds'] is None: host['Binds'] = []
                host['Binds'].append(mount['Name'] + ':' + mount['Destination'] + (':rw' if mount['RW'] else ':ro'))
        config['HostConfig'] = host
        config['NetworkingConfig'] = {'EndpointsConfig': {name: self.endpoint(name, val) for name, val in networks.items()}}
        response = self.client.api._post_json(self.client.api._url('/containers/create'),
                                               data=config, params={'name': self.name})
        result = self.client.api._result(response, json=True)
        self.current = self.client.containers.get(result['Id'])
        return self.current

    def rollback(self):
        if self.current.id != self.original.id:
            self.current.remove(force=True)
            self.current = self.original
        if self.renamed:
            self.original.rename(self.name)
            self.renamed = False
        for name in self.disconnected[:]:
            endpoint = self.endpoint(name, self.attrs['NetworkSettings']['Networks'][name])
            ipam = endpoint.get('IPAMConfig') or {}
            self.client.networks.get(name).connect(self.original, aliases=endpoint.get('Aliases'),
                ipv4_address=ipam.get('IPv4Address'), ipv6_address=ipam.get('IPv6Address'))
            self.disconnected.remove(name)
        if self.compose_changed:
            raw, _ = read_file(self.compose_helper, '/compose/' + self.compose_name)
            if raw != self.compose_original:
                self.compose_replace(self.compose_new, self.compose_original)
            self.compose_changed = False
        return self.original

    def commit(self):
        if self.current.id != self.original.id:
            self.original.remove()
        if self.compose_helper:
            self.compose_helper.exec_run(['rm', '-f', '/compose/.' + self.marker + '.bak'])

    def close(self):
        if self.compose_helper:
            self.compose_helper.remove(force=True)
