"""确定性人员匹配、分页复核和显式批量确认；不创建账号或授予权限。"""
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
import json
import re
import tempfile
import time
from urllib.parse import quote

from models import db, GiteaServer
from svn_migration.common import digest, encode, file_lock, require
from svn_migration.engine import Process
from .models import GitMigrationAccountMapping, GitMigrationInventory, GitMigrationSource
from .store import get, root
from .target import Receiver
from . import people, rules


def profile(user):
    require(isinstance(user, dict) and type(user.get('id')) is int and user['id'] > 0,
            '账号列表缺少稳定用户编号', 'source_format')
    return {'id': user['id'], 'login': user.get('login') or user.get('username') or '',
            'name': user.get('full_name') or user.get('name') or '', 'email': user.get('email') or '',
            'active': user.get('active', True) and user.get('state', 'active') == 'active' and not user.get('prohibit_login', False),
            'admin': user.get('is_admin') is True}


def normalized(value):
    # 不删除点号、加号或域名，不把近似用户名当作同一身份。
    return value.strip().casefold() if isinstance(value, str) else ''


def permissions(members):
    result = {}
    for member in members:
        rights = set()
        if member.get('active') is not False:
            for grant in member.get('grants') or []:
                if grant.get('expires_unix', 0) and grant['expires_unix'] <= time.time():
                    continue
                rights.update(k for k, enabled in (grant.get('abilities') or {}).items() if enabled)
            for source in member.get('sources') or []:
                if source.get('expires_unix', 0) and source['expires_unix'] <= time.time():
                    continue
                for unit, access in (source.get('units') or {}).items():
                    if access not in ('read', 'write', 'admin'):
                        continue
                    rights.update({'repo.code': {'read_code'}, 'repo.issues': {'read_issues'},
                                   'repo.pulls': {'read_pulls'}}.get(unit, set()))
                    if unit == 'repo.code' and access in ('write', 'admin'):
                        rights.add('push_code')
        result[member['user_id']] = sorted(rights)
    return result


def target_accounts(receiver):
    result, seen = [], set()
    for page in range(1, 1001):
        batch = receiver.api('GET', f'/admin/users?page={page}&limit=100')
        require(isinstance(batch, list), '目标账号列表格式无效')
        if not batch:
            return result
        for user in batch:
            item = profile(user)
            require(item['id'] not in seen, '目标账号分页重复，不能确认完整名单', 'pagination')
            require(item['login'], '目标账号缺少登录名')
            seen.add(item['id'])
            result.append(item)
        require(len(result) <= 50000, '目标账号超过五万，本次未生成不完整匹配', 'capacity')
    require(False, '目标账号分页超限', 'capacity')


def needed_rows(snapshot, source_users):
    by_id = {u['id']: u for u in source_users}
    by_login = {normalized(u['login']): u for u in source_users}
    details = {d['key']: d for d in people.details(snapshot)}
    grouped = {}
    rule_rights = rules.principal_rights(snapshot)
    for key in rules.requirements(snapshot):
        parts = key.split(':', 2)
        person = parts[1] in ('person', 'user')
        user = by_id.get(int(parts[2])) if person and parts[2].isdigit() else by_login.get(normalized(parts[2])) if person else None
        canonical = f"{parts[0]}:person:{user['id']}" if user else key
        detail = details.get(key, {})
        row = grouped.setdefault(canonical, {'key': canonical, 'principal_keys': [], 'kind': 'person' if person else 'group',
            'source': user or {'id': int(parts[2]) if parts[2].isdigit() else 0, 'login': detail.get('username') or parts[2],
                               'name': detail.get('name') or '', 'email': '', 'active': False, 'admin': False},
            'roles': [], 'required_rights': [], 'confirmed': False, 'selected_ids': [], 'manual': False, 'resolution': 'account'})
        if not person:
            row['source']['name'] = ('可信 CI 回执账号' if key == 'gitlab:ci:issuer' else
                'GitLab ' + {'30':'开发者及以上','40':'维护者及以上','50':'所有者'}.get(parts[2], '权限角色 '+parts[2]) if parts[1] == 'access' else
                detail.get('username') or f"{'GitLab' if parts[0] == 'gitlab' else 'Gitea'} 团队或群组 {parts[2]}")
        row['principal_keys'].append(key)
        row['roles'] = sorted(set(row['roles']) | set(detail.get('roles') or (['可信 CI 回执'] if key == 'gitlab:ci:issuer' else ['规则主体'])))
        rights = {'read_pulls'} if parts[1] == 'review-team' else rule_rights.get(key, set())
        row['required_rights'] = sorted(set(row['required_rights']) | rights)
    # 负责人所属单元分开检查，评审只需要合并请求读取权限。
    by_key = {key: row for row in grouped.values() for key in row['principal_keys']}
    for item in snapshot.get('projects', []):
        for dataset in item['datasets']:
            if dataset['category'] not in ('议题', '议题与合并请求', '合并请求', '合并请求详情', '评审'):
                continue
            for value in dataset['rows']:
                assignees, reviewers = people.references(value if dataset['category'] != '评审' else {}, snapshot['kind'],
                    [value] if dataset['category'] == '评审' else [])
                for key in assignees:
                    row = by_key[key]
                    right = 'read_pulls' if value.get('pull_request') or dataset['category'] in ('合并请求', '合并请求详情') else 'read_issues'
                    row['required_rights'] = sorted(set(row['required_rights']) | {right})
                for key, _ in reviewers:
                    row = by_key[key]
                    row['required_rights'] = sorted(set(row['required_rights']) | {'read_pulls'})
    critical = rules.governance_requirements(snapshot)
    for row in grouped.values():
        row['critical'] = bool(set(row['principal_keys']) & critical) or row['kind'] != 'person'
    return sorted(grouped.values(), key=lambda row: row['key'])


def eligible(account, row, rights):
    return bool(account['active'] and (account['admin'] or set(row['required_rights']) <= set(rights.get(account['id'], []))))


def suggest(rows, accounts, rights):
    indexes = {field: defaultdict(list) for field in ('login', 'email', 'name')}
    account_map = {u['id']: u for u in accounts}
    for user in accounts:
        for field, index in indexes.items():
            value = normalized(user[field])
            if value:
                index[value].append(user['id'])
    claims = defaultdict(list)
    for row in rows:
        source = row['source']
        exact = set(indexes['login'].get(normalized(source['login']), [])) | set(indexes['email'].get(normalized(source['email']), []))
        similar = set(indexes['name'].get(normalized(source['name']), []))
        candidates = sorted(exact or similar) if row['kind'] == 'person' else []
        row.update(candidate_ids=candidates, match_status='unmatched', reason='未找到可直接确认的目标账号')
        if row['kind'] != 'person':
            row['reason'] = '团队、角色或可信 CI 账号需要管理员明确指定'
        elif len(exact) == 1 and source['active']:
            account = account_map[next(iter(exact))]
            if eligible(account, row, rights):
                row.update(match_status='automatic', selected_ids=[account['id']], reason='邮箱一致 · 唯一候选' if normalized(source['email']) and normalized(source['email']) == normalized(account['email']) else '账号名一致 · 唯一候选')
                claims[account['id']].append(row)
            else:
                row['reason'] = '目标账号不可用或缺少所需群组权限'
        elif candidates:
            row.update(match_status='ambiguous', reason='存在多个身份线索或仅姓名相同，请人工核对')
    for collisions in claims.values():
        if len(collisions) > 1:
            for row in collisions:
                row.update(match_status='ambiguous', selected_ids=[], reason='多个来源人员指向同一目标账号，请人工核对')
    return rows


@contextmanager
def connection(server, tick=lambda: None):
    with tempfile.TemporaryDirectory(prefix='mapping-', dir=root()) as folder:
        receiver = Receiver(server, Process(folder, tick=tick), folder)
        try:
            receiver.check()
            yield receiver
        finally:
            receiver.close()


def enqueue(data, actor_id):
    from .runs import target_identity
    require(isinstance(data, dict) and set(data) == {'inventory_id', 'server_id', 'group_id', 'request_key'}, '人员匹配参数不完整')
    for name in ('inventory_id', 'server_id', 'group_id'):
        require(type(data[name]) is int and data[name] > 0, '人员匹配编号无效')
    require(isinstance(data['request_key'], str) and re.fullmatch(r'[A-Za-z0-9_-]{16,80}', data['request_key']), '请求标识无效')
    with file_lock(root() / 'enqueue.lock', blocking=True):
        old = GitMigrationAccountMapping.query.filter_by(request_key=data['request_key']).first()
        if old:
            require(old.actor_id == actor_id and all(getattr(old, k) == data[k] for k in ('inventory_id', 'server_id', 'group_id')), '请求标识已用于其他匹配', 'conflict', 409)
            return old
        inventory = get(GitMigrationInventory, data['inventory_id'])
        source = get(GitMigrationSource, inventory.source_id)
        require(inventory.status == 'succeeded' and source.version == inventory.source_version and not source.archived, '盘点已失效，请重新盘点', 'stale', 409)
        server = get(GiteaServer, data['server_id'])
        row = GitMigrationAccountMapping(**data, actor_id=actor_id, source_id=source.id, source_version=source.version,
            context_json=encode({'target': target_identity(server), 'inventory_digest': digest(json.loads(inventory.result_json))}))
        db.session.add(row)
        db.session.commit()
        return row


def scan(row, provider, tick):
    from .runs import target_identity
    inventory = get(GitMigrationInventory, row.inventory_id)
    snapshot = json.loads(inventory.result_json)
    identity = provider.authenticate()
    require(identity['actor'] == snapshot['actor'] and identity['version'] == snapshot['version'], '来源身份或版本已变化', 'stale')
    source_users = [profile(u) for u in provider.collection('/users' if provider.kind == 'gitlab' else '/admin/users')]
    context = json.loads(row.context_json)
    server = get(GiteaServer, row.server_id)
    require(target_identity(server) == context['target'], '目标连接已变化，请重新建立匹配', 'stale')
    with connection(server, tick) as receiver:
        selection = receiver.selected_scope(row.group_id)
        accounts = target_accounts(receiver)
        rights = permissions(receiver.members(f'/governance/groups/{row.group_id}/members'))
        rows = suggest(needed_rows(snapshot, source_users), accounts, rights)
        previous = json.loads(row.result_json)
        old_rows = {r['key']: r for r in previous.get('rows', [])}
        old_accounts = {a['id']: a for a in previous.get('accounts', [])}
        account_map = {a['id']: a for a in accounts}
        old_selection = context.get('selection') or {}
        # 新增其他成员不抹掉已确认身份；仍逐账号核对当前有效权限。
        if (old_selection.get('id'), old_selection.get('path')) == (selection['id'], selection['path']):
            for item in rows:
                old = old_rows.get(item['key'])
                if old and old['source'] == item['source'] and old['principal_keys'] == item['principal_keys']:
                    if old.get('resolution') == 'source_identity' and not item['critical']:
                        item.update(selected_ids=[], confirmed=old['confirmed'], manual=True, resolution='source_identity')
                    elif old['selected_ids'] and all(old_accounts.get(uid) == account_map.get(uid) and uid in account_map
                            and eligible(account_map[uid], item, rights) for uid in old['selected_ids']):
                        item.update(selected_ids=old['selected_ids'], confirmed=old['confirmed'], manual=old['manual'])
        pending = context.pop('confirmation', None)
        legacy = context.pop('confirm_automatic', [])
        if not pending and legacy:
            pending = {'keys': legacy, 'automatic': True}
        if pending:
            current_rows = {item['key']: item for item in rows}
            for key in pending['keys']:
                old, item = old_rows[key], current_rows.get(key)
                require(item is not None and item.get('resolution') != 'source_identity'
                        and item['source'] == old['source'] and item['selected_ids'] == old['selected_ids']
                        and item['selected_ids'] and context.get('selection') == selection
                        and all(old_accounts.get(uid) == account_map.get(uid) and eligible(account_map[uid], item, rights)
                                for uid in item['selected_ids'])
                        and (not pending['automatic'] or item['match_status'] == 'automatic' and not item['manual']),
                        '批量确认期间人员或权限发生变化，请刷新并重新核对', 'stale')
            for key in pending['keys']:
                current_rows[key]['confirmed'] = True
        context.update(selection=selection, target_actor={k: receiver.actor[k] for k in ('id', 'login')})
    row.context_json = encode(context)
    return {'rows': rows, 'accounts': accounts, 'rights': rights}


def stale(row):
    from .runs import target_identity
    source = db.session.get(GitMigrationSource, row.source_id)
    inventory = db.session.get(GitMigrationInventory, row.inventory_id)
    server = db.session.get(GiteaServer, row.server_id)
    context = json.loads(row.context_json)
    return not source or source.archived or source.version != row.source_version or not inventory or inventory.status != 'succeeded' or digest(json.loads(inventory.result_json)) != context['inventory_digest'] or not server or target_identity(server) != context['target']


FILTERS = ('all', 'automatic', 'ambiguous', 'unmatched', 'confirmed', 'retained', 'rules')


def row_matches(item, query='', status='all'):
    visible = (status == 'all' or status == 'confirmed' and item['confirmed']
        or status == 'retained' and item.get('resolution') == 'source_identity'
        or status == 'rules' and item.get('critical', False) and not item['confirmed']
        or status in ('automatic', 'ambiguous', 'unmatched') and item['match_status'] == status
            and (status == 'automatic' or not item['confirmed']))
    return visible and (not query or normalized(query) in normalized(encode(item['source']) + ' '.join(item['roles'])))


def can_confirm(item, accounts, rights):
    return bool(not item['confirmed'] and item.get('resolution') != 'source_identity' and item['selected_ids']
        and all(uid in accounts and eligible(accounts[uid], item, rights) for uid in item['selected_ids']))


def critical_keys(row):
    inventory = get(GitMigrationInventory, row.inventory_id)
    return rules.governance_requirements(json.loads(inventory.result_json))


def enrich(rows, critical):
    # 旧草稿也从不可变盘点推导权限范围，不依赖浏览器或旧版字段。
    for item in rows:
        item['critical'] = item['kind'] != 'person' or bool(set(item['principal_keys']) & critical)
        item['preservable'] = not item['critical'] and all(':person:' in key for key in item['principal_keys'])


def retention_preview(row, rows):
    pending = [r for r in rows if not r['confirmed'] and not r['manual'] and r['match_status'] in ('ambiguous', 'unmatched')]
    retained = [r for r in pending if r['preservable']]
    blocked = [r for r in rows if r['critical'] and not r['confirmed']]
    return {'count': len(retained), 'blocked': len(blocked), 'remaining': len(rows) - sum(r['confirmed'] for r in rows) - len(retained),
        'impact_hash': digest([row.id, row.revision, [(r['key'], r['source'], r['principal_keys']) for r in retained]]),
        'roles': {role: sum(role in r['roles'] for r in retained) for role in ('负责人', '当前评审人')},
        'blocked_sample': [{'name': r['source']['name'] or r['source']['login'], 'roles': r['roles']} for r in blocked[:5]]}


def public(row, query='', status='all', page=1, size=50):
    require(status in FILTERS, '人员筛选条件无效')
    require(type(page) is int and page > 0 and type(size) is int and 1 <= size <= 100 and len(query) <= 200, '人员分页参数无效')
    data = json.loads(row.result_json)
    rows = data.get('rows', [])
    enrich(rows, critical_keys(row))
    stats = {key: sum(r['match_status'] == key and (key == 'automatic' or not r['confirmed']) for r in rows)
             for key in ('automatic', 'ambiguous', 'unmatched')}
    stats.update(total=len(rows), confirmed=sum(r['confirmed'] for r in rows),
        retained=sum(r['confirmed'] and r.get('resolution') == 'source_identity' for r in rows),
        mapped=sum(r['confirmed'] and r.get('resolution') != 'source_identity' for r in rows),
        rules=sum(r['critical'] and not r['confirmed'] for r in rows),
        automatic_pending=sum(r['match_status'] == 'automatic' and not r['confirmed'] and not r['manual'] for r in rows))
    matching = [r for r in rows if row_matches(r, query, status)]
    accounts = {a['id']: a for a in data.get('accounts', [])}
    rights = {int(k): v for k, v in data.get('rights', {}).items()}
    visible = []
    for item in matching[(page - 1) * size:page * size]:
        visible.append({**item, 'can_confirm': can_confirm(item, accounts, rights),
            'candidates': [{**accounts[uid], 'eligible': eligible(accounts[uid], item, rights)} for uid in item['candidate_ids'][:100] if uid in accounts],
            'selected': [accounts[uid] for uid in item['selected_ids'] if uid in accounts]})
    invalid = stale(row)
    source = db.session.get(GitMigrationSource, row.source_id)
    server = db.session.get(GiteaServer, row.server_id)
    return {'id': row.id, 'source_id': row.source_id, 'source_name': source.name if source else '',
        'server_name': server.name if server else '', 'inventory_id': row.inventory_id, 'server_id': row.server_id, 'group_id': row.group_id,
        'status': row.status, 'revision': row.revision, 'error': row.error, 'stale': bool(invalid), 'stats': stats,
        'ready': row.status == 'succeeded' and not invalid and stats['confirmed'] == len(rows),
        'rows': visible, 'filtered_total': len(matching), 'selectable_total': sum(not r['confirmed'] for r in matching),
        'confirmable_total': sum(can_confirm(r, accounts, rights) for r in matching),
        'retention_preview': retention_preview(row, rows), 'target': json.loads(row.context_json).get('selection')}


def selected_rows(rows, selection):
    require(isinstance(selection, dict), '请选择需要确认的人员')
    if selection.get('mode') == 'keys':
        require(set(selection) == {'mode', 'keys'}, '人员选择参数无效')
        keys = selection['keys']
        require(isinstance(keys, list) and 0 < len(keys) <= 50000 and all(isinstance(k, str) for k in keys)
                and len(keys) == len(set(keys)), '人员选择清单无效')
        by_key = {r['key']: r for r in rows}
        require(all(k in by_key and not by_key[k]['confirmed'] for k in keys), '所选人员已确认或不在固定清单内', 'stale', 409)
        return [by_key[k] for k in keys]
    require(selection.get('mode') == 'filtered' and set(selection) == {'mode', 'status', 'query', 'excluded'}, '人员选择范围无效')
    status, query, excluded = selection['status'], selection['query'], selection['excluded']
    require(status in FILTERS and isinstance(query, str) and len(query) <= 200 and isinstance(excluded, list)
            and len(excluded) <= 50000 and all(isinstance(k, str) for k in excluded), '人员筛选选择无效')
    matches = [r for r in rows if not r['confirmed'] and row_matches(r, query, status)]
    omitted = set(excluded)
    require(omitted <= {r['key'] for r in matches}, '排除项已离开当前选择范围', 'stale', 409)
    return [r for r in matches if r['key'] not in omitted]


def mutate(ident, data):
    require(set(data) <= {'revision', 'action', 'key', 'target_ids', 'selection', 'impact_hash', 'acknowledged'}, '人员确认参数无效')
    with file_lock(root() / 'enqueue.lock', blocking=True):
        db.session.expire_all()
        row = get(GitMigrationAccountMapping, ident)
        require(type(data.get('revision')) is int and data['revision'] == row.revision, '匹配草稿已变化，请刷新后重试', 'stale', 409)
        action = data.get('action')
        require(action in ('select', 'confirm', 'confirm_automatic', 'confirm_selected', 'retain_unmatched', 'refresh'), '人员确认操作无效')
        require(not stale(row) and (row.status == 'succeeded' or action == 'refresh' and row.status in ('failed', 'interrupted')),
                '匹配尚未完成或已失效', 'stale', 409)
        if action == 'refresh':
            context = json.loads(row.context_json)
            context.pop('confirm_automatic', None)
            context.pop('confirmation', None)
            row.context_json = encode(context)
            row.status, row.error, row.error_code, row.completed_at = 'queued', '', '', None
        else:
            state = json.loads(row.result_json)
            enrich(state['rows'], critical_keys(row))
            accounts = {a['id']: a for a in state['accounts']}
            rights = {int(k): v for k, v in state['rights'].items()}
            selected = [r for r in state['rows'] if r['key'] == data.get('key')]
            if action == 'retain_unmatched':
                preview = retention_preview(row, state['rows'])
                require(data.get('acknowledged') is True and data.get('impact_hash') == preview['impact_hash'],
                        '请查看最新影响范围并确认保留来源身份', 'stale', 409)
                require(preview['count'], '没有可统一保留来源身份的人员')
                for item in state['rows']:
                    if not item['confirmed'] and not item['manual'] and item['match_status'] in ('ambiguous', 'unmatched') and item['preservable']:
                        item.update(selected_ids=[], confirmed=True, manual=True, resolution='source_identity')
            elif action in ('confirm_automatic', 'confirm_selected'):
                selected = ([r for r in state['rows'] if r['match_status'] == 'automatic' and not r['confirmed'] and not r['manual']]
                            if action == 'confirm_automatic' else selected_rows(state['rows'], data.get('selection')))
                require(selected, '没有待确认的人员')
                require(all(can_confirm(item, accounts, rights) for item in selected), '所选人员仍有未确定或不可用的目标账号，请先处理')
                context = json.loads(row.context_json)
                context['confirmation'] = {'keys': [r['key'] for r in selected], 'automatic': action == 'confirm_automatic'}
                row.context_json = encode(context)
                row.status, row.error, row.error_code, row.completed_at = 'queued', '', '', None
                db.session.commit()
                return row
            else:
                require(len(selected) == 1, '人员不在当前固定清单中')
                if action == 'select':
                    ids = data.get('target_ids')
                    require(isinstance(ids, list) and all(type(uid) is int and uid in accounts for uid in ids) and len(ids) == len(set(ids)) and len(ids) <= 100, '请选择名单中的目标账号')
                    require(selected[0]['kind'] != 'person' or len(ids) <= 1, '单个来源人员只能对应一个目标账号')
                    selected[0].update(selected_ids=ids, confirmed=False, manual=True, resolution='account')
                else:
                    require(can_confirm(selected[0], accounts, rights) or selected[0]['confirmed'] and selected[0].get('resolution') != 'source_identity', '请先选择可用的目标账号')
                    with connection(get(GiteaServer, row.server_id)) as receiver:
                        context = json.loads(row.context_json)
                        receiver.verify_selected_scope(context['selection'])
                        require({k: receiver.actor[k] for k in ('id','login')} == context['target_actor'], '目标管理员身份已变化', 'stale')
                        rights = permissions(receiver.members(f'/governance/groups/{row.group_id}/members'))
                        for uid in selected[0]['selected_ids']:
                            checked = profile(receiver.api('GET', '/users/' + quote(accounts[uid]['login'], safe='')))
                            require(checked == accounts[uid] and eligible(checked, selected[0], rights), '目标账号身份、状态或权限已变化，请刷新账号列表', 'stale', 409)
                    selected[0]['confirmed'] = True
            row.result_json = encode(state)
        db.session.commit()
        return row


def resolve(ident, inventory, server, group_id, receiver, include_resolutions=False):
    row = get(GitMigrationAccountMapping, ident)
    require(row.inventory_id == inventory.id and row.server_id == server.id and row.group_id == group_id and public(row)['ready'], '人员映射未全部确认或与当前迁移不符', 'mapping_incomplete', 409)
    context, state = json.loads(row.context_json), json.loads(row.result_json)
    critical = critical_keys(row)
    enrich(state['rows'], critical)
    receiver.verify_selected_scope(context['selection'])
    require({k: receiver.actor[k] for k in ('id','login')} == context['target_actor'], '目标管理员身份已变化', 'stale')
    rights = permissions(receiver.members(f'/governance/groups/{group_id}/members'))
    accounts = {a['id']: a for a in state['accounts']}
    checked = {u['id']: u for u in target_accounts(receiver)}
    mapping, retained = {}, {}
    for item in state['rows']:
        preserve = item.get('resolution') == 'source_identity'
        if preserve:
            require(item['preservable'] and not item['selected_ids'], '有效权限主体不能保留为未绑定身份', 'mapping_incomplete', 409)
        else:
            require(item['selected_ids'], '人员映射缺少目标账号', 'mapping_incomplete', 409)
        for uid in item['selected_ids']:
            require(uid in checked and checked[uid] == accounts[uid] and eligible(checked[uid], item, rights), '已确认的目标账号变化，请重新核对人员匹配', 'stale', 409)
        for key in item['principal_keys']:
            mapping[key] = [accounts[uid]['login'] for uid in item['selected_ids']]
            if preserve:
                retained[key] = {'source_id': row.source_id, 'source': item['source'], 'roles': item['roles']}
    require(set(mapping) == set(rules.requirements(json.loads(inventory.result_json))), '人员确认清单与固定盘点不一致')
    return (mapping, retained, {'mapping_id': row.id, 'revision': row.revision}) if include_resolutions else mapping
