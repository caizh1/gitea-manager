"""独立 SSH 用户密钥、只读部署密钥和禁用账号的真实 Git 权限样本。"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time

from .errors import CheckFailure


def ssh_environment(root, private_key):
    directory = (root / 'identity-keys').resolve()
    key = Path(private_key)
    if (directory.parent != root.resolve() or (root / 'identity-keys').is_symlink()
            or key.is_symlink() or key.resolve().parent != directory or not key.is_file()):
        raise CheckFailure('SSH 身份不属于本轮专属密钥目录')
    env = {name: value for name, value in os.environ.items() if not name.startswith('GIT_CONFIG_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0', GIT_LFS_SKIP_SMUDGE='1',
        GIT_SSH_COMMAND=shlex.join(['ssh', '-F', '/dev/null', '-i', str(key), '-o', 'IdentitiesOnly=yes',
            '-o', 'IdentityAgent=none', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(root / 'git_known_hosts')]))
    return env


def git(lab, fixture, args, cwd=None, check=True):
    result = subprocess.run(['git', *args], cwd=cwd, env=ssh_environment(lab.root, fixture['私钥文件']),
                            capture_output=True, timeout=120)
    if check and result.returncode:
        raise CheckFailure('SSH 样本操作失败：' + lab.scrub(result.stderr.decode(errors='replace')[-1000:]))
    return result


def write_denied(result):
    message = result.stderr.decode(errors='replace').lower()
    return result.returncode != 0 and ('not authorized to write to' in message or 'user permission denied for writing' in message)


def seed(lab, node='a'):
    directory = lab.root / 'identity-keys'
    directory.mkdir(mode=0o700, exist_ok=True)
    samples = lab.state['nodes'][node].setdefault('seed', {}).setdefault('ssh_keys', {})
    for role, user in (('writer', 'labwriter'), ('reader', 'labreader'), ('disabled', 'labdisabled'),
                       ('deploy', None), ('outsider', None)):
        private = directory / (node + '-' + role)
        public = private.with_suffix('.pub')
        if not private.exists():
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'remote-' + role, '-f', str(private)], check=True)
        if not public.is_file():
            raise CheckFailure('已有 SSH 私钥缺少公钥，拒绝覆盖原身份')
        private.chmod(0o600)
        material = public.read_text().strip()
        endpoint = '/repos/labadmin/proof/keys' if role == 'deploy' else '/users/' + str(user) + '/keys'
        item = None
        if role != 'outsider':
            existing = lab.api(node, 'GET', endpoint).json()
            title = '跨网段专用身份-' + role
            item = next((row for row in existing if row['title'] == title), None)
            if item is None:
                create = endpoint if role == 'deploy' else '/admin/users/' + user + '/keys'
                item = lab.api(node, 'POST', create, json={'title': title, 'key': material, 'read_only': role == 'deploy'}).json()
            if item['key'].split()[:2] != material.split()[:2]:
                raise CheckFailure('同名 SSH 样本密钥身份不同，拒绝覆盖')
            if role == 'deploy' and item.get('read_only') is not True:
                raise CheckFailure('部署密钥没有实际回读为只读')
        samples[role] = {'私钥文件': str(private), '公钥SHA256': hashlib.sha256(material.encode()).hexdigest(),
                         '用户': user, '服务端密钥ID': item['id'] if item else None}
        lab.save()


def verify(lab, node):
    samples = lab.state['nodes'][node].get('seed', {}).get('ssh_keys')
    if not samples or set(samples) != {'writer', 'reader', 'disabled', 'deploy', 'outsider'}:
        raise CheckFailure('尚未准备完整 SSH 权限样本，不能宣称密钥迁移已验证')
    for role in ('writer', 'reader', 'disabled', 'deploy'):
        fixture = samples[role]
        endpoint = '/repos/labadmin/proof/keys' if role == 'deploy' else '/users/' + fixture['用户'] + '/keys'
        rows = lab.api(node, 'GET', endpoint).json()
        actual = next((row for row in rows if row['id'] == fixture['服务端密钥ID']), None)
        public = Path(fixture['私钥文件']).with_suffix('.pub').read_text().split()[:2]
        if actual is None or actual['key'].split()[:2] != public or (role == 'deploy' and actual.get('read_only') is not True):
            raise CheckFailure('恢复后的 SSH 密钥身份或只读属性不一致：' + role)
    remote = 'ssh://git@%s:2222/labadmin/proof.git' % lab.state['addresses'][node]
    branch = 'ssh-key-write-' + str(time.time_ns())
    directory = lab.root / ('ssh-key-client-' + node + '-' + str(time.time_ns()))
    writer = samples['writer']
    git(lab, writer, ['clone', '--branch', 'main', remote, str(directory)])
    git(lab, writer, ['checkout', '-b', branch], cwd=directory)
    (directory / 'ssh-write-proof.txt').write_text('使用独立 SSH 用户密钥创建的真实提交。\n')
    git(lab, writer, ['add', 'ssh-write-proof.txt'], cwd=directory)
    git(lab, writer, ['-c', 'user.name=SSH 权限验证', '-c', 'user.email=ssh@lab.invalid',
                     'commit', '-m', '验证恢复后的 SSH 写入'], cwd=directory)
    git(lab, writer, ['push', remote, 'HEAD:refs/heads/' + branch], cwd=directory)
    wanted = git(lab, writer, ['rev-parse', 'HEAD'], cwd=directory).stdout.decode().strip()
    remote_refs = git(lab, writer, ['ls-remote', remote, 'refs/heads/' + branch]).stdout.decode().split()
    if not remote_refs or remote_refs[0] != wanted:
        raise CheckFailure('SSH 写入返回成功但服务端引用不匹配')
    checks = [{'说明': '独立写用户密钥真实 clone、commit、push 和引用读回', '通过': True}]
    for role in ('reader', 'deploy'):
        # 先证明同一密钥可真实读取，再验证明确的权限拒绝，不能用网络失败冒充只读保护。
        git(lab, samples[role], ['fetch', remote, 'main'], cwd=directory)
        denied_ref = branch + '-' + role + '-denied'
        result = git(lab, samples[role], ['push', remote, 'HEAD:refs/heads/' + denied_ref], cwd=directory, check=False)
        if not write_denied(result):
            raise CheckFailure(role + ' 未得到明确的服务器写权限拒绝：' + lab.scrub(result.stderr.decode(errors='replace')[-800:]))
        if git(lab, writer, ['ls-remote', remote, 'refs/heads/' + denied_ref]).stdout.strip():
            raise CheckFailure('SSH 权限拒绝后远端实际出现了新分支')
        checks.append({'说明': role + ' 密钥读取成功，真实推送权限拒绝且无远端写入', '通过': True})
    for role, message in (('disabled', 'your account is disabled'), ('outsider', 'permission denied')):
        result = git(lab, samples[role], ['ls-remote', remote], check=False)
        if result.returncode == 0 or message not in result.stderr.decode(errors='replace').lower():
            raise CheckFailure(role + ' 未得到明确的身份拒绝')
        checks.append({'说明': role + ' 身份明确拒绝读取私有仓库', '通过': True})
    path = lab.root / ('ssh-key-verification-' + node + '-' + str(time.time_ns()) + '.json')
    path.write_text(json.dumps({'说明': '真实 SSH 身份、部署密钥及授权验证；只有对应恢复后执行才证明迁移',
        '节点': node, '写入分支': branch, '写入提交': wanted, '断言': checks}, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)
    shutil.rmtree(directory)
