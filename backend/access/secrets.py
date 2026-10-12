"""目录查询密码使用独立持久密钥，轮换会话签名不影响解密。"""
import fcntl
import os
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from flask import current_app
from models import db
from access.errors import AccessError


def key_path():
    configured = current_app.config.get('ACCESS_KEY_FILE') or os.environ.get('ACCESS_KEY_FILE')
    if configured:
        return Path(configured)
    database = db.engine.url.database
    if not database or database == ':memory:':
        raise AccessError('内存数据库必须明确配置独立目录密钥文件', 503)
    return Path(database).resolve().parent / 'access.key'


def cipher(create=False):
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not path.exists():
            if not create:
                raise AccessError('目录密钥丢失，请恢复原密钥，禁止生成替代密钥', 503)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
        try:
            return Fernet(path.read_bytes().strip())
        except (ValueError, OSError):
            raise AccessError('目录密钥不可读取或格式错误', 503) from None


def encrypt(value):
    return cipher(create=True).encrypt(value.encode()).decode() if value else ''


def decrypt(value):
    if not value:
        return ''
    try:
        return cipher().decrypt(value.encode()).decode()
    except InvalidToken:
        raise AccessError('目录密码无法解密，请恢复原目录密钥', 503) from None
