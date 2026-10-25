"""受控接收端传输：只写本任务隔离仓库，每次变更保存回执。"""
import base64
from pathlib import Path
from urllib.parse import quote

from svn_migration.common import require
from svn_migration.gitea import Gitea
from .git_snapshot import parse_refs, GitSnapshot
from .providers import Provider
from .transport import Transport


class Receiver(Gitea):
    def check(self):
        super().check()
        capability = self.api('GET', '/governance/migration-capabilities', missing=True)
        require(isinstance(capability, dict) and capability.get('protocol') == 1,
                '目标尚未安装兼容的迁移接收端，请先部署配套增强版；未创建目标仓库', 'capability')
        return capability

    def environment(self, remote):
        require(self.actor is not None, '目标身份尚未核对')
        token = base64.b64encode((self.actor['login'] + ':' + self.token).encode()).decode()
        pairs = [('http.' + remote + '.extraHeader', 'Authorization: Basic ' + token),
                 ('http.followRedirects', 'false'), ('credential.helper', ''), ('http.proxy', ''),
                 ('protocol.allow', 'never'), ('protocol.http.allow', 'always'),
                 ('protocol.https.allow', 'always'), ('lfs.url', remote + '/info/lfs')]
        if isinstance(self.verify, str):
            pairs.append(('http.sslCAInfo', self.verify))
        env = {'GIT_CONFIG_COUNT': str(len(pairs)), 'GIT_LFS_SKIP_SMUDGE': '1',
               'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': '',
               'http_proxy': '', 'https_proxy': '', 'all_proxy': '', 'NO_PROXY': '*', 'no_proxy': '*'}
        for i, (key, value) in enumerate(pairs):
            env['GIT_CONFIG_KEY_' + str(i)] = key
            env['GIT_CONFIG_VALUE_' + str(i)] = value
        return env

    def stage(self, key, source_digest, checkpoint, save):
        """新建回执丢失时仅凭本任务随机标记恢复，不按业务仓库同名接管。"""
        require(len(key) == 32 and all(c in '0123456789abcdef' for c in key), '迁移任务标识无效')
        name = 'gm-migrate-' + key
        marker = 'Gitea Manager 迁移隔离仓库 ' + key + ' ' + source_digest
        owner = self.actor['login']
        repo = self.repository(owner, name, missing=True)
        if repo is None:
            require(not checkpoint.get('repo_id'), '已登记隔离仓库消失，不能重新创建替代品')
            checkpoint['creating_repo'] = {'name': name, 'marker': marker}
            save(checkpoint)
            repo = self.api('POST', '/user/repos', {'name': name, 'description': marker, 'private': True,
                                                 'auto_init': False, 'default_branch': 'main'})
            checkpoint['repo_id'] = repo['id']
            save(checkpoint)
        else:
            require(checkpoint.get('repo_id') == repo['id'] or checkpoint.get('creating_repo') == {'name': name, 'marker': marker},
                    '已有仓库没有本任务的创建意图或回执，拒绝接管')
        require(repo.get('private') and not repo.get('mirror') and not repo.get('archived')
                and repo.get('owner', {}).get('id') == self.actor['id'] and repo.get('description') == marker,
                '隔离仓库身份、说明或权限已变化')
        checkpoint['repo_id'] = repo['id']
        checkpoint['key'] = key
        self.api('PATCH', '/repos/' + quote(owner, safe='') + '/' + quote(name, safe=''), {'has_actions': False})
        repo = self.repository(owner, name)
        require(repo.get('has_actions') is False, '隔离仓库必须关闭 Actions，避免迁入工作流自动运行')
        reply = self.api('POST', '/governance/migration-sessions',
                         {'key': key, 'repo_id': repo['id'], 'source_digest': source_digest})
        require(reply.get('repo_id') == repo['id'] and reply.get('source_digest') == source_digest,
                '接收端会话回执与固定快照不一致')
        checkpoint['session_id'] = reply['id']
        checkpoint['repo_name'] = name
        save(checkpoint)
        return repo

    def transfer_code(self, snapshot, directory, repo, checkpoint, save, pull_objects=()):
        current_repo = self.repository(self.actor['login'], repo['name'])
        require(current_repo.get('id') == repo['id'] and current_repo.get('has_actions') is False
                and current_repo.get('private') is True, '推送前隔离状态已改变')
        local = Path(directory) / 'repository.git'
        require(local.is_dir(), '代码快照不存在')
        remote = self.remote(self.actor['login'], repo['name'])
        env = self.environment(remote)
        refs = {k: v for k, v in snapshot['refs'].items() if k != 'HEAD'}
        for pull in pull_objects:
            ref = 'refs/pull/' + str(pull['number']) + '/head'
            require(ref not in refs or refs[ref] == pull['head_commit'], '合并请求引用发生冲突')
            refs[ref] = pull['head_commit']
        import hashlib
        normal = {k: v for k, v in refs.items() if k.startswith(('refs/heads/', 'refs/tags/'))}
        extra = {k: v for k, v in refs.items() if k not in normal}
        temporary = {'refs/heads/gm-transfer-' + checkpoint['key'] + '-' + hashlib.sha256(k.encode()).hexdigest()[:12]: v for k, v in extra.items()}
        require(not set(temporary) & set(refs), '传输分支与来源分支冲突')
        allowed = {**refs, **temporary}
        current = parse_refs(self.process.run(['git', 'ls-remote', '--refs', remote], env=env))
        require(all(k in allowed and allowed[k] == value for k, value in current.items()),
                '隔离仓库出现非本快照引用，拒绝覆盖')
        transport = {**normal, **temporary}
        names = sorted(transport)
        for offset in range(0, len(names), 100):
            batch = names[offset:offset + 100]
            leases = ['--force-with-lease=' + name + ':' + current.get(name, '') for name in batch]
            specs = [transport[name] + ':' + name for name in batch]
            self.process.run(['git', 'push', '--atomic', *leases, remote, *specs], cwd=local, env=env)
        self.process.run(['git', 'lfs', 'push', '--all', remote], cwd=local, env=env)
        ref_objects = [{'kind': 'git_ref', 'source_id': hashlib.sha256(name.encode()).hexdigest(),
                        'ref': name, 'commit': value} for name, value in sorted(extra.items())]
        if ref_objects:
            self.import_objects(checkpoint['key'], ref_objects, checkpoint, save)
        names = sorted(temporary)
        for offset in range(0, len(names), 100):
            batch = names[offset:offset + 100]
            self.process.run(['git', 'push', '--atomic',
                *['--force-with-lease=' + name + ':' + temporary[name] for name in batch],
                remote, *[':' + name for name in batch]], cwd=local, env=env)
        actual = parse_refs(self.process.run(['git', 'ls-remote', '--refs', remote], env=env))
        require(actual == refs, '目标代码引用回读不一致')
        default = snapshot.get('default_ref', '')
        if default:
            require(default.startswith('refs/heads/'), '来源默认分支无效')
            self.api('PATCH', '/repos/' + quote(self.actor['login'], safe='') + '/' + quote(repo['name'], safe=''),
                     {'default_branch': default[len('refs/heads/'):]})
        checkpoint['code_refs'] = actual
        checkpoint['code_transferred'] = True
        save(checkpoint)

    def import_objects(self, key, objects, checkpoint, save):
        receipts = checkpoint.setdefault('objects', {})
        for offset in range(0, len(objects), 50):
            batch = objects[offset:offset + 50]
            reply = self.api('POST', '/governance/migration-sessions/' + key + '/objects', {'objects': batch})
            require(isinstance(reply, list) and len(reply) == len(batch), '原生对象写入回执数量不符')
            for expected, receipt in zip(batch, reply):
                require(receipt['kind'] == expected['kind'] and receipt['source_id'] == expected['source_id'],
                        '原生对象写入回执身份不符')
                receipts[receipt['kind'] + ':' + receipt['source_id']] = receipt
            save(checkpoint)
        actual, after = {}, 0
        while True:
            page = self.api('GET', '/governance/migration-sessions/' + key + '?after_id=' + str(after))
            for row in page['objects']:
                receipt = row['receipt']
                ident = receipt['kind'] + ':' + receipt['source_id']
                require(row.get('matches') is True and ident in receipts and receipts[ident] == receipt,
                        '目标原生对象回读不一致')
                require(ident not in actual, '目标回执分页重复')
                actual[ident] = receipt
            next_id = page['next_id']
            if not next_id:
                break
            require(next_id > after, '目标回执分页没有前进')
            after = next_id
        require(set(actual) == set(receipts), '目标原生对象回执不完整')
        checkpoint['objects_verified'] = True
        save(checkpoint)

    def verify_code(self, snapshot, repo, directory, checkpoint, save):
        transport = Transport(self.base, verify=self.verify, tick=self.process.tick)
        try:
            provider = Provider(transport, 'gitea', self.actor['login'], self.token, 'token')
            provider.authenticate()
            project = {'id': repo['id'], 'path': self.actor['login'] + '/' + repo['name']}
            actual = GitSnapshot(provider, project, directory, tick=self.process.tick).capture()
            expected_refs = dict(checkpoint.get('code_refs', snapshot['refs']))
            if 'HEAD' in snapshot['refs']:
                expected_refs['HEAD'] = snapshot['refs']['HEAD']
            require(actual['refs'] == expected_refs and actual['default_ref'] == snapshot['default_ref'],
                    '目标重新克隆后的代码引用或默认分支不一致')
            require(actual['lfs_objects'] == snapshot['lfs_objects'], '目标重新下载的 LFS 对象不一致')
            checkpoint['code_verified'] = True
            save(checkpoint)
        finally:
            transport.close()

    def import_asset(self, key, metadata, local_file, checkpoint, save):
        """流式上传并重新下载核对，不把 HTTP 成功视为文件完整。"""
        import hashlib
        import json
        import requests
        from svn_migration.common import Uncertain
        path = Path(local_file)
        require(path.is_file() and path.stat().st_size == metadata['size'], '附件快照缺失或大小改变')
        value = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                self.process.tick()
                value.update(chunk)
        require(value.hexdigest() == metadata['sha256'], '附件快照内容已改变')
        url = self.base + '/api/v1/governance/migration-sessions/' + key + '/assets'
        header = base64.b64encode(json.dumps(metadata, ensure_ascii=False, separators=(',', ':')).encode()).decode()
        require(len(header) <= 16000, '附件元数据过大')
        try:
            with path.open('rb') as stream, self.session.post(url, data=stream, headers={
                    'X-Migration-Metadata': header, 'Content-Type': 'application/octet-stream',
                    'Content-Length': str(metadata['size'])}, timeout=(10, 7200),
                    allow_redirects=False, verify=self.verify) as response:
                if response.status_code >= 500:
                    raise Uncertain('附件上传回执不确定，保留现场后重试核对')
                require(response.status_code == 200, '附件上传失败，HTTP ' + str(response.status_code))
                receipt = response.json()
            require(receipt.get('source_id') == metadata['source_id'] and receipt.get('sha256') == metadata['sha256']
                    and receipt.get('size') == metadata['size'] and receipt.get('attachment_id', 0) > 0, '附件回执不符')
            sha, count = hashlib.sha256(), 0
            with self.session.get(url + '/' + str(receipt['id']), stream=True, timeout=(10, 60),
                                  allow_redirects=False, verify=self.verify) as response:
                require(response.status_code == 200, '目标附件无法回读')
                for chunk in response.iter_content(1024 * 1024):
                    self.process.tick()
                    count += len(chunk)
                    require(count <= metadata['size'], '目标附件超过预期大小')
                    sha.update(chunk)
            require(count == metadata['size'] and sha.hexdigest() == metadata['sha256'], '目标附件重新下载核对失败')
        except requests.RequestException:
            raise Uncertain('附件传输连接中断，需要继续核对') from None
        checkpoint.setdefault('assets', {})[metadata['source_id']] = receipt
        save(checkpoint)
        return receipt

    def transfer_wiki(self, provider, project, key, repo, directory, checkpoint, save):
        """Wiki 按独立 Git 仓库保留全部历史，传输后重新克隆核对。"""
        import secrets
        from .git_snapshot import remote_refs
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if 'wiki_snapshot' not in checkpoint:
            local = directory / ('source-' + secrets.token_hex(8))
            checkpoint['wiki_snapshot'] = GitSnapshot(provider, project, local, tick=self.process.tick, wiki=True).capture()
            checkpoint['wiki_directory'] = str(local)
            save(checkpoint)
        snapshot = checkpoint['wiki_snapshot']
        if not snapshot['refs']:
            checkpoint['wiki_verified'] = True
            save(checkpoint)
            return
        require(snapshot['default_ref'].startswith('refs/heads/'), 'Wiki 默认分支无法确认')
        require(not snapshot['lfs_objects'], 'Wiki 中发现 LFS 对象，目标 Wiki LFS 能力尚未验证')
        branch = snapshot['default_ref'][len('refs/heads/'):]
        response = self.api('POST', '/governance/migration-sessions/' + key + '/wiki', {'branch': branch})
        require(response.get('branch') == branch, 'Wiki 初始化回执不符')
        remote = self.remote(self.actor['login'], repo['name'])[:-4] + '.wiki.git'
        env = self.environment(remote)
        refs = {k: v for k, v in snapshot['refs'].items() if k != 'HEAD'}
        current = parse_refs(self.process.run(['git', 'ls-remote', '--refs', remote], env=env))
        require(all(k in refs and refs[k] == v for k, v in current.items()), '目标 Wiki 有非本任务内容，拒绝覆盖')
        names = sorted(refs)
        for offset in range(0, len(names), 100):
            batch = names[offset:offset + 100]
            self.process.run(['git', 'push', '--atomic',
                *['--force-with-lease=' + name + ':' + current.get(name, '') for name in batch],
                remote, *[refs[name] + ':' + name for name in batch]],
                cwd=Path(checkpoint['wiki_directory']) / 'repository.git', env=env)
        http = Transport(self.base, verify=self.verify, tick=self.process.tick)
        try:
            target = Provider(http, 'gitea', self.actor['login'], self.token, 'token')
            target.authenticate()
            actual = GitSnapshot(target, {'id': repo['id'], 'path': self.actor['login'] + '/' + repo['name']},
                                 directory / ('verify-' + secrets.token_hex(8)), tick=self.process.tick, wiki=True).capture()
            require(actual['refs'] == snapshot['refs'] and actual['default_ref'] == snapshot['default_ref'], 'Wiki 重新克隆核验失败')
        finally:
            http.close()
        checkpoint['wiki_verified'] = True
        save(checkpoint)
