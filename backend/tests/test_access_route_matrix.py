"""从应用真实蓝图登记生成权限矩阵，逐接口执行匿名及无授权角色的拒绝检查。"""
import ast
import importlib
import re
import unittest
from pathlib import Path

from tests import test_organization_access as fixture
from access.admin import PLATFORM_ENDPOINTS, ACCOUNT_ENDPOINTS
from access.control import PUBLIC, MACHINE, PLATFORM, ORGANIZATION, endpoints
from access.models import ManagerAccount
from models import db


def register_all_routes(app):
    """复用生产入口的蓝图列表，测试不启动调度器或触碰部署数据。"""
    tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text())
    factory = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'create_app')
    names = {}
    for node in ast.walk(factory):
        if isinstance(node, ast.ImportFrom) and node.module and (node.module.startswith('routes.') or node.module == 'access.internal'):
            module = importlib.import_module(node.module)
            names.update({alias.asname or alias.name: getattr(module, alias.name) for alias in node.names})
    for node in ast.walk(factory):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'register_blueprint':
            blueprint = names[node.args[0].id]
            if blueprint.name not in app.blueprints:
                app.register_blueprint(blueprint, **{kw.arg: ast.literal_eval(kw.value) for kw in node.keywords})


def permission_kind(endpoint):
    if endpoint in PUBLIC: return '公开认证'
    if endpoint in MACHINE: return '服务身份'
    if endpoint in endpoints(PLATFORM) | PLATFORM_ENDPOINTS: return '平台管理员'
    if endpoint in endpoints(ORGANIZATION): return '平台或组织管理员'
    if endpoint in {'auth.logout'} | ACCOUNT_ENDPOINTS: return '本人账号'
    return '未登记，默认拒绝'


def matrix(app):
    return [(rule.rule, method, rule.endpoint, permission_kind(rule.endpoint))
            for rule in sorted(app.url_map.iter_rules(), key=lambda row: (row.rule, row.endpoint))
            if rule.rule.startswith('/api/') and rule.endpoint != 'unclassified'
            for method in sorted(rule.methods - {'HEAD', 'OPTIONS'})]


def render_matrix(app):
    rows = matrix(app)
    lines = ['# 管理接口权限矩阵', '',
        '本表由生产入口蓝图登记及统一权限清单生成。组织上下文使用绑定 ID；组织业务仍须按实际仓库稳定 ID、实例代次及资源快照再次核验。', '',
        f'共 {len(rows)} 个方法与路径组合。HEAD 继承 GET；OPTIONS 不代表业务授权。未登记接口默认拒绝。', '',
        '自动化逐项检查平台接口的组织管理员请求返回 403、匿名请求返回 401；组织接口的未授权目录账号返回 403 或 404，写请求携带有效 CSRF。公开认证与服务接口分别执行自身认证协议。', '',
        '| 路径 | 方法 | 端点 | 允许角色 | 数据范围 |',
        '| --- | --- | --- | --- | --- |']
    scopes = {'公开认证': '登录与公开认证源信息', '服务身份': '专用服务令牌、签名或组件协议核验',
              '平台管理员': '平台管理范围', '平台或组织管理员': '平台全部；组织仅当前授权绑定与实际资源归属',
              '本人账号': '当前登录账号', '未登记，默认拒绝': '禁止访问'}
    lines += [f'| `{path}` | {method} | `{endpoint}` | {role} | {scopes[role]} |' for path, method, endpoint, role in rows]
    return '\n'.join(lines) + '\n'


class AccessRouteMatrixTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.OrganizationAccessTest()
        self.fixture.setUp()
        self.app = self.fixture.app
        register_all_routes(self.app)
        with self.app.app_context():
            source_id = db.session.get(ManagerAccount, self.fixture.account_id).source_id
            account = ManagerAccount(source_id=source_id, subject='entryUUID:00000000-0000-0000-0000-000000000099',
                                     username='unassigned', display_name='未授权目录账号')
            db.session.add(account); db.session.commit(); account_id = account.id
        self.unassigned = self.fixture.login_as(account_id)
        self.anonymous = self.app.test_client()
        self.anonymous.access_csrf = self.anonymous.get('/api/session', base_url='https://localhost').json['csrf_token']

    def tearDown(self):
        self.fixture.tearDown()
        self.fixture.doCleanups()

    def call(self, client, path, method, binding=None):
        values = {'job': 'a'*32, 'name': 'test-user', 'filename': 'test-file'}
        path = re.sub(r'<(?:[^:>]+:)?([^>]+)>', lambda match: values.get(match.group(1), '1'), path)
        headers = {'X-CSRF-Token': client.access_csrf}
        if binding: headers['X-Organization-ID'] = str(binding)
        return client.open(path, method=method, json={} if method not in ('GET', 'HEAD') else None,
                           headers=headers, base_url='https://localhost')

    def test_every_registered_endpoint_is_explicit_and_has_matrix_row(self):
        rows = matrix(self.app)
        self.assertGreater(len(rows), 150)
        self.assertEqual([row for row in rows if row[3] == '未登记，默认拒绝'], [])

    def test_every_platform_method_rejects_organization_and_anonymous_users(self):
        for path, method, endpoint, role in matrix(self.app):
            if role != '平台管理员': continue
            with self.subTest(端点=endpoint, 方法=method):
                self.assertEqual(self.call(self.fixture.client, path, method, 1).status_code, 403)
                self.assertEqual(self.call(self.anonymous, path, method).status_code, 401)

    def test_every_organization_method_rejects_unassigned_and_anonymous_users(self):
        for path, method, endpoint, role in matrix(self.app):
            if role != '平台或组织管理员': continue
            with self.subTest(端点=endpoint, 方法=method):
                self.assertIn(self.call(self.unassigned, path, method).status_code, (403, 404))
                self.assertEqual(self.call(self.anonymous, path, method).status_code, 401)


if __name__ == '__main__':
    unittest.main()
