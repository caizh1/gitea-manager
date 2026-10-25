"""明确主体映射后的分支保护转换；无法证明等价的规则保留为阻断项。"""
from urllib.parse import quote
from svn_migration.common import require
from services.policy_rules import normalize_rule, API_FIELDS

PRINCIPAL_FIELDS = {
    'push_whitelist_usernames': 'push_whitelist_teams',
    'force_push_allowlist_usernames': 'force_push_allowlist_teams',
    'merge_whitelist_usernames': 'merge_whitelist_teams',
    'approvals_whitelist_username': 'approvals_whitelist_teams',
}


def principal_key(entry):
    if entry.get('user_id'):
        return 'gitlab:user:' + str(entry['user_id'])
    if entry.get('group_id'):
        return 'gitlab:group:' + str(entry['group_id'])
    return 'gitlab:access:' + str(entry.get('access_level', 0))


def requirements(inventory):
    keys = set()
    for item in inventory.get('projects', []):
        for dataset in item['datasets']:
            if dataset['category'] in ('分支保护', '标签保护'):
                for rule in dataset['rows']:
                    if dataset['category']=='标签保护' and 'create_access_levels' in rule:keys.add('gitlab:access:40')
                    for field in ('push_access_levels', 'merge_access_levels', 'create_access_levels'):
                        keys.update(principal_key(p) for p in rule.get(field, []) if p.get('access_level') != 0 or p.get('user_id') or p.get('group_id'))
                    keys.update('gitea:user:' + name for name in rule.get('whitelist_usernames', []) or [])
                    keys.update('gitea:team:' + name for name in rule.get('whitelist_teams', []) or [])
                    for user_field, team_field in PRINCIPAL_FIELDS.items():
                        keys.update('gitea:user:' + name for name in rule.get(user_field, []) or [])
                        keys.update('gitea:team:' + name for name in rule.get(team_field, []) or [])
    return sorted(keys)


def validate_mapping(raw, required, receiver):
    require(isinstance(raw, dict) and set(raw) == set(required), '请完成所有规则主体映射；不能增加未使用的映射')
    result = {}
    for key, names in raw.items():
        require(isinstance(names, list) and 0 < len(names) <= 100 and all(isinstance(n, str) and n.strip() == n and n for n in names),
                '每个规则主体必须指定目标已有用户')
        users = []
        for name in sorted(set(names)):
            user = receiver.api('GET', '/users/' + quote(name, safe=''))
            require(user.get('active', True) and not user.get('prohibit_login'), '规则映射账号不可用')
            users.append({'id': user['id'], 'login': user['login']})
        result[key] = users
    return result


def convert(inventory, kind, mapping, normalized):
    rules = []
    for dataset in inventory['datasets']:
        if dataset['category'] != '分支保护':
            continue
        complete = True
        for source in dataset['rows']:
            if kind == 'gitea':
                if any(source.get(field) for field in ('enable_bypass_allowlist', 'require_governance_approval', 'code_owner_approval_required')):
                    complete = False
                    continue
                raw = {key: value for key, value in source.items() if key in API_FIELDS}
                for user_field, team_field in PRINCIPAL_FIELDS.items():
                    keys = ['gitea:user:' + name for name in source.get(user_field, []) or []]
                    keys += ['gitea:team:' + name for name in source.get(team_field, []) or []]
                    require(all(key in mapping for key in keys), '来源分支规则缺少目标人员映射')
                    raw[user_field] = sorted({user['login'] for key in keys for user in mapping[key]})
                    raw[team_field] = []
                rule = normalize_rule(raw, imported=True)
                rule.pop('team_org', None)
                rules.append(rule)
                continue
            name = source['name']
            if source.get('code_owner_approval_required'):
                complete = False
                continue
            def users(field):
                values = set()
                for principal in source.get(field, []):
                    if principal.get('access_level') == 0 and not principal.get('user_id') and not principal.get('group_id'):
                        continue
                    key = principal_key(principal)
                    require(key in mapping, '分支保护缺少主体映射：' + key)
                    values.update(user['login'] for user in mapping[key])
                return sorted(values)
            push, merge = users('push_access_levels'), users('merge_access_levels')
            raw = {'rule_name': name, 'enable_push': bool(push),
                   'enable_push_whitelist': bool(push), 'push_whitelist_usernames': push,
                   'enable_merge_whitelist': True, 'merge_whitelist_usernames': merge,
                   'enable_force_push': bool(source.get('allow_force_push') and push),
                   'enable_force_push_allowlist': bool(source.get('allow_force_push') and push),
                   'force_push_allowlist_usernames': push if source.get('allow_force_push') else [],
                   'block_admin_merge_override': True}
            rule = normalize_rule(raw)
            rule.pop('team_org', None)
            rule['migration_gitlab'] = True
            rules.append(rule)
        if complete:
            normalized['pending'] = [p for p in normalized['pending'] if p.get('path') != dataset['path']]
    rules.extend(convert_tags(inventory, kind, mapping, normalized))
    return rules


def apply(receiver, repo, rules, mapping, progress, save):
    if not rules:
        return
    path = '/repos/' + quote(repo['full_name'], safe='/')
    for users in mapping.values():
        for expected in users:
            actual = receiver.api('GET', '/users/' + quote(expected['login'], safe=''))
            require(actual['id'] == expected['id'], '映射账号已更换，禁止沿用同名账号')
            permission = receiver.api('GET', path + '/collaborators/' + quote(expected['login'], safe='') + '/permission')
            require(permission.get('permission') in ('write', 'admin', 'owner'), '映射账号没有目标代码写入权限；请由管理员配置已有群组授权')
    state = progress.setdefault('rules', {})
    for rule in rules:
        if rule.get('_tag'):
            apply_tag(receiver, path, rule, progress, save)
            continue
        name = rule['rule_name']
        current = receiver.api('GET', path + '/branch_protections')
        existing = next((r for r in current if r.get('rule_name') == name), None)
        if existing is None:
            require(not state.get(name, {}).get('verified'), '已写入的迁移规则消失，不能自动重建')
            state[name] = {'intent': rule}
            save()
            receiver.session.headers['X-Gitea-Migration-Key'] = progress['key']
            try:
                receiver.api('POST', path + '/branch_protections', rule)
            finally:
                receiver.session.headers.pop('X-Gitea-Migration-Key', None)
        else:
            require(name in state, '目标已有同名规则，不自动覆盖')
        current = receiver.api('GET', path + '/branch_protections')
        actual = next((r for r in current if r.get('rule_name') == name), None)
        require(actual is not None, '目标规则写入后未找到')
        require(not any(actual.get(field) for field in ('enable_bypass_allowlist', 'require_governance_approval', 'code_owner_approval_required')),
                '目标规则出现未规划的增强约束或绕过名单')
        normalized = normalize_rule({k: v for k, v in actual.items() if k in API_FIELDS}, imported=True)
        normalized.pop('team_org', None)
        if rule.get('migration_gitlab'):
            require(actual.get('migration_gitlab') is True, '目标未保留 GitLab 规则叠加语义')
            normalized['migration_gitlab'] = True
        require(normalized == rule, '目标规则或人员名单回读不一致')
        state[name]['verified'] = True
        save()


def convert_tags(inventory, kind, mapping, normalized):
    import re
    result=[]
    for dataset in inventory['datasets']:
        if dataset['category']!='标签保护':continue
        for source in dataset['rows']:
            if kind=='gitlab':
                name=source['name']
                pattern='/^'+re.escape(name).replace(r'\*','.*')+'$/'
                keys=[principal_key(p) for p in source.get('create_access_levels',[]) if p.get('access_level') or p.get('user_id') or p.get('group_id')]
            else:
                pattern=source['name_pattern']
                keys=['gitea:user:'+n for n in source.get('whitelist_usernames',[]) or []]+['gitea:team:'+n for n in source.get('whitelist_teams',[]) or []]
            require(all(key in mapping for key in keys),'标签保护缺少主体映射')
            converted={'_tag':True,'name_pattern':pattern,'whitelist_usernames':sorted({u['login'] for key in keys for u in mapping[key]}),'whitelist_teams':[]}
            if kind=='gitlab':
                require('gitlab:access:40' in mapping,'标签删除维护者映射缺失')
                converted.update(migration_gitlab=True,delete_whitelist_usernames=sorted(u['login'] for u in mapping['gitlab:access:40']))
            result.append(converted)
        normalized['pending']=[v for v in normalized['pending'] if v.get('path')!=dataset['path']]
    return result


def apply_tag(receiver, path, rule, progress, save):
    wanted={k:v for k,v in rule.items() if k!='_tag'}
    key='标签:'+rule['name_pattern'];state=progress.setdefault('rules',{})
    rows=receiver.api('GET',path+'/tag_protections')
    current=next((r for r in rows if r['name_pattern']==rule['name_pattern']),None)
    if current is None:
        require(not state.get(key,{}).get('verified'),'已写入的标签规则消失')
        state[key]={'intent':wanted};save()
        receiver.session.headers['X-Gitea-Migration-Key']=progress['key']
        try:receiver.api('POST',path+'/tag_protections',wanted)
        finally:receiver.session.headers.pop('X-Gitea-Migration-Key',None)
    else:require(key in state,'目标已有同名标签规则，不能接管')
    rows=receiver.api('GET',path+'/tag_protections')
    current=next((r for r in rows if r['name_pattern']==rule['name_pattern']),None)
    require(current and all(sorted(current.get(k) or [])==sorted(v) if isinstance(v,list) else current.get(k)==v for k,v in wanted.items()),'标签规则回读不一致')
    state[key]['verified']=True;save()
