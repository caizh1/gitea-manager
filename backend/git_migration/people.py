"""负责人及当前评审请求只映射到既有目标账号，历史作者仍保留来源身份。"""
from svn_migration.common import require


def references(row, kind, reviews=()):
    assignees = list(row.get('assignees') or [])
    if row.get('assignee') and not assignees:
        assignees = [row['assignee']]
    requests = [(user, False, 'person') for user in (row.get('requested_reviewers') or row.get('reviewers') or [])]
    requests += [(team, False, 'review-team') for team in row.get('requested_reviewers_teams') or []]
    for review in reviews:
        if review.get('state') == 'REQUEST_REVIEW':
            team = review.get('team')
            requests.append((team or review.get('user'), bool(review.get('official')), 'review-team' if team else 'person'))
    def key(person, role='person'):
        require(isinstance(person, dict) and type(person.get('id')) is int and person['id'] > 0, '来源人员缺少稳定编号')
        return kind + ':' + role + ':' + str(person['id'])
    return [key(p) for p in assignees], [(key(p, role), official) for p, official, role in requests]


def requirements(snapshot):
    result = set()
    for item in snapshot.get('projects', []):
        kind = snapshot.get('kind')
        require(kind in ('gitlab', 'gitea'), '人员映射缺少来源平台')
        for dataset in item['datasets']:
            if dataset['category'] not in ('议题', '议题与合并请求', '合并请求', '合并请求详情', '评审'):
                continue
            for row in dataset['rows']:
                assignees, reviewers = references(row if dataset['category'] != '评审' else {}, kind,
                                                  [row] if dataset['category'] == '评审' else [])
                result.update(assignees)
                result.update(key for key, _ in reviewers)
    return result


def mapped(row, kind, mapping, pending, reviews=()):
    assignees, reviewers = references(row, kind, reviews)
    missing = set(assignees) | {key for key, _ in reviewers}
    missing -= mapping.keys()
    if missing:
        pending.append({'category': '人员映射', 'source_id': str(row['id']), 'reason': '需要明确目标已有账号：' + '、'.join(sorted(missing))})
        return {}
    def ids(keys):
        return sorted({user['id'] for key in keys for user in mapping[key]})
    return {'assignee_ids': ids(assignees), 'reviewer_ids': ids(key for key, _ in reviewers),
            'official_reviewer_ids': ids(key for key, official in reviewers if official)}


def details(snapshot):
    result = {}
    def add(person, role, team=False):
        require(isinstance(person, dict) and type(person.get('id')) is int, '来源人员缺少稳定编号')
        key = snapshot['kind'] + (':review-team:' if team else ':person:') + str(person['id'])
        value = result.setdefault(key, {'key': key, 'username': person.get('username') or person.get('login') or person.get('name'),
                                        'name': person.get('name') or person.get('full_name') or '', 'roles': []})
        if role not in value['roles']: value['roles'].append(role)
    for item in snapshot.get('projects', []):
        for dataset in item['datasets']:
            if dataset['category'] not in ('议题', '议题与合并请求', '合并请求', '合并请求详情', '评审'): continue
            for row in dataset['rows']:
                for person in row.get('assignees') or ([row['assignee']] if row.get('assignee') else []): add(person,'负责人')
                for person in row.get('requested_reviewers') or row.get('reviewers') or []: add(person,'当前评审人')
                for team in row.get('requested_reviewers_teams') or []: add(team,'当前评审团队',True)
                if dataset['category']=='评审' and row.get('state')=='REQUEST_REVIEW':
                    add(row.get('team') or row.get('user'),'当前评审人',bool(row.get('team')))
    return sorted(result.values(),key=lambda row:row['key'])
