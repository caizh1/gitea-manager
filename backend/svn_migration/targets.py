"""目标群组选择与只读路径预览；新建群组必须来自用户的显式操作。"""
from contextlib import contextmanager
import tempfile
import time

from .common import MigrationError, require, repo_name, slug, string, work_root
from .gitea import Gitea


class Deadline:
    def __init__(self):
        self.started = time.monotonic()

    def tick(self):
        require(time.monotonic() - self.started < 45, '目标读取超时，请缩小范围后重试', 'timeout', 408)


@contextmanager
def connection(server, ca=''):
    require(server.role != 'archive', '归档实例不能作为迁移目标')
    require(isinstance(ca, str) and len(ca) <= 128000, '目标 CA 无效')
    with tempfile.TemporaryDirectory(prefix='target-query-', dir=work_root()) as directory:
        api = Gitea(server, Deadline(), directory, ca)
        api.request_timeout = (8, 15)
        try:
            api.check()
            yield api
        finally:
            api.close()


def positive_id(value, label, zero=False):
    require(type(value) is int and value >= (0 if zero else 1), label + '无效')
    return value


def public_group(row):
    return {'id': row['id'], 'name': row.get('name') or row['full_path'],
            'full_path': row['full_path'], 'parent_id': row.get('parent_id', 0),
            'visibility': row.get('visibility'), 'archived': bool(row.get('archived')),
            'can_create_group': bool(row.get('abilities', {}).get('create_group')),
            'selectable': bool(row.get('abilities', {}).get('create_project'))
                and not row.get('archived') and not row.get('delete_after')}


def groups(server, data):
    parent = positive_id(data.get('parent_id', 0), '父群组', zero=True)
    after = positive_id(data.get('after_id', 0), '分页位置', zero=True)
    with connection(server, data.get('target_ca_pem', '')) as api:
        rows = api.api('GET', f'/governance/groups?parent_id={parent}&after_id={after}')
        require(isinstance(rows, list) and len(rows) <= 100, '目标群组分页格式不兼容', 'capability')
        require(all(type(row.get('id')) is int and row['id'] > after
                    and row.get('parent_id', 0) == parent for row in rows), '目标群组分页身份无效', 'capability')
        return {'groups': [public_group(row) for row in rows],
                'next_id': max(row['id'] for row in rows) if len(rows) == 100 else 0}


def paths(server, data):
    ident = positive_id(data.get('group_id'), '目标群组')
    names = data.get('names')
    require(isinstance(names, list) and 0 < len(names) <= 1000, '请选择 1 至 1000 个仓库')
    names = [repo_name(name) for name in names]
    with connection(server, data.get('target_ca_pem', '')) as api:
        group = api.selectable_group(ident)
        child_paths = api.child_group_paths(ident)
        result, seen = [], set()
        for name in names:
            path = group['full_path'] + '/' + name
            duplicate = name.casefold() in seen
            seen.add(name.casefold())
            existing = api.repository(group['full_path'], name, missing=True)
            occupied_group = path.casefold() in child_paths
            result.append({'name': name, 'path': path, 'available': not duplicate and existing is None and not occupied_group,
                           'message': '计划内仓库名称重复' if duplicate else
                               ('目标仓库已存在，不会覆盖' if existing else '该路径已被子群组占用' if occupied_group else '路径可用')})
        return {'group': public_group(group), 'repositories': result}


def create_group(server, data):
    """不自动重试不确定的远端写入；失败后须刷新列表确认。"""
    parent = positive_id(data.get('parent_id', 0), '父群组', zero=True)
    name = string(data.get('name'), '群组名称', 100)
    path = slug(data.get('path'))
    from .guards import target_guard
    with target_guard(server.id), connection(server, data.get('target_ca_pem', '')) as api:
        if parent:
            group = api.selectable_group(parent)
            require(group.get('abilities', {}).get('create_group'), '无权在此群组下新建子群组', 'authorization', 403)
        row = api.api('POST', '/governance/groups', {'parent_id': parent, 'name': name,
                                                  'path': path, 'visibility': 2})
        require(isinstance(row, dict) and type(row.get('id')) is int, '新建群组回执无法确认，请刷新列表核对', 'uncertain', 409)
        return public_group(api.selectable_group(row['id']))
