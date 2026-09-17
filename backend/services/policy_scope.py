"""组织归属和名单身份绑定。名称用于 Gitea API，稳定 ID 用于阻止同名替换。"""
import copy
import re
from urllib.parse import quote

from services.policy_rules import PolicyError, LIST_FIELDS, normalize_rules, fingerprint

USER_FIELDS = [k for k in LIST_FIELDS if k.endswith('_usernames') or k == 'approvals_whitelist_username']
TEAM_FIELDS = [k for k in LIST_FIELDS if k.endswith('_teams')]


def object_id(value, label):
    if type(value) is not int or value <= 0:
        raise PolicyError(label + '缺少有效的稳定 ID，请重新选择')
    return value


def name_value(value, label, team=False):
    valid = isinstance(value, str) and bool(value) and len(value) <= 200 and value == value.strip() and not any(ord(c) < 32 for c in value)
    if not valid or not team and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', value):
        raise PolicyError(label + '名称无效，请从候选中选择')
    return value


def organization(client, value):
    if not isinstance(value, dict):
        raise PolicyError('请先选择模板所属组织；旧模板需先归属组织并核验名单')
    oid = object_id(value.get('id'), '组织')
    name = name_value(value.get('name'), '组织')
    actual = client.request('GET', '/orgs/' + quote(name, safe=''))
    if not isinstance(actual, dict) or actual.get('id') != oid:
        raise PolicyError('组织已删除、改名或同名重建，请重新选择组织')
    return {'id': oid, 'name': actual['name'], 'server_id': client.server_id}


def scoped_rules(raw, org):
    if not isinstance(raw, list):
        raise PolicyError('规则必须是数组')
    for rule in raw:
        if not isinstance(rule, dict):
            raise PolicyError('规则必须是对象')
        team_org = rule.get('team_org', '')
        if not isinstance(team_org, str):
            raise PolicyError('团队组织信息格式错误，请重新选择组织')
        if any(rule.get(k) for k in TEAM_FIELDS) and team_org.lower() not in ('', org['name'].lower()):
            raise PolicyError('规则包含其他组织的团队，请通过“复制到其他组织”明确重新选择；不能按同名自动转换')
    return normalize_rules([{**r, 'team_org': org['name']} for r in raw])


def selected_names(rules, fields):
    return sorted({name for r in rules for k in fields for name in r[k]})


def verify_identities(client, org, rules, supplied=None, *, resolve_names=False, refresh_names=False):
    """首次导入/明确归属才允许名称解析。旧身份通过 ID 查询，改名需明确确认刷新。"""
    if not isinstance(supplied, dict):
        supplied = {}
    instance_key = fingerprint([client.server_id, client.url])
    if supplied.get('instance_key') and supplied['instance_key'] != instance_key:
        raise PolicyError('实例地址已变化，旧名单身份不能直接复用；请重新选择实例和名单')
    result, replacements = {'users': [], 'teams': []}, {'users': {}, 'teams': {}}
    for kind, fields in (('users', USER_FIELDS), ('teams', TEAM_FIELDS)):
        refs = supplied.get(kind, [])
        if not isinstance(refs, list) or len(refs) > 10000 or any(not isinstance(x, dict) for x in refs):
            raise PolicyError('名单身份格式错误')
        indexed = {}
        for ref in refs:
            name = name_value(ref.get('name'), '名单', team=kind == 'teams')
            if name.lower() in indexed and indexed[name.lower()].get('id') != ref.get('id'):
                raise PolicyError('同名名单包含多个身份，请重新选择')
            indexed[name.lower()] = ref
        for name in selected_names(rules, fields):
            ref = indexed.get(name.lower())
            if ref:
                rid = object_id(ref.get('id'), '名单 ' + name)
                if ref.get('server_id') != client.server_id or kind == 'teams' and ref.get('organization_id') != org['id']:
                    raise PolicyError('名单 ' + name + ' 属于其他实例或组织，请重新选择')
                if kind == 'users':
                    data = client.request('GET', '/users/search', params={'uid': rid, 'limit': 1})
                    values = data.get('data', []) if isinstance(data, dict) else []
                    actual = values[0] if len(values) == 1 else None
                else:
                    actual = client.request('GET', f'/teams/{rid}', allow_missing=True)
                if not actual or actual.get('id') != rid:
                    raise PolicyError(f'{name} 的原身份已删除或不可见，禁止自动使用同名新对象；请移除后重新选择')
            elif resolve_names:
                if kind == 'users':
                    actual = client.request('GET', '/users/' + quote(name, safe=''))
                else:
                    candidates = client.pages('/orgs/' + quote(org['name'], safe='') + '/teams/search', {'q': name})
                    actual = next((x for x in candidates if x.get('name', '').lower() == name.lower()), None)
                    if actual:
                        # 1.26.4 团队搜索的 organization 为 null，详情接口才提供所属组织。
                        actual = client.request('GET', f'/teams/{object_id(actual.get("id"), "团队")}', allow_missing=True)
                if not actual:
                    raise PolicyError('未找到名单 ' + name + '，请重新选择')
            else:
                raise PolicyError('名单 ' + name + ' 尚未绑定身份，请先核验名单或从候选重新选择')
            actual_name = actual.get('login' if kind == 'users' else 'name')
            name_value(actual_name, '名单', team=kind == 'teams')
            if kind == 'teams' and (actual.get('organization') or {}).get('id') != org['id']:
                raise PolicyError('团队 ' + name + ' 不属于模板组织，禁止同名替换')
            if kind == 'users' and (not actual.get('active', True) or actual.get('prohibit_login')):
                raise PolicyError('人员 ' + name + ' 已停用或禁止登录')
            if actual_name != name and not refresh_names:
                raise PolicyError(f'{name} 已改名为 {actual_name}，请先核验名单并确认更新显示名称')
            item = {'id': object_id(actual.get('id'), '名单'), 'name': actual_name, 'server_id': client.server_id}
            if kind == 'teams':
                item['organization_id'] = org['id']
            result[kind].append(item)
            replacements[kind][name] = actual_name
    updated = copy.deepcopy(rules)
    for rule in updated:
        for kind, fields in (('users', USER_FIELDS), ('teams', TEAM_FIELDS)):
            for field in fields:
                rule[field] = sorted({replacements[kind][name] for name in rule[field]})
    result['instance_key'] = instance_key
    return result, updated


def template_scope(template):
    import json
    if not template.organization_id:
        raise PolicyError('此模板尚未归属组织，请先选择组织并核验名单；历史记录仍可查看')
    return {'organization': {'id': template.organization_id, 'name': template.organization_name, 'server_id': template.server_id},
            'identities': json.loads(template.identities_json or '{}'), 'source': json.loads(template.source_json or '{}')}


def validate_repo_scope(client, repo, scope):
    org = scope.get('organization', {})
    if (org.get('server_id') != client.server_id or org.get('id') != repo['owner']['id']
            or org.get('name', '').lower() != repo['owner']['login'].lower()):
        raise PolicyError('仓库不在模板所属组织内，或已转移/组织已改名；请重新选择组织及仓库并预览')
    key = scope.get('identities', {}).get('instance_key')
    if key and key != fingerprint([client.server_id, client.url]):
        raise PolicyError('实例地址已变化，旧名单身份不能直接复用，请重新选择名单')
