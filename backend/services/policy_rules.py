"""分支保护字段规范化、确定性通配匹配和差异计算。"""
import hashlib
import json
from collections import deque
from functools import lru_cache


BOOL_FIELDS = '''enable_push enable_push_whitelist push_whitelist_deploy_keys
enable_force_push enable_force_push_allowlist force_push_allowlist_deploy_keys
enable_merge_whitelist enable_status_check enable_approvals_whitelist
block_on_rejected_reviews block_on_official_review_requests block_on_outdated_branch
dismiss_stale_approvals ignore_stale_approvals require_signed_commits
block_admin_merge_override'''.split()
LIST_FIELDS = '''push_whitelist_usernames push_whitelist_teams
force_push_allowlist_usernames force_push_allowlist_teams
merge_whitelist_usernames merge_whitelist_teams status_check_contexts
approvals_whitelist_username approvals_whitelist_teams'''.split()
TEXT_FIELDS = ['protected_file_patterns', 'unprotected_file_patterns']
API_FIELDS = ['rule_name', 'priority', 'required_approvals'] + BOOL_FIELDS + LIST_FIELDS + TEXT_FIELDS
MAX_RULES = 50


class PolicyError(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def normalize_rule(raw, imported=False):
    if not isinstance(raw, dict):
        raise PolicyError('规则必须是对象')
    unknown = set(raw) - set(API_FIELDS) - {'team_org', 'branch_name', 'created_at', 'updated_at', 'id'}
    if unknown:
        raise PolicyError('当前版本不支持以下字段：' + '、'.join(sorted(unknown)))
    name = raw.get('rule_name') or (raw.get('branch_name') if imported else '')
    if not isinstance(name, str) or not name.strip() or len(name) > 200 or any(ord(c) < 32 for c in name):
        raise PolicyError('分支规则名称不能为空，且不能超过 200 字符或包含控制字符')
    name = name.strip()
    glob_machine(name)
    rule = {'rule_name': name}
    for key, default, maximum in [('priority', 1, 1000000), ('required_approvals', 0, 100)]:
        value = raw.get(key, default)
        if type(value) is not int or value < (1 if key == 'priority' else 0) or value > maximum:
            raise PolicyError(f'{key} 数值超出允许范围')
        rule[key] = value
    for key in BOOL_FIELDS:
        value = raw.get(key, False)
        if type(value) is not bool:
            raise PolicyError(f'{key} 必须为布尔值')
        rule[key] = value
    for key in LIST_FIELDS:
        value = raw.get(key) or [] if imported else raw.get(key, [])
        if not isinstance(value, list) or len(value) > 200 or any(not isinstance(x, str) or not x.strip() or len(x) > 200 for x in value):
            raise PolicyError(f'{key} 必须是有效的名称数组，最多 200 项')
        rule[key] = sorted(set(x.strip() for x in value))
    for key in TEXT_FIELDS:
        value = raw.get(key, '')
        if not isinstance(value, str) or len(value) > 4000:
            raise PolicyError(f'{key} 必须是最多 4000 字符的文本')
        rule[key] = value.strip()
    # 原生网页与 PATCH 接口允许独立设置强推；不能用 POST 的限制裁剪读取结果。
    if not rule['enable_push']:
        rule['enable_push_whitelist'] = False
    if not rule['enable_force_push']:
        rule['enable_force_push_allowlist'] = False
    groups = [('enable_push_whitelist', 'push_whitelist_usernames', 'push_whitelist_teams', 'push_whitelist_deploy_keys'),
              ('enable_force_push_allowlist', 'force_push_allowlist_usernames', 'force_push_allowlist_teams', 'force_push_allowlist_deploy_keys'),
              ('enable_merge_whitelist', 'merge_whitelist_usernames', 'merge_whitelist_teams', None),
              ('enable_approvals_whitelist', 'approvals_whitelist_username', 'approvals_whitelist_teams', None)]
    for enabled, users, teams, keys in groups:
        if not rule[enabled]:
            rule[users], rule[teams] = [], []
            if keys:
                rule[keys] = False
    if not rule['enable_status_check']:
        rule['status_check_contexts'] = []
    if rule['dismiss_stale_approvals']:
        rule['ignore_stale_approvals'] = False
    team_org = raw.get('team_org', '')
    if not isinstance(team_org, str) or len(team_org) > 100:
        raise PolicyError('团队所属组织无效')
    rule['team_org'] = team_org.strip().lower() if any(rule[k] for k in LIST_FIELDS if k.endswith('_teams')) else ''
    if not imported:
        if any(rule[k] for k in LIST_FIELDS if k.endswith('_teams')) and not rule['team_org']:
            raise PolicyError('使用团队时必须指定团队所属组织')
        for pattern in rule['status_check_contexts'] + [part.strip() for key in TEXT_FIELDS for part in rule[key].split(';') if part.strip()]:
            if len(pattern) > 200:
                raise PolicyError('单个匹配模式不能超过 200 字符')
            glob_machine(pattern)
    return rule


def normalize_rules(raw, imported=False):
    if not isinstance(raw, list) or not raw or len(raw) > MAX_RULES:
        raise PolicyError(f'模板需要 1 至 {MAX_RULES} 条分支规则')
    rules = [normalize_rule(r, imported) for r in raw]
    names = [r['rule_name'] for r in rules]
    if len(names) != len(set(names)):
        raise PolicyError('模板中不能包含重复的规则名称')
    for i, a in enumerate(rules):
        for b in rules[i+1:]:
            if a['priority'] == b['priority'] and overlaps(a['rule_name'], b['rule_name']):
                raise PolicyError(f'规则 {a["rule_name"]} 与 {b["rule_name"]} 匹配范围重叠，必须设置不同优先级')
    return sorted(rules, key=lambda r: (r['priority'], r['rule_name']))


def api_rule(rule):
    return {key: rule[key] for key in API_FIELDS}


def canonical_current(raw):
    return sorted([api_rule(normalize_rule(r, imported=True)) for r in raw], key=lambda r: r['rule_name'])


def plan_changes(current, desired):
    before = canonical_current(current)
    current_map = {r['rule_name']: r for r in before}
    managed = {r['rule_name'] for r in desired}
    for target in desired:
        for existing in before:
            if existing['rule_name'] not in managed and overlaps(target['rule_name'], existing['rule_name']):
                raise PolicyError(f'规则冲突：{target["rule_name"]} 与保留规则 {existing["rule_name"]} 重叠；请先在 Gitea 明确调整规则范围')
    changes = []
    after = dict(current_map)
    for rule in desired:
        wanted = api_rule(rule)
        old = current_map.get(rule['rule_name'])
        fields = [{'field': k, 'before': old.get(k) if old else None, 'after': v}
                  for k, v in wanted.items() if old is None or old.get(k) != v]
        if fields:
            changes.append({'rule_name': rule['rule_name'], 'action': 'update' if old else 'create', 'fields': fields})
        after[rule['rule_name']] = wanted
    return before, sorted(after.values(), key=lambda r: r['rule_name']), changes


# 使用有限自动机求两个 glob 语言的交集，避免把当前分支样本误当作未来分支的证明。
# 支持 Gitea 的 *、**、?、字符组、反向字符组、范围、花括号分支及反斜线转义。
ALL = ((0, 0x10ffff),)
NO_SLASH = ((0, 46), (48, 0x10ffff))


@lru_cache(maxsize=1)
def case_variants():
    groups = {}
    # 构造单字符折叠类，覆盖 Kelvin、长 s、希腊字母等 Unicode 别名。
    for code in range(0x110000):
        char = chr(code)
        folded = char.casefold()
        if char != folded:
            groups.setdefault(folded, []).append(code)
    return groups


def merge_ranges(ranges):
    result = []
    for lo, hi in sorted(ranges):
        if result and lo <= result[-1][1] + 1:
            result[-1] = (result[-1][0], max(hi, result[-1][1]))
        else:
            result.append((lo, hi))
    return tuple(result)


@lru_cache(maxsize=512)
def glob_machine(pattern):
    edges, eps = [], []
    pos = 0
    plain = not any(c in pattern for c in '*?[]{}\\')
    def state():
        edges.append([]); eps.append([])
        return len(edges) - 1
    def literal(c):
        values = {ord(c)}
        if plain:
            values.update(ord(x) for x in (c.lower(), c.upper(), c.casefold()) if len(x) == 1)
            values.update(case_variants().get(c.casefold(), []))
        return tuple((x, x) for x in sorted(values))
    def sequence(stops=''):
        nonlocal pos
        start = end = state()
        while pos < len(pattern) and pattern[pos] not in stops:
            c = pattern[pos]; pos += 1
            nxt = state()
            if c == '{':
                while True:
                    a, b = sequence(',}')
                    eps[end].append(a); eps[b].append(nxt)
                    if pos >= len(pattern):
                        raise PolicyError('分支匹配模式中的花括号未闭合')
                    sep = pattern[pos]; pos += 1
                    if sep == '}':
                        break
            elif c == '*':
                double = pos < len(pattern) and pattern[pos] == '*'
                if double:
                    pos += 1
                eps[end].append(nxt)
                edges[end].append((ALL if double else NO_SLASH, end))
            else:
                if c == '?':
                    ranges = NO_SLASH
                elif c == '\\':
                    if pos >= len(pattern):
                        raise PolicyError('分支匹配模式的转义不完整')
                    if pattern[pos].isalnum():
                        raise PolicyError('本期不支持正则转义，请改用明确的 glob 字符组')
                    ranges = literal(pattern[pos]); pos += 1
                elif c == '[':
                    negate = pos < len(pattern) and pattern[pos] in '!^'
                    if negate:
                        pos += 1
                    ranges = []
                    def class_char():
                        nonlocal pos
                        char = pattern[pos]; pos += 1
                        if char == '\\':
                            if pos >= len(pattern) or pattern[pos].isalnum():
                                raise PolicyError('字符组转义不完整或使用了未支持的正则转义')
                            char = pattern[pos]; pos += 1
                        return ord(char)
                    while pos < len(pattern) and pattern[pos] != ']':
                        lo = class_char()
                        hi = lo
                        if pos + 1 < len(pattern) and pattern[pos] == '-' and pattern[pos+1] != ']':
                            pos += 1
                            hi = class_char()
                        if hi < lo:
                            raise PolicyError('分支匹配模式字符范围无效')
                        ranges.append((lo, hi))
                    if pos >= len(pattern) or not ranges:
                        raise PolicyError('分支匹配模式字符组无效')
                    pos += 1
                    ranges = merge_ranges(ranges)
                    if negate:
                        complement, previous = [], 0
                        for lo, hi in ranges:
                            if previous < lo:
                                complement.append((previous, lo-1))
                            previous = hi+1
                        if previous <= 0x10ffff:
                            complement.append((previous, 0x10ffff))
                        ranges = tuple(complement)
                elif c in ']}':
                    raise PolicyError('分支匹配模式包含未配对的括号')
                else:
                    ranges = literal(c)
                edges[end].append((ranges, nxt))
            end = nxt
        return start, end
    start, end = sequence()
    closures = []
    for n in range(len(edges)):
        seen, todo = {n}, [n]
        while todo:
            for m in eps[todo.pop()]:
                if m not in seen:
                    seen.add(m); todo.append(m)
        closures.append(frozenset(seen))
    return edges, closures, start, end


def matches(pattern, name):
    edges, closure, start, end = glob_machine(pattern)
    states = closure[start]
    for c in name:
        states = frozenset(n for s in states for ranges, target in edges[s]
                           if any(a <= ord(c) <= b for a, b in ranges) for n in closure[target])
    return end in states


@lru_cache(maxsize=4096)
def overlaps(a, b):
    ea, ca, sa, fa = glob_machine(a)
    eb, cb, sb, fb = glob_machine(b)
    pending, seen = deque([(sa, sb)]), set()
    while pending:
        x, y = pending.popleft()
        if (x, y) in seen:
            continue
        seen.add((x, y))
        if fa in ca[x] and fb in cb[y]:
            return True
        for i in ca[x]:
            for j in cb[y]:
                for ra, ta in ea[i]:
                    for rb, tb in eb[j]:
                        if any(max(lo, low) <= min(hi, high) for lo, hi in ra for low, high in rb):
                            pending.append((ta, tb))
    return False
