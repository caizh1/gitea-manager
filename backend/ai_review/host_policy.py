"""管理员维护的精确主机名单；原子替换使独立进程立即读取新配置。"""
import fcntl
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

KINDS = ('AI_REVIEW_GITEA_HOSTS', 'AI_REVIEW_MODEL_HOSTS', 'AI_REVIEW_WEBHOOK_HOSTS')


def address(value):
    from .common import ReviewError
    try:
        if not isinstance(value, str) or len(value) > 500 or any(c.isspace() for c in value) or '\\' in value:
            raise ValueError()
        url = urlsplit(value)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError()
        url.port
        return value.rstrip('/'), url.hostname.lower()
    except ValueError:
        raise ReviewError('请输入完整的 HTTP/HTTPS 服务地址，不含凭据、查询或片段') from None


def path():
    value = os.environ.get('AI_REVIEW_HOST_POLICY_FILE')
    return Path(value) if value else None


def ensure():
    target = path()
    if target is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / (target.name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not target.exists():
            _write(target, {key: sorted({h.strip().lower() for h in os.environ.get(key, '').split(',') if h.strip()}) for key in KINDS})


def _write(target, value):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            os.chmod(out.name, 0o600)
            json.dump(value, out, ensure_ascii=False, sort_keys=True)
            out.flush(); os.fsync(out.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def read():
    from .common import ReviewError
    target = path()
    if target is None:
        return {key: {h.strip().lower() for h in os.environ.get(key, '').split(',') if h.strip()} for key in KINDS}
    try:
        # 仅 Manager 启动或管理员向导可以初始化；读进程不自行扩大名单。
        if target.stat().st_size > 32768:
            raise ValueError()
        value = json.loads(target.read_text())
        if set(value) != set(KINDS) or any(not isinstance(v, list) or any(not isinstance(h, str) for h in v) for v in value.values()):
            raise ValueError()
        return {key: set(value[key]) for key in KINDS}
    except (OSError, ValueError):
        raise ReviewError('主机配置不可读取，请检查 Manager 初始化和持久卷') from None


def approve(values):
    """只由已通过登录、同源与 CSRF 校验的配置入口调用。"""
    from .common import ReviewError
    target = path()
    if target is None:
        raise ReviewError('请先使用一键升级包启用自动接入配置')
    if set(values) - set(KINDS):
        raise ReviewError('未知的主机配置类型')
    additions = {key: {address(url)[1] for url in urls} for key, urls in values.items()}
    ensure()
    with (target.parent / (target.name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = read()
        for key, hosts in additions.items():
            current[key].update(hosts)
        if sum(len(v) for v in current.values()) > 500:
            raise ReviewError('主机配置超过支持数量')
        _write(target, {key: sorted(current[key]) for key in KINDS})
