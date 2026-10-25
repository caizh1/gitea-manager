"""独立保留 Manager 的非恢复业务记录，验证跨实例恢复不误改管理数据。"""
import json

from .driver import CheckFailure


def seed(lab):
    if lab.state.get('manager_canaries'):
        return
    a, b = (lab.state['nodes'][node]['server_id'] for node in ('a', 'b'))
    schedule = lab.manager_api('POST', '/schedules', json={'name': '保留的禁用调度记录', 'enabled': False,
        'source_server_id': a, 'target_ids': [b], 'schedule_hour': 1, 'schedule_minute': 17}).json()
    rule = lab.manager_api('POST', '/commit-rules', json={'name': '未应用的保留规则', 'enabled': False,
        'server_id': a, 'pattern': '^KEEP-', 'reject_message': '仅用于管理数据保留检查'}).json()
    lab.state['manager_canaries'] = {'schedule': schedule, 'rule': rule,
        'host_ip': lab.manager_api('GET', '/settings').json()}
    lab.save()


def verify(lab, include_permissions=True):
    expected = lab.state.get('manager_canaries')
    if not expected:
        raise CheckFailure('尚未建立 Manager 保留记录，不能声称已验证其他管理业务')
    schedules = lab.manager_api('GET', '/schedules').json()
    current = next((item for item in schedules if item['id'] == expected['schedule']['id']), None)
    if current != expected['schedule']:
        raise CheckFailure('Gitea 恢复修改了 Manager 的保留调度记录')
    rules = lab.manager_api('GET', '/commit-rules').json()['rules']
    current = next((item for item in rules if item['id'] == expected['rule']['id']), None)
    if current != expected['rule']:
        raise CheckFailure('Gitea 恢复修改了 Manager 的保留门禁规则')
    if lab.manager_api('GET', '/settings').json() != expected['host_ip']:
        raise CheckFailure('Gitea 恢复修改了 Manager 系统设置')
    session = lab.manager_api('GET', '/session').json()
    if not session.get('authenticated'):
        raise CheckFailure('恢复后的 Manager 会话不可用')
    for node in ('a', 'b'):
        server = lab.manager_api('GET', '/servers/%s' % lab.state['nodes'][node]['server_id']).json()
        if server['host'] != lab.state['addresses'][node] or server['connection_mode'] != 'ssh':
            raise CheckFailure('Manager 目标身份被误改')
    if include_permissions and expected.get('permissions'):
        from .manager_permissions import capture
        if capture(lab) != expected['permissions']:
            raise CheckFailure('Manager 目录配置或账号权限不同，详见独立基线')
