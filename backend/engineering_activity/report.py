"""从逐仓日汇总生成看板、排行及明细。"""
from datetime import date, datetime, timedelta
from statistics import median
from urllib.parse import quote

from flask import request

from access.errors import AccessError
from models import db
from .git_data import line_counts
from .models import Activity, Daily, Roster
from .rules import UNKNOWN, day
from .worker import repository_key, scoped_states


METRICS = ('additions', 'deletions', 'changes', 'commits', 'pr_open', 'pr_merge', 'bug', 'pr_close',
           'bug_reopen', 'merge_commits', 'unassigned_bugs')
CORE = ('changes', 'commits', 'pr_open', 'pr_merge', 'bug')
CODE = ('commit', 'main_commit')


def empty_values():
    return dict.fromkeys(METRICS, 0)


def materialize(repo, paths, changed_days=None):
    """保留提交级数据，重分类时重建日汇总。一个仓库原子切换。"""
    if changed_days is not None and not changed_days:
        return
    buckets = {}
    data = Activity.query.filter_by(repository=repo.id)
    daily = Daily.query.filter_by(repository=repo.id)
    if changed_days is not None:
        data = data.filter(Activity.day.in_(changed_days))
        daily = daily.filter(Daily.day.in_(changed_days))
    for event in data.order_by(Activity.occurred).yield_per(500):
        branch = event.values.get('branch') or ''
        key = (event.day, event.kind, event.person, branch)
        bucket = buckets.setdefault(key, [event.name, empty_values(), []])
        bucket[0] = event.name
        totals, durations = bucket[1:]
        if event.kind in CODE:
            added, removed = line_counts(event.values['files'], paths)['code']
            totals['additions'] += added
            totals['deletions'] += removed
            totals['changes'] += added + removed
            totals['merge_commits' if event.values['merge'] else 'commits'] += 1
        else:
            totals[event.kind] += 1
            if event.kind == 'bug' and event.person == UNKNOWN:
                totals['unassigned_bugs'] += 1
            if event.kind == 'pr_merge' and event.values.get('seconds') is not None:
                durations.append(event.values['seconds'])
    daily.delete()
    db.session.bulk_insert_mappings(Daily, [
        {'repository': repo.id, 'day': d, 'kind': kind, 'person': actor, 'branch': branch, 'name': name,
         'values': {'counts': counts, 'durations': durations}}
        for (d, kind, actor, branch), (name, counts, durations) in buckets.items()
    ])


def query_args():
    today = date.fromisoformat(day(datetime.utcnow()))
    try:
        start = date.fromisoformat(request.args.get('start', (today - timedelta(days=29)).isoformat()))
        end = date.fromisoformat(request.args.get('end', today.isoformat()))
    except ValueError:
        raise AccessError('查询日期格式错误', 400) from None
    if start > end or end > today or (end - start).days > 365:
        raise AccessError('请选择截至今天且不超过 366 天的日期范围', 400)
    view = request.args.get('view', 'development')
    metric = request.args.get('metric', 'changes')
    if view not in ('development', 'main') or metric not in CORE:
        raise AccessError('统计指标或视图无效', 400)
    return start, end, view, metric


def saved_repos(server, scope, visible):
    allowed = {repository_key(row): row for row in visible.values()}
    return {stored.id: (stored, allowed[key]) for stored in scoped_states(server, scope).all()
            if (key := (stored.repo_id, stored.organization_id, stored.birth)) in allowed}


def roster_days(server, key, team, start, end, organization_id):
    if not team:
        return {}
    rows = Roster.query.filter_by(server_id=server.id, epoch=key[1], team_id=team,
                                  organization_id=organization_id).filter(Roster.day.between(str(start), str(end))).all()
    return {row.day: row.members for row in rows}


def coverage(states, expected, dates, roster, team):
    output = {}
    for value in dates:
        code_missing = general_missing = 0
        for state, _ in states.values():
            health = state.health
            if not state.synced or value < day(datetime.fromisoformat(state.checkpoint['start'])) or value > day(state.synced):
                general_missing += 1
                continue
            if health.get('error') and value >= day(state.synced):
                general_missing += 1
                continue
            if health.get('visibility_partial') or value <= health['history_partial_before'] or value in health['gaps']:
                code_missing += 1
        general_missing += expected - len(states)
        output[value] = {
            'code': code_missing + general_missing,
            'events': general_missing,
            'roster': bool(team and value not in roster),
        }
    return output


def entries(states, start, end, view, detail=False):
    model = Activity if detail else Daily
    exclude = 'commit' if view == 'main' else 'main_commit'
    return model.query.filter(model.repository.in_(states), model.day.between(str(start), str(end)),
                              model.kind != exclude)


def in_view(row, view, branch, repo):
    if view != 'main' or row.kind in CODE or not row.kind.startswith('pr_'):
        return True
    return branch == repo.get('default_branch')


def matches(person, value, people, team):
    return (not person or person == value) and (not team or value in people)


def totals_add(target, values):
    for key, value in values.items():
        target[key] += value


def dashboard(server, key, states, visible, team, org_id, who, args):
    start, end, view, metric = args
    dates = [(start + timedelta(days=index)).isoformat() for index in range((end - start).days + 1)]
    roster = roster_days(server, key, team, start, end, org_id)
    quality = coverage(states, len(visible), dates, roster, team)
    daily = {value: empty_values() for value in dates}
    people = {}
    per_day = {}
    rank = {}
    durations = []
    total = empty_values()
    active = set()
    for row in entries(states, start, end, view).order_by(Daily.day).yield_per(600):
        _, repo = states[row.repository]
        people[row.person] = row.name
        if not in_view(row, view, row.branch, repo) or not matches(who, row.person, roster.get(row.day, {}), team):
            continue
        values = row.values['counts']
        totals_add(total, values)
        totals_add(daily[row.day], values)
        person_total = rank.setdefault(row.person, empty_values())
        totals_add(person_total, values)
        result = per_day.setdefault((row.day, row.person), {'values': empty_values(), 'repositories': set()})
        totals_add(result['values'], values)
        result['repositories'].add(repo['full_name'])
        durations.extend(row.values['durations'])
        if row.person != UNKNOWN and any(values[metric] for metric in CORE):
            active.add(row.person)

    for date_key, members in roster.items():
        for member, name in members.items():
            people[member] = name
            if not who or who == member:
                per_day.setdefault((date_key, member), {'values': empty_values(), 'repositories': set()})

    ranking = sorted(((p, v) for p, v in rank.items() if p != UNKNOWN and v[metric]),
                     key=lambda row: (-row[1][metric], people[row[0]].casefold(), row[0]))
    rows = sorted(per_day, key=lambda entry: (people[entry[1]].casefold(), entry[1]))
    rows.sort(key=lambda entry: entry[0], reverse=True)
    try:
        page = int(request.args.get('page', '1'))
        if not 1 <= page <= 100000:
            raise ValueError()
    except ValueError:
        raise AccessError('日报页码无效', 400) from None
    items = [{'day': d, 'person': p, 'name': people[p], **per_day[(d, p)]['values'],
              'repositories': sorted(per_day[(d, p)]['repositories'])} for d, p in rows[(page - 1) * 30:page * 30]]
    newest_roster = roster[max(roster)] if roster else {}
    open_pulls = sum(1 for state, repo in states.values() for pull in state.health.get('open', [])
                     if (view != 'main' or pull['branch'] == repo.get('default_branch'))
                     and matches(who, pull['person'], newest_roster, team))
    gaps = []
    by_repo_id = {state.repo_id: state for state, _ in states.values()}
    for repo_id, info in visible.items():
        stored = by_repo_id.get(repo_id)
        health = stored.health if stored else {}
        reason = '尚未同步' if not stored or not stored.synced else health.get('error') or health.get('hook_error')
        earlier = health.get('history_partial_before')
        uncertain = health.get('visibility_partial')
        days = [value for value in health.get('gaps', []) if str(start) <= value <= str(end)]
        if reason or uncertain or days or (earlier and earlier >= str(start)):
            gaps.append({'name': info['full_name'], 'error': reason, 'history_before': earlier if earlier and earlier >= str(start) else None,
                         'private_activity': uncertain, 'days': days})
    return {'summary': {**total, 'active': len(active), 'waiting': open_pulls,
                        'median_merge_seconds': median(durations) if durations else None},
            'series': [{'day': d, **daily[d], 'coverage': quality[d]} for d in dates],
            'ranking': [{'person': p, 'name': people[p], **counts} for p, counts in ranking[:10]],
            'people': [{'id': p, 'name': name} for p, name in sorted(people.items(), key=lambda item: (item[1], item[0]))],
            'daily': items, 'total_rows': len(rows), 'page_size': 30,
            'coverage': {'repositories': len(visible), 'synced': sum(bool(state.synced) for state, _ in states.values()),
                         'partial': any(any(info.values()) for info in quality.values()), 'gaps': gaps}}


def details(server, key, states, team, org_id, who, args):
    start, end, view, _ = args
    if start != end:
        raise AccessError('请指定一天查看明细', 400)
    try:
        page = int(request.args.get('page', '1'))
        if not 1 <= page <= 100000:
            raise ValueError()
    except ValueError:
        raise AccessError('明细页码无效', 400) from None
    people = roster_days(server, key, team, start, end, org_id).get(str(start), {})
    query = entries(states, start, end, view, detail=True)
    if who:
        query = query.filter(Activity.person == who)
    if team:
        query = query.filter(Activity.person.in_(people))
    items = []
    selected_kind = request.args.get('kind', '')
    for row in query.order_by(Activity.occurred.desc(), Activity.repository, Activity.kind, Activity.key).yield_per(250):
        if selected_kind == 'bug_unassigned' and (row.kind != 'bug' or row.person != UNKNOWN):
            continue
        if selected_kind not in ('', 'bug_unassigned') and row.kind != selected_kind:
            continue
        stored, repo = states[row.repository]
        val = row.values
        if not in_view(row, view, val.get('branch'), repo):
            continue
        kinds = {'pr_open': 'pulls', 'pr_merge': 'pulls', 'pr_close': 'pulls', 'bug': 'issues', 'bug_reopen': 'issues'}
        base = server.gitea_url.rstrip('/')
        if '://' not in base:
            base = 'http://' + base
        url = base + '/' + '/'.join(quote(part, safe='') for part in repo['full_name'].split('/'))
        target = 'commit/' + row.key if row.kind in CODE else f"{kinds[row.kind]}/{val['number']}"
        public = {name: val[name] for name in ('title', 'branch', 'author', 'pusher', 'merge', 'reason', 'pull', 'history') if name in val}
        if row.kind in CODE:
            public['lines'] = line_counts(val['files'], stored.checkpoint.get('paths', {}))
        items.append({'id': f'{row.repository}:{row.kind}:{row.key}', 'kind': row.kind,
                      'time': row.occurred.isoformat() + 'Z', 'person': row.name,
                      'repository': repo['full_name'], 'url': f'{url}/{target}', **public})
    return {'items': items[(page - 1) * 50:page * 50], 'total': len(items), 'page_size': 50}
