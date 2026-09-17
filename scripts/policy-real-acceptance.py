#!/usr/bin/env python3
"""在本机隔离样本上验证真实推送、强推、审批和状态检查；不会连接其他实例。"""
import base64
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor, urlopen

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'work' / '本地环境'
VALUES = dict(line.split('=', 1) for line in (STATE / '.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
FIXTURES = json.loads((STATE / '真实验收样本.json').read_text())
REPORT = {'说明': '真实 Gitea 1.26.4 本机隔离验收，不包含模拟测试或生产性能承诺', '检查': []}
OPENER = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
CSRF = ''
SERVER_ID = None
REPO_IDS = {item['名称'].split('/')[-1]: item['ID'] for item in FIXTURES['仓库']}


def record(name, detail):
    REPORT['检查'].append({'项目': name, '结果': '通过', '证据': detail})
    (STATE / '真实行为验收结果.json').write_text(json.dumps(REPORT, ensure_ascii=False, indent=2) + '\n')
    print(name + '：通过', flush=True)


def api(method, path, payload=None, user=None, manager=False, expected=(200, 201, 202, 204)):
    headers = {'Content-Type': 'application/json'}
    if manager:
        headers['X-Policy-CSRF'] = CSRF
    elif user:
        password = FIXTURES['用户'][user]['密码']
        headers['Authorization'] = 'Basic ' + base64.b64encode((user + ':' + password).encode()).decode()
    else:
        headers['Authorization'] = 'token ' + VALUES['GITEA_TOKEN']
    req = Request(('http://127.0.0.1:5480/api' if manager else 'http://127.0.0.1:3000/api/v1') + path,
                  method=method, headers=headers, data=json.dumps(payload).encode() if payload is not None else None)
    try:
        response = (OPENER.open if manager else urlopen)(req, timeout=45)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read()
        if response.code not in expected:
            raise AssertionError(f'{method} {path} 实际 HTTP {response.code}，预期 {expected}；{body.decode()[:350]}')
        return json.loads(body) if body else None


def manager(method, path, payload=None, **kwargs):
    return api(method, '/repository-policies' + path, payload, manager=True, **kwargs)


def wait_batch(batch_id, expected):
    start = time.monotonic()
    last = None
    while time.monotonic() - start < 1200:
        data = manager('GET', f'/batches/{batch_id}')
        batch = data['batch']
        status = (batch['status'], batch['progress_percent'])
        if status != last:
            print(f'批次 {batch_id}：{status[0]} {status[1]}%', flush=True); last = status
        if batch['status'] in expected:
            return data
        if batch['status'] in ('interrupted', 'completed_with_errors'):
            raise AssertionError(json.dumps(data, ensure_ascii=False)[:2500])
        time.sleep(2)
    raise AssertionError('批次等待超时')


def create_policy_template(name, rules):
    org = api('GET', '/orgs/policy-qa')
    scope = {'id': org['id'], 'name': org['name'], 'server_id': SERVER_ID}
    # 固定隔离样本明确选择账户和团队；线上交互须由管理员确认身份。
    verified = manager('POST', '/identities/verify', {'server_id': SERVER_ID,
        'organization': scope, 'rules': rules, 'confirm_names': True})
    return manager('POST', '/templates', {'server_id': SERVER_ID, 'name': name,
        'organization': scope, 'rules': verified['rules'], 'identities': verified['identities']})


def apply_rules(name, repo_ids, rules):
    template = create_policy_template(name, rules)
    batch = manager('POST', '/batches', {'template_id': template['id'], 'content_hash': template['content_hash'], 'repo_ids': repo_ids})
    preview = wait_batch(batch['id'], ('ready',))
    assert not preview['batch']['counts'].get('blocked'), preview['items']
    manager('POST', f'/batches/{batch["id"]}/apply', {})
    result = wait_batch(batch['id'], ('completed',))
    record(name + ' 经 Manager 预览应用回读', {'批次': batch['id'], '结果': result['batch']['counts']})
    return template


def git(args, directory, expected=True):
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT='0', GIT_ASKPASS=str(STATE / 'Git验收凭据助手.sh'),
               POLICY_GIT_USER='policy-developer', POLICY_GIT_PASSWORD=FIXTURES['用户']['policy-developer']['密码'])
    p = subprocess.run(['git', '-c', 'credential.helper=', *args], cwd=directory, env=env, text=True, capture_output=True)
    if (p.returncode == 0) != expected:
        raise AssertionError(f'Git {args[0]} 预期成功={expected}，退出码={p.returncode}，{p.stderr[-1300:]}')
    return (p.stdout + p.stderr).strip()


def clone(name):
    path = STATE / ('Git行为验收-' + name + '-' + str(time.time_ns()))
    git(['clone', 'http://127.0.0.1:3000/policy-qa/' + name + '.git', str(path)], ROOT)
    git(['config', 'user.name', 'policy-developer'], path)
    git(['config', 'user.email', 'policy-developer@example.invalid'], path)
    return path


def commit(path, content):
    (path / '验收样本.txt').write_text(content + '\n')
    git(['add', '验收样本.txt'], path)
    git(['commit', '-m', 'test: 验证仓库策略保护'], path)
    return git(['rev-parse', 'HEAD'], path)


def main():
    global CSRF, SERVER_ID
    api('POST', '/login', {'password': VALUES['MANAGER_PASSWORD']}, manager=True)
    CSRF = manager('GET', '/csrf')['token']
    SERVER_ID = next(s['id'] for s in manager('GET', '/servers') if s['name'] == '本地策略验收')
    version = api('GET', '/version')['version']
    assert version == '1.26.4'
    record('运行版本', version)
    org = api('GET', '/orgs/policy-qa')
    source = manager('GET', f'/repositories/{REPO_IDS["template-source"]}/rules?server_id={SERVER_ID}&org=policy-qa&org_id={org["id"]}')['rules']
    # 该批次由浏览器操作创建；脚本只读取结果，不替代页面提交。
    batches = manager('GET', f'/batches?server_id={SERVER_ID}')['items']
    standard = next(t for t in manager('GET', f'/templates?server_id={SERVER_ID}') if t['name'] == '研发保护标准' and t['organization'])
    bulk = next(b for b in batches if b['template_id'] == standard['id'] and b['total'] == 30 and b['submitted_at'] and b['status'] in ('apply_queued', 'applying', 'completed'))
    wait_batch(bulk['id'], ('completed',))
    fields = manager('GET', '/schema')['api_fields']
    expected_rule = {k: source[0][k] for k in fields}
    for number in range(1, 31):
        current = api('GET', f'/repos/policy-qa/batch-{number:02d}/branch_protections')
        actual = next(r for r in current if r['rule_name'] == 'main')
        assert {k: actual[k] for k in fields} == expected_rule, number
        if number == 1:
            archive = next(r for r in current if r['rule_name'] == 'archive/*')
            assert archive['priority'] == 9 and not archive['enable_push']
        else:
            assert len(current) == 1
    record('30 个真实仓库全部字段与未管理规则核验', {'批次': bulk['id'], '仓库数': 30, '每条规则字段数': len(fields), '错误': 0})
    apply_rules('真实验收：审批与 CI', [REPO_IDS['probe-pull']], source)
    apply_rules('真实验收：禁止强推', [REPO_IDS['probe-force']], [{'rule_name': 'main', 'priority': 1, 'enable_push': True, 'enable_force_push': False}])
    helper = STATE / 'Git验收凭据助手.sh'
    helper.write_text('#!/bin/sh\n# 仅从当前进程环境读取本地测试凭据，不落入 Git URL。\ncase "$1" in *Username*) printf "%s\\n" "$POLICY_GIT_USER";; *) printf "%s\\n" "$POLICY_GIT_PASSWORD";; esac\n')
    helper.chmod(0o700)
    path = clone('probe-pull')
    head = commit(path, '主分支直接推送应被拒绝 ' + str(time.time_ns()))
    rejected = git(['push', 'origin', 'HEAD:main'], path, expected=False)
    assert 'protected' in rejected.lower() or '保护' in rejected
    record('真实 Git 直接 Push 被拒绝', {'仓库': 'policy-qa/probe-pull', 'Git退出': '非零', '服务端保护拒绝': True})
    branch = '验收分支-' + str(time.time_ns())
    git(['push', 'origin', 'HEAD:refs/heads/' + branch], path)
    pr = api('POST', '/repos/policy-qa/probe-pull/pulls', {'title': '真实双人审批与 CI 验收', 'head': branch, 'base': 'main'}, user='policy-developer')
    endpoint = f'/repos/policy-qa/probe-pull/pulls/{pr["number"]}'
    def blocked(label):
        # 等待 Gitea 异步计算可合并状态，避免把暂未就绪误记为保护生效。
        for _ in range(30):
            state = api('GET', endpoint)
            if state.get('mergeable'):
                break
            time.sleep(0.2)
        assert state.get('mergeable'), 'PR 未通过基础可合并性检查'
        response = api('POST', endpoint + '/merge', {'do': 'merge', 'head_commit_id': head}, expected=(403, 405))
        assert not api('GET', endpoint)['merged']
        record(label, {'PR': pr['number'], '拒绝原因': response})
    blocked('零审批禁止合并')
    for index, user in enumerate(['policy-reviewer-one', 'policy-reviewer-two']):
        api('POST', endpoint + '/reviews', {'event': 'APPROVED', 'commit_id': head, 'body': '验收审批通过'}, user=user)
        if index == 0:
            blocked('一人审批仍禁止合并')
    blocked('两人审批但缺少 CI 仍禁止合并')
    status_path = '/repos/policy-qa/probe-pull/statuses/' + head
    api('POST', status_path, {'state': 'failure', 'context': 'policy/test', 'description': '真实验收：失败状态'})
    blocked('CI 失败禁止合并')
    api('POST', status_path, {'state': 'success', 'context': 'policy/test', 'description': '真实验收：成功状态'})
    api('POST', endpoint + '/merge', {'do': 'merge', 'head_commit_id': head}, expected=(200,))
    assert api('GET', endpoint)['merged']
    record('双人审批且 CI 成功允许合并', {'PR': pr['number'], '已合并': True})
    path = clone('probe-force')
    original = git(['rev-parse', 'HEAD'], path)
    commit(path, '允许正常提交 ' + str(time.time_ns()))
    git(['push', 'origin', 'HEAD:main'], path)
    record('允许直接 Push 的规则接受正常 Git 推送', 'policy-qa/probe-force')
    git(['reset', '--hard', original], path)
    rejected = git(['push', '--force', 'origin', 'HEAD:main'], path, expected=False)
    assert 'protected' in rejected.lower() or 'force' in rejected.lower()
    record('真实 Force Push 被拒绝', 'policy-qa/probe-force')
    apply_rules('真实验收：明确允许强推', [REPO_IDS['probe-force']], [{'rule_name': 'main', 'priority': 1, 'enable_push': True, 'enable_force_push': True}])
    git(['push', '--force', 'origin', 'HEAD:main'], path)
    record('明确启用 Force Push 后真实强推成功', 'policy-qa/probe-force')
    print('真实行为验收全部通过。报告：' + str(STATE / '真实行为验收结果.json'), flush=True)


if __name__ == '__main__':
    main()
