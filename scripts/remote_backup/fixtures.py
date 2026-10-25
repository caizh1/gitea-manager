"""通过公开 API 和 Git 生成真实数据；样本凭据不进入公开证据。"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tarfile
import zipfile

import requests

from .driver import CheckFailure


def command(args, cwd=None, env=None, timeout=3600):
    result = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise CheckFailure('数据准备命令退出码 %s：%s' % (result.returncode, result.stderr.decode(errors='replace')[-1000:]))
    return result.stdout


def git(lab, node, args, cwd=None, username='labadmin'):
    env = os.environ.copy()
    state = lab.state['nodes'][node]
    password = state['password'] if username == 'labadmin' else state['passwords'][username]
    credential = base64.b64encode((username + ':' + password).encode()).decode()
    env.update(GIT_CONFIG_COUNT='3', GIT_CONFIG_KEY_0='http.extraHeader', GIT_CONFIG_VALUE_0='Authorization: Basic ' + credential,
               GIT_CONFIG_KEY_1='pack.threads', GIT_CONFIG_VALUE_1='2',
               GIT_CONFIG_KEY_2='pack.windowMemory', GIT_CONFIG_VALUE_2='64m', GIT_TERMINAL_PROMPT='0')
    return command(['git', *args], cwd=cwd, env=env)


def ensure_repo(lab, node, name, **extra):
    result = lab.api(node, 'GET', '/repos/labadmin/' + name, expected=None)
    if result.status_code == 404:
        return lab.api(node, 'POST', '/user/repos', json={'name': name, 'private': True, **extra}).json()
    if result.status_code != 200:
        raise CheckFailure('无法读取样本仓库')
    return result.json()


def deterministic_file(path, length):
    if path.exists() and path.stat().st_size == length:
        return hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()
    block = hashlib.shake_256(b'remote-backup-fixed-data-v1').digest(1024 * 1024)
    digest = hashlib.sha256()
    with path.open('wb') as stream:
        remaining = length
        while remaining:
            data = block[:min(remaining, len(block))]
            stream.write(data)
            digest.update(data)
            remaining -= len(data)
    path.chmod(0o600)
    return digest.hexdigest()


def seed_users(lab, node):
    state = lab.state['nodes'][node]
    state.setdefault('passwords', {})
    state.setdefault('tokens', {})
    for username in ('labwriter', 'labreader', 'labdenied', 'labdisabled', 'labtwofactor', 'labreviewer'):
        password = state['passwords'].setdefault(username, secrets.token_urlsafe(20))
        response = lab.api(node, 'GET', '/users/' + username, expected=None)
        if response.status_code == 404:
            lab.api(node, 'POST', '/admin/users', json={'username': username, 'password': password,
                'email': username + '@lab.invalid', 'must_change_password': False, 'send_notify': False})
        if username not in state['tokens']:
            response = lab.request(node, 'POST', '/api/v1/users/' + username + '/tokens',
                 auth=(username, password), headers={'Authorization': ''}, json={'name': 'remote-lab', 'scopes': ['all']})
            state['tokens'][username] = response.json()['sha1']
    lab.save()
    if 'scoped_token' not in state:
        response = lab.request(node, 'POST', '/api/v1/users/labwriter/tokens',
            auth=('labwriter', state['passwords']['labwriter']), headers={'Authorization': ''},
            json={'name': '只读仓库令牌', 'scopes': ['read:repository']})
        state['scoped_token'] = response.json()['sha1']
        lab.save()


def seed_twofactor(lab, node):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from .oracle import sql
    state = lab.state['nodes'][node]
    if state.get('totp'):
        return
    secret = base64.b32encode(secrets.token_bytes(20)).decode()
    iv = secrets.token_bytes(16)
    cipher = Cipher(algorithms.AES(hashlib.md5(state['secret'].encode()).digest()), modes.CFB(iv)).encryptor()
    encrypted = base64.b64encode(iv + cipher.update(base64.b64encode(secret.encode())) + cipher.finalize()).decode()
    sql(lab, node, "INSERT INTO two_factor (uid,secret) SELECT id,'%s' FROM \"user\" WHERE name='labtwofactor'" % encrypted)
    state['totp'] = secret
    lab.save()


def seed_git(lab, node):
    fixtures = lab.root / 'seeds'
    for name in ('qemu', 'openssd'):
        path = fixtures / (name + '.git')
        if not path.is_dir():
            raise CheckFailure('完整 Git 样本尚未准备：' + name)
        shallow = command(['git', '--git-dir', str(path), 'rev-parse', '--is-shallow-repository']).decode().strip()
        if shallow != 'false':
            raise CheckFailure('不能使用浅克隆代替完整历史样本')
        ensure_repo(lab, node, name)
        git(lab, node, ['--git-dir', str(path), 'push', '--mirror', lab.gitea_url(node) + '/labadmin/' + name + '.git'])
        default = command(['git', '--git-dir', str(path), 'symbolic-ref', 'HEAD']).decode().strip().removeprefix('refs/heads/')
        lab.api(node, 'PATCH', '/repos/labadmin/' + name, json={'default_branch': default})
    ensure_repo(lab, node, 'empty')
    ensure_repo(lab, node, 'proof', auto_init=True, default_branch='main')
    for user, permission in (('labwriter', 'write'), ('labreader', 'read')):
        lab.api(node, 'PUT', '/repos/labadmin/proof/collaborators/' + user, json={'permission': permission})
    lab.api(node, 'POST', '/user/keys', json={'title': '跨网段测试密钥', 'key': (lab.root / 'id_rsa.pub').read_text()}, expected=(201, 422))
    keys = []
    for host in ('a', 'b'):
        raw = lab.command(host, ['docker', 'exec', 'gitea', 'cat', '/data/ssh/ssh_host_ed25519_key.pub'])[1].decode().split()
        keys.append('[%s]:2222 %s %s' % (lab.state['addresses'][host], raw[0], raw[1]))
    (lab.root / 'git_known_hosts').write_text('\n'.join(keys) + '\n')


def seed_collaboration(lab, node):
    state = lab.state['nodes'][node]
    seed = state.setdefault('seed', {})
    if 'issue' not in seed:
        label = lab.api(node, 'POST', '/repos/labadmin/proof/labels', json={'name': '灾备验收', 'color': '0066cc'}).json()
        milestone = lab.api(node, 'POST', '/repos/labadmin/proof/milestones', json={'title': '恢复后继续协作'}).json()
        issue = lab.api(node, 'POST', '/repos/labadmin/proof/issues', json={'title': '备份前的问题', 'body': '正文需要完整保留',
                         'labels': [label['id']], 'milestone': milestone['id']}).json()
        seed['issue'] = issue['number']
        lab.api(node, 'POST', '/repos/labadmin/proof/issues/%s/comments' % issue['number'], json={'body': '恢复前已有评论'})
        release = lab.api(node, 'POST', '/repos/labadmin/proof/releases', json={'tag_name': 'backup-v1', 'name': '灾备测试发布', 'body': '发布正文'}).json()
        seed['release'] = release['id']
        asset = lab.api(node, 'POST', '/repos/labadmin/proof/releases/%s/assets' % release['id'],
                        params={'name': 'release-proof.txt'}, files={'attachment': ('release-proof.txt', '发布附件完整性'.encode())}).json()
        seed['release_asset'] = asset['id']
        seed['release_sha256'] = hashlib.sha256('发布附件完整性'.encode()).hexdigest()
        lab.save()
    if 'wiki' not in seed:
        lab.api(node, 'POST', '/repos/labadmin/proof/wiki/new', json={'title': 'Home',
            'content_base64': base64.b64encode('恢复前的完整 Wiki 正文'.encode()).decode(), 'message': '建立 Wiki 样本'})
        seed['wiki'] = '恢复前的完整 Wiki 正文'
        lab.save()
    work = lab.root / ('proof-work-' + node)
    if not work.exists():
        git(lab, node, ['clone', lab.gitea_url(node) + '/labadmin/proof.git', str(work)])
        git(lab, node, ['config', 'user.name', '灾备验收'], cwd=work)
        git(lab, node, ['config', 'user.email', 'lab@lab.invalid'], cwd=work)
        git(lab, node, ['checkout', '-b', 'feature-proof'], cwd=work)
        (work / 'proof.txt').write_text('分支数据和合并验证\n')
        (work / '.gitmodules').write_text('[submodule "deps/openssd"]\n path = deps/openssd\n url = ../openssd.git\n')
        head = command(['git', '--git-dir', str(lab.root / 'seeds' / 'openssd.git'), 'rev-parse', 'HEAD']).decode().strip()
        git(lab, node, ['update-index', '--add', '--cacheinfo', '160000', head, 'deps/openssd'], cwd=work)
        git(lab, node, ['add', 'proof.txt'], cwd=work)
        git(lab, node, ['add', '.gitmodules'], cwd=work)
        git(lab, node, ['commit', '-m', '加入灾备分支样本'], cwd=work)
        git(lab, node, ['push', '-u', 'origin', 'feature-proof'], cwd=work)
    if 'pr' not in seed:
        pr = lab.api(node, 'POST', '/repos/labadmin/proof/pulls', json={'title': '恢复后继续评审',
                     'body': '保留 PR 与评论关联', 'head': 'feature-proof', 'base': 'main'}).json()
        seed['pr'] = pr['number']
        lab.save()


def seed_oci(lab, node):
    """使用 Registry V2 写入真实可拉取的单层 OCI 镜像。"""
    from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
    seed = lab.state['nodes'][node]['seed']
    if any(item['type'] == 'oci' for item in seed['packages']):
        return
    session = requests.Session()
    session.trust_env = False
    session.auth = ('labadmin', lab.state['nodes'][node]['password'])
    base = lab.gitea_url(node)
    token_response = session.get(base + '/v2/token', params={'service': 'container_registry', 'scope': 'repository:labadmin/remote-proof:pull,push'}, timeout=30)
    token_response.raise_for_status()
    token = token_response.json().get('token') or token_response.json().get('access_token')
    session.auth = None
    session.headers['Authorization'] = 'Bearer ' + token
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode='w') as archive:
        data = '容器软件包恢复验证\n'.encode()
        item = tarfile.TarInfo('proof.txt'); item.size = len(data); item.mtime = 0
        archive.addfile(item, io.BytesIO(data))
    layer_bytes = layer.getvalue()
    layer_digest = 'sha256:' + hashlib.sha256(layer_bytes).hexdigest()
    config = json.dumps({'architecture': 'amd64', 'os': 'linux', 'config': {},
                         'rootfs': {'type': 'layers', 'diff_ids': [layer_digest]}, 'history': [{}]}, separators=(',', ':')).encode()
    descriptors = []
    for data, media_type in [(config, 'application/vnd.oci.image.config.v1+json'), (layer_bytes, 'application/vnd.oci.image.layer.v1.tar')]:
        digest = 'sha256:' + hashlib.sha256(data).hexdigest()
        response = session.post(base + '/v2/labadmin/remote-proof/blobs/uploads/', timeout=30)
        response.raise_for_status()
        location = urljoin(base, response.headers['Location'])
        if urlparse(location).netloc != urlparse(base).netloc:
            raise CheckFailure('Registry 返回非本实例上传地址')
        url = urlparse(location)
        query = parse_qsl(url.query) + [('digest', digest)]
        target = urlunparse(url._replace(query=urlencode(query)))
        response = session.put(target, data=data, headers={'Content-Type': 'application/octet-stream'}, timeout=60)
        response.raise_for_status()
        descriptors.append({'mediaType': media_type, 'digest': digest, 'size': len(data)})
    manifest = json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                           'config': descriptors[0], 'layers': [descriptors[1]]}, separators=(',', ':')).encode()
    response = session.put(base + '/v2/labadmin/remote-proof/manifests/1.0.0', data=manifest,
                           headers={'Content-Type': 'application/vnd.oci.image.manifest.v1+json'}, timeout=30)
    response.raise_for_status()
    seed['packages'].append({'name': 'oci', 'type': 'oci', 'manifest': hashlib.sha256(manifest).hexdigest(),
                             'blobs': descriptors})
    lab.save()


def seed_lfs(lab, node):
    if lab.state['nodes'][node].get('seed', {}).get('lfs'):
        return
    blob = lab.root / 'lfs-proof.bin'
    digest = deterministic_file(blob, 32 * 1024 * 1024)
    size = blob.stat().st_size
    response = lab.request(node, 'POST', '/labadmin/proof.git/info/lfs/objects/batch',
           auth=('labadmin', lab.state['nodes'][node]['password']),
           headers={'Authorization': '', 'Accept': 'application/vnd.git-lfs+json', 'Content-Type': 'application/vnd.git-lfs+json'},
           json={'operation': 'upload', 'transfers': ['basic'], 'objects': [{'oid': digest, 'size': size}]})
    obj = response.json()['objects'][0]
    if obj.get('error'):
        raise CheckFailure('LFS 上传授权失败')
    upload = obj.get('actions', {}).get('upload')
    if upload:
        # requests 按文件长度发送正文；不能沿用要求客户端自行编码的 chunked 提示。
        headers = {key: value for key, value in upload.get('header', {}).items()
                   if key.lower() not in ('transfer-encoding', 'content-length')}
        headers['Content-Length'] = str(size)
        with blob.open('rb') as stream:
            result = requests.put(upload['href'], headers=headers, data=stream, timeout=180)
            result.raise_for_status()
    work = lab.root / ('proof-work-' + node)
    git(lab, node, ['lfs', 'install', '--local'], cwd=work)
    object_path = work / '.git' / 'lfs' / 'objects' / digest[:2] / digest[2:4] / digest
    object_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(blob, object_path)
    pointer = 'version https://git-lfs.github.com/spec/v1\noid sha256:%s\nsize %d\n' % (digest, size)
    (work / 'lfs-proof.bin').write_text(pointer)
    (work / '.gitattributes').write_text('lfs-proof.bin filter=lfs diff=lfs merge=lfs -text\n')
    git(lab, node, ['add', '.gitattributes', 'lfs-proof.bin'], cwd=work)
    if git(lab, node, ['status', '--porcelain'], cwd=work).strip():
        git(lab, node, ['commit', '-m', '加入 LFS 正文引用'], cwd=work)
    git(lab, node, ['push', 'origin', 'feature-proof'], cwd=work)
    lab.state['nodes'][node]['seed']['lfs'] = {'oid': digest, 'size': size}
    lab.save()


def seed_packages(lab, node):
    package_dir = lab.root / 'payloads'
    package_dir.mkdir(exist_ok=True)
    seed = lab.state['nodes'][node].setdefault('seed', {})
    seed.setdefault('packages', [])
    existing = {item['name'] for item in seed['packages']}
    if 'private-payload.bin' not in existing:
        org = lab.api(node, 'GET', '/orgs/labpackages', expected=None)
        if org.status_code == 404:
            lab.api(node, 'POST', '/orgs', json={'username': 'labpackages', 'full_name': '私有软件包组织', 'visibility': 'private'})
        body = '私有软件包正文'.encode()
        for version in ('1.0.0', '2.0.0'):
            endpoint = '/api/packages/labpackages/generic/private-proof/' + version + '/private-payload.bin'
            lab.request(node, 'PUT', endpoint, data=body, headers={'Content-Type': 'application/octet-stream'})
            seed['packages'].append({'name': 'private-payload.bin', 'version': version, 'endpoint': endpoint,
                                     'sha256': hashlib.sha256(body).hexdigest(), 'size': len(body), 'type': 'generic', 'private': True})
        lab.save()
    for size in (1024, 32 * 1024**2, 256 * 1024**2, 1024**3):
        name = 'payload-%s.bin' % size
        path = package_dir / name
        digest = deterministic_file(path, size)
        if name not in existing:
            endpoint = '/api/packages/labadmin/generic/disaster-proof/1.0.0/' + name
            with path.open('rb') as stream:
                lab.request(node, 'PUT', endpoint, data=stream, headers={'Content-Type': 'application/octet-stream'}, timeout=1800)
            seed['packages'].append({'name': name, 'endpoint': endpoint, 'sha256': digest, 'size': size, 'type': 'generic'})
            lab.save()
    # 可被 pip 安装的真实 wheel；没有网络依赖。
    name = 'remote_proof-1.0.0-py3-none-any.whl'
    wheel = package_dir / name
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('remote_proof/__init__.py', 'def verify():\n    return "灾备正文可用"\n')
        archive.writestr('remote_proof-1.0.0.dist-info/METADATA', 'Metadata-Version: 2.1\nName: remote-proof\nVersion: 1.0.0\n')
        archive.writestr('remote_proof-1.0.0.dist-info/WHEEL', 'Wheel-Version: 1.0\nGenerator: remote-lab\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        archive.writestr('remote_proof-1.0.0.dist-info/RECORD', '')
    if 'pypi' not in existing:
        with wheel.open('rb') as stream:
            lab.request(node, 'POST', '/api/packages/labadmin/pypi',
                files={'content': (name, stream, 'application/octet-stream')},
                data={':action': 'file_upload', 'protocol_version': '1', 'name': 'remote-proof', 'version': '1.0.0',
                      'sha256_digest': hashlib.sha256(wheel.read_bytes()).hexdigest()}, expected=(200, 201))
        seed['packages'].append({'name': 'pypi', 'sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(), 'type': 'pypi', 'filename': name})
        lab.save()
    # npm 发布遵循 npm registry 的文档及 tarball 格式。
    if 'npm' not in existing:
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w:gz') as archive:
            for filename, body in {'package/package.json': b'{"name":"remote-proof","version":"1.0.0","main":"index.js"}',
                                   'package/index.js': b'module.exports = { verify: () => 42 };\n'}.items():
                info = tarfile.TarInfo(filename); info.size = len(body); info.mtime = 0
                archive.addfile(info, io.BytesIO(body))
        body = raw.getvalue()
        package = {'name': 'remote-proof', '_id': 'remote-proof', 'dist-tags': {'latest': '1.0.0'},
                   'versions': {'1.0.0': {'name': 'remote-proof', 'version': '1.0.0',
                       'dist': {'shasum': hashlib.sha1(body).hexdigest(),
                                'integrity': 'sha512-' + base64.b64encode(hashlib.sha512(body).digest()).decode(),
                                'tarball': lab.gitea_url(node) + '/api/packages/labadmin/npm/remote-proof/-/remote-proof-1.0.0.tgz'}}},
                   '_attachments': {'remote-proof-1.0.0.tgz': {'content_type': 'application/octet-stream', 'data': base64.b64encode(body).decode(), 'length': len(body)}}}
        lab.request(node, 'PUT', '/api/packages/labadmin/npm/remote-proof', json=package)
        seed['packages'].append({'name': 'npm', 'endpoint': '/api/packages/labadmin/npm/remote-proof/-/remote-proof-1.0.0.tgz',
                                'sha256': hashlib.sha256(body).hexdigest(), 'type': 'npm'})
        lab.save()
