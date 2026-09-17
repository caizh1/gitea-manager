#!/usr/bin/env python3
"""固定本机样本的签名、文件例外、名单清空和安全阻塞验收。"""
import importlib.util
import json
import subprocess
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location('真实行为', Path(__file__).with_name('policy-real-acceptance.py'))
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)
checks = []


def record(name, detail):
    checks.append({'项目': name, '结果': '通过', '证据': detail})
    (qa.STATE / '真实边界验收结果.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2) + '\n')
    print(name + '：通过', flush=True)


qa.record = record


def main():
    qa.api('POST', '/login', {'password': qa.VALUES['MANAGER_PASSWORD']}, manager=True)
    qa.CSRF = qa.manager('GET', '/csrf')['token']
    qa.SERVER_ID = next(s['id'] for s in qa.manager('GET', '/servers') if s['name'] == '本地策略验收')
    repo = qa.api('GET', '/repos/policy-qa/probe-files', expected=(200, 404))
    if not repo or 'id' not in repo:
        repo = qa.api('POST', '/orgs/policy-qa/repos', {'name': 'probe-files', 'private': True, 'auto_init': True, 'default_branch': 'main'})
    rid = repo['id']
    path = qa.clone('probe-files')
    original = qa.git(['rev-parse', 'HEAD'], path)
    qa.apply_rules('真实验收：强制签名', [rid], [{'rule_name': 'main', 'enable_push': True, 'require_signed_commits': True}])
    qa.commit(path, '未签名提交应被拒绝 ' + str(time.time_ns()))
    rejected = qa.git(['push', 'origin', 'HEAD:main'], path, expected=False)
    head = qa.git(['rev-parse', 'HEAD'], path)
    logs = subprocess.run(['docker', 'logs', '--tail', '40', 'gitea-policy-local'], capture_output=True, text=True, check=True)
    assert 'Unverified commit: ' + head in logs.stdout + logs.stderr
    record('未签名提交被真实 Git 拦截', 'Git 拒绝；Gitea 日志明确标记该提交 Unverified commit，客户端仅显示通用内部错误')
    qa.apply_rules('真实验收：关闭签名限制', [rid], [{'rule_name': 'main', 'enable_push': True, 'require_signed_commits': False}])
    qa.git(['push', 'origin', 'HEAD:main'], path)
    record('关闭签名限制后同一提交成功推送', '正反对照确认拒绝由签名规则触发')
    original = head
    qa.git(['reset', '--hard', original], path)
    qa.apply_rules('真实验收：文件例外', [rid], [{'rule_name': 'main', 'enable_push': False, 'unprotected_file_patterns': 'docs/**'}])
    (path / 'docs').mkdir(exist_ok=True)
    (path / 'docs' / '说明.md').write_text('允许文档文件例外直接推送\n')
    qa.git(['add', 'docs'], path); qa.git(['commit', '-m', 'docs: 验证文档例外'], path)
    qa.git(['push', 'origin', 'HEAD:main'], path)
    record('文件例外允许文档直接 Push', '禁止直接 Push 时 docs/** 仍可推送')
    qa.commit(path, '非文档文件不得通过例外')
    qa.git(['push', 'origin', 'HEAD:main'], path, expected=False)
    record('文件例外没有放行其他路径', '根目录文件推送被拒绝')
    qa.git(['reset', '--hard', 'origin/main'], path)
    qa.apply_rules('真实验收：文件保护', [rid], [{'rule_name': 'main', 'enable_push': True, 'protected_file_patterns': 'blocked/**'}])
    (path / 'blocked').mkdir(exist_ok=True); (path / 'blocked' / '配置.txt').write_text('保护路径\n')
    qa.git(['add', 'blocked'], path); qa.git(['commit', '-m', 'test: 验证受保护文件'], path)
    qa.git(['push', 'origin', 'HEAD:main'], path, expected=False)
    record('允许 Push 时保护文件仍被拒绝', 'blocked/** 不可直接改动')
    qa.git(['reset', '--hard', 'origin/main'], path)
    rule = {'rule_name': 'main', 'enable_push': True, 'enable_push_whitelist': True, 'push_whitelist_usernames': ['policy-reviewer-one']}
    qa.apply_rules('真实验收：推送人员限制', [rid], [rule])
    qa.commit(path, '不在推送允许名单中 ' + str(time.time_ns()))
    qa.git(['push', 'origin', 'HEAD:main'], path, expected=False)
    record('不在允许名单的代码写入者不能 Push', 'policy-developer 有仓库写权限，但不在指定名单')
    rule['push_whitelist_usernames'] = ['policy-developer']
    rule.update(enable_force_push=True, enable_force_push_allowlist=True, force_push_allowlist_usernames=['policy-reviewer-one'])
    qa.apply_rules('真实验收：独立强推名单', [rid], [rule])
    qa.git(['push', 'origin', 'HEAD:main'], path)
    qa.git(['reset', '--hard', 'HEAD~1'], path)
    qa.git(['push', '--force', 'origin', 'HEAD:main'], path, expected=False)
    record('直接 Push 许可不能绕过独立 Force Push 名单', '同一开发者普通推送成功，强推失败')
    rule.update(push_whitelist_usernames=[], force_push_allowlist_usernames=[], enable_status_check=True, status_check_contexts=[])
    qa.apply_rules('真实验收：启用状态下清空名单', [rid], [rule])
    actual = qa.api('GET', '/repos/policy-qa/probe-files/branch_protections')[0]
    assert actual['enable_push_whitelist'] and actual['push_whitelist_usernames'] == []
    assert actual['enable_force_push_allowlist'] and actual['force_push_allowlist_usernames'] == []
    assert actual['enable_status_check'] and actual['status_check_contexts'] == []
    record('名单和 CI 上下文可显式清空且保留开关', '空数组未被 PATCH 忽略')
    standard = next(t for t in qa.manager('GET', f'/templates?server_id={qa.SERVER_ID}') if t['name'] == '研发保护标准')
    ids = [qa.REPO_IDS['probe-conflict'], qa.REPO_IDS['probe-archived'], qa.FIXTURES['其他组织仓库']['ID'], 999999, qa.REPO_IDS['batch-01']]
    batch = qa.manager('POST', '/batches', {'template_id': standard['id'], 'content_hash': standard['content_hash'], 'repo_ids': ids})
    data = qa.wait_batch(batch['id'], ('ready',))
    assert data['batch']['counts'] == {'blocked': 4, 'unchanged': 1}, data
    record('真实预检阻断冲突、归档、跨组织团队和不存在仓库', {'批次': batch['id'], '结果': [{'仓库': r['repo_name'], '状态': r['status'], '原因': r['error']} for r in data['items']]})
    # 只操作隔离探针仓库，模拟预览之后管理员新增另一个规则。
    template = qa.create_policy_template('真实验收：过期预览', [{'rule_name': 'main', 'enable_push': False}])
    batch = qa.manager('POST', '/batches', {'template_id': template['id'], 'content_hash': template['content_hash'], 'repo_ids': [rid]})
    qa.wait_batch(batch['id'], ('ready',))
    extra_name = 'preserve-' + str(time.time_ns()) + '/*'
    qa.api('POST', '/repos/policy-qa/probe-files/branch_protections', {'rule_name': extra_name, 'priority': 99})
    qa.manager('POST', f'/batches/{batch["id"]}/apply', {})
    # 该批次预期被安全阻断，不能按成功批次等待规则处理。
    for _ in range(60):
        data = qa.manager('GET', f'/batches/{batch["id"]}')
        if data['batch']['status'] == 'completed_with_errors': break
        time.sleep(1)
    assert data['batch']['counts'] == {'blocked': 1}, data
    assert any(r['rule_name'] == extra_name for r in qa.api('GET', '/repos/policy-qa/probe-files/branch_protections'))
    record('真实仓库预览过期后不覆盖任何规则', {'批次': batch['id'], '原因': data['items'][0]['error']})
    qa.manager('DELETE', f'/templates/{template["id"]}')
    assert qa.manager('GET', f'/batches/{batch["id"]}')['template_snapshot'][0]['rule_name'] == 'main'
    record('删除模板后真实历史快照仍可读取', {'批次': batch['id']})


if __name__ == '__main__':
    main()
