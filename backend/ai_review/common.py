"""公共约束。异常只包含受控中文信息，不带远端响应或凭据。"""
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


class ReviewError(Exception):
    pass


class Stale(ReviewError):
    pass


class PublishUnknown(ReviewError):
    pass


class WriteRejected(ReviewError):
    """服务器明确拒绝写入；与已发送但结果不确定的请求区分。"""
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def secret_file(name):
    path = os.environ.get(name + '_FILE', '')
    if not path:
        raise ReviewError('部署秘密尚未配置：' + name + '_FILE')
    try:
        value = Path(path).read_text().strip()
    except OSError:
        raise ReviewError('无法读取部署秘密文件') from None
    if len(value) < 32:
        raise ReviewError('部署秘密长度不足')
    return value


def endpoint(value, allowlist_env):
    if not isinstance(value, str) or len(value) > 500:
        raise ReviewError('服务地址格式错误')
    try:
        url = urlsplit(value)
        from .host_policy import read
        allowed = read().get(allowlist_env, set())
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.hostname.lower() not in allowed
                or any(c.isspace() for c in value) or '\\' in value):
            raise ValueError()
        url.port
    except ValueError:
        raise ReviewError('服务地址必须匹配部署白名单，且不能包含凭据、查询或片段') from None
    return value.rstrip('/')


def valid_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ReviewError('Gitea 返回了无效的提交标识')
    return value


def safe_path(value):
    if (not isinstance(value, str) or not value or len(value) > 1000 or value.startswith('/')
            or any(p in ('', '.', '..') for p in value.split('/'))
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ReviewError('文件路径不支持安全定位')
    return value
