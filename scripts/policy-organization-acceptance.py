#!/usr/bin/env python3
"""组织模板真实 Git 与时序边界验收；仅使用 local-env.py 的 3000/5480 隔离环境。

先执行 local-env.py init 与 policy-real-fixtures.py。每次创建独立的 org-center-behavior
测试仓库；凭据只从 work/本地环境读取。成功及失败证据保留在 work/组织策略模板。
"""
import importlib.util
import json
from pathlib import Path
import time

spec = importlib.util.spec_from_file_location('行为基线', Path(__file__).with_name('policy-real-acceptance.py'))
qa = importlib.util.module_from_spec(spec); spec.loader.exec_module(qa)
api, manager = qa.api, qa.manager
OUT = qa.ROOT / 'work/组织策略模板'
OUT.mkdir(parents=True, exist_ok=True)
REPORT = {'说明': '真实 Gitea 1.26.4，独立测试仓库；不是生产或人工效率验收', '检查': []}


def record(name, evidence):
    REPORT['检查'].append({'项目': name, '结果': '通过', '证据': evidence})
    (OUT / '真实行为边界.json').write_text(json.dumps(REPORT, ensure_ascii=False, indent=2) + '\n')
    print(name + '：通过', flush=True)


qa.record = record


def template(name, rules):
    org = api('GET', '/orgs/policy-qa')
    scope = {'id': org['id'], 'name': 'policy-qa', 'server_id': qa.SERVER_ID}
    verified = manager('POST', '/identities/verify', {'server_id': qa.SERVER_ID,
        'organization': scope, 'rules': rules, 'confirm_names': True})
    return manager('POST', '/templates', {'server_id': qa.SERVER_ID, 'name': name,
        'organization': scope, **verified})


def preview(tpl, repos):
    batch = manager('POST', '/batches', {'template_id': tpl['id'], 'content_hash': tpl['content_hash'], 'repo_ids': repos})
    return qa.wait_batch(batch['id'], ('ready',))


def main():
    api('POST', '/login', {'password': qa.VALUES['MANAGER_PASSWORD']}, manager=True)
    qa.CSRF = manager('GET', '/csrf')['token']
    qa.SERVER_ID = next(s['id'] for s in manager('GET', '/servers') if s['name'] == '本地策略验收')
    assert api('GET', '/version')['version'] == '1.26.4'
    suffix = str(time.time_ns())[-11:]
    names = {kind: 'org-center-behavior-' + kind + '-' + suffix for kind in ('pull', 'force', 'transfer', 'permission', 'rule-change', 'healthy')}
    repos = {}
    for kind, name in names.items():
        repos[kind] = api('POST', '/orgs/policy-qa/repos', {'name': name, 'private': True,
            'auto_init': True, 'default_branch': 'main', 'description': '组织策略中心固定行为验收样本'})
        api('PUT', f'/repos/policy-qa/{name}/collaborators/policy-developer', {'permission': 'write'})
    for user in ('policy-reviewer-one', 'policy-reviewer-two'):
        api('PUT', f'/repos/policy-qa/{names["pull"]}/collaborators/{user}', {'permission': 'read'})
    REPORT['样本仓库'] = {k: {'id': v['id'], '名称': v['full_name']} for k, v in repos.items()}
    helper = qa.STATE / 'Git验收凭据助手.sh'
    helper.write_text('#!/bin/sh\n# 仅从进程环境读取隔离测试凭据。\ncase "$1" in *Username*) printf "%s\\n" "$POLICY_GIT_USER";; *) printf "%s\\n" "$POLICY_GIT_PASSWORD";; esac\n')
    helper.chmod(0o700)
    rules = [{'rule_name': 'main', 'priority': 1, 'enable_push': False, 'required_approvals': 2,
        'enable_approvals_whitelist': True, 'approvals_whitelist_username': ['policy-reviewer-one', 'policy-reviewer-two'],
        'enable_status_check': True, 'status_check_contexts': ['org-policy/check'], 'block_admin_merge_override': True}]
    qa.apply_rules('组织验收：双人审批及状态检查 ' + suffix, [repos['pull']['id']], rules)
    path = qa.clone(names['pull']); head = qa.commit(path, '组织模板直接推送保护')
    rejected = qa.git(['push', 'origin', 'HEAD:main'], path, expected=False)
    assert 'protected' in rejected.lower()
    record('真实 Git 直接推送被拒绝', {'仓库': repos['pull']['full_name'], '服务端保护拒绝': True})
    branch = 'policy-test-' + suffix
    qa.git(['push', 'origin', 'HEAD:refs/heads/' + branch], path)
    pr = api('POST', f'/repos/policy-qa/{names["pull"]}/pulls', {'title': '组织模板审批与 CI 行为验收', 'head': branch, 'base': 'main'}, user='policy-developer')
    endpoint = f'/repos/policy-qa/{names["pull"]}/pulls/{pr["number"]}'
    for _ in range(30):
        if api('GET', endpoint).get('mergeable'): break
        time.sleep(.2)
    assert api('GET', endpoint).get('mergeable'), '基础可合并性尚未成立，不能当作保护生效'
    def blocked(label):
        response = api('POST', endpoint + '/merge', {'do': 'merge', 'head_commit_id': head}, expected=(403, 405))
        assert not api('GET', endpoint)['merged']
        record(label, {'PR': pr['number'], '拒绝原因': response})
    blocked('零审批禁止合并')
    for index, user in enumerate(('policy-reviewer-one', 'policy-reviewer-two')):
        api('POST', endpoint + '/reviews', {'event': 'APPROVED', 'commit_id': head, 'body': '组织模板固定样本审批'}, user=user)
        if index == 0: blocked('一人审批仍禁止合并')
    blocked('两人审批但缺少 CI 禁止合并')
    status = f'/repos/policy-qa/{names["pull"]}/statuses/{head}'
    api('POST', status, {'state': 'failure', 'context': 'org-policy/check'})
    blocked('CI 失败禁止合并')
    api('POST', status, {'state': 'success', 'context': 'org-policy/check'})
    api('POST', endpoint + '/merge', {'do': 'merge', 'head_commit_id': head}, expected=(200,))
    assert api('GET', endpoint)['merged']
    record('双人审批且 CI 成功允许合并', {'PR': pr['number'], '已合并': True})
    qa.apply_rules('组织验收：禁止强推 ' + suffix, [repos['force']['id']], [{'rule_name': 'main', 'enable_push': True}])
    path = qa.clone(names['force']); original = qa.git(['rev-parse', 'HEAD'], path)
    qa.commit(path, '允许正常推送'); qa.git(['push', 'origin', 'HEAD:main'], path)
    record('允许直接推送时真实 Git 提交成功', repos['force']['full_name'])
    qa.git(['reset', '--hard', original], path)
    rejected = qa.git(['push', '--force', 'origin', 'HEAD:main'], path, expected=False)
    assert 'protected' in rejected.lower() or 'force' in rejected.lower()
    record('禁止强推时真实 Force Push 被拒绝', repos['force']['full_name'])
    qa.apply_rules('组织验收：允许强推 ' + suffix, [repos['force']['id']], [{'rule_name': 'main', 'enable_push': True, 'enable_force_push': True}])
    qa.git(['push', '--force', 'origin', 'HEAD:main'], path)
    record('允许强推时真实 Force Push 成功', repos['force']['full_name'])
    tpl = template('组织验收：失效预览与失败隔离 ' + suffix, [{'rule_name': 'main', 'enable_push': True,
        'enable_push_whitelist': True, 'push_whitelist_usernames': ['policy-developer']}])
    batch = preview(tpl, [repos[k]['id'] for k in ('transfer', 'permission', 'rule-change', 'healthy')])
    assert batch['batch']['counts'] == {'changes': 4}, batch
    other = api('GET', '/orgs/policy-qa-other')
    api('POST', f'/repos/policy-qa/{names["transfer"]}/transfer', {'new_owner': other['name']}, expected=(202,))
    api('PUT', f'/repos/policy-qa/{names["permission"]}/collaborators/policy-developer', {'permission': 'read'})
    api('POST', f'/repos/policy-qa/{names["rule-change"]}/branch_protections', {'rule_name': 'main', 'required_approvals': 1})
    manager('POST', f'/batches/{batch["batch"]["id"]}/apply', {})
    result = qa.wait_batch(batch['batch']['id'], ('completed_with_errors',))
    assert result['batch']['counts'] == {'blocked': 3, 'success': 1}, result
    record('仓库转移、权限变化、规则变化分别阻塞且不影响正常仓库',
        {'批次': result['batch']['id'], '结果': result['batch']['counts'], '明细': [{'仓库': x['repo_name'], '状态': x['status'], '原因': x['error']} for x in result['items']]})
    print('组织模板真实行为与时序边界验收完成', flush=True)


if __name__ == '__main__':
    try: main()
    except Exception as error:
        REPORT['失败'] = str(error)
        (OUT / '真实行为边界.json').write_text(json.dumps(REPORT, ensure_ascii=False, indent=2) + '\n')
        raise
