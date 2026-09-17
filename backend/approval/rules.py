"""确定性审批计票；不访问网络，不以页面状态代替授权。"""
import hashlib
import json
import re

from services.policy_rules import matches, PolicyError


class ApprovalError(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def positive(value, name, maximum=2**53-1):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ApprovalError(name + '必须是范围内的正整数')
    return value


def branch_name(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 200 or value.startswith(('/', '-'))
            or value.endswith(('/', '.', '.lock')) or any(x in value for x in ('..', '@{', '//'))
            or any(ord(c) <= 32 or ord(c) == 127 or c in '~^:?*[\\' for c in value)
            or any(p.startswith('.') or p.endswith('.lock') for p in value.split('/'))):
        raise ApprovalError('请选择明确的目标分支，不能使用通配符或无效引用')
    return value


def normalize(value):
    if not isinstance(value, dict) or set(value) - {'name', 'branches', 'total', 'groups', 'reset_on_push'}:
        raise ApprovalError('审批规则字段无效')
    name = value.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
        raise ApprovalError('规则名称需要 1 至 100 个可显示字符')
    branches = value.get('branches')
    if not isinstance(branches, list) or not 1 <= len(branches) <= 100:
        raise ApprovalError('请选择 1 至 100 个目标分支')
    branches = sorted(set(branch_name(x) for x in branches))
    total = positive(value.get('total'), '总票数', 100)
    groups = value.get('groups')
    if not isinstance(groups, list) or not 1 <= len(groups) <= 20:
        raise ApprovalError('请选择 1 至 20 个必需审批组')
    normalized = []
    for group in groups:
        if not isinstance(group, dict) or set(group) != {'team_id', 'required'}:
            raise ApprovalError('审批组必须包含稳定团队 ID 和要求票数')
        normalized.append({'team_id': positive(group['team_id'], '团队 ID'),
                           'required': positive(group['required'], '组票数', 100)})
    if len({g['team_id'] for g in normalized}) != len(normalized):
        raise ApprovalError('同一审批组不能重复配置')
    # 当前只支持对每个新代码版本重新批准，避免隐式沿用旧 SHA。
    if value.get('reset_on_push', True) is not True:
        raise ApprovalError('当前版本要求新提交重新批准')
    return {'name': name.strip(), 'branches': branches, 'total': total,
            'groups': sorted(normalized, key=lambda g: g['team_id']), 'reset_on_push': True}


def native_rule(rules, branch):
    if not isinstance(rules, list):
        raise ApprovalError('原生分支规则未完整读取')
    selected = []
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get('rule_name'), str):
            raise ApprovalError('原生分支规则格式错误')
        try:
            if matches(rule['rule_name'], branch):
                selected.append(rule)
        except PolicyError as exc:
            raise ApprovalError(str(exc)) from None
    # 对齐官方 1.26.4：优先级在前，同优先级时精确名称在前，然后创建时间。
    def order(r):
        priority = r.get('priority')
        if type(priority) is not int:
            raise ApprovalError('原生规则缺少优先级')
        return priority, bool(re.search(r'[*?\[{}]', r['rule_name'])), r.get('created_at', '')
    selected.sort(key=order)
    if not selected:
        raise ApprovalError('目标分支没有原生保护，不能安全增加强制审批')
    if len(selected) > 1 and order(selected[0]) == order(selected[1]):
        raise ApprovalError('原生规则优先级无法确定，请先核对')
    return selected[0]


def safe_native(rule):
    if rule.get('enable_push') is not False or rule.get('block_admin_merge_override') is not True:
        raise ApprovalError('原生规则允许直接推送或管理员覆盖；保留现状，禁止强制启用')
    if rule.get('unprotected_file_patterns'):
        raise ApprovalError('原生规则存在文件推送例外；保留现状，禁止强制启用')
    return rule


def evaluate(rule, pr, reviews, eligible, teams, native):
    """eligible 为当前原生审批池内具备权限且活动的用户；teams 为实时成员集合。"""
    head = pr.get('head', {}).get('sha')
    if not isinstance(head, str) or not re.fullmatch('[0-9a-f]{40}', head):
        raise ApprovalError('PR 当前提交不完整')
    author = positive(pr.get('user', {}).get('id'), '作者 ID')
    if not isinstance(reviews, list):
        raise ApprovalError('审批列表未完整读取')
    latest = {}
    ids = set()
    for review in reviews:
        rid = positive(review.get('id'), '审批 ID')
        if rid in ids:
            raise ApprovalError('审批分页存在重复记录，必须重新读取')
        ids.add(rid)
        state = review.get('state')
        if state not in ('APPROVED', 'REQUEST_CHANGES', 'COMMENT', 'PENDING', 'REQUEST_REVIEW'):
            raise ApprovalError('发现未支持的审批状态')
        if state == 'REQUEST_REVIEW' and not review.get('user'):
            continue
        uid = positive(review.get('user', {}).get('id'), '审批人 ID')
        # 普通评论与待提交草稿不替换已批准的状态；新的正式决定才替换。
        if state in ('APPROVED', 'REQUEST_CHANGES') and rid > latest.get(uid, {}).get('id', 0):
            latest[uid] = review
    approved = []
    rejected = []
    for uid, review in latest.items():
        if type(review.get('dismissed')) is not bool or type(review.get('stale')) is not bool:
            raise ApprovalError('审批缺少撤回或过期状态，不能判定通过')
        if uid == author or uid not in eligible or review['dismissed']:
            continue
        if review['state'] == 'REQUEST_CHANGES':
            rejected.append(uid)
        elif (review['commit_id'] == head and not review['stale']
              and review.get('decision_event_id', 1) > pr.get('last_code_event_id', 0)):
            approved.append({'id': uid, 'login': review['user']['login'], 'review_id': review['id']})
    approved.sort(key=lambda u: u['id'])
    voters = {a['id'] for a in approved}
    group_rows = []
    for group in rule['groups']:
        team = teams.get(group['team_id'])
        if not team:
            raise ApprovalError('必需审批组已删除或不再属于目标仓库')
        members = set(team['members']) & set(eligible)
        votes = sorted(voters & members)
        group_rows.append({'team_id': group['team_id'], 'name': team['name'], 'required': group['required'],
                           'count': len(votes), 'approved_ids': votes,
                           'satisfied': len(votes) >= group['required'], 'eligible_count': len(members)})
    applicable = pr.get('base', {}).get('ref') in rule['branches']
    missing = []
    if len(approved) < rule['total']:
        missing.append(f'总票数不足：{len(approved)}/{rule["total"]}')
    missing.extend(f'{g["name"]}：{g["count"]}/{g["required"]}' for g in group_rows if not g['satisfied'])
    if rejected and native.get('block_on_rejected_reviews'):
        missing.append('原生规则存在请求修改')
    if pr.get('state') != 'open' or pr.get('merged'):
        missing.append('PR 已关闭或合并')
    return {'head': head, 'applicable': applicable, 'satisfied': applicable and not missing,
            'total': {'count': len(approved), 'required': rule['total']}, 'groups': group_rows,
            'approved': approved, 'missing': missing, 'native_required': native.get('required_approvals', 0),
            'native_rule': native['rule_name'], 'notice': '新增审批要求；原生审批、CI、冲突等检查仍独立生效'}
