"""保存 Manager 专属目录与账号的 API 权限基线，并用真实角色变更反证。"""
import copy
import json
import shutil
import time

from .errors import CheckFailure, EnvironmentBlocked
from .host import write_json


def capture(lab):
    fixture = lab.state.get('manager_role_fixture') or {}
    if not fixture.get('directory_id') or not fixture.get('account_id'):
        raise CheckFailure('尚无本轮真实目录及账号夹具，不能采权限基线')
    directory = lab.manager_api('GET', '/access/directory').json()
    accounts = lab.manager_api('GET', '/access/accounts').json()['items']
    account = next((row for row in accounts if row['id'] == fixture['account_id']), None)
    if (directory.get('id') != fixture['directory_id'] or directory.get('name') != fixture['name']
            or not account or account.get('username') != 'labldap' or account.get('source') != 'ldap'):
        raise CheckFailure('目录或账号不属于已登记夹具，拒绝采集或操作')
    return {'目录': directory, '账号': account}


def baseline(lab, allow_fixture_refresh=False):
    actual = capture(lab)
    if (actual['目录']['enabled'] or actual['账号']['enabled']
            or actual['账号']['is_platform_admin'] or actual['账号']['grants']):
        raise CheckFailure('权限夹具未处于停用且无授权状态，拒绝建立基线')
    expected = lab.state['manager_canaries'].get('permissions')
    if expected and actual != expected:
        previous = copy.deepcopy(expected)
        if allow_fixture_refresh:
            # 只有本工具完整角色复验结束后，才接受配置保存导致的版本递增。
            old_version = previous['目录']['version']
            if actual['目录']['version'] > old_version:
                previous['目录']['version'] = actual['目录']['version']
        if actual != previous:
            raise CheckFailure('权限基线存在未解释差异，拒绝用新基线覆盖')
    path = lab.root / ('manager-permissions-baseline-' + str(time.time_ns()) + '.json')
    write_json(path, {'说明': '完整保留公开API返回的专属目录配置及账号业务权限字段，不含绑定密码',
                     '本工具角色复验后的版本更新': allow_fixture_refresh, '权限': actual})
    lab.state['manager_canaries']['permissions'] = actual
    lab.state['manager_canaries']['permissions_evidence'] = str(path)
    lab.save()
    return str(path)


def run(lab):
    from .manager_canaries import verify
    from .manager_checks import state
    lab.login()
    with lab.case('manager-partial', 'Manager 权限基线及停用账号角色字段的真实反证'):
        if shutil.disk_usage(lab.root).free < 6 * 1024**3:
            raise EnvironmentBlocked('权限反证只保存元数据，仍要求六 GiB 余量')
        original = state(lab)
        evidence = baseline(lab)
        verify(lab)
        before = capture(lab)
        account_id = before['账号']['id']
        result = {'说明': '只修改已停用专属账号的角色标志；始终不允许其登录，原审计保留',
                  '状态': '执行中', '之前权限基线': evidence, '检出': False}
        path = lab.root / ('manager-permissions-challenge-' + str(time.time_ns()) + '.json')
        try:
            lab.manager_api('PATCH', '/access/accounts/' + str(account_id), json={'is_platform_admin': True})
            changed = capture(lab)
            if changed['账号']['enabled'] or not changed['账号']['is_platform_admin']:
                raise CheckFailure('真实反证没有命中已停用账号的角色标志')
            result['真实变更'] = changed
            try:
                verify(lab)
            except CheckFailure as exc:
                if str(exc) != 'Manager 目录配置或账号权限不同，详见独立基线':
                    raise
                result['检出'] = True
            else:
                raise CheckFailure('权限判定器未检出真实角色标志变更')
        finally:
            # 操作只针对事先确认的停用夹具，始终恢复原角色，不回滚数据库。
            lab.manager_api('PATCH', '/access/accounts/' + str(account_id), json={'is_platform_admin': False})
            after = capture(lab)
            result['之后权限'] = after
            result['原九类管理业务相同'] = state(lab) == original
            result['权限恢复相同'] = after == before
            result['状态'] = '通过' if result['检出'] and result['权限恢复相同'] and result['原九类管理业务相同'] else '失败'
            write_json(path, result)
            verify(lab)
        if result['状态'] != '通过':
            raise CheckFailure('权限反证没有完成检出或完整收尾')
