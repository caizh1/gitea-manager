"""迁移源业务配置，仅保留明确的目标部署参数；不把配置或密码写入日志。"""
import base64
import configparser
import hashlib
import io
import json
from pathlib import Path
import re


DEPLOYMENT_CONFIG = {
    ('database', key) for key in ('host', 'name', 'user', 'passwd', 'path', 'ssl_mode')
} | {('server', key) for key in ('domain', 'root_url', 'local_root_url', 'http_addr', 'http_port',
    'ssh_domain', 'ssh_port', 'ssh_listen_host', 'ssh_listen_port', 'cert_file', 'key_file', 'protocol')}
KEY_CONFIG = {('security', 'secret_key'), ('security', 'secret_key_uri')}
STARTUP_CONFIG = {('security', 'SECRET_KEY'), ('security', 'INTERNAL_TOKEN'),
                  ('server', 'LFS_JWT_SECRET'), ('oauth2', 'JWT_SECRET'),
                  ('oauth2', 'JWT_SIGNING_PRIVATE_KEY_FILE')}


def parse(raw):
    if len(raw) > 2 * 1024 * 1024:
        raise RuntimeError('业务配置超过支持大小，尚未修改目标')
    try:
        result = configparser.ConfigParser(interpolation=None, strict=True)
        result.read_string('[DEFAULT]\n' + raw.decode('utf-8'))
        seen = set()
        for section in result.sections():
            normalized = section.lower()
            if normalized in seen:
                raise ValueError()
            seen.add(normalized)
            if section != normalized:
                items = dict(result._sections[section])
                result.remove_section(section)
                result.add_section(normalized)
                for key, value in items.items():
                    result.set(normalized, key, value)
        return result
    except (UnicodeError, ValueError, configparser.Error):
        raise RuntimeError('备份或目标的业务配置无法安全解析，尚未修改目标') from None


def literal(value):
    value = value.strip()
    for delimiter in ('"""', '`', '"', "'"):
        if value.startswith(delimiter):
            end = value.rfind(delimiter)
            if end < len(delimiter):
                raise RuntimeError('业务配置引号不完整')
            tail = value[end + len(delimiter):].strip()
            if tail and not tail.startswith(('#', ';')):
                raise RuntimeError('业务配置引号后有无法安全解释的内容')
            return value[len(delimiter):end]
    value = re.split('[;#]', value, maxsplit=1)[0].strip()
    return value


def values(config):
    result = {('default', key): literal(value) for key, value in config.defaults().items()}
    for section in config.sections():
        result.update({(section, key): literal(value) for key, value in config._sections[section].items()})
    return result


class RestoreConfig:
    def __init__(self, root, workspace, target_raw, secret_uri):
        self.root = Path(root)
        self.config = parse((self.root / 'app.ini').read_bytes())
        target = parse(target_raw)
        for section, key in DEPLOYMENT_CONFIG:
            self.config.remove_option(section, key) if self.config.has_section(section) else None
            if key in target._sections.get(section, {}):
                self.set(section, key, target._sections[section][key])
        self.set('security', 'SECRET_KEY', '')
        self.set('security', 'SECRET_KEY_URI', secret_uri)
        self._startup_files(Path(workspace))
        self.directory = self.root / 'data/conf'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.expected = values(self.config)

    def set(self, section, key, value):
        if not self.config.has_section(section):
            self.config.add_section(section)
        self.config.set(section, key, value)

    def _startup_files(self, workspace):
        manifest = workspace / 'manager-startup-files.json'
        if not manifest.exists():
            for section, key in STARTUP_CONFIG - {('security', 'SECRET_KEY')}:
                if self.config.get(section, key + '_URI', fallback=''):
                    raise RuntimeError('旧备份缺少启动依赖快照，拒绝引用源外置秘密文件')
            return
        if manifest.stat().st_size > 16 * 1024 * 1024:
            raise RuntimeError('启动依赖快照过大')
        data = json.loads(manifest.read_bytes())
        if data.get('版本') != 1:
            raise RuntimeError('启动依赖快照版本不受支持')
        seen = set()
        for entry in data['文件']:
            pair = entry['配置节'], entry['配置项']
            if pair not in STARTUP_CONFIG or pair in seen:
                raise RuntimeError('启动依赖配置不受支持或重复')
            seen.add(pair)
            if pair == ('security', 'SECRET_KEY'):
                continue
            body = base64.b64decode(entry['内容'], validate=True)
            name = 'manager-startup-' + pair[0] + '-' + pair[1].lower()
            staged = self.root / 'data/conf' / name
            if not body or len(body) > 2 * 1024 * 1024 or staged.is_symlink() or staged.read_bytes() != body:
                raise RuntimeError('启动依赖正文与备份快照不一致')
            path = '/data/gitea/conf/' + name
            self.set(*pair, path if pair[1].endswith('_FILE') else '')
            if not pair[1].endswith('_FILE'):
                self.set(pair[0], pair[1] + '_URI', 'file:' + path)

    def environment(self, environment):
        result = dict(environment)
        for name in environment:
            if not name.upper().startswith('GITEA__'):
                continue
            parts = name.split('__')
            file = parts[-1].upper() == 'FILE'
            if len(parts) != (4 if file else 3) or not parts[2]:
                raise RuntimeError('目标包含无法规范映射的 Gitea 环境覆盖')
            pair = (parts[1].lower() or 'default'), parts[2].lower()
            if pair in DEPLOYMENT_CONFIG or pair in KEY_CONFIG:
                continue
            value = self.expected.get(pair, '')
            if file:
                filename = 'manager-restored-env-' + hashlib.sha256(('\0'.join(pair)).encode()).hexdigest()
                path = self.directory / filename
                if path.exists() and path.read_bytes() != value.encode():
                    raise RuntimeError('备份占用恢复环境保留文件名')
                path.write_bytes(value.encode())
                path.chmod(0o600)
                result[name] = '/data/gitea/conf/' + filename
            else:
                result[name] = value
        return result

    def write(self):
        # Gitea的Go INI与environment-to-ini使用规范大写键名；不能沿用ConfigParser的小写输出。
        canonical = configparser.ConfigParser(interpolation=None)
        canonical.optionxform = str
        for key, value in self.config.defaults().items():
            canonical['DEFAULT'][key.upper()] = value
        for section in self.config.sections():
            canonical.add_section(section)
            for key, value in self.config._sections[section].items():
                canonical.set(section, key.upper(), value)
        output = io.StringIO()
        canonical.write(output)
        raw = output.getvalue()
        if raw.startswith('[DEFAULT]\n'):
            raw = raw[len('[DEFAULT]\n'):]
        path = self.directory / 'app.ini'
        path.write_text(raw, encoding='utf-8')
        path.chmod(0o600)

    def verify(self, raw):
        actual = values(parse(raw))
        # 空的附加环境项等价于未配置；任何源中明确的值、尤其认证策略必须一致。
        if any(actual.get(pair, '') != value for pair, value in self.expected.items()) or any(
                value and pair not in self.expected for pair, value in actual.items()):
            raise RuntimeError('恢复后的业务配置或部署参数与迁移计划不一致，拒绝发布成功')
