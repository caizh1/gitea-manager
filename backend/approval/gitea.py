"""只读原生审批适配，完整分页、总时间预算、身份回读和故障拒绝。"""
import json
import time
from urllib.parse import quote, urlsplit

import requests

from .rules import ApprovalError, native_rule, safe_native, digest, positive
from .native_metadata import NativeMetadata
from .timeline import read_complete, validate_reference


def review_revision(reviews):
    """仅核对会影响审批身份及有效性的原生字段，忽略正文、头像及本地派生字段。"""
    fields=('id','state','commit_id','stale','dismissed','official','submitted_at')
    users=('id','login','active','prohibit_login')
    return digest(sorted([{**{k:r.get(k) for k in fields},
                           'user':{k:(r.get('user') or {}).get(k) for k in users}}
                          for r in reviews],key=lambda r:positive(r['id'],'审批 ID')))


def reset_event_id(timeline):
    """普通提交由审批 SHA 绑定；迟到的普通 push 通知不能撤销已批准的新 SHA。"""
    latest = 0
    for event in timeline:
        reset = event.get('type') == 'change_target_branch'
        if event.get('type') == 'pull_push':
            try:
                body = json.loads(event['body'])
            except (KeyError, TypeError, ValueError):
                raise ApprovalError('代码变更事件不完整，无法确认审批有效性') from None
            if not isinstance(body, dict) or type(body.get('is_force_push')) is not bool:
                raise ApprovalError('代码变更事件缺少强推标志')
            reset = body['is_force_push']
        if reset:
            latest = max(latest, positive(event.get('id'), '代码变更事件 ID'))
    return latest


class Reader:
    def __init__(self, server, seconds=8):
        self.url = server.gitea_url.rstrip('/')
        parsed = urlsplit(self.url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ApprovalError('Gitea 地址必须是没有内嵌凭据的 HTTP(S) 地址')
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(Authorization='token ' + server.api_token)
        self.deadline = time.monotonic() + seconds
        self.requests = 0
        self.total = None
        self.metadata = NativeMetadata(server, self.deadline)

    def close(self):
        self.session.close()
        self.metadata.close()

    def request(self, path, params=None):
        remaining = self.deadline - time.monotonic()
        self.requests += 1
        if remaining <= 0 or self.requests > 160:
            raise ApprovalError('审批信息读取超出预算，未放行')
        if not path.startswith('/') or path.startswith('//'):
            raise ApprovalError('原生接口路径无效')
        try:
            with self.session.get(self.url + '/api/v1' + path, params=params, timeout=(min(2, remaining), min(2, remaining)),
                                  allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise ApprovalError(f'原生接口 HTTP {response.status_code}，未完成审批核验')
                raw = bytearray()
                for chunk in response.iter_content(32768):
                    raw.extend(chunk)
                    if len(raw) > 2*1024*1024 or time.monotonic() > self.deadline:
                        raise ApprovalError('审批响应超过大小或时间预算')
                import json
                value = json.loads(raw)
                count = response.headers.get('X-Total-Count')
                self.total = int(count) if count is not None else None
                if self.total is not None and self.total < 0:
                    raise ValueError()
                return value
        except (requests.RequestException, ValueError):
            raise ApprovalError('原生接口超时或响应损坏，未放行') from None

    def _governance_request(self, method, path, payload=None, allow_missing=False):
        """原生治理接口共用同一请求数、总时间和响应大小预算。"""
        remaining = self.deadline - time.monotonic()
        self.requests += 1
        if remaining <= 0 or self.requests > 160:
            raise ApprovalError('原生治理操作超出预算')
        if not path.startswith('/') or path.startswith('//'):
            raise ApprovalError('原生治理接口路径无效')
        try:
            with self.session.request(method, self.url + '/api/v1' + path, json=payload,
                                      timeout=(min(2, remaining), min(3, remaining)),
                                      allow_redirects=False, stream=True) as response:
                if allow_missing and response.status_code == 404:
                    return None
                if response.status_code not in (200, 201):
                    raise ApprovalError(f'原生治理接口 HTTP {response.status_code}')
                raw = bytearray()
                for chunk in response.iter_content(32768):
                    raw.extend(chunk)
                    if len(raw) > 2*1024*1024 or time.monotonic() > self.deadline:
                        raise ApprovalError('原生治理响应超过大小或时间预算')
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ApprovalError('原生治理响应格式无效')
                return value
        except (requests.RequestException, ValueError):
            raise ApprovalError('原生治理接口超时或响应损坏') from None

    def governance_capabilities(self):
        """探测原生治理接口；404表示旧版实例，其他错误保持失败。"""
        value = self._governance_request('GET', '/governance/capabilities', allow_missing=True)
        if value is not None and value.get('version') != 1:
            raise ApprovalError('原生治理能力响应不兼容')
        return value

    def write(self, method, path, payload):
        return self._governance_request(method, path, payload)

    def governance_read(self, path):
        return self._governance_request('GET', path)

    def _legacy_approval_pools(self, repo, branches):
        """把旧分支保护的名称白名单回读为稳定用户 ID，无白名单时严格保留旧的代码写入候选集。"""
        path = self.path(repo)
        protections = self.pages(path + '/branch_protections', key='rule_name')
        teams, users, access = {}, {}, {}
        for team in self.pages(path + '/teams'):
            full = self.request('/teams/' + str(team['id']))
            members = self.pages('/teams/' + str(team['id']) + '/members')
            teams[full['name']] = {u['id'] for u in members}
            units = full.get('units_map') or {}
            read = units.get('repo.pulls', full.get('permission')) in ('read', 'write', 'admin', 'owner')
            write = units.get('repo.code', full.get('permission')) in ('write', 'admin', 'owner')
            for user in members:
                users[user['id']] = user
                flags = access.setdefault(user['id'], {'read':False, 'write':False})
                flags['read'] |= read; flags['write'] |= write
        for user in self.pages(path + '/collaborators'):
            users[user['id']] = user
            permission = self.request(path + '/collaborators/' + quote(user['login'], safe='') + '/permission').get('permission')
            flags = access.setdefault(user['id'], {'read':False, 'write':False})
            flags['read'] |= permission in ('read', 'write', 'admin', 'owner')
            flags['write'] |= permission in ('write', 'admin', 'owner')
        by_login = {u['login']:uid for uid,u in users.items()}
        pools = {}
        for branch in branches:
            protection = native_rule(protections, branch)
            if protection.get('enable_approvals_whitelist'):
                names = set(protection.get('approvals_whitelist_username') or [])
                team_names = set(protection.get('approvals_whitelist_teams') or [])
                if names - set(by_login) or team_names - set(teams):
                    raise ApprovalError('旧审批白名单无法完整解析为稳定 ID')
                candidates = {by_login[name] for name in names}
                candidates.update(uid for name in team_names for uid in teams[name])
                candidates = {uid for uid in candidates if access.get(uid, {}).get('read')}
            else:
                candidates = {uid for uid, flags in access.items() if flags['write']}
            candidates = {uid for uid in candidates if users[uid].get('active') is True and users[uid].get('prohibit_login') is False}
            pools[branch] = sorted(candidates)
        return pools, teams

    def native_rule_plan(self, policy):
        capabilities = self.governance_capabilities()
        if not capabilities or not capabilities.get('approval_rules'):
            raise ApprovalError('该实例没有可用的原生审批规则接口')
        repo = self.request('/repositories/' + str(policy.repo_id))
        rule = json.loads(policy.rule_json)
        pools, teams = self._legacy_approval_pools(repo, rule['branches'])
        common = {'scope_type':'repository','scope_id':policy.repo_id,'group_ids':[], 'team_ids':[], 'user_ids':[],
                  'all_eligible':False,'respect_native_approval_pool':True,
                  'branch_mode':'branches','enabled':True,'locked':False}
        def title(suffix):
            full = rule['name'] + suffix
            if len(full) <= 100:
                return full
            # 保留稳定摘要，避免多个长规则被截断为同名。
            marker = ' · ' + digest(full)[:10]
            return full[:100-len(marker)] + marker
        teams_by_id = {}
        for team in self.pages(self.path(repo) + '/teams'):
            full = self.request('/teams/' + str(team['id']))
            teams_by_id[team['id']] = teams.get(full['name'], set())
        rules = []
        # 规则身份只由旧策略的固定分支和团队 ID 决定；候选成员变化不能产生另一批规则。
        branches = sorted(set(rule['branches']))
        rules.append({**common, 'name':title(' · 总票数'), 'required':rule['total'],
                      'branches':branches, 'all_eligible':True})
        for group in rule['groups']:
            if group['team_id'] not in teams_by_id:
                raise ApprovalError('必需审批团队不存在')
            for branch in branches:
                candidates = set(pools[branch]) & teams_by_id[group['team_id']]
                if len(candidates) < group['required']:
                    raise ApprovalError('必需审批团队在旧候选池内人数不足：' + branch)
            rules.append({**common, 'name':title(' · 团队 '+str(group['team_id'])),
                          'required':group['required'], 'branches':branches, 'team_ids':[group['team_id']]})
        current = self.governance_read('/governance/repositories/' + str(repo['id']) + '/approval-rules')
        effective = current.get('settings') or {}
        current_settings = effective.get('settings') or {}
        sources = effective.get('sources') or {}
        settings = {
            'prevent_author': True,
            'prevent_committer': bool(current_settings.get('prevent_committer')),
            'prevent_overrides': True,
            'reset_on_change': True,
            'require_reauthentication': bool(current_settings.get('require_reauthentication')),
        }
        for name, value in settings.items():
            source = sources.get(name) or {}
            if source.get('locked') and source.get('value') is not value:
                raise ApprovalError('原生上层策略锁定了不兼容的审批设置：' + name)
        return {'capabilities':capabilities, 'repository_id':repo['id'], 'full_path':repo.get('full_path') or repo['full_name'],
                'native_url':self.url + '/governance/repositories/' + str(repo['id']) + '/approval-rules',
                'settings_before':current_settings, 'settings':settings, 'rules':rules}

    def pages(self, path, params=None, key='id'):
        rows, ids, expected = [], set(), None
        for page in range(1, 101):
            data = self.request(path, {**(params or {}), 'page': page, 'limit': 50})
            if not isinstance(data, list):
                raise ApprovalError('原生分页格式错误')
            if page == 1:
                expected = self.total
            elif expected != self.total:
                raise ApprovalError('读取期间列表总数变化，请重新核对')
            for row in data:
                if not isinstance(row, dict):
                    raise ApprovalError('原生分页记录不是对象')
                rid = positive(row.get(key), '原生对象 ID') if key == 'id' else row.get(key)
                if not rid:
                    raise ApprovalError('原生分页记录缺少稳定身份')
                if rid in ids:
                    raise ApprovalError('原生分页重复，未使用不完整结果')
                ids.add(rid)
            rows.extend(data)
            if not data or len(data) < 50:
                if expected is not None and expected != len(rows):
                    raise ApprovalError('原生分页总数不符')
                return rows
        raise ApprovalError('原生分页超出验收规模')

    @staticmethod
    def path(repo):
        return '/repos/' + quote(repo['owner']['login'], safe='') + '/' + quote(repo['name'], safe='')

    def repository(self, policy):
        version = self.request('/version')
        if version.get('version') != '1.26.4':
            raise ApprovalError('仅适配官方 Gitea 1.26.4，版本变化必须重新验收')
        repo = self.request('/repositories/' + str(policy.repo_id))
        if (repo.get('id') != policy.repo_id or repo.get('owner', {}).get('id') != policy.owner_id
                or repo.get('full_name') != policy.repo_name):
            raise ApprovalError('仓库已更名、转移或身份不符，需要新预检')
        if not repo.get('permissions', {}).get('admin') or repo.get('archived') or repo.get('mirror') or not repo.get('has_pull_requests'):
            raise ApprovalError('仓库权限或功能不满足接入条件')
        return repo

    def context(self, policy, rule, number=None):
        repo = self.repository(policy)
        path = self.path(repo)
        self.metadata.bind()
        protections = self.pages(path + '/branch_protections', key='rule_name')
        natives = {b: safe_native(native_rule(protections, b)) for b in rule['branches']}
        for branch in rule['branches']:
            actual = self.request(path + '/branches/' + quote(branch, safe=''))
            if actual.get('name') != branch:
                raise ApprovalError('目标分支不存在或身份不匹配')
        pr, reviews = None, []
        if number is not None:
            pr = self.request(path + '/pulls/' + str(number))
            if pr.get('number') != number or pr.get('base', {}).get('repo', {}).get('id') != policy.repo_id:
                raise ApprovalError('PR 身份不匹配')
            reviews = self.pages(path + f'/pulls/{number}/reviews')
            initial_reviews=review_revision(reviews)
            settings = self.request('/settings/api')
            limit = positive(settings.get('max_response_items'), '原生分页上限')
            timeline, timeline_reference = read_complete(self, path+f'/issues/{number}/timeline',
                lambda: self.metadata.snapshot(policy.repo_id, number), repo_id=policy.repo_id,
                number=number, pr_id=pr['id'], reviews=reviews, page_size=min(limit, 50),with_reference=True)
            pr['last_code_event_id'] = reset_event_id(timeline)
            for review in reviews:
                review['decision_event_id'] = max((e['id'] for e in timeline if e.get('type') == 'review' and e.get('review_id') == review['id']), default=0)
        repo_teams = self.pages(path + '/teams')
        teams, users, permissions = {}, {}, {}
        for team in repo_teams:
            if (team.get('organization') or {}).get('id') != policy.owner_id:
                # 1.26.4 仓库团队列表可能不包含组织，按稳定 ID 回读。
                full = self.request('/teams/' + str(team['id']))
                if (full.get('organization') or {}).get('id') != policy.owner_id:
                    raise ApprovalError('团队组织身份不匹配')
                team = full
            members = self.pages('/teams/' + str(team['id']) + '/members')
            teams[team['id']] = {'name': team['name'], 'members': [u['id'] for u in members]}
            units = team.get('units_map')
            for user in members:
                users[user['id']] = user
                access = permissions.setdefault(user['id'], {'read': False, 'write': False})
                level = units.get('repo.pulls') if units else team.get('permission')
                access['read'] |= level in ('read', 'write', 'admin', 'owner')
                level = units.get('repo.code') if units else team.get('permission')
                access['write'] |= level in ('write', 'admin', 'owner')
        for user in self.pages(path + '/collaborators'):
            users[user['id']] = user
            perm = self.request(path + '/collaborators/' + quote(user['login'], safe='') + '/permission')
            level = perm.get('permission')
            access = permissions.setdefault(user['id'], {'read': False, 'write': False})
            access['read'] |= level in ('read', 'write', 'admin', 'owner')
            access['write'] |= level in ('write', 'admin', 'owner')
        # 原生审批池允许非团队协作者；复核每个实际投票者，不能依赖历史 official 字段。
        for review in reviews:
            user = review.get('user')
            if not user:
                if review.get('state') == 'REQUEST_REVIEW':
                    continue
                raise ApprovalError('审批人已删除或身份不明')
            if user['id'] not in users:
                users[user['id']] = self.request('/users/' + quote(user['login'], safe=''))
                perm = self.request(path + '/collaborators/' + quote(user['login'], safe='') + '/permission')
                level = perm.get('permission')
                permissions[user['id']] = {'read': level in ('read', 'write', 'admin', 'owner'),
                                            'write': level in ('write', 'admin', 'owner')}
        eligible_by_branch = {}
        for branch, native in natives.items():
            pool = set()
            allowed_names = set(native.get('approvals_whitelist_username', []))
            allowed_teams = set(native.get('approvals_whitelist_teams', []))
            missing = allowed_teams - {t['name'] for t in teams.values()}
            if missing:
                raise ApprovalError('原生审批池团队身份无法确认')
            pool.update(uid for t in teams.values() if t['name'] in allowed_teams for uid in t['members'])
            eligible = set()
            for uid, user in users.items():
                if type(user.get('active')) is not bool or type(user.get('prohibit_login')) is not bool:
                    raise ApprovalError('无法确认审批账户是否有效')
                if not user['active'] or user['prohibit_login']:
                    continue
                permission = permissions[uid]
                if native.get('enable_approvals_whitelist'):
                    if permission['read'] and (user['login'] in allowed_names or uid in pool):
                        eligible.add(uid)
                elif permission['write']:
                    eligible.add(uid)
            eligible_by_branch[branch] = eligible
            if len(eligible) < rule['total']:
                raise ApprovalError('原生审批池内合格人数不足以满足新增总票数')
            for group in rule['groups']:
                team = teams.get(group['team_id'])
                if not team or len(set(team['members']) & eligible) < group['required']:
                    raise ApprovalError('必需审批组不存在、缺少资格或被原生审批池排除')
        # 读取末尾再次核对 PR 的代码与目标身份，事件延迟不影响当前判定。
        if pr:
            current = self.request(path + '/pulls/' + str(number))
            keys = lambda p: (p['id'], p['head']['sha'], p['base']['sha'], p['base']['ref'], p['state'], p['merged'])
            if keys(pr) != keys(current):
                raise ApprovalError('读取期间 PR 代码或目标发生变化，请重新核对')
            # 团队和资格读取期间仍可能发生撤回；不能直接沿用前面已读取的批准。
            # 这是读取一致性复核，不构成与原生审批写入共享的锁；APR-RACE-001 仍未关闭。
            if review_revision(self.pages(path+f'/pulls/{number}/reviews')) != initial_reviews:
                raise ApprovalError('读取期间审批发生变化，未沿用旧批准')
            final_reference=validate_reference(self.metadata.snapshot(policy.repo_id,number),policy.repo_id,number,pr['id'])
            if final_reference != timeline_reference:
                raise ApprovalError('资格读取期间时间线变化，未沿用旧审批边界')
        return {'repo': repo, 'protections': protections, 'native': natives, 'teams': teams,
                'eligible': eligible_by_branch, 'pr': pr, 'reviews': reviews,
                'fingerprint': digest({'repo': [repo['id'], repo['full_name'], repo['owner']['id']],
                                       'protections': protections, 'teams': teams,
                                       'eligible': {k: sorted(v) for k, v in eligible_by_branch.items()}})}
