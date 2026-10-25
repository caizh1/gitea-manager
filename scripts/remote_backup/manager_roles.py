"""经真实 TLS、LDAP 和 Gitea 目录验证 Manager 组织角色，不创建业务任务。"""
import json
import shutil
import time
from pathlib import Path

import requests

from .driver import CheckFailure
from .errors import EnvironmentBlocked
from .manager_checks import denial, session_client, state
from .network import control
from .scenarios import write_json


def request(client, base, method, path, payload=None):
    response = client.request(method, base + path, json=payload, timeout=30)
    if response.status_code not in (200, 201, 204):
        raise CheckFailure('角色准备或允许操作失败：%s %s，HTTP=%s' % (method, path, response.status_code))
    return response.json()


def login(lab, client, base, source='local'):
    public = request(client, base, 'GET', '/session')
    client.headers['X-CSRF-Token'] = public['csrf_token']
    payload = {'source': source, 'username': 'admin' if source == 'local' else 'labldap',
               'password': lab.state['manager_password'] if source == 'local' else lab.state['integrations']['ldap_password']}
    actual = request(client, base, 'POST', '/login', payload)
    if not actual.get('authenticated') or actual['user']['is_platform_admin'] != (source == 'local'):
        raise CheckFailure('真实登录身份或平台角色不正确')
    client.headers['X-CSRF-Token'] = actual['csrf_token']
    return actual


def run(lab):
    from .manager_canaries import verify
    lab.login()
    with lab.case('manager-partial', '真实 TLS 与 LDAP 的组织角色、备份权限拒绝和撤权'):
        if shutil.disk_usage(lab.root).free < 6 * 1024**3:
            raise EnvironmentBlocked('无归档角色验收仍要求六 GiB 余量，未修改权限')
        before = state(lab)
        fixture = lab.state.get('manager_role_fixture')
        original = lab.manager_api('GET', '/access/directory').json()
        if original.get('id') and (not fixture or original['id'] != fixture.get('directory_id')):
            raise EnvironmentBlocked('已有非本夹具的 Manager 目录，拒绝覆盖')
        if not fixture:
            fixture = {'name': '跨网段角色验收目录-' + str(time.time_ns())}
            lab.state['manager_role_fixture'] = fixture
            lab.save()
        stamp = str(time.time_ns())
        path = lab.root / ('manager-roles-' + stamp + '.json')
        result = {'说明': '真实目录登录及组织授权；不模拟身份、不插入会话、不请求实际备份恢复',
                  '状态': '执行中', '断言': [], '开始UTC': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        write_json(lab.root / ('manager-roles-before-' + stamp + '.json'), before)
        administrator, member = session_client(), session_client()
        base = None
        config = {'name': fixture['name'], 'type': 'binddn', 'enabled': True,
            'host': lab.state['addresses']['integration'], 'port': 1389, 'security_protocol': 'unencrypted',
            'bind_dn': 'cn=admin,dc=lab,dc=invalid', 'bind_password': lab.state['integrations']['ldap_password'],
            'user_base': 'dc=lab,dc=invalid', 'filter': '(uid=%s)',
            'attribute_username': 'uid', 'attribute_name': 'cn', 'attribute_mail': 'mail'}
        def check(description):
            result['断言'].append({'说明': description, '通过': True})
            write_json(path, result)
        try:
            tls = control(lab, 'manager-role-tls', enabled=True)['证据']
            base = tls['URL']
            certificate = Path(tls['CA文件'])
            if base != 'https://backend:5443/api' or not certificate.is_relative_to(lab.root):
                raise CheckFailure('角色 TLS 地址或 CA 不属于固定本轮入口')
            administrator.verify = member.verify = str(certificate)
            deadline = time.monotonic() + 30
            while True:
                try:
                    administrator.get(base + '/session', timeout=2).raise_for_status()
                    break
                except requests.RequestException:
                    if time.monotonic() > deadline:
                        raise CheckFailure('真实 TLS 入口未就绪') from None
                    time.sleep(0.2)
            with session_client() as untrusted:
                try:
                    untrusted.get(base + '/session', timeout=3)
                except requests.exceptions.SSLError:
                    pass
                else:
                    raise CheckFailure('未信任的测试 CA 没有被拒绝')
            check('实际 TLS 正向校验成功，未信任 CA 明确拒绝')
            login(lab, administrator, base)
            for endpoint in ('/directory/test-connection', '/directory/test-login'):
                payload = {'configuration': config, 'username': 'labldap', 'password': config['bind_password']}
                value = request(administrator, base, 'POST', '/access' + endpoint, payload)
                if value.get('ok') is not True:
                    raise CheckFailure('实际目录连接或用户认证未成功')
            check('真实 OpenLDAP 连接与用户绑定认证通过')
            saved = request(administrator, base, 'PUT', '/access/directory', config)
            fixture['directory_id'] = saved['id']; lab.save()
            accounts = request(administrator, base, 'GET', '/access/accounts')['items']
            matching = [x for x in accounts if x['username'] == 'labldap' and x['source'] == 'ldap']
            if matching and matching[0]['id'] != fixture.get('account_id'):
                raise CheckFailure('发现非本夹具的同名目录账号，拒绝修改')
            account = request(administrator, base, 'POST', '/access/directory/users', {'username': 'labldap'})
            fixture['account_id'] = account['id']; lab.save()
            if account['source'] != 'ldap' or account['is_platform_admin'] or account['grants']:
                raise CheckFailure('新目录账号已有非预期权限')
            request(administrator, base, 'PATCH', '/access/accounts/' + str(account['id']), {'enabled': True})
            actual = login(lab, member, base, 'ldap')
            if actual['organizations'] or 'backups' in actual['capabilities']:
                raise CheckFailure('未授权目录账号获得组织或备份能力')
            denial(member.get(base + '/servers', timeout=10), 403, '请先选择已授权组织')
            denial(member.get(base + '/backups', timeout=10), 403, '此功能仅平台管理员可用')
            check('真实未授权目录账号登录后无组织、无备份能力且接口明确拒绝')
            bindings = {}
            for node in ('a', 'b'):
                bindings[node] = request(administrator, base, 'POST', '/access/organizations',
                    {'server_id': lab.state['nodes'][node]['server_id']})['items']
                if not bindings[node]:
                    raise CheckFailure('真实 Gitea 没有可用于非空组织隔离验收的目录')
            selected, foreign = bindings['a'][0], bindings['b'][0]
            fixture['binding_id'] = selected['binding_id']; lab.save()
            request(administrator, base, 'POST', '/access/grants',
                {'account_id': account['id'], 'binding_id': selected['binding_id']})
            member.headers['X-Organization-ID'] = str(selected['binding_id'])
            servers = request(member, base, 'GET', '/servers')
            if (len(servers) != 1 or servers[0]['id'] != lab.state['nodes']['a']['server_id']
                    or set(servers[0]) != {'id', 'name', 'version'}):
                raise CheckFailure('组织服务器投影泄露连接配置或其他实例')
            organizations = request(member, base, 'GET', '/repository-policies/organizations?server_id=' + str(selected['server_id']))
            if len(organizations['items']) != 1 or organizations['items'][0]['id'] != selected['organization_id']:
                raise CheckFailure('组织可见目录不准确')
            check('真实授权只显示对应组织及单实例的三字段投影')
            success_id = next(x['id'] for x in before['/backups'] if x['status'] == 'success')
            protected = [('GET', '/backups', None), ('GET', '/restore-tasks', None),
                ('GET', '/schedules', None), ('GET', '/settings', None), ('GET', '/access/accounts', None),
                ('GET', '/backups/%s/download' % success_id, None),
                ('POST', '/backups', {'source_server_id': selected['server_id']}),
                ('POST', '/restore', {'backup_id': success_id,
                                      'target_server_id': selected['server_id']}),
                ('POST', '/settings', before['/settings'])]
            for method, endpoint, payload in protected:
                denial(member.request(method, base + endpoint, json=payload, timeout=10), 403, '此功能仅平台管理员可用')
            check('组织角色九类平台读取、敏感归档下载和真实备份恢复提交均在权限层拒绝，未创建任务')
            denial(member.get(base + '/servers', headers={'X-Organization-ID': str(foreign['binding_id'])}, timeout=10),
                   404, '组织不存在或无权访问')
            denial(member.get(base + '/servers?server_id=' + str(foreign['server_id']), timeout=10),
                   404, '资源不存在或无权访问')
            check('越权组织编号及混入另一实例编号均明确拒绝')
            request(administrator, base, 'DELETE', '/access/grants/%s/%s' % (account['id'], selected['binding_id']))
            denial(member.get(base + '/servers', timeout=10), 404, '组织不存在或无权访问')
            check('撤销授权后原会话及原组织请求立即失效')
            request(administrator, base, 'PATCH', '/access/accounts/' + str(account['id']), {'enabled': False})
            denial(member.get(base + '/backups', timeout=10), 401, '请先登录')
            member.headers.pop('X-Organization-ID', None)
            public = member.get(base + '/session', timeout=10).json()
            member.headers['X-CSRF-Token'] = public['csrf_token']
            denial(member.post(base + '/login', json={'source': 'ldap', 'username': 'labldap',
                'password': lab.state['integrations']['ldap_password']}, timeout=15), 401, '用户名或密码不正确')
            check('停用后原 Cookie 失效，真实目录密码也不能重新登录')
            verify(lab, include_permissions=False)
            after = state(lab)
            write_json(lab.root / ('manager-roles-after-' + stamp + '.json'), after)
            if after != before:
                raise CheckFailure('组织权限检查改变了原九类管理业务记录')
            check('原九类管理业务逐字段相同，保留记录及 SSH 实例身份一致')
            result['状态'] = '通过'
        except Exception as exc:
            result.update(状态='失败', 原因=lab.scrub(str(exc)))
            raise
        finally:
            try:
                if base and fixture.get('directory_id'):
                    if fixture.get('account_id'):
                        request(administrator, base, 'PATCH', '/access/accounts/' + str(fixture['account_id']),
                                {'enabled': False, 'is_platform_admin': False})
                    if fixture.get('account_id') and fixture.get('binding_id'):
                        request(administrator, base, 'DELETE', '/access/grants/%s/%s' %
                            (fixture['account_id'], fixture['binding_id']))
                    request(administrator, base, 'PUT', '/access/directory', dict(config, enabled=False))
                    result['专属目录及账号已停用'] = True
            except Exception as exc:
                result.update(状态='失败', 目录收尾异常=lab.scrub(str(exc)))
                raise
            finally:
                try:
                    control(lab, 'manager-role-tls', enabled=False)
                    result['TLS入口已停止保留'] = True
                    if result['状态'] == '通过':
                        from .manager_permissions import baseline
                        result['收尾权限基线'] = baseline(lab, allow_fixture_refresh=True)
                except Exception as exc:
                    result.update(状态='失败', 入口收尾异常=lab.scrub(str(exc)))
                    raise
                finally:
                    administrator.close(); member.close()
                    result['结束UTC'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                    write_json(path, result)
