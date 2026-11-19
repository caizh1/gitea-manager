"""以 Gitea 活动记录和固定 Git 分支状态采集研发事件。"""
import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from models import db
from services.policy_gitea import PolicyError
from services.upgrade_maintenance import active
from .closing import evidence, install_hook, newly_received
from .git_data import OID, GitUnavailable, fetch
from .models import Activity
from .rules import SHANGHAI, UNKNOWN, bug_events, day, person, utc


CODE = ('commit', 'main_commit')
class RefPending(PolicyError):
    pass


COMPARE = re.compile(r'/compare/([0-9a-f]{40}|[0-9a-f]{64})\.\.\.?([0-9a-f]{40}|[0-9a-f]{64})$')


def endpoint(repo):
    return '/repos/' + '/'.join(quote(part, safe='') for part in repo['full_name'].split('/'))


def page_rows(client, path, params=None):
    seen = set()
    for page in range(1, 2001):
        rows = client.request('GET', path, params={**(params or {}), 'limit': 50, 'page': page})
        if not isinstance(rows, list):
            raise PolicyError('研发上库分页响应格式错误')
        for row in rows:
            if row['id'] not in seen:
                seen.add(row['id'])
                yield row
        if client.total_count and len(seen) >= client.total_count or not rows:
            return
        if len(rows) < 50 and not client.total_count:
            return
    raise PolicyError('研发上库分页超过采集上限')


def feeds(client, path, stop_id, since):
    rows = []
    newest = 0
    stopped_at = None
    for item in page_rows(client, path + '/activities/feeds'):
        activity_id = item['id']
        date = utc(item['created'])
        newest = max(newest, activity_id)
        if date < since or (stopped_at and date < stopped_at):
            break
        if activity_id == stop_id:
            stopped_at = date
        # Gitea 按秒倒序，同一秒内可能先返回旧 ID。
        if not stop_id or activity_id > stop_id:
            rows.append(item)
    return sorted(rows, key=lambda row: row['id']), newest or stop_id, not stop_id or stopped_at is not None


def activity_payload(item):
    ref = item.get('ref_name') or ''
    if not ref.startswith('refs/heads/'):
        return None
    if item['op_type'] == 'delete_branch':
        return ref, None, None
    if item['op_type'] != 'commit_repo' or not item.get('content'):
        return None
    try:
        content = json.loads(item['content'])
        new = (content.get('HeadCommit') or {}).get('Sha1')
        compare = COMPARE.search(urlsplit(content.get('CompareURL') or '').path)
        old = compare[1] if compare and compare[2] == new else None
    except (ValueError, AttributeError, TypeError):
        return ref, '', None
    return ref, new if OID.fullmatch(new or '') else '', old


def reconstruct(rows, current, checkpoint, stable_default):
    """回放分支变更，产出变更前的状态。已有快照可核对新建、删除和强推。"""
    updates = []
    creates = set()
    for row in rows:
        ref = row.get('ref_name', '')
        if row['op_type'] == 'commit_repo' and ref.startswith('refs/heads/') and not row.get('content'):
            creates.add((ref, row['act_user_id']))
            continue
        payload = activity_payload(row)
        if payload is not None:
            key = (payload[0], row['act_user_id'])
            new_branch = key in creates
            creates.discard(key)
            updates.append((row, *payload, new_branch))

    if checkpoint is not None:
        heads = dict(checkpoint)
        result = []
        for row, ref, new, old, created in updates:
            previous = heads.get(ref)
            if new == '' or (old and previous != old and not created and previous is not None):
                return [], False
            if new is None:
                heads.pop(ref, None)
                continue
            if (previous is None and not created) or (previous is not None and created):
                return [], False
            result.append((row, ref, new, previous, tuple(heads.values()), stable_default))
            heads[ref] = new
        return (result, True) if heads == current and not creates else ([], False)

    # 首次回补仅使用可重建的区间，删除事件没有记录旧分支头。
    heads = dict(current)
    result = []
    for row, ref, new, old, created in reversed(updates):
        if not new or heads.get(ref) != new or (not old and not created):
            break
        if created:
            heads.pop(ref)
            old = None
        else:
            heads[ref] = old
        result.append((row, ref, new, old, tuple(heads.values()), False))
    return list(reversed(result)), False


class Changes:
    def __init__(self, repo):
        self.repo = repo
        self.writes = {}
        self.removals = []
        self.changed_days = set()

    def has(self, kind, key):
        return ((kind, key) in self.writes or
                db.session.get(Activity, (self.repo.id, kind, key)) is not None)

    def remove(self, kind, key=None, prefix=None):
        self.removals.append((kind, key, prefix))

    def apply(self):
        for kind, key, prefix in self.removals:
            query = Activity.query.filter_by(repository=self.repo.id, kind=kind)
            if key is not None:
                query = query.filter_by(key=str(key))
            if prefix is not None:
                query = query.filter(Activity.key.startswith(prefix))
            self.changed_days.update(day for (day,) in query.with_entities(Activity.day).distinct())
            query.delete(synchronize_session='fetch')
        for (kind, key), fields in self.writes.items():
            row = db.session.get(Activity, (self.repo.id, kind, key))
            if row is None:
                row = Activity(repository=self.repo.id, kind=kind, key=key)
                db.session.add(row)
            elif all(getattr(row, field) == value for field, value in fields.items()):
                continue
            if row.day:
                self.changed_days.add(row.day)
            self.changed_days.add(fields['day'])
            for field, value in fields.items():
                setattr(row, field, value)

    def put(self, kind, key, date, user=None, values=None, identity=None):
        if date is None:
            return
        actor, name = identity or person(user)
        self.writes[(kind, str(key))] = dict(occurred=date, day=day(date), person=actor, name=name,
                                             values=values or {})


def source_identity(client, path, sha):
    detail = client.request('GET', path + '/git/commits/' + sha)
    if detail.get('sha') != sha:
        raise PolicyError('Gitea 返回的提交与请求对象不一致')
    identity, name = person(detail.get('author'))
    return (identity, name) if identity != UNKNOWN else (UNKNOWN, '待映射')


def record_pushes(client, path, changes, git, replay, default, merges):
    failures = set()
    metadata = {}
    parents = {}

    def lookup(sha):
        if sha not in parents:
            if not OID.fullmatch(sha or ''):
                raise GitUnavailable('旧分支提交无有效对象 ID')
            detail = client.request('GET', path + '/git/commits/' + sha, allow_missing=True)
            if not detail or detail.get('sha') != sha or not isinstance(detail.get('parents'), list):
                raise GitUnavailable('Gitea 无法提供强推前提交的父链')
            values = [parent.get('sha') for parent in detail['parents']]
            if any(not OID.fullmatch(value or '') for value in values):
                raise GitUnavailable('Gitea 返回无效父提交')
            parents[sha] = values
        return parents[sha]

    for row, ref, head, previous, known, main_proven in replay:
        date = utc(row['created'])
        pusher = person(row.get('act_user'))[1]
        try:
            development = git.new_commits(head, known, lookup)
            main = git.new_commits(head, [previous] if previous else [], lookup) if main_proven and ref == default else []
            # 改回旧提交可改变主线状态，事件表按 SHA 保留首次可证实的进入日期。
            for kind, commits in zip(CODE, (development, main)):
                for sha in commits:
                    if changes.has(kind, sha):
                        continue
                    if sha not in metadata:
                        author, title, merge, files = git.commit(sha)
                        identity = source_identity(client, path, sha)
                        metadata[sha] = (identity, author, title, merge, files)
                    identity, author, title, merge, files = metadata[sha]
                    pull = merges.get(sha)
                    changes.put(kind, sha, date, identity=identity, values={
                        'title': title, 'author': author, 'pusher': pusher, 'branch': ref[len('refs/heads/'):],
                        'merge': merge, 'files': files, 'pull': pull, 'feed': row['id'],
                    })
        except GitUnavailable:
            failures.add(day(date))
    return sorted(failures)


def pull_rows(client, path, cutoff):
    pulls = {}
    for row in page_rows(client, path + '/pulls', {'state': 'all', 'sort': 'recentupdate'}):
        updated = utc(row.get('updated_at'))
        if updated and updated < cutoff:
            break
        pulls[row['number']] = row
    # 当前待合并 PR 包含早于历史回补窗口的记录。
    for row in page_rows(client, path + '/pulls', {'state': 'open'}):
        pulls[row['number']] = row
    return pulls


def record_pulls(changes, pulls, start):
    for row in pulls.values():
        index = row['number']
        values = {'title': row['title'], 'number': index, 'branch': (row.get('base') or {}).get('ref', '')}
        created, merged = utc(row.get('created_at')), utc(row.get('merged_at'))
        closed = utc(row.get('closed_at'))
        if created and created >= start:
            changes.put('pr_open', index, created, row.get('user'), values)
        if merged and merged >= start:
            changes.put('pr_merge', index, merged, row.get('user'),
                {**values, 'sha': row.get('merge_commit_sha'),
                 'seconds': (merged - created).total_seconds() if created and merged >= created else None})
        if closed and not merged and closed >= start:
            changes.put('pr_close', index, closed, row.get('user'), values)
        else:
            changes.remove('pr_close', index)


def record_issues(client, path, changes, pulls, labels, start, cutoff, reclassify):
    if reclassify:
        changes.remove('bug')
        changes.remove('bug_reopen')
    merges = {}
    for pr in pulls.values():
        sha = pr.get('merge_commit_sha')
        if sha and pr.get('merged'):
            merges.setdefault(sha, []).append(pr)

    def pull_match(sha, timestamp):
        candidates = [pr for pr in merges.get(sha, []) if utc(pr.get('merged_at')) <= timestamp]
        return candidates[0] if len(candidates) == 1 else None

    params = {'state': 'all', 'type': 'issues', 'since': cutoff.isoformat() + 'Z'}
    issues = {item['number']: item for item in page_rows(client, path + '/issues', params)}
    for index in sorted(newly_received(changes.repo, cutoff) - issues.keys()):
        issue = client.request('GET', f'{path}/issues/{index}', allow_missing=True)
        if issue:
            issues[index] = issue
    for issue in issues.values():
        if issue.get('pull_request'):
            continue
        index = issue['number']
        tagged = any(label['name'].casefold() in labels for label in issue.get('labels') or [])
        if not tagged:
            changes.remove('bug', index)
            changes.remove('bug_reopen', prefix=f'{index}:')
            continue
        timeline = list(page_rows(client, f'{path}/issues/{index}/timeline'))
        fixed, reopens = bug_events(issue, timeline, pull_match, lambda *args: evidence(changes.repo, *args))
        history = [{'type': entry['type'], 'time': entry.get('created_at'),
                    'person': person(entry.get('user'))[1]}
                   for entry in timeline if entry['type'] in ('close', 'reopen')]
        values = {'title': issue['title'], 'number': index, 'history': history}
        if fixed and utc(fixed[0]['created_at']) >= start:
            close, user, reason, pr = fixed
            changes.put('bug', index, utc(close['created_at']), user, {
                **values, 'reason': reason, 'timeline': close['id'], 'pull': pr['number'] if pr else None,
            })
        else:
            changes.remove('bug', index)
            if issue['state'] == 'closed' and utc(issue.get('closed_at')) and utc(issue['closed_at']) >= start:
                changes.put('bug', index, utc(issue['closed_at']),
                    values={**values, 'reason': '缺少关闭时间线'})
        for item in reopens:
            date = utc(item['created_at'])
            if date and date >= start:
                changes.put('bug_reopen', f"{index}:{item['id']}", date, item.get('user'),
                    {**values, 'timeline': item['id']})


def collect_repo(client, info, repo, settings, cutoff_time, callback, activity_visible):
    if active():
        raise PolicyError('系统升级期间暂停采集')
    path = endpoint(info)
    hook_error = ''
    if callback and repo.checkpoint.get('hook') != callback:
        try:
            install_hook(client, path, info, callback, repo.epoch)
        except PolicyError:
            hook_error = '缺陷关闭通知配置失败，请核对 Gitea Webhook 权限和 Manager 回调地址'
    changes = Changes(repo)
    old = repo.checkpoint
    first_day = date.fromisoformat(day(cutoff_time)) - timedelta(days=89)
    start = utc(old.get('start')) or datetime.combine(first_day, datetime.min.time(), SHANGHAI).astimezone(
        timezone.utc).replace(tzinfo=None)
    since = (repo.synced - timedelta(hours=2)) if repo.synced else start
    labels = settings['labels']
    reclassify = old.get('labels') != labels
    pulls = {} if info.get('empty') or not info.get('has_pull_requests', True) else pull_rows(
        client, path, start if reclassify else since)
    record_pulls(changes, pulls, start)
    if info.get('has_issues', True):
        record_issues(client, path, changes, pulls, labels, start, start if reclassify else since, reclassify)

    default = 'refs/heads/' + info.get('default_branch', '')
    health = dict(repo.health)
    # 先后比对活动流顶端，避免把尚未稳定的仓库状态当作采集结果。
    rows, newest, found = ([], 0, True) if info.get('empty') else feeds(
        client, path, old.get('feed'), start)
    if info.get('empty'):
        heads, verified, live_default, failures = {}, True, True, []
    else:
        refs_changed = any(row['op_type'] in ('commit_repo', 'delete_branch') for row in rows)
        unchanged = old.get('heads') if found and not refs_changed else None
        with fetch(client, info, unchanged) as (git, heads):
            _, observed, _ = feeds(client, path, newest, cutoff_time - timedelta(hours=1))
            if observed != newest:
                raise PolicyError('仓库在采集中更新，将自动重试')
            checkpoint = old.get('heads') if found else None
            if checkpoint is not None and not old.get('feed') and old.get('heads'):
                # 导入或活动清理后接入时已存在的分支，使用实际接入检查点。
                rows = [row for row in rows if utc(row['created']) >= repo.synced]
            live_default = old.get('default') == default
            replay, verified = reconstruct(rows, heads, checkpoint, live_default)
            if not verified and checkpoint is not None:
                pending_since = utc(health.get('awaiting_refs_since')) or cutoff_time
                if cutoff_time - pending_since < timedelta(minutes=2):
                    raise RefPending('Git 分支已更新，等待 Gitea 活动记录补齐后重试')
                replay, _ = reconstruct(rows, heads, None, False)
            failures = record_pushes(client, path, changes, git, replay, default, {
                pr['merge_commit_sha']: pr['number'] for pr in pulls.values() if pr.get('merge_commit_sha')
            })
    gaps = set(health.get('gaps', [])) | set(failures)
    if repo.synced and (not verified or not live_default):
        first = repo.synced
        while day(first) <= day(cutoff_time):
            gaps.add(day(first))
            first += timedelta(days=1)

    if active():
        raise PolicyError('系统升级期间暂停采集')
    changes.apply()
    repo.health = {'visibility_partial': not activity_visible, 'hook_error': hook_error, 'gaps': sorted(gaps), 'history_partial_before': health.get('history_partial_before', day(cutoff_time)),
                   'open': [{'person': person(pr.get('user'))[0], 'branch': (pr.get('base') or {}).get('ref')}
                            for pr in pulls.values() if pr['state'] == 'open']}
    repo.checkpoint = {'start': start.isoformat() + 'Z', 'feed': newest, 'heads': heads,
                       'default': default, 'labels': labels,
                       'paths': settings['paths'].get(str(info['id']), {}),
                       'hook': '' if hook_error else callback}
    repo.synced = cutoff_time

    return None if old.get('paths') != repo.checkpoint['paths'] else changes.changed_days
