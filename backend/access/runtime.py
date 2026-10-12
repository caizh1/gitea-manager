"""会话与内部服务运行密钥；不会覆盖 Review 主密钥或业务凭据。"""
import os
import secrets
import fcntl
from pathlib import Path
from sqlalchemy.engine import make_url


def signing_key(configured, database_url):
    if configured and configured != 'change-me-in-production':
        if len(configured) < 32:
            raise RuntimeError('会话签名密钥至少需要 32 字符')
        return configured
    if os.environ.get('SECRET_KEY') == 'change-me-in-production':
        raise RuntimeError('部署仍使用公开的默认会话密钥，请通过升级入口生成独立密钥')
    database = make_url(database_url).database
    if not database or database == ':memory:':
        raise RuntimeError('必须设置随机 SECRET_KEY')
    path = Path(database).resolve().parent / '.session-key'
    return persistent_secret(path)


def persistent_secret(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            value = path.read_text().strip()
        else:
            value = secrets.token_urlsafe(48)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
    if len(value) < 32:
        raise RuntimeError('持久化运行密钥不完整，拒绝生成替代密钥')
    return value


def configure_transport(app):
    """只信任显式配置的直连代理地址；不接受任意客户端伪造转发协议。"""
    import ipaddress
    import socket
    from flask import request
    from flask.sessions import SecureCookieSessionInterface
    networks = [ipaddress.ip_network(value.strip()) for value in
                os.environ.get('TRUSTED_PROXY_NETWORKS', '').split(',') if value.strip()]
    original = app.wsgi_app
    hosts = [value.strip() for value in os.environ.get('TRUSTED_PROXY_HOSTS', '').split(',') if value.strip()]

    def transport(environ, start_response):
        try:
            trusted = any(ipaddress.ip_address(environ.get('REMOTE_ADDR', '')) in network for network in networks)
        except ValueError:
            trusted = False
        if not trusted:
            for host in hosts:
                try:
                    addresses = {row[4][0] for row in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
                    if environ.get('REMOTE_ADDR') in addresses:
                        trusted = True
                        break
                except OSError:
                    continue
        if trusted and environ.get('HTTP_X_FORWARDED_PROTO') in ('http', 'https'):
            environ['wsgi.url_scheme'] = environ['HTTP_X_FORWARDED_PROTO']
        if trusted and environ.get('HTTP_X_REAL_IP'):
            try:
                environ['REMOTE_ADDR'] = str(ipaddress.ip_address(environ['HTTP_X_REAL_IP']))
            except ValueError:
                pass
        return original(environ, start_response)

    class SecureSession(SecureCookieSessionInterface):
        def get_cookie_secure(self, app):
            return bool(request.is_secure or app.config.get('SESSION_COOKIE_SECURE'))

    app.wsgi_app = transport
    app.session_interface = SecureSession()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
