"""在专属 Manager 上真实核对会话边界、历史失败和告警；不创建恢复任务。"""
import copy
import hashlib
import json
import shutil
import time

import requests

from .errors import CheckFailure, EnvironmentBlocked


READ_PATHS = ('/servers', '/backups', '/restore-tasks', '/schedules', '/settings',
              '/commit-rules', '/mirrors', '/alerts', '/alerts/summary')


def denial(response, status, reason):
    """只有正确拒绝原因才算通过，不能把网络或其他保护的错误算成权限验证。"""
    try:
        value = response.json()
    except ValueError:
        value = None
    if (response.status_code != status or not isinstance(value, dict)
            or value.get('error') != reason):
        raise CheckFailure('管理接口没有给出预期身份或安全令牌拒绝，HTTP=%s' % response.status_code)


def session_client():
    client = requests.Session()
    client.trust_env = False
    return client


def login(lab, client):
    before = client.get(lab.manager_url + '/session', timeout=10).json()
    if before.get('authenticated') or not before.get('csrf_token'):
        raise CheckFailure('独立登录客户端不是未登录状态，或缺少安全令牌')
    response = client.post(lab.manager_url + '/login', json={'source': 'local', 'username': 'admin',
        'password': lab.state['manager_password']}, headers={'X-CSRF-Token': before['csrf_token']}, timeout=15)
    response.raise_for_status()
    actual = response.json()
    if (not actual.get('authenticated') or not actual.get('user', {}).get('is_platform_admin')
            or 'backups' not in actual.get('capabilities', ())
            or not actual.get('csrf_token') or actual['csrf_token'] == before['csrf_token']):
        raise CheckFailure('真实管理员登录、能力或会话安全令牌轮换不符合预期')
    client.headers['X-CSRF-Token'] = actual['csrf_token']
    return before['csrf_token']


def state(lab):
    value = {path: lab.manager_api('GET', path).json() for path in READ_PATHS}
    if any(row['status'] in ('running', 'pending') for path in ('/backups', '/restore-tasks') for row in value[path]):
        raise CheckFailure('存在运行中的备份或恢复，不能执行静态管理历史核对')
    return value


def failed_history(lab, baseline):
    """现有真实失败必须与任务、步骤、实例和告警逐项对应。"""
    failed = [row for row in baseline['/backups'] if row['status'] == 'failed' and row.get('diagnostic_id')]
    if not failed:
        raise CheckFailure('没有真实失败备份，不可用空集合声称告警检查通过')
    task = failed[0]
    if not task.get('error_msg') or not task.get('completed_at'):
        raise CheckFailure('失败备份没有明确原因或终止时间')
    steps = lab.manager_api('GET', '/backups/%s/steps' % task['id']).json()
    if (not steps or not any(step.get('status') == 'failed' for step in steps)
            or any(step.get('backup_id') != task['id'] for step in steps)
            or any(step.get('status') == 'running' for step in steps)):
        raise CheckFailure('失败任务步骤缺失、身份错误或仍显示运行')
    matches = [row for row in baseline['/alerts'] if row['alert_type'] == 'backup_failed'
               and row['source_id'] == task['id'] and row['server_id'] == task['source_server_id']]
    if len(matches) != 1 or matches[0]['message'] != task['error_msg'][:2000]:
        raise CheckFailure('真实失败备份与告警的身份或原因没有唯一对应')
    active = [row for row in baseline['/alerts'] if row['status'] == 'active']
    summary = baseline['/alerts/summary']
    if (len(baseline['/alerts']) >= 200 or summary['active_count'] != len(active)
            or (summary['latest_alert'] != active[0] if active else summary['latest_alert'] is not None)):
        raise CheckFailure('告警摘要与完整可见列表不一致，或超出列表上限尚未独立核对')
    return {'备份ID': task['id'], '告警ID': matches[0]['id'], '失败步骤数': sum(row['status'] == 'failed' for row in steps),
            '步骤数': len(steps), '可见告警数': len(baseline['/alerts']), '活动告警数': len(active)}


def run(lab):
    from .scenarios import write_json
    from .manager_canaries import verify
    lab.login()
    with lab.case('manager-partial', '真实管理会话、访问边界、失败步骤和告警一致性'):
        if shutil.disk_usage(lab.root).free < 6 * 1024**3:
            raise EnvironmentBlocked('无归档副本的管理检查仍要求六 GiB 余量，空间不足未操作')
        stamp = str(time.time_ns())
        path = lab.root / ('manager-checks-' + stamp + '.json')
        checks = []
        result = {'说明': '只覆盖专属 Manager 的真实会话和现有失败历史；未验证组织角色、运行期页面及全部管理功能',
                  '断言': checks, '状态': '执行中', '开始时间UTC': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        before = state(lab)
        write_json(lab.root / ('manager-before-' + stamp + '.json'), before)
        try:
            with session_client() as anonymous:
                public = anonymous.get(lab.manager_url + '/session', timeout=10)
                value = public.json()
                if (value.get('authenticated') or value.get('user') is not None
                        or value.get('capabilities') or not value.get('csrf_token')):
                    raise CheckFailure('匿名会话包含已认证身份或能力，或未提供登录安全令牌')
                for endpoint in READ_PATHS:
                    response = anonymous.get(lab.manager_url + endpoint, timeout=10)
                    denial(response, 401, '请先登录')
                    checks.append({'说明': '匿名读取明确拒绝：' + endpoint, '通过': True})
                payload = before['/settings']
                response = anonymous.post(lab.manager_url + '/settings', json=payload, timeout=10)
                denial(response, 403, '安全令牌无效，请刷新页面')
                response = anonymous.post(lab.manager_url + '/settings', json=payload,
                    headers={'X-CSRF-Token': value['csrf_token']}, timeout=10)
                denial(response, 401, '请先登录')
                checks.append({'说明': '匿名写入分别命中安全令牌与身份拒绝', '通过': True})
            with session_client() as administrator, session_client() as replay:
                old_csrf = login(lab, administrator)
                checks.append({'说明': '真实管理员登录、能力与安全令牌轮换', '通过': True})
                for supplied in ('', 'invalid-remote-test-csrf', old_csrf):
                    response = administrator.post(lab.manager_url + '/settings', json=before['/settings'],
                        headers={'X-CSRF-Token': supplied}, timeout=10)
                    denial(response, 403, '安全令牌无效，请刷新页面')
                checks.append({'说明': '缺失、伪造及登录前安全令牌均拒绝写入', '通过': True})
                for headers in ({'Origin': 'https://outside.invalid'}, {'Sec-Fetch-Site': 'cross-site'}):
                    for method in ('GET', 'POST'):
                        response = administrator.request(method, lab.manager_url + '/settings',
                            json=before['/settings'] if method == 'POST' else None, headers=headers, timeout=10)
                        denial(response, 403, '不允许跨站访问管理接口')
                checks.append({'说明': '已登录会话跨站读写均明确拒绝', '通过': True})
                for endpoint in READ_PATHS:
                    response = administrator.get(lab.manager_url + endpoint, timeout=10)
                    if (response.status_code != 200 or response.json() != before[endpoint]
                            or response.headers.get('Cache-Control') != 'no-store'
                            or 'Cookie' not in response.headers.get('Vary', '')):
                        raise CheckFailure('管理员刷新后业务数据或缓存保护不一致：' + endpoint)
                checks.append({'说明': '管理员实际读取和重复刷新九类接口，历史与禁缓存保持', '通过': True})
                replay.cookies = copy.deepcopy(administrator.cookies)
                replay.headers.update(administrator.headers)
                response = administrator.post(lab.manager_url + '/logout', timeout=10)
                response.raise_for_status()
                if response.json().get('authenticated') is not False:
                    raise CheckFailure('退出后会话仍为已登录')
                denial(replay.get(lab.manager_url + '/settings', timeout=10), 401, '请先登录')
                checks.append({'说明': '真实退出后旧 Cookie 与令牌重放被拒绝', '通过': True})
                login(lab, administrator)
                checks.append({'说明': '退出后重新登录成功', '通过': True})
            result['失败任务与告警'] = failed_history(lab, before)
            checks.append({'说明': '真实失败备份、步骤、实例、错误与告警摘要一致', '通过': True})
            verify(lab)
            after = state(lab)
            write_json(lab.root / ('manager-after-' + stamp + '.json'), after)
            if after != before:
                raise CheckFailure('会话与负向请求改变了 Manager 业务历史')
            checks.append({'说明': '全部管理历史逐字段相同，保留记录与 SSH 实例身份正确', '通过': True})
            result.update(状态='通过', 前后管理数据SHA256=hashlib.sha256(
                json.dumps(before, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
        except Exception as exc:
            result.update(状态='失败', 原因=lab.scrub(str(exc)))
            raise
        finally:
            result['结束时间UTC'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            write_json(path, result)
