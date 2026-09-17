#!/usr/bin/env python3
"""仅为本机隔离 Gitea 创建可重复的真实验收样本。"""
import json
import os
import secrets
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'work' / '本地环境'
VALUES = dict(line.split('=', 1) for line in (STATE / '.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
OUTPUT = STATE / '真实验收样本.json'


def gitea(method, path, payload=None, expected=(200, 201, 204), token=None):
    request = Request('http://127.0.0.1:3000/api/v1' + path, method=method,
        data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
        headers={'Authorization': 'token ' + (token or VALUES['GITEA_TOKEN']), 'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=45) as response:
            data = response.read()
            return json.loads(data) if data else None
    except HTTPError as exc:
        if exc.code in expected:
            return None
        raise RuntimeError(f'验收请求失败：{method} {path}，HTTP {exc.code}：{exc.read().decode()[:500]}') from None


def save(data):
    OUTPUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    os.chmod(OUTPUT, 0o600)


def main():
    data = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {'说明': '仅用于本机隔离环境，包含测试账号凭据，请勿提交或分发', '用户': {}, '仓库': []}
    for username in ['policy-developer', 'policy-reviewer-one', 'policy-reviewer-two', 'policy-outsider']:
        if username not in data['用户']:
            data['用户'][username] = {'密码': secrets.token_urlsafe(24)}; save(data)
        if not gitea('GET', '/users/' + username, expected=(404,)):
            gitea('POST', '/admin/users', {'username': username, 'email': username + '@example.invalid',
                'password': data['用户'][username]['密码'], 'must_change_password': False, 'send_notify': False})
    for org in ['policy-qa', 'policy-qa-other']:
        if not gitea('GET', '/orgs/' + org, expected=(404,)):
            gitea('POST', '/orgs', {'username': org, 'full_name': '策略真实验收组织', 'visibility': 'private'})
    existing = gitea('GET', '/orgs/policy-qa/repos?limit=100')
    names = [f'batch-{i:02d}' for i in range(1, 31)] + ['template-source', 'probe-force', 'probe-pull', 'probe-archived', 'probe-conflict']
    by_name = {repo['name']: repo for repo in existing}
    for name in names:
        if name not in by_name:
            by_name[name] = gitea('POST', '/orgs/policy-qa/repos', {'name': name, 'private': True, 'auto_init': True, 'default_branch': 'main', 'description': '仓库策略中心真实验收样本'})
    data['仓库'] = [{'ID': by_name[name]['id'], '名称': by_name[name]['full_name']} for name in names]
    teams = gitea('GET', '/orgs/policy-qa/teams')
    team = next((t for t in teams if t['name'] == 'reviewers'), None)
    if not team:
        team = gitea('POST', '/orgs/policy-qa/teams', {'name': 'reviewers', 'description': '真实审批验收团队', 'permission': 'write', 'units': ['repo.code', 'repo.pulls'], 'includes_all_repositories': True})
    for username in ['policy-developer', 'policy-reviewer-one', 'policy-reviewer-two']:
        gitea('PUT', f'/teams/{team["id"]}/members/{username}')
    data['审批团队'] = {'ID': team['id'], '名称': 'reviewers', '组织': 'policy-qa'}
    other = gitea('GET', '/repos/policy-qa-other/probe-other', expected=(404,))
    if not other:
        other = gitea('POST', '/orgs/policy-qa-other/repos', {'name': 'probe-other', 'private': True, 'auto_init': True, 'default_branch': 'main'})
    data['其他组织仓库'] = {'ID': other['id'], '名称': other['full_name']}
    rule = {'rule_name': 'main', 'priority': 1, 'enable_push': False, 'enable_force_push': False,
            'required_approvals': 2, 'enable_approvals_whitelist': True,
            'approvals_whitelist_username': ['policy-reviewer-one', 'policy-reviewer-two'],
            'approvals_whitelist_teams': ['reviewers'], 'enable_status_check': True,
            'status_check_contexts': ['policy/test'], 'block_admin_merge_override': True,
            'block_on_rejected_reviews': True, 'block_on_official_review_requests': True, 'dismiss_stale_approvals': True}
    for full_name, rules in [('policy-qa/template-source', [rule]), ('policy-qa/batch-01', [{'rule_name': 'archive/*', 'priority': 9, 'enable_push': False}]),
                             ('policy-qa/probe-conflict', [{'rule_name': '*', 'priority': 1, 'enable_push': True}])]:
        current = gitea('GET', '/repos/' + full_name + '/branch_protections')
        current_names = {r['rule_name'] for r in current}
        for item in rules:
            if item['rule_name'] not in current_names:
                gitea('POST', '/repos/' + full_name + '/branch_protections', item)
    gitea('PATCH', '/repos/policy-qa/probe-archived', {'archived': True})
    save(data)
    print(f'真实验收样本已准备：{len(names)} 个主组织仓库、1 个其他组织仓库、4 个测试账号；凭据保存在本地受限文件。')


if __name__ == '__main__':
    main()
