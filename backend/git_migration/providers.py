"""GitLab 与开源 Gitea 的认证及完整分页发现，不修改来源业务数据。"""
import base64
import re
import time
from urllib.parse import urlsplit

from svn_migration.common import MigrationError, require
from .transport import SourceHTTPError


def version_tuple(value):
    require(isinstance(value, str), '来源未提供版本号', 'source_format')
    match = re.match(r'^v?(\d+)\.(\d+)\.(\d+)(?:[-+.]|$)', value)
    require(match is not None, '来源版本格式无法识别，不能推定兼容', 'version')
    return tuple(int(v) for v in match.groups())


class Provider:
    def __init__(self, transport, kind, username, secret, auth_method='password'):
        require(kind in ('gitlab', 'gitea'), '请选择 GitLab 或开源 Gitea')
        require(auth_method in ('password', 'token'), '来源认证方式无效')
        self.http, self.kind = transport, kind
        self.username, self.secret, self.auth_method = username, secret, auth_method
        self.access_token = ''
        self.refresh_token = ''
        self.expires_at = 0.0
        self.actor = None
        self.version = ''
        self.schema = None

    def authenticate(self):
        if self.kind == 'gitlab' and self.auth_method == 'password':
            self._exchange()
        actor = self.get('/user')
        require(isinstance(actor, dict) and type(actor.get('id')) is int and actor['id'] > 0,
                '来源认证未返回有效用户身份', 'source_format')
        require(actor.get('is_admin') is True, '实例级迁移需要来源管理员；普通账号不能证明完整读取范围', 'authorization')
        require(actor.get('state', 'active') == 'active' and actor.get('active', True)
                and not actor.get('prohibit_login', False), '来源账号不可用', 'authorization')
        self.actor = {'id': actor['id'], 'username': actor.get('username') or actor.get('login')}
        require(isinstance(self.actor['username'], str) and self.actor['username'], '来源用户缺少登录名', 'source_format')
        info = self.get('/version')
        require(isinstance(info, dict), '来源版本接口格式无效', 'source_format')
        self.version = info.get('version', '')
        minimum = (16, 1, 0) if self.kind == 'gitlab' else (1, 10, 3)
        require(version_tuple(self.version) >= minimum, '来源版本低于本期规划下限', 'version')
        return {'kind': self.kind, 'version': self.version, 'actor': self.actor,
                'edition': '企业版' if self.kind == 'gitlab' and info.get('enterprise') is True else
                           '未确认许可等级' if self.kind == 'gitlab' else '开源 Gitea',
                'compatibility': '仅已确认认证与版本下限，尚未证明资料完整可迁移'}

    def _exchange(self):
        data = ({'grant_type': 'refresh_token', 'refresh_token': self.refresh_token}
                if self.refresh_token else {'grant_type': 'password', 'username': self.username,
                                           'password': self.secret, 'scope': 'read_api read_repository'})
        try:
            result, _ = self.http.request('POST', '/oauth/token', data=data)
        except SourceHTTPError as error:
            if error.http_status in (400, 401, 403, 404, 422):
                raise MigrationError('来源未接受密码授权或令牌续期；可能启用多因素认证或禁用密码授权，请改用访问令牌', 'token_required') from None
            raise
        require(isinstance(result, dict) and isinstance(result.get('access_token'), str)
                and result['access_token'] and isinstance(result.get('refresh_token', ''), str),
                '来源认证返回的令牌格式无效', 'source_format')
        seconds = result.get('expires_in')
        require(type(seconds) is int and seconds >= 60, '来源认证未提供有效令牌期限', 'source_format')
        self.access_token = result['access_token']
        self.refresh_token = result.get('refresh_token', '')
        self.expires_at = time.monotonic() + seconds - 30

    def credentials(self):
        if self.auth_method == 'token':
            prefix = 'Bearer ' if self.kind == 'gitlab' else 'token '
            return {'headers': {'Authorization': prefix + self.secret}}
        if self.kind == 'gitea':
            encoded = base64.b64encode((self.username + ':' + self.secret).encode('utf-8')).decode('ascii')
            return {'headers': {'Authorization': 'Basic ' + encoded}}
        if time.monotonic() >= self.expires_at:
            self._exchange()
        return {'headers': {'Authorization': 'Bearer ' + self.access_token}}

    def get(self, path):
        prefix = '/api/v4' if self.kind == 'gitlab' else '/api/v1'
        result, _ = self.http.request('GET', prefix + path, **self.credentials())
        return result

    def collection(self, path, **kwargs):
        prefix = '/api/v4' if self.kind == 'gitlab' else '/api/v1'
        pagination = {'size_key': 'per_page' if self.kind == 'gitlab' else 'limit'}
        if self.kind == 'gitea' and path not in ('/repos/search', '/admin/orgs'):
            pagination = self.gitea_pagination(path)
        return self.http.collection(prefix + path, credentials=self.credentials,
                                    **pagination, **kwargs)

    def gitea_pagination(self, path):
        # 旧版部分列表一次返回全集；不能按“第二页重复”猜测接口是否分页。
        if self.schema is None:
            self.schema, _ = self.http.request('GET', '/swagger.v1.json', **self.credentials())
            require(isinstance(self.schema, dict) and isinstance(self.schema.get('paths'), dict),
                    '来源接口描述无效，不能确认旧版分页约定', 'source_format')
        candidates = []
        for template, definition in self.schema['paths'].items():
            expression = re.sub(r'\\\{[^}]+\\\}', '[^/]+', re.escape(template))
            if re.fullmatch(expression, path) and isinstance(definition, dict) and 'get' in definition:
                candidates.append((template.count('{'), definition))
        if not candidates:
            # 未在描述文件出现的接口仍读取实际响应，404/403 将作为盘点缺口。
            return {'size_key': 'limit'}
        definition = min(candidates, key=lambda item: item[0])[1]
        parameters = definition.get('parameters', []) + definition['get'].get('parameters', [])
        names = set()
        for parameter in parameters:
            reference = parameter.get('$ref', '')
            if reference.startswith('#/parameters/'):
                parameter = self.schema.get('parameters', {}).get(reference.rsplit('/', 1)[-1], {})
            if parameter.get('in') == 'query':
                names.add(parameter.get('name'))
        return {'size_key': 'per_page' if 'per_page' in names else 'limit', 'paginated': 'page' in names}

    def discover(self):
        identity = self.authenticate()
        if self.kind == 'gitlab':
            rows = self.collection('/projects', params={'order_by': 'id', 'sort': 'asc', 'simple': 'false'})
            groups = self.collection('/groups', params={'all_available': 'true', 'order_by': 'id', 'sort': 'asc'})
        else:
            rows = self.collection('/repos/search', key='data', params={'sort': 'id', 'order': 'asc', 'private': 'true'})
            groups = self.collection('/admin/orgs')
        projects = []
        for row in rows:
            require(type(row.get('id')) is int and row['id'] > 0, '来源项目缺少有效数字 ID', 'source_format')
            if self.kind == 'gitlab':
                path, clone = row.get('path_with_namespace'), row.get('http_url_to_repo')
                namespace = row.get('namespace') or {}
                group_id = namespace.get('id') if namespace.get('kind') == 'group' else None
                visibility = row.get('visibility')
            else:
                path, clone = row.get('full_name'), row.get('clone_url')
                group_id = (row.get('owner') or {}).get('id')
                visibility = 'private' if row.get('private') else 'public'
            require(isinstance(path, str) and '/' in path and not any(part in ('', '.', '..') for part in path.split('/')),
                    '来源仓库缺少完整命名空间', 'source_format')
            require(isinstance(clone, str), '来源仓库缺少 Git HTTP 地址', 'source_format')
            parsed = urlsplit(clone)
            require(parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username
                    and not parsed.password and not parsed.query and not parsed.fragment,
                    '来源 Git 地址包含不支持的认证或地址格式', 'source_format')
            projects.append({'id': row['id'], 'path': path, 'name': row.get('name') or path.rsplit('/', 1)[1],
                             'clone_url': clone, 'group_id': group_id, 'visibility': visibility,
                             'archived': bool(row.get('archived')), 'default_branch': row.get('default_branch'),
                             'web_url': row.get('web_url') or row.get('html_url') or '',
                             'description': row.get('description') or ''})
        namespaces = [{'id': row['id'], 'path': row.get('full_path') or row.get('username') or row.get('name'),
                       'parent_id': row.get('parent_id'), 'name': row.get('name') or row.get('full_name')}
                      for row in groups]
        require(all(type(g['id']) is int and g['id'] > 0 for g in namespaces), '来源群组 ID 无效', 'source_format')
        require(all(isinstance(g['path'], str) and g['path'] for g in namespaces), '来源群组路径无效', 'source_format')
        return {**identity, 'projects': projects, 'groups': namespaces,
                'scope': '当前管理员通过来源列表接口可读取的对象；正式执行前仍须确认完整范围并停止写入',
                'migration_verified': False}
