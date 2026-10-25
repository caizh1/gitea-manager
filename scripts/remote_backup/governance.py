"""真实父子群组、成员权限和原生审批门禁的恢复验证。"""
import base64
import json
import time
from .driver import CheckFailure
from .fixtures import git


def seed(lab, node):
    state = lab.state['nodes'][node]
    if state.get('governance'):
        activate(lab, node)
        return
    parent = lab.api(node, 'POST', '/governance/groups', json={'path': 'lab-parent', 'name': '灾备父群组', 'visibility': 2}).json()
    child = lab.api(node, 'POST', '/governance/groups', json={'path': 'lab-child', 'parent_id': parent['id'], 'name': '灾备子群组', 'visibility': 2}).json()
    for user, role in [('labwriter', 30), ('labreader', 20)]:
        identity = lab.api(node, 'GET', '/users/' + user).json()
        current = lab.api(node, 'GET', '/governance/groups/%s' % parent['id']).json()
        lab.api(node, 'PUT', '/governance/groups/%s/members/%s' % (parent['id'], identity['id']),
                json={'role': role, 'revision': current['revision']})
    repo = lab.api(node, 'POST', '/orgs/' + child['full_path'] + '/repos',
                   json={'name': 'protected', 'private': True, 'auto_init': True, 'default_branch': 'main'}).json()
    rule = lab.api(node, 'POST', '/repos/' + repo['full_name'] + '/branch_protections', json={
        'rule_name': 'main', 'enable_push': False, 'required_approvals': 1,
        'enable_approvals_whitelist': True, 'approvals_whitelist_username': ['labreader'],
        'block_admin_merge_override': True, 'dismiss_stale_approvals': True}).json()
    state['governance'] = {'parent': parent, 'child': child, 'repo': repo['full_name'], 'rule': rule}
    lab.save()
    hook = '/data/git/repositories/labadmin/proof.git/hooks/pre-receive.d/lab-deny'
    content = '#!/bin/sh\nwhile read old new ref; do\ncase "$ref" in refs/heads/blocked-lab) echo "灾备自定义门禁阻断" >&2; exit 42;; esac\ndone\n'
    lab.command(node, ['docker', 'exec', '-i', 'gitea', 'sh', '-c', 'cat > "$1"; chmod 751 "$1"; chown git:git "$1"', 'sh', hook], input_data=content)
    activate(lab, node)


def activate(lab, node):
    saved = lab.state['nodes'][node]['governance']
    if saved.get('fixture_version') == 2:
        return
    path = '/repos/' + saved['repo']
    repo = lab.api(node, 'GET', path).json()
    rules = lab.api(node, 'GET', path + '/branch_protections').json()
    evidence = lab.root / ('governance-activation-before-' + node + '-' + str(time.time_ns()) + '.json')
    evidence.write_text(json.dumps({'说明': '激活增强版原生引用入口前的夹具规则，保留未激活状态的原始证据', '规则': rules}, ensure_ascii=False, indent=2) + '\n')
    evidence.chmod(0o600)
    lab.api(node, 'PUT', path + '/collaborators/labreviewer', json={'permission': 'write'})
    lab.api(node, 'POST', '/governance/repositories/%s/reference-hook' % repo['id'], json={})
    rule = lab.api(node, 'PATCH', path + '/branch_protections/main', json={
        'require_governance_approval': True, 'required_approvals': 1,
        'enable_approvals_whitelist': True, 'approvals_whitelist_username': ['labreviewer'],
        'block_admin_merge_override': True, 'dismiss_stale_approvals': True}).json()
    if not rule.get('require_governance_approval') or rule.get('approvals_whitelist_username') != ['labreviewer']:
        raise CheckFailure('审批夹具未完成实际入口激活或审批人被过滤，拒绝生成审批通过结论')
    saved.update(rule=rule, fixture_version=2)
    lab.save()


def verify(lab, node):
    saved = lab.state['nodes'][node]['governance']
    child = lab.api(node, 'GET', '/governance/groups/%s' % saved['child']['id']).json()
    if child['parent_id'] != saved['parent']['id']:
        raise CheckFailure('群组父子关系没有恢复')
    path = '/repos/' + saved['repo']
    lab.api(node, 'GET', path, user='labreader')
    if lab.api(node, 'GET', path, user='labdenied', expected=None).status_code not in (403, 404):
        raise CheckFailure('无权用户获得了子群组仓库读取权限')
    rules = lab.api(node, 'GET', path + '/branch_protections').json()
    if not any(rule.get('required_approvals') == 1 and rule.get('block_admin_merge_override')
               and rule.get('require_governance_approval') and rule.get('approvals_whitelist_username') == ['labreviewer'] for rule in rules):
        raise CheckFailure('原生审批或管理员限制没有恢复')
    issue = lab.api(node, 'POST', path + '/issues', user='labwriter', json={'title': '恢复后继承写权限'}).json()
    if not issue.get('number'):
        raise CheckFailure('继承成员不能继续协作')
    branch = 'verify-' + str(time.time_ns())
    lab.api(node, 'POST', path + '/branches', user='labwriter', json={'new_branch_name': branch, 'old_branch_name': 'main'})
    changed = lab.api(node, 'POST', path + '/contents/' + branch + '-proof.txt', user='labwriter',
        json={'branch': branch, 'message': '恢复后的真实修改', 'content': base64.b64encode('审批后合并的正文'.encode()).decode()}).json()
    pull = lab.api(node, 'POST', path + '/pulls', user='labwriter',
        json={'title': '恢复后的审批验证', 'head': branch, 'base': 'main'}).json()
    merge_path = path + '/pulls/%s/merge' % pull['number']
    wait_mergeable(lab, node, path, pull['number'])
    blocked = lab.api(node, 'POST', merge_path, json={'Do': 'merge'}, expected=None)
    proof = lab.root / ('approval-denial-' + node + '-' + str(pull['number']) + '.json')
    proof.write_text(json.dumps({'说明': '无审批时的真实管理员合并请求', 'HTTP状态': blocked.status_code,
        '响应': lab.scrub(blocked.text[:500]), '合并请求': pull['number']}, ensure_ascii=False, indent=2) + '\n')
    proof.chmod(0o600)
    if blocked.status_code != 403:
        raise CheckFailure('缺少审批时没有得到明确的权限拒绝，检查中或一般冲突不能冒充审批门禁通过')
    lab.api(node, 'POST', path + '/pulls/%s/reviews' % pull['number'], user='labreviewer',
        json={'event': 'APPROVED', 'body': '恢复后审批规则仍然生效', 'commit_id': changed['commit']['sha']})
    deadline = time.monotonic() + 60
    while True:
        wait_mergeable(lab, node, path, pull['number'])
        merged = lab.api(node, 'POST', merge_path, json={'Do': 'merge'}, expected=None)
        if merged.status_code in (200, 201, 204):
            break
        # 原生异步引用检查尚未结束时返回确定未执行的 405；其他结果不盲目重放写入。
        retry = merged.status_code == 405 and merged.json().get('message') == 'Please try again later'
        if not retry or time.monotonic() >= deadline:
            raise CheckFailure('有效审批后合并失败：HTTP %s，%s' % (merged.status_code, lab.scrub(merged.text[:500])))
        time.sleep(0.5)
    if not lab.api(node, 'GET', path + '/pulls/%s' % pull['number']).json().get('merged'):
        raise CheckFailure('有效审批后没有真正合并')
    try:
        git(lab, node, ['push', lab.gitea_url(node) + '/labadmin/proof.git', 'HEAD:refs/heads/blocked-lab'], cwd=lab.root / 'proof-work-a')
    except CheckFailure as exc:
        if '灾备自定义门禁阻断' not in str(exc):
            raise CheckFailure('Git push 失败原因不是恢复后的自定义门禁') from exc
    else:
        raise CheckFailure('恢复后自定义 Hook 没有执行')


def wait_mergeable(lab, node, path, identifier):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        pull = lab.api(node, 'GET', path + '/pulls/%s' % identifier).json()
        if pull.get('mergeable'):
            return
        time.sleep(0.5)
    raise CheckFailure('原生异步合并检查未就绪，不能把检查中状态当作审批拒绝')
