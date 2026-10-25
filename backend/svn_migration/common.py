"""迁移输入校验、持久目录和凭据边界。"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit, urlunsplit, unquote, quote

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app
from models import db


class MigrationError(Exception):
    def __init__(self, message, code='invalid', status=400):
        super().__init__(message)
        self.code, self.status = code, status


class Uncertain(MigrationError):
    def __init__(self, message):
        super().__init__(message, 'uncertain', 409)


class Cancelled(MigrationError):
    def __init__(self):
        super().__init__('任务已取消，保留远端资源和迁移现场', 'cancelled', 409)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def require(condition, message, code='invalid', status=400):
    if not condition:
        raise MigrationError(message, code, status)


def string(value, name, maximum=200, empty=False):
    require(isinstance(value, str) and len(value) <= maximum and (empty or value.strip()), name + '格式无效')
    require(not any(ord(c) < 32 or ord(c) == 127 for c in value), name + '不能包含控制字符')
    return value.strip()


def slug(value):
    value = string(value, '英文标识', 80)
    require(re.fullmatch(r'[a-z][a-z0-9-]{0,79}', value), '英文标识仅接受小写字母、数字和短横线')
    return value


def repo_name(value):
    value = string(value, '目标仓库名', 120)
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value) and not value.lower().endswith('.git'), '目标仓库名无效，请使用英文、数字、短横线或下划线')
    return value


def source_url(value, schemes=('http', 'https', 'svn')):
    value = string(value, '连接地址', 2000)
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError:
        raise MigrationError('连接端口无效') from None
    require(parsed.scheme in schemes and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment, '连接地址必须使用支持的协议，且不能包含凭据、查询或片段')
    path = unquote(parsed.path)
    require(not any(ord(c) < 32 or c == '\\' for c in path) and not any(p in ('.', '..') for p in path.split('/')), '连接路径无效')
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), quote(path.rstrip('/'), safe='/@:+,=-._~'), '', ''))


def within_source(url, base):
    u, b = urlsplit(url), urlsplit(base)
    return (u.scheme, u.netloc) == (b.scheme, b.netloc) and (u.path == b.path or u.path.startswith(b.path.rstrip('/') + '/'))


def layout(value):
    require(isinstance(value, dict) and not set(value) - {'trunk', 'branches', 'tags', 'default_branch', 'project'}, '仓库布局字段无效')
    require(type(value.get('project', False)) is bool, '项目纳管标记无效')
    result = {'trunk': 'trunk', 'branches': 'branches', 'tags': 'tags', 'default_branch': 'main'} | value
    require(result['trunk'] != '.' or not (result['branches'] or result['tags']), '根目录整体迁移不能同时配置独立分支或标签目录')
    for key in ('trunk', 'branches', 'tags'):
        path = result[key]
        require(isinstance(path, str) and len(path) <= 500, '布局路径无效')
        if key == 'trunk' and path == '.':
            continue
        if key != 'trunk' and not path:
            continue
        require(path and not path.startswith('/') and all(p not in ('', '.', '..') for p in path.split('/')) and not any(c in path for c in '\\@\r\n\x00'), '布局须使用不含特殊控制字符的相对路径')
    selected = [result[k] for k in ('trunk', 'branches', 'tags') if result[k]]
    require(len(set(selected)) == len(selected) and not any(a != b and b.startswith(a + '/') for a in selected for b in selected), '主干、分支和标签路径不能重叠')
    string(result['default_branch'], '默认分支', 200)
    return result


def data_root():
    database = db.engine.url.database
    require(database and database != ':memory:', 'SVN 迁移需要持久化 SQLite 数据库', 'environment', 503)
    return Path(database).resolve().parent


def work_root():
    root = data_root() / 'svn-migrations'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def atomic_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as out:
        temp = Path(out.name)
        try:
            os.chmod(temp, 0o600)
            out.write(encode(value) + '\n'); out.flush(); os.fsync(out.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


@contextlib.contextmanager
def file_lock(path, blocking=False):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise MigrationError('相关任务正在执行，请稍后重试', 'busy', 409) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def cipher(create=False):
    path = data_root() / 'svn-migration.key'
    with file_lock(data_root() / '.svn-key.lock', blocking=True):
        if not path.exists():
            require(create, '迁移密钥缺失，请恢复原密钥', 'secret', 503)
            from .models import SvnSource
            require(not SvnSource.query.filter(SvnSource.password_ciphertext != '').first(), '已有加密凭据但密钥缺失，禁止生成替代密钥', 'secret', 503)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as out:
                out.write(Fernet.generate_key()); out.flush(); os.fsync(out.fileno())
        try:
            return Fernet(path.read_bytes().strip())
        except (ValueError, OSError):
            raise MigrationError('迁移密钥损坏，请恢复原密钥', 'secret', 503) from None


def encrypt(value):
    return cipher(create=True).encrypt(value.encode()).decode() if value else ''


def decrypt(value):
    try:
        return cipher().decrypt(value.encode()).decode() if value else ''
    except InvalidToken:
        raise MigrationError('迁移凭据无法解密，请恢复原密钥', 'secret', 503) from None
