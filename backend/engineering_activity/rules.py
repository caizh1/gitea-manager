"""研发上库的日期和缺陷关闭归属规则。"""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from access.errors import AccessError

SHANGHAI = timezone(timedelta(hours=8), 'Asia/Shanghai')
UNKNOWN = 'unknown'
LABEL_DEFAULTS = ('bug', '缺陷')


def utc(value):
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.year < 1971:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def day(dt):
    return dt.replace(tzinfo=timezone.utc).astimezone(SHANGHAI).date().isoformat()


def person(user):
    if not user or not isinstance(user.get('id'), int) or user['id'] <= 0:
        return UNKNOWN, '待归属'
    return f"user:{user['id']}", (user.get('full_name') or user.get('login') or str(user['id']))[:256]


def bug_events(issue, timeline, pull_match, evidence=lambda *_: None):
    """倒查关闭时指派，从关闭事件的提交证据定位修复 PR。"""
    timeline = sorted(timeline, key=lambda row: (utc(row.get('created_at')) or datetime.min, row['id']))
    status = [row for row in timeline if row['type'] in ('close', 'reopen')]
    reopen = [row for row in status if row['type'] == 'reopen']
    final = status[-1] if status and status[-1]['type'] == 'close' and issue['state'] == 'closed' else None
    if not final:
        return None, reopen

    closed = utc(final['created_at'])
    actor = (final.get('user') or {}).get('id')
    recorded = evidence(issue['number'], closed, actor)
    close_sha = final.get('ref_commit_sha') or recorded
    if close_sha:
        matched = pull_match(close_sha, closed)
        if matched:
            return (final, matched['user'], '修复 PR', matched), reopen
        return (final, None, '关闭提交未关联到唯一修复 PR', None), reopen

    possible_reference = any(row.get('ref_action') in ('close', 'closes') and row.get('ref_issue')
                             for row in timeline[:timeline.index(final)])
    if recorded is None and possible_reference:
        return (final, None, '缺少关闭通知，无法确认是否由关联 PR 修复', None), reopen

    # 从 Issue 当前名单倒放关闭后的变更，还原关闭瞬间的名单。
    assigned = {entry['id']: entry for entry in issue.get('assignees') or []}
    stable = True
    for entry in reversed(timeline[timeline.index(final) + 1:]):
        if entry['type'] != 'assignees':
            continue
        target = entry.get('assignee')
        if not target or entry.get('assignee_team'):
            stable = False
            continue
        if entry.get('removed_assignee'):
            assigned[target['id']] = target
        else:
            assigned.pop(target['id'], None)
    owner = next(iter(assigned.values())) if stable and len(assigned) == 1 else None
    return (final, owner, '关闭时唯一指派人' if owner else '关闭时指派人不明确', None), reopen


def validate_settings(payload):
    if not isinstance(payload, dict):
        raise AccessError('统计配置格式错误', 400)
    labels = payload.get('labels', list(LABEL_DEFAULTS))
    paths = payload.get('paths', {})
    teams = payload.get('teams', [])
    if not isinstance(labels, list) or not labels or len(labels) > 25 or any(
        not isinstance(item, str) or not 1 <= len(item.strip()) <= 80 for item in labels
    ):
        raise AccessError('请填写 1 至 25 个有效的缺陷标签', 400)
    if not isinstance(teams, list) or len(teams) > 30 or any(type(item) is not int or item < 1 for item in teams):
        raise AccessError('研发团队格式错误', 400)
    if not isinstance(paths, dict) or len(paths) > 200:
        raise AccessError('仓库目录规则格式错误', 400)
    for repo_id, rules in paths.items():
        if not repo_id.isascii() or not repo_id.isdecimal() or not isinstance(rules, dict):
            raise AccessError('仓库目录规则格式错误', 400)
        for group, roots in rules.items():
            if group not in ('generated', 'third_party') or not isinstance(roots, list) or len(roots) > 50 or any(
                not isinstance(root, str) or len(root) > 250 or '\\' in root or
                any(part in ('', '.', '..') for part in root.split('/')) for root in roots
            ):
                raise AccessError('目录规则必须是相对仓库根目录的路径', 400)
            rules[group] = sorted(set(root.casefold() for root in roots))
    webhook_base = payload.get('webhook_base', '')
    if not isinstance(webhook_base, str) or len(webhook_base) > 512:
        raise AccessError('Manager 回调地址格式错误', 400)
    webhook_base = webhook_base.strip().rstrip('/')
    if webhook_base:
        try:
            url = urlsplit(webhook_base)
            valid = url.scheme in ('http', 'https') and url.hostname and not any(
                (url.username, url.password, url.query, url.fragment, url.path))
        except ValueError:
            valid = False
        if not valid:
            raise AccessError('Manager 回调地址须为 Gitea 可达的 HTTP(S) 根地址', 400)
    return {'webhook_base': webhook_base, 'labels': sorted({item.strip().casefold() for item in labels}), 'teams': sorted(set(teams)), 'paths': paths}
