#!/usr/bin/env python3
"""真实验证目标授权变化及令牌失效同时发生后，仍可恢复凭据并安全终止。"""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import time
import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('manager-state', 'target-state'):
        parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--expect-rejection', action='store_true', help='仅供修复前反例，保留待恢复现场')
    args = parser.parse_args()
    manager, target = (json.loads(p.read_text()) for p in (args.manager_state, args.target_state))
    for name, label in ((manager['name'] + '-worker', 'git-migration-manager'), (target['name'], 'git-migration-receiver')):
        info = json.loads(subprocess.check_output(['docker', 'inspect', name], text=True))[0]
        assert info['Config']['Labels'].get('io.gitea-manager.test') == label
    client, native = requests.Session(), requests.Session()
    client.trust_env = native.trust_env = False
    native.auth = ('migration-admin', target['password'])
    base = manager['base'] + '/api'

    def call(method, path, data=None):
        r = client.request(method, base + path, json=data, timeout=60)
        r.raise_for_status()
        return r.json()

    def api(method, path, data=None):
        r = native.request(method, target['base'] + '/api/v1' + path, json=data, timeout=60)
        r.raise_for_status()
        return r.json() if r.content else None

    def stopped():
        for _ in range(180):
            row = call('GET', path)
            if row['status'] == 'uncertain':
                return row
            assert row['status'] in ('queued', 'running', 'interrupted'), '没有得到预期的停止状态'
            time.sleep(0.5)
        raise RuntimeError('等待停止状态超时')

    client.headers['X-CSRF-Token'] = call('GET', '/session')['csrf_token']
    client.headers['X-CSRF-Token'] = call('POST', '/login', {'password': manager['password']})['csrf_token']
    path = '/git-migrations/runs/' + str(args.run_id)
    before = call('GET', path)
    assert before['status'] in ('interrupted', 'uncertain', 'quarantined')
    assert any(p.get('repo_id') for p in before['checkpoint'].values())
    original_plan = before['plan']
    group_id = original_plan['selection']['id']
    group = api('GET', '/governance/groups/' + str(group_id))
    reviewer = target['test_users']['migration-reviewer']['id']
    api('DELETE', '/governance/groups/' + str(group_id) + '/members/' + str(reviewer) + '?revision=' + str(group['revision']))
    tokens = [t for t in api('GET', '/users/migration-admin/tokens') if t.get('token_last_eight') == target['token'][-8:]]
    assert len(tokens) == 1, '不能唯一核对被撤销的旧令牌'
    token = api('POST', '/users/migration-admin/tokens', {'name': '授权变化恢复-' + secrets.token_hex(4), 'scopes': ['all']})['sha1']
    api('DELETE', '/users/migration-admin/tokens/' + str(tokens[0]['id']))
    # 私有回执先保存可用的新令牌；不会改写 Manager 中的旧凭据或执行授权。
    target['token'] = token
    args.target_state.write_text(json.dumps(target, ensure_ascii=False, indent=2) + '\n')
    args.target_state.chmod(0o600)
    call('POST', path + '/resume', {})
    rejected = stopped()
    assert '401' in rejected['error'], '必须证明原目标令牌确已失效'
    replacement = client.post(base + path + '/target-credentials', json={'token': token}, timeout=60)
    report = {'说明': '真实群组成员移除与原令牌撤销，不修改任务数据库或原执行授权', '任务': args.run_id,
              '旧令牌真实失效': True, '目标授权已变化': True, '更新凭据响应': replacement.status_code}
    output = args.manager_state.parent / ('scope-credential-' + str(args.run_id) + '.json')
    if args.expect_rejection:
        assert replacement.status_code == 409, '未复现预期的修复前缺陷'
        report['结果'] = '已复现修复前缺陷，现场保留'
    else:
        replacement.raise_for_status()
        assert call('GET', path)['plan'] == original_plan
        call('POST', path + '/resume', {})
        blocked = stopped()
        assert '目标群组' in blocked['error'], '恢复凭据不能绕过原授权快照'
        assert {k: v.get('repo_id') for k, v in blocked['checkpoint'].items()} == {k: v.get('repo_id') for k, v in before['checkpoint'].items()}
        assert not any(p.get('handed_off') for p in blocked['checkpoint'].values())
        report.update({'结果': '通过', '原执行授权保持不变': True, '授权变化仍阻止继续迁移': True, '等待原任务安全终止': True})
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print('授权变化与凭据恢复验收：' + str(output))


if __name__ == '__main__':
    main()
