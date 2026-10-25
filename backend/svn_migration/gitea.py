"""仅通过增强版 API 创建私有群组和仓库，不修改账号或业务授权。"""
import base64
from pathlib import Path
from urllib.parse import quote
import requests

from .common import MigrationError, Uncertain, require, source_url, digest


def target_fingerprint(server):
    return digest([server.gitea_url, server.api_token, server.access_epoch])


class Gitea:
    def __init__(self, server, process, credential_dir, ca_pem=''):
        self.base = source_url(server.gitea_url, ('http', 'https'))
        self.token, self.process = server.api_token, process
        require(self.token and not any(c.isspace() for c in self.token), '目标实例缺少有效的管理令牌')
        self.verify = True
        if ca_pem:
            ca = Path(credential_dir) / 'target-ca.pem'
            ca.write_text(ca_pem); ca.chmod(0o600)
            self.verify = str(ca)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers['Authorization'] = 'token ' + self.token
        self.actor = None
        self.request_timeout = (10, 60)

    def close(self):
        self.session.close()

    def api(self, method, path, body=None, missing=False):
        self.process.tick()
        try:
            response = self.session.request(method, self.base + '/api/v1' + path, json=body,
                                            timeout=self.request_timeout, allow_redirects=False, verify=self.verify)
        except requests.RequestException:
            if method != 'GET':
                raise Uncertain('Gitea 写入回执丢失，请核对远端结果后恢复') from None
            raise MigrationError('无法连接 Gitea，请核对地址、证书和网络', 'connection') from None
        if response.status_code == 404 and missing:
            return None
        if response.status_code >= 500 and method != 'GET':
            raise Uncertain('Gitea 写入返回服务端错误，结果需要核对')
        require(200 <= response.status_code < 300, f'Gitea 请求失败：{method} {path}，HTTP {response.status_code}', 'gitea')
        try:
            return response.json() if response.content else None
        except ValueError:
            if method != 'GET':
                raise Uncertain('Gitea 写入返回无效回执，结果需要核对') from None
            raise MigrationError('Gitea 返回无效数据', 'gitea') from None

    def check(self):
        self.actor = self.api('GET', '/user')
        require(self.actor.get('is_admin') and self.actor.get('active', True), '迁移需要有效的 Gitea 管理员令牌')
        result = self.api('GET', '/governance/groups?parent_id=0')
        require(isinstance(result, (dict, list)), '目标缺少增强版群组接口', 'capability')
        return {'id': self.actor['id'], 'login': self.actor['login']}

    def members(self, path):
        result, after = [], 0
        while True:
            separator = '&' if '?' in path else '?'
            page = self.api('GET', path + separator + 'after_id=' + str(after))
            if isinstance(page, list):
                result.extend(page)
                if not page:
                    return result
                next_id = max(m['user_id'] for m in page)
                require(next_id > after, '成员分页未前进', 'capability')
                after = next_id
                continue
            require(isinstance(page, dict) and isinstance(page.get('members'), list), '成员接口未提供完整数据', 'capability')
            result.extend(page['members'])
            next_id = page.get('next_id', 0)
            if not next_id:
                return result
            require(next_id > after, '成员分页未前进', 'capability')
            after = next_id

    def group_private(self, group_id, expected_path, actor_id):
        group = self.api('GET', '/governance/groups/' + str(group_id))
        require(group.get('id') == group_id and group.get('full_path') == expected_path and group.get('parent_id', 0) == 0,
                '来源群组身份或父级已经改变', 'privacy')
        require(group.get('visibility') == 2 and not group.get('archived'), '来源群组不再处于私有可用状态', 'privacy')
        members = self.members('/governance/groups/' + str(group_id) + '/members')
        require(all(m.get('user_id') == actor_id for m in members), '来源群组已有业务成员，需要新的私有群组', 'privacy')
        shares = self.api('GET', '/governance/groups/' + str(group_id) + '/shares')
        require(shares == [] or isinstance(shares, dict) and not shares.get('shares') and not shares.get('next_id'), '来源群组已有共享授权', 'privacy')
        return group

    def selectable_group(self, group_id):
        group = self.api('GET', '/governance/groups/' + str(group_id))
        require(isinstance(group, dict) and group.get('id') == group_id
                and group.get('kind', 'group') == 'group' and group.get('full_path'),
                '目标群组身份无效', 'target_group')
        require(not group.get('archived') and not group.get('delete_after'),
                '目标群组已归档或等待删除', 'target_group')
        require(group.get('abilities', {}).get('create_project') is True,
                '当前目标账号没有在此群组创建仓库的权限', 'authorization', 403)
        return group

    def child_group_paths(self, group_id):
        result, after = set(), 0
        for _ in range(101):
            rows = self.api('GET', f'/governance/groups?parent_id={group_id}&after_id={after}')
            require(isinstance(rows, list), '子群组接口格式不兼容', 'capability')
            if not rows:
                return result
            result.update(row['full_path'].casefold() for row in rows)
            next_id = max(row['id'] for row in rows)
            require(next_id > after, '子群组分页未前进', 'capability')
            after = next_id
            if len(rows) < 100:
                return result
        raise MigrationError('子群组数量超过读取上限', 'capacity')

    def group_shares(self, group_id):
        rows, after = [], 0
        for _ in range(101):
            page = self.api('GET', f'/governance/groups/{group_id}/shares?after_id={after}')
            items = page if isinstance(page, list) else page.get('shares') if isinstance(page, dict) else None
            require(isinstance(items, list), '群组共享接口格式不兼容', 'capability')
            rows.extend(items)
            require(len(rows) <= 10000, '群组共享数量超过读取上限', 'capacity')
            next_id = max((row['id'] for row in items), default=0) if isinstance(page, list) else page.get('next_id', 0)
            if not next_id:
                return sorted(rows, key=lambda row: row['id'])
            require(type(next_id) is int and next_id > after, '群组共享分页未前进', 'capability')
            after = next_id
        raise MigrationError('群组共享分页超过读取上限', 'capacity')

    def selected_scope(self, group_id):
        """只读冻结用户选中的群组身份与有效授权，不改写现有成员或共享。"""
        group = self.selectable_group(group_id)
        chain, seen, current = [], set(), group
        for _ in range(64):
            require(current['id'] not in seen, '群组父级存在循环', 'target_group')
            seen.add(current['id'])
            chain.append({key: current.get(key) for key in ('id', 'full_path', 'parent_id', 'visibility', 'archived', 'delete_after')}
                         | {'shares': self.group_shares(current['id'])})
            parent = current.get('parent_id', 0)
            if not parent:
                break
            current = self.api('GET', '/governance/groups/' + str(parent))
            require(isinstance(current, dict) and current.get('id') == parent,
                    '目标群组父级无法核对', 'target_group')
        else:
            raise MigrationError('群组层级超过读取上限', 'capacity')
        members = self.members('/governance/groups/' + str(group_id) + '/members')
        normalized = sorted([{'user_id': row['user_id'], 'active': row.get('active'),
                              'effective_role': row.get('effective_role'),
                              'grants': sorted(row.get('grants') or [], key=digest),
                              'native_teams': sorted(row.get('native_teams') or [])}
                             for row in members], key=lambda row: row['user_id'])
        return {'id': group_id, 'path': group['full_path'], 'name': group.get('name') or group['full_path'],
                'access_fingerprint': digest([chain, normalized]),
                'member_ids': sorted({row['user_id'] for row in members})}

    def verify_selected_scope(self, selection):
        current = self.selected_scope(selection['id'])
        require(current == selection, '目标群组的路径或权限已变化，请重新预检', 'target_group_changed', 409)
        return current

    def repository(self, owner, name, missing=False):
        return self.api('GET', '/repos/' + quote(owner, safe='/') + '/' + quote(name, safe=''), missing=missing)

    def repository_private(self, repo, owner, name, group_id, actor_id, selection=None):
        # 子群组的 full_name 使用内部兼容组织名；对外身份取 full_path，并核对稳定 owner ID。
        require(repo.get('private') is True and not repo.get('archived')
                and (repo.get('full_path') or repo.get('full_name', '')).casefold() == (owner + '/' + name).casefold()
                and (not selection or repo.get('owner', {}).get('id') == group_id),
                '目标仓库身份、名称或私有状态已变化', 'privacy')
        if selection:
            self.verify_selected_scope(selection)
        else:
            self.group_private(group_id, owner, actor_id)
        members = self.members('/governance/repositories/' + str(repo['id']) + '/members?include_inherited=true')
        allowed = set(selection['member_ids']) | {actor_id} if selection else {actor_id}
        require(all(m.get('user_id') in allowed for m in members), '目标仓库出现所选群组之外的成员，停止迁移', 'privacy')
        shares = self.api('GET', '/governance/repositories/' + str(repo['id']) + '/shares')
        require(isinstance(shares, dict) and not shares.get('shares') and not shares.get('next_id'), '目标仓库已有共享授权', 'privacy')

    def git_env(self):
        require(self.actor is not None, '尚未验证目标管理员')
        authorization = base64.b64encode((self.actor['login'] + ':' + self.token).encode()).decode()
        result = {'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'http.extraHeader',
                  'GIT_CONFIG_VALUE_0': 'Authorization: Basic ' + authorization,
                  'GIT_CONFIG_KEY_1': 'http.followRedirects', 'GIT_CONFIG_VALUE_1': 'false'}
        if isinstance(self.verify, str):
            result['GIT_SSL_CAINFO'] = self.verify
        return result

    def remote(self, owner, name):
        return self.base + '/' + quote(owner, safe='/') + '/' + quote(name, safe='') + '.git'

    def refs(self, owner, name):
        output = self.process.run(['git', 'ls-remote', '--refs', self.remote(owner, name)], env=self.git_env())
        return {line.split('\t')[1]: line.split('\t')[0] for line in output.decode().splitlines()}
