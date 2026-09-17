"""策略接口适配；凭据只在服务端使用，所有请求共享数据库限速。"""
import json
import time
import threading
from urllib.parse import quote, urlsplit

import requests
from sqlalchemy.exc import IntegrityError

from models import db, PolicyRequestSlot
from services.policy_rules import PolicyError, fingerprint, normalize_rules, matches

_organization_cache = {}
_organization_lock = threading.Lock()


def connection_hash(server):
    return fingerprint([server.gitea_url.rstrip('/'), server.api_token])


def reserve_slot(server_id, rate=2):
    if db.session.get(PolicyRequestSlot, server_id) is None:
        try:
            db.session.add(PolicyRequestSlot(server_id=server_id, next_at=0))
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
    now = time.time()
    next_at = db.session.execute(db.text('''UPDATE policy_request_slots
        SET next_at = MAX(next_at, :now) + :interval WHERE server_id = :sid
        RETURNING next_at'''), {'now': now, 'interval': 1 / rate, 'sid': server_id}).scalar_one()
    db.session.commit()
    return max(0, next_at - 1 / rate - time.time())


class PolicyGitea:
    def __init__(self, server):
        self.server_id = server.id
        self.url = server.gitea_url.rstrip('/')
        if not self.url.startswith(('http://', 'https://')):
            self.url = 'http://' + self.url
        parsed = urlsplit(self.url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise PolicyError('Gitea 地址不能包含凭据、查询参数或片段')
        self.token = server.api_token

    def request(self, method, path, **kwargs):
        allow_missing = kwargs.pop('allow_missing', False)
        time.sleep(reserve_slot(self.server_id))
        try:
            response = requests.request(method, self.url + '/api/v1' + path,
                                        headers={'Authorization': 'token ' + self.token},
                                        timeout=(5, 30), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise PolicyError('Gitea 网络请求失败或超时；写请求的实际结果必须重新读取确认') from None
        if response.status_code == 404 and allow_missing:
            return None
        if response.status_code not in (200, 201, 204):
            reasons = {401: 'Token 无效或已过期', 403: '权限不足或规则已存在', 404: '仓库、用户或接口不存在',
                       422: 'Gitea 拒绝规则或人员团队配置', 429: 'Gitea 请求限流', 503: 'Gitea 暂不可用'}
            # 不记录不可信的响应正文，其中可能包含地址、凭据或私有数据。
            raise PolicyError(f'Gitea HTTP {response.status_code}：{reasons.get(response.status_code, "请求未成功")}' )
        if response.status_code == 204:
            return None
        self.total_count = int(getattr(response, 'headers', {}).get('X-Total-Count', '0') or 0)
        try:
            return response.json()
        except ValueError:
            raise PolicyError('Gitea 返回了无效 JSON') from None

    def pages(self, path, params=None):
        result, page = [], 1
        while True:
            data = self.request('GET', path, params={**(params or {}), 'page': page, 'limit': 50})
            values = data.get('data') if isinstance(data, dict) else data
            if not isinstance(values, list):
                raise PolicyError('Gitea 分页响应格式错误')
            result.extend(values)
            if len(values) < 50:
                return result
            page += 1
            if page > 2000:
                raise PolicyError('仓库数据超出本期支持规模')

    def check_version(self):
        version = self.request('GET', '/version').get('version', '')
        if version != '1.26.4':
            raise PolicyError(f'当前仅完成 Gitea 1.26.4 适配，目标版本为 {version or "未知"}')

    def organizations(self, query='', page=1):
        # 社区版 /orgs 没有关键词参数。短暂缓存可见目录后搜索，写入仍逐次校验真实 ID。
        key = fingerprint([self.server_id, self.url, self.token])
        with _organization_lock:
            cached = _organization_cache.get(key)
            if not cached or cached[0] < time.monotonic():
                values = self.pages('/orgs')
                items = [{'id': x['id'], 'name': x['name'], 'label': x['name'], 'server_id': self.server_id} for x in values]
                _organization_cache.clear() if len(_organization_cache) > 32 else None
                _organization_cache[key] = (time.monotonic() + 30, items)
            else:
                items = cached[1]
        selected = [x for x in items if query.lower() in x['name'].lower()]
        return {'items': selected[(page-1)*20:page*20], 'page': page, 'total': len(selected), 'has_more': page*20 < len(selected)}

    def candidates(self, kind, query='', page=1, organization='', organization_id=None):
        """每次只读取一页；候选可见性不代表目标仓库适用权限。"""
        limit = 20
        if organization:
            from services.policy_scope import organization as check_organization
            org = check_organization(self, {'id': organization_id, 'name': organization})
            organization = org['name']
        elif kind == 'teams':
            raise PolicyError('请选择模板所属组织')
        member_page = kind == 'users' and organization and not query
        path = ('/orgs/' + quote(organization, safe='') + '/members') if member_page else '/users/search' if kind == 'users' else '/orgs/' + quote(organization, safe='') + '/teams/search'
        data = self.request('GET', path, params={'q': query, 'page': page, 'limit': limit})
        if member_page and isinstance(data, list):
            data = {'data': data}
        if not isinstance(data, dict) or data.get('ok') is False or not isinstance(data.get('data'), list):
            raise PolicyError('Gitea 返回了无效的名单查询结果')
        items = []
        for item in data['data']:
            name = item.get('login' if kind == 'users' else 'name') if isinstance(item, dict) else None
            if not isinstance(name, str) or not name:
                raise PolicyError('Gitea 返回的名单缺少名称')
            # 不把邮箱、权限明细或其他个人信息转发到浏览器。
            label = name if kind == 'users' else organization + ' / ' + name
            from services.policy_scope import object_id
            row = {'id': object_id(item.get('id'), '候选名单'), 'server_id': self.server_id, 'name': name, 'label': label}
            if kind == 'teams':
                # 搜索本身在已验证的组织路径下；详情的稳定归属在保存时逐 ID 核验。
                if item.get('organization') and item['organization'].get('id') != organization_id:
                    raise PolicyError('团队候选的组织身份不一致')
                row['organization_id'] = organization_id
            items.append(row)
        total = getattr(self, 'total_count', 0)
        return {'items': items, 'page': page, 'has_more': page * limit < total or (not total and len(items) == limit)}

    def repo(self, repo_id):
        repo = self.request('GET', f'/repositories/{repo_id}')
        if repo.get('id') != repo_id:
            raise PolicyError('仓库身份校验失败')
        if not repo.get('permissions', {}).get('admin'):
            raise PolicyError('当前 Token 不具备该仓库的管理权限')
        if repo.get('archived') or repo.get('mirror'):
            raise PolicyError('归档仓库和拉取镜像仓库不在本期支持范围')
        return repo

    @staticmethod
    def path(repo):
        return '/repos/' + quote(repo['owner']['login'], safe='') + '/' + quote(repo['name'], safe='')

    @staticmethod
    def identity(repo):
        result = {k: repo[k] for k in ('id', 'full_name', 'default_branch')} | {'owner_id': repo['owner']['id']}
        url = repo.get('html_url', '')
        if isinstance(url, str) and urlsplit(url).scheme in ('http', 'https') and not urlsplit(url).username:
            result['html_url'] = url
        return result

    def protections(self, repo):
        data = self.request('GET', self.path(repo) + '/branch_protections')
        if not isinstance(data, list):
            raise PolicyError('分支保护响应格式错误')
        return data

    def import_rules(self, repo_id):
        self.check_version()
        repo = self.repo(repo_id)
        rules = self.protections(repo)
        for rule in rules:
            rule['team_org'] = repo['owner']['login']
        return {'repo': self.identity(repo), 'rules': normalize_rules(rules, imported=True) if rules else []}

    def validate_people(self, repo, rules, identities=None):
        path = self.path(repo)
        users, team_names = {}, {}
        for rule in rules:
            teams = {n for k, v in rule.items() if k.endswith('_teams') for n in v}
            if teams and rule['team_org'].lower() != repo['owner']['login'].lower():
                raise PolicyError(f'团队属于 {rule["team_org"]}，不能按同名映射到 {repo["owner"]["login"]}；请另存模板并明确选择目标团队')
            for key, values in rule.items():
                target = team_names if key.endswith('_teams') else users if key.endswith('_usernames') or key == 'approvals_whitelist_username' else None
                if target is not None:
                    requirement = 'repo.pulls' if key.startswith('approvals_') else 'repo.code'
                    for name in values:
                        target.setdefault(name, set()).add(requirement)
        available = None
        memberships = {}
        evidence = {'users': [], 'teams': []}
        def check_identity(kind, name, actual):
            if identities is None:
                return
            ref = next((r for r in identities.get(kind, []) if r['name'].lower() == name.lower()), None)
            if not ref or ref.get('server_id') != self.server_id or actual.get('id') != ref.get('id'):
                raise PolicyError(f'{name} 的身份已删除、同名重建或未核验，请重新选择名单并预览')
            if kind == 'teams' and ref.get('organization_id') != repo['owner']['id']:
                raise PolicyError(f'团队 {name} 的组织身份不一致')
        def repository_teams():
            nonlocal available
            if available is None:
                organization = self.request('GET', '/orgs/' + quote(repo['owner']['login'], safe=''), allow_missing=True)
                available = self.pages(path + '/teams') if organization else []
            return available
        def permits(permission, requirement):
            if requirement == 'repo.pulls' and repo.get('has_pull_requests') is False:
                return False
            return permission in (('read', 'write', 'admin', 'owner') if requirement == 'repo.pulls' else ('write', 'admin', 'owner'))
        def team_permits(team, requirement):
            units = team.get('units_map')
            return permits(units.get(requirement) if units else team.get('permission'), requirement)
        for name, requirements in sorted(users.items()):
            user = self.request('GET', '/users/' + quote(name, safe=''))
            check_identity('users', name, user)
            permission = self.request('GET', path + '/collaborators/' + quote(name, safe='') + '/permission')
            access = {'id': user.get('id'), 'name': name, 'permission': permission.get('permission'), 'via_teams': []}
            if not user.get('active', True) or user.get('prohibit_login', False):
                raise PolicyError(f'人员 {name} 已停用或被禁止登录')
            for requirement in sorted(requirements):
                allowed = permits(permission.get('permission'), requirement)
                # 分单元团队总体 permission 可能为 none；审批名单允许 PR 读取权限，推送/合并名单要求代码写入权限。
                if not allowed:
                    for team in repository_teams():
                        if not team_permits(team, requirement):
                            continue
                        key = (team['id'], name)
                        if key not in memberships:
                            memberships[key] = bool(self.request('GET', f'/teams/{team["id"]}/members/' + quote(name, safe=''), allow_missing=True))
                        if memberships[key]:
                            access['via_teams'].append({'id': team['id'], 'units': team.get('units_map'), 'permission': team.get('permission'), 'requirement': requirement})
                            allowed = True
                            break
                if not allowed:
                    label = 'PR 读取' if requirement == 'repo.pulls' else '代码写入'
                    raise PolicyError(f'人员 {name} 没有目标仓库的{label}权限；请在 Gitea“仓库设置 → 协作者”或“组织 → 团队 → 权限/仓库”中处理')
            evidence['users'].append(access)
        for name, requirements in sorted(team_names.items()):
            team = next((t for t in repository_teams() if t['name'].lower() == name.lower()), None)
            if not team or any(not team_permits(team, requirement) for requirement in requirements):
                raise PolicyError(f'团队 {name} 不存在或没有规则要求的代码写入 / PR 读取权限；请在 Gitea“组织 → 团队 → 权限/仓库”中检查授权')
            check_identity('teams', name, team)
            evidence['teams'].append({'id': team.get('id'), 'name': name, 'permission': team.get('permission'), 'units': team.get('units_map')})
        return evidence

    def matching_branches(self, repo, rules):
        branches = self.pages(self.path(repo) + '/branches') if not repo.get('empty') else []
        return [{'rule_name': r['rule_name'], 'priority': r['priority'],
                 'branches': [b['name'] for b in branches if matches(r['rule_name'], b['name'])],
                 'future_branches': True} for r in rules]
