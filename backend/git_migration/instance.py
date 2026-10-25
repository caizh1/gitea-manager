"""连接别名门禁，避免同一实例绕过按服务器编号的操作互斥。"""
from urllib.parse import urlsplit

from models import GiteaServer
from svn_migration.common import file_lock, require
from .store import root


ACTIVE = ('queued', 'running', 'interrupted', 'uncertain', 'quarantined')


def connection_lock():
    return file_lock(root() / 'connections.lock', blocking=True)


def identity(value):
    def field(name, default=''):
        return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)
    parsed = urlsplit(field('gitea_url').strip())
    try:
        port = parsed.port or {'http': 80, 'https': 443}.get(parsed.scheme)
    except ValueError:
        port = None
    url = (parsed.scheme.lower(), (parsed.hostname or '').lower().rstrip('.'), port, parsed.path.rstrip('/')) if parsed.hostname else None
    host, container = str(field('host')).strip().lower(), str(field('gitea_container', 'gitea')).strip()
    ssh = (host, str(field('ssh_port', 22)), container) if host and container else None
    return url, ssh


def same_instance(left, right):
    a, b = identity(left), identity(right)
    return any(x is not None and x == y for x, y in zip(a, b))


def unique_target(server):
    aliases = [row.id for row in GiteaServer.query.all() if row.id != server.id and same_instance(server, row)]
    require(not aliases, '目标实例存在重复连接记录，请先合并连接记录后再迁移，避免备份恢复绕过实例互斥', 'instance_alias', 409)


def validate_connection_change(data, server_id=None):
    from .models import GitMigrationRun
    current = GiteaServer.query.get(server_id) if server_id else None
    candidate = {name: data.get(name, getattr(current, name, default)) for name, default in
                 (('gitea_url', ''), ('host', ''), ('ssh_port', 22), ('gitea_container', 'gitea'))}
    for run in GitMigrationRun.query.filter(GitMigrationRun.status.in_(ACTIVE)).all():
        target = GiteaServer.query.get(run.server_id)
        require(target is None or run.server_id == server_id or not same_instance(candidate, target),
                '该实例存在未完成的 Git 迁移，不能新增或修改为它的连接别名', 'instance_alias', 409)
