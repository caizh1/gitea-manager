"""只读固定 Git 引用和对象；凭据按来源 URL 限定，失败不输出远端正文。"""
import base64
import hashlib
from pathlib import Path
import shutil
from urllib.parse import quote

from svn_migration.common import MigrationError, require
from svn_migration.engine import Process


def parse_refs(raw):
    result = {}
    for line in raw.decode('utf-8', errors='strict').splitlines():
        digest, name = line.split('\t', 1)
        if name.endswith('^{}'):
            continue
        require(name == 'HEAD' or name.startswith('refs/'), '来源返回了无效引用', 'source_format')
        require(len(digest) in (40, 64) and all(c in '0123456789abcdef' for c in digest),
                '来源返回了无效对象摘要', 'source_format')
        require(name not in result, '来源引用重复', 'source_format')
        result[name] = digest
    return result


def remote_refs(raw):
    lines, head = [], ''
    for line in raw.splitlines():
        if line.startswith(b'ref: '):
            target, name = line[5:].decode().split('\t', 1)
            require(name == 'HEAD' and not head and target.startswith('refs/heads/'),
                    '来源默认分支引用无效', 'source_format')
            head = target
        else:
            lines.append(line)
    return parse_refs(b'\n'.join(lines)), head


class GitSnapshot:
    def __init__(self, provider, project, folder, tick=lambda: None, wiki=False):
        self.provider, self.project = provider, project
        self.folder = Path(folder).resolve()
        require(not self.folder.exists(), '代码快照目录已存在，不能覆盖既有现场', 'conflict', 409)
        self.folder.mkdir(parents=True, mode=0o700)
        def guarded_tick():
            tick()
            require(shutil.disk_usage(self.folder).free >= 4 * 1024 ** 3,
                    '代码快照工作盘剩余不足 4 GiB，已停止读取并保留现场', 'disk')
        guarded_tick()
        self.process = Process(self.folder / 'commands', tick=guarded_tick)
        parts = project['path'].split('/')
        require(len(parts) >= 2 and all(p and p not in ('.', '..') for p in parts), '来源路径无效')
        self.url = provider.http.base + '/' + '/'.join(quote(p, safe='') for p in parts) + ('.wiki.git' if wiki else '.git')

    def environment(self):
        self.provider.credentials()
        if self.provider.kind == 'gitlab':
            token = self.provider.secret if self.provider.auth_method == 'token' else self.provider.access_token
            authorization = 'Basic ' + base64.b64encode(('oauth2:' + token).encode()).decode()
        else:
            # Gitea Git HTTP 只使用 Basic；令牌放在密码位。
            authorization = 'Basic ' + base64.b64encode((self.provider.actor['username'] + ':' + self.provider.secret).encode()).decode()
        pairs = [('credential.helper', ''), ('http.followRedirects', 'false'), ('http.proxy', ''),
                 ('http.' + self.url + '.extraHeader', 'Authorization: ' + authorization),
                 ('protocol.allow', 'never'), ('protocol.http.allow', 'always'), ('protocol.https.allow', 'always'),
                 ('lfs.url', self.url + '/info/lfs'), ('lfs.concurrenttransfers', '2')]
        if isinstance(self.provider.http.verify, str):
            pairs.append(('http.sslCAInfo', self.provider.http.verify))
        env = {'GIT_CONFIG_COUNT': str(len(pairs)), 'GIT_LFS_SKIP_SMUDGE': '1'}
        env.update({key: '' for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')})
        env.update(NO_PROXY='*', no_proxy='*')
        for index, (key, value) in enumerate(pairs):
            env['GIT_CONFIG_KEY_' + str(index)] = key
            env['GIT_CONFIG_VALUE_' + str(index)] = value
        return env

    def git(self, *args, cwd=None):
        try:
            return self.process.run(['git', *args], cwd=cwd, env=self.environment(), timeout=7200)
        except MigrationError as error:
            if error.code == 'command':
                raise MigrationError('Git 读取失败，请核对来源访问和仓库状态；未完成代码快照', 'git_read') from None
            raise

    def capture(self):
        self.check_identity()
        before, default_ref = remote_refs(self.git('ls-remote', '--symref', self.url))
        repository = self.folder / 'repository.git'
        self.git('clone', '--mirror', '--', self.url, str(repository))
        local = parse_refs(self.git('for-each-ref', '--format=%(objectname)%09%(refname)', cwd=repository))
        require(local == {k: v for k, v in before.items() if k != 'HEAD'},
                '克隆结果与来源固定引用不一致，请在源端停写后重试', 'source_changed')
        self.git('fsck', '--full', '--strict', cwd=repository)
        self.git('lfs', 'fetch', '--all', 'origin', cwd=repository)
        lfs = []
        lfs_root = repository / 'lfs' / 'objects'
        if lfs_root.exists():
            for path in sorted(lfs_root.glob('*/*/*')):
                require(path.is_file() and not path.is_symlink(), 'LFS 对象文件异常', 'verification')
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        self.process.tick()
                        digest.update(chunk)
                require(digest.hexdigest() == path.name, 'LFS 对象内容摘要不一致', 'verification')
                lfs.append({'oid': path.name, 'size': path.stat().st_size})
        after, after_default_ref = remote_refs(self.git('ls-remote', '--symref', self.url))
        self.check_identity()
        require(before == after and default_ref == after_default_ref,
                '读取期间源引用已变化，请在源端停写后重试', 'source_changed')
        # 源地址不含凭据；内容不写入共享日志，现场只在管理员数据卷保存。
        return {'project_id': self.project['id'], 'path': self.project['path'], 'refs': before, 'default_ref': default_ref,
                'lfs_objects': lfs, 'boundary': '已核对可见 Git 引用与下载对象；Wiki、协作资料和目标回读尚未核验'}

    def verify_refs(self, snapshot):
        self.check_identity()
        refs, default = remote_refs(self.git('ls-remote', '--symref', self.url))
        require(refs == snapshot['refs'] and default == snapshot['default_ref'],
                '来源代码或 Wiki 在迁移期间变化，禁止交接', 'source_changed')

    def check_identity(self):
        path = ('/projects/' + str(self.project['id'])) if self.provider.kind == 'gitlab' else (
            '/repos/' + '/'.join(quote(p, safe='') for p in self.project['path'].split('/')))
        current = self.provider.get(path)
        name = current.get('path_with_namespace') if self.provider.kind == 'gitlab' else (current.get('full_path') or current.get('full_name'))
        require(current.get('id') == self.project['id'] and name == self.project['path'],
                '代码读取期间仓库身份或路径改变，不能使用此快照', 'source_changed')
