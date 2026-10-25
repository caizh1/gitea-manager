"""独立于产品校验器的 Git、业务表、文件及配置对照。"""
import configparser
import hashlib
import json
import os
import base64
import hmac
import struct
import time
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import io
from urllib.parse import urlparse, unquote

from .driver import CheckFailure
from .fixtures import git


# 仅排除访问本身改变的字段；不整表忽略用户、仓库、权限或流水线。
VOLATILE_FIELDS = {
    'user': {'last_login_unix', 'updated_unix'},
    'access_token': {'updated_unix'},
    'repository': {'updated_unix'},
    # Runner 心跳保存 last_online 时，ORM 同时更新 updated；其他 Runner 配置列全部比较。
    'action_runner': {'last_online', 'updated'},
    # 原生治理事务锁计数不是业务配置；unknown 操作的时间字段在行级条件中处理。
    'governance_write_lock': {'revision'},
}
DEPLOYMENT_CONFIG = {
    ('database', key) for key in ('host', 'name', 'user', 'passwd', 'path', 'ssl_mode')
} | {('server', key) for key in ('domain', 'root_url', 'local_root_url', 'http_addr', 'http_port',
                                'ssh_domain', 'ssh_port', 'ssh_listen_host', 'ssh_listen_port', 'cert_file', 'key_file')}
SECRET_CONFIG = {('security', key) for key in ('secret_key', 'secret_key_uri')} | {
    ('oauth2', key) for key in ('jwt_secret', 'jwt_secret_uri')}


def collection_rules():
    return {'访问变化字段': {key: sorted(value) for key, value in VOLATILE_FIELDS.items()},
            '条件变化字段': {'governance_repository_creation.next_attempt_unix': "仅 state='unknown' 的已有核对操作",
                'app_state.runtime-state.revision': '原始列仍完整采集；只有该行其他字段及所有其他行完全一致时，允许隔离验证与正式发布导致计数增加二'},
            '数据库比较': '完整 public 数据表集合，不用表名白名单跳过业务或运行数据',
            '目标部署配置': [list(pair) for pair in sorted(DEPLOYMENT_CONFIG)]}


def sql(lab, node, statement):
    raw = lab.command(node, ['docker', 'exec', '-u', 'postgres', 'postgres', 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
                            '-U', 'gitea', '-d', 'gitea', '-Atc', statement])[1].decode().strip()
    return raw


def tables_query(table_names):
    """一条只读语句采集全部表，保留相同行排序和精确排除项，减少 SSH 握手。"""
    if not table_names:
        raise CheckFailure('数据库没有 public 表，拒绝生成空业务基线')
    queries = []
    for table in table_names:
        quoted = '"' + table.replace('"', '""') + '"'
        literal = "'" + table.replace("'", "''") + "'"
        expression = 'to_jsonb(t)' + ''.join(" - '%s'" % field for field in sorted(VOLATILE_FIELDS.get(table, set())))
        if table == 'governance_repository_creation':
            expression = "CASE WHEN t.state='unknown' THEN to_jsonb(t) - 'next_attempt_unix' ELSE to_jsonb(t) END"
        queries.append("SELECT %s AS k, coalesce(json_agg(v ORDER BY v::text),'[]') AS d "
                       'FROM (SELECT %s AS v FROM %s t) q' % (literal, expression, quoted))
    return 'SELECT json_object_agg(k,d) FROM (' + ' UNION ALL '.join(queries) + ') all_tables'


def settle(lab, node):
    """正式静态基线先排空流水线，并等待原生启动统计核对完成；不忽略业务计数。"""
    active = int(sql(lab, node, 'SELECT count(*) FROM action_run WHERE status IN (0,5,6,7)'))
    if active:
        raise CheckFailure('静态基线仍有非终态流水线，必须先明确排空，不替用户取消或忽略')
    query = ('SELECT coalesce(json_agg(i.id),\'[]\') FROM issue i WHERE num_comments != '
             '(SELECT count(*) FROM comment c WHERE c.issue_id=i.id AND c.type IN (0,22))')
    before = json.loads(sql(lab, node, query))
    lab.command(node, ['docker', 'restart', 'gitea'])
    lab.wait_gitea(node)
    deadline = time.monotonic() + 60
    while True:
        after = json.loads(sql(lab, node, query))
        if not after:
            break
        if time.monotonic() >= deadline:
            raise CheckFailure('原生启动统计尚未稳定，拒绝冻结不一致的 Issue 评论计数')
        time.sleep(1)
    path = lab.root / ('quiescence-' + node + '-' + str(time.time_ns()) + '.json')
    path.write_text(json.dumps({'说明': '仅重启本轮 Gitea 并等待原生统计任务；评论及计数字段仍完整比较',
        '重启前计数不一致Issue': before, '重启后计数不一致Issue': after,
        '非终态流水线数': active}, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def snapshot(lab, node):
    refs = {}
    repositories = lab.command(node, ['docker', 'exec', 'gitea', 'find', '/data/git/repositories', '-type', 'd', '-name', '*.git', '-prune'])[1].decode().splitlines()
    for path in sorted(repositories):
        quoted = shlex.quote(path)
        command = 'git --git-dir %s fsck --full --no-reflogs >/dev/null && git --git-dir %s rev-list --objects --all --no-object-names | LC_ALL=C sort | sha256sum' % (quoted, quoted)
        digest = lab.command(node, ['docker', 'exec', '-u', 'git', 'gitea', 'sh', '-ec', command])[1].decode().strip().split()[0]
        raw = lab.command(node, ['docker', 'exec', '-u', 'git', 'gitea', 'git', '--git-dir', path, 'for-each-ref', '--format=%(refname) %(objectname)'])[1].decode().splitlines()
        head = lab.command(node, ['docker', 'exec', 'gitea', 'cat', path + '/HEAD'])[1].decode().strip()
        refs[path.removeprefix('/data/git/repositories/')] = {'refs': sorted(raw), 'head': head, 'objects_sha256': digest}
    table_names = json.loads(sql(lab, node, "SELECT json_agg(tablename ORDER BY tablename) FROM pg_tables WHERE schemaname='public'"))
    tables = json.loads(sql(lab, node, tables_query(table_names)))
    raw_config = lab.command(node, ['docker', 'exec', 'gitea', 'cat', '/data/gitea/conf/app.ini'])[1].decode()
    config = configparser.ConfigParser(interpolation=None, strict=False)
    # Gitea 顶层配置是独立配置项，不能经 ConfigParser DEFAULT 自动继承到每个业务节。
    config.read_string('[__global__]\n' + raw_config)
    settings = {}
    key_value = config.get('security', 'SECRET_KEY', fallback='')
    key_uri = config.get('security', 'SECRET_KEY_URI', fallback='')
    if key_uri:
        parsed = urlparse(key_uri)
        if parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost'):
            raise CheckFailure('独立核验器尚不支持该密钥 URI，不跳过密钥检查')
        key_value = lab.command(node, ['docker', 'exec', 'gitea', 'cat', unquote(parsed.path)])[1].decode().strip()
    keys = {'security.secret_key': hashlib.sha256(key_value.encode()).hexdigest()}
    for section in config.sections():
        for key, value in config.items(section):
            pair = (section.lower(), key.lower())
            if pair in DEPLOYMENT_CONFIG or pair in SECRET_CONFIG:
                continue
            settings['.'.join(pair)] = hashlib.sha256(value.encode()).hexdigest()
    # 应用正文存储；排除可重建索引、日志和运行临时文件。
    files = {}
    roots = ('/data/gitea/attachments', '/data/gitea/avatars', '/data/gitea/repo-avatars',
             '/data/gitea/packages', '/data/gitea/actions_artifacts', '/data/gitea/actions_log', '/data/git/lfs')
    script = 'for p in ' + ' '.join(shlex.quote(path) for path in roots) + '; do if test -d "$p"; then find "$p" -type f -exec sha256sum {} \;; fi; done'
    for line in lab.command(node, ['docker', 'exec', 'gitea', 'sh', '-ec', script])[1].decode().splitlines():
        digest, path = line.split(None, 1)
        files[path] = digest
    value = {'Git仓库': refs, '业务表': tables, '业务配置': settings, '正文文件': files, '数据密钥': keys,
            '规则': collection_rules()}
    directory = lab.root / 'snapshots'
    directory.mkdir(exist_ok=True, mode=0o700)
    path = directory / (node + '-' + str(time.time_ns()) + '.json')
    value['证据文件'] = str(path)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)
    return value


def compare(expected, actual):
    if expected.get('规则') != actual.get('规则'):
        raise CheckFailure('独立采集规则不一致；保留旧快照，先以相同新版规则重新采集，不能将工具差异判作产品损坏')
    from .audit_compare import recovery_append_only
    from .runtime_compare import native_runtime_transition
    audit_append = recovery_append_only(expected['业务表'], actual['业务表'])
    differences = []
    for section in ('Git仓库', '业务表', '业务配置', '正文文件', '数据密钥'):
        left, right = expected[section], actual[section]
        for key in sorted(set(left) | set(right)):
            if left.get(key) != right.get(key):
                if section == '业务表' and key in ('governance_audit_event', 'governance_audit_scope') and audit_append:
                    continue
                if section == '业务表' and key == 'app_state' and native_runtime_transition(left.get(key, []), right.get(key, [])):
                    continue
                differences.append({'范围': section, '对象': key, '原因': '缺失' if key not in right else '多出' if key not in left else '内容不同'})
    return differences


def verify_packages(lab, node):
    packages = lab.state['nodes'][node]['seed']['packages']
    for item in packages:
        if item['type'] not in ('generic', 'npm'):
            continue
        response = lab.request(node, 'GET', item['endpoint'], stream=True)
        digest, length = hashlib.sha256(), 0
        for block in response.iter_content(1024 * 1024):
            digest.update(block); length += len(block)
        response.close()
        if digest.hexdigest() != item['sha256'] or ('size' in item and length != item['size']):
            raise CheckFailure('下载软件包正文不匹配：' + item['name'])
        if item.get('private'):
            response = lab.request(node, 'GET', item['endpoint'], headers={'Authorization': ''}, expected=None)
            if response.status_code not in (401, 403, 404):
                raise CheckFailure('匿名用户获得私有软件包正文')
    # 从恢复后的 PyPI 接口下载并安装，随后实际导入。
    directory = lab.root / 'clients' / ('pypi-' + node + '-' + str(time.time_ns()))
    directory.mkdir(parents=True)
    state = lab.state['nodes'][node]
    wheel = next(item for item in packages if item['type'] == 'pypi')
    downloaded = lab.request(node, 'GET', '/api/packages/labadmin/pypi/files/remote-proof/1.0.0/' + wheel['filename'])
    if hashlib.sha256(downloaded.content).hexdigest() != wheel['sha256']:
        raise CheckFailure('PyPI 实际下载文件的完整摘要不同')
    url = lab.gitea_url(node).replace('http://', 'http://labadmin:' + state['password'] + '@')
    env = os.environ.copy()
    env['PIP_INDEX_URL'] = url + '/api/packages/labadmin/pypi/simple'
    args = ['python', '-m', 'pip', 'install', '--disable-pip-version-check', '--no-deps', '--no-cache-dir',
            '--trusted-host', lab.state['addresses'][node], '--target', str(directory), 'remote-proof==1.0.0']
    result = subprocess.run(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise CheckFailure('从恢复后 PyPI 安装失败：' + lab.scrub(result.stderr.decode(errors='replace')[-800:]))
    result = subprocess.run(['python', '-c', 'import sys; sys.path.insert(0,sys.argv[1]); import remote_proof; assert remote_proof.verify()=="灾备正文可用"', str(directory)], capture_output=True)
    if result.returncode:
        raise CheckFailure('软件包安装后无法实际使用')
    directory = lab.root / 'clients' / ('npm-' + node + '-' + str(time.time_ns()))
    directory.mkdir(parents=True)
    auth = base64.b64encode(('labadmin:' + state['password']).encode()).decode()
    host = lab.state['addresses'][node] + ':3000'
    registry = lab.gitea_url(node) + '/api/packages/labadmin/npm/'
    config = directory / '.npmrc'
    config.write_text('registry=%s\n//%s/api/packages/labadmin/npm/:_auth=%s\nalways-auth=true\n' % (registry, host, auth))
    config.chmod(0o600)
    result = subprocess.run(['npm', 'install', '--ignore-scripts', '--no-audit', '--no-fund', '--userconfig', str(config),
                             '--prefix', str(directory), 'remote-proof@1.0.0'], capture_output=True)
    if result.returncode:
        raise CheckFailure('从恢复后 npm 安装失败：' + lab.scrub(result.stderr.decode(errors='replace')[-800:]))
    result = subprocess.run(['node', '-e', 'if(require(process.argv[1]).verify()!==42)process.exit(1)',
                             str(directory / 'node_modules' / 'remote-proof')], capture_output=True)
    if result.returncode:
        raise CheckFailure('npm 软件包安装后无法使用')
    image = next(item for item in packages if item['type'] == 'oci')
    # Docker 可复用本地层；另经无缓存 HTTP 会话核验远端 manifest 和每一层实际正文。
    import requests
    registry = requests.Session(); registry.trust_env = False
    response = registry.get(lab.gitea_url(node) + '/v2/token', auth=('labadmin', state['password']),
        params={'service': 'container_registry', 'scope': 'repository:labadmin/remote-proof:pull'}, timeout=30)
    response.raise_for_status()
    access = response.json()
    registry.headers.update({'Authorization': 'Bearer ' + (access.get('token') or access['access_token']),
                             'Cache-Control': 'no-cache'})
    response = registry.get(lab.gitea_url(node) + '/v2/labadmin/remote-proof/manifests/1.0.0',
        headers={'Accept': 'application/vnd.oci.image.manifest.v1+json'}, timeout=30)
    response.raise_for_status()
    if hashlib.sha256(response.content).hexdigest() != image['manifest']:
        raise CheckFailure('OCI 远端 manifest 完整摘要不同')
    for blob in image['blobs']:
        response = registry.get(lab.gitea_url(node) + '/v2/labadmin/remote-proof/blobs/' + blob['digest'], timeout=30)
        response.raise_for_status()
        if len(response.content) != blob['size'] or 'sha256:' + hashlib.sha256(response.content).hexdigest() != blob['digest']:
            raise CheckFailure('OCI 远端配置或 layer 正文不同')
    lab.command(node, ['docker', 'login', '127.0.0.1:3000', '--username', 'labadmin', '--password-stdin'],
                input_data=state['password'] + '\n')
    image_name = '127.0.0.1:3000/labadmin/remote-proof:1.0.0'
    lab.command(node, ['docker', 'pull', image_name])
    metadata = json.loads(lab.command(node, ['docker', 'image', 'inspect', image_name])[1])
    if not any(value.endswith('@sha256:' + image['manifest']) for value in metadata[0].get('RepoDigests', [])):
        raise CheckFailure('实际拉取的 OCI manifest 摘要不同')
    identifier = lab.command(node, ['docker', 'create', image_name, '/unused'])[1].decode().strip()
    try:
        body = lab.command(node, ['docker', 'cp', identifier + ':/proof.txt', '-'])[1]
        with tarfile.open(fileobj=io.BytesIO(body)) as archive:
            if archive.extractfile(archive.getmembers()[0]).read().decode() != '容器软件包恢复验证\n':
                raise CheckFailure('实际拉取的 OCI layer 正文不同')
    finally:
        lab.command(node, ['docker', 'rm', identifier])
    endpoint = '/api/packages/labadmin/generic/post-restore/' + str(time.time_ns()) + '/new.bin'
    body = '恢复后继续发布软件包'.encode()
    lab.request(node, 'PUT', endpoint, data=body)
    if lab.request(node, 'GET', endpoint).content != body:
        raise CheckFailure('恢复后新版本发布或下载失败')


def verify_git(lab, node):
    for name in ('qemu', 'openssd', 'proof'):
        result = git(lab, node, ['ls-remote', lab.gitea_url(node) + '/labadmin/' + name + '.git'])
        if b'refs/heads/' not in result:
            raise CheckFailure('HTTP Git 未返回分支：' + name)
    for name in ('qemu', 'openssd'):
        target = lab.root / ('restored-' + node + '-' + name + '.git')
        if target.exists():
            git(lab, node, ['--git-dir', str(target), 'fetch', '--prune', lab.gitea_url(node) + '/labadmin/' + name + '.git', '+refs/*:refs/*'])
        else:
            git(lab, node, ['clone', '--mirror', lab.gitea_url(node) + '/labadmin/' + name + '.git', str(target)])
        git(lab, node, ['--git-dir', str(target), 'fsck', '--full', '--no-reflogs'])
        source_refs = git(lab, node, ['--git-dir', str(lab.root / 'seeds' / (name + '.git')), 'for-each-ref', '--format=%(refname) %(objectname)'])
        actual_refs = git(lab, node, ['--git-dir', str(target), 'for-each-ref', '--format=%(refname) %(objectname)'])
        if source_refs != actual_refs:
            raise CheckFailure('完整克隆的引用与固定样本不一致：' + name)
    env = os.environ.copy()
    env['GIT_SSH_COMMAND'] = 'ssh -i /evidence/id_rsa -o IdentitiesOnly=yes -o UserKnownHostsFile=/evidence/git_known_hosts -o StrictHostKeyChecking=yes'
    result = subprocess.run(['git', 'ls-remote', 'ssh://git@%s:2222/labadmin/proof.git' % lab.state['addresses'][node]], env=env, capture_output=True)
    if result.returncode:
        raise CheckFailure('恢复后的 SSH Git 访问失败：' + result.stderr.decode(errors='replace')[-800:])
    for user, expected in (('labreader', (200,)), ('labdenied', (403, 404)), ('labdisabled', (401, 403))):
        response = lab.api(node, 'GET', '/repos/labadmin/proof', user=user, expected=None)
        if response.status_code not in expected:
            raise CheckFailure('%s 访问结果应为 %s，实际 %d' % (user, expected, response.status_code))
    location = lab.root / ('git-write-' + node + '-' + str(time.time_ns()))
    remote = lab.gitea_url(node) + '/labadmin/proof.git'
    git(lab, node, ['clone', '--branch', 'main', remote, str(location)], username='labwriter')
    branch = 'restore-write-' + str(time.time_ns())
    git(lab, node, ['checkout', '-b', branch], cwd=location)
    (location / 'write-proof.txt').write_text('恢复后的写入验证\n')
    git(lab, node, ['add', 'write-proof.txt'], cwd=location)
    git(lab, node, ['-c', 'user.name=灾备写入用户', '-c', 'user.email=writer@lab.invalid',
                    'commit', '-m', '恢复后允许写入'], cwd=location)
    git(lab, node, ['push', 'origin', branch], cwd=location, username='labwriter')
    denied_branch = branch + '-denied'
    try:
        git(lab, node, ['push', 'origin', 'HEAD:refs/heads/' + denied_branch], cwd=location, username='labreader')
    except CheckFailure:
        if git(lab, node, ['ls-remote', remote, 'refs/heads/' + denied_branch]).strip():
            raise CheckFailure('只读用户虽然收到错误，远端分支实际已被写入')
    else:
        raise CheckFailure('只读用户通过真实 Git push 获得写权限')
    for user in ('labdenied', 'labdisabled'):
        try:
            git(lab, node, ['ls-remote', remote], username=user)
        except CheckFailure:
            pass
        else:
            raise CheckFailure(user + ' 可以通过 Git 协议读取私有仓库')


def verify_lfs(lab, node):
    expected = lab.state['nodes'][node]['seed']['lfs']
    response = lab.request(node, 'POST', '/labadmin/proof.git/info/lfs/objects/batch',
         auth=('labadmin', lab.state['nodes'][node]['password']),
         headers={'Authorization': '', 'Accept': 'application/vnd.git-lfs+json', 'Content-Type': 'application/vnd.git-lfs+json'},
         json={'operation': 'download', 'transfers': ['basic'], 'objects': [expected]})
    obj = response.json()['objects'][0]
    if obj.get('error'):
        raise CheckFailure('LFS 元数据未恢复')
    action = obj['actions']['download']
    import requests
    result = requests.get(action['href'], headers=action.get('header', {}), timeout=120)
    if result.status_code != 200 or len(result.content) != expected['size'] or hashlib.sha256(result.content).hexdigest() != expected['oid']:
        raise CheckFailure('LFS 正文下载校验失败')
    directory = lab.root / ('lfs-checkout-' + node + '-' + str(time.time_ns()))
    git(lab, node, ['clone', '--branch', 'feature-proof', lab.gitea_url(node) + '/labadmin/proof.git', str(directory)])
    git(lab, node, ['lfs', 'install', '--local'], cwd=directory)
    git(lab, node, ['lfs', 'pull'], cwd=directory)
    if hashlib.file_digest((directory / 'lfs-proof.bin').open('rb'), 'sha256').hexdigest() != expected['oid']:
        raise CheckFailure('真实 LFS checkout 正文不一致')
    git(lab, node, ['submodule', 'update', '--init'], cwd=directory)
    wanted = git(lab, node, ['--git-dir', str(lab.root / 'seeds' / 'openssd.git'), 'rev-parse', 'HEAD']).strip()
    actual = git(lab, node, ['rev-parse', 'HEAD'], cwd=directory / 'deps' / 'openssd').strip()
    if wanted != actual:
        raise CheckFailure('恢复后子模块提交不一致')
    branch = 'lfs-write-' + str(time.time_ns())
    git(lab, node, ['checkout', '-b', branch], cwd=directory)
    body = hashlib.shake_256(branch.encode()).digest(1024 * 1024)
    (directory / 'new-object.bin').write_bytes(body)
    git(lab, node, ['lfs', 'track', 'new-object.bin'], cwd=directory)
    git(lab, node, ['add', '.gitattributes', 'new-object.bin'], cwd=directory)
    git(lab, node, ['-c', 'user.name=灾备 LFS 验证', '-c', 'user.email=lfs@lab.invalid',
                    'commit', '-m', '恢复后上传新的 LFS 对象'], cwd=directory)
    git(lab, node, ['push', 'origin', branch], cwd=directory)
    fresh = lab.root / ('lfs-readback-' + node + '-' + str(time.time_ns()))
    git(lab, node, ['clone', '--branch', branch, lab.gitea_url(node) + '/labadmin/proof.git', str(fresh)])
    if (fresh / 'new-object.bin').read_bytes() != body:
        raise CheckFailure('新上传的 LFS 对象不能从空客户端下载')
    lab.note('lfs-readback', node + '：新 LFS 对象上传和空客户端读取', '通过',
             '分支=%s，字节=%d，SHA256=%s' % (branch, len(body), hashlib.sha256(body).hexdigest()))
    # 首败克隆保留；只有全部读回断言通过后才删除本用例专属的临时客户端。
    shutil.rmtree(directory)
    shutil.rmtree(fresh)


def verify_identity(lab, node):
    state = lab.state['nodes'][node]
    # Gitea 持久化防重放口令；重启后的重复核验也必须等待下一个时间步。
    previous = sql(lab, node, "SELECT last_used_passcode FROM two_factor WHERE uid=(SELECT id FROM \"user\" WHERE name='labtwofactor')")
    for _ in range(35):
        step = struct.pack('>Q', int(time.time()) // 30)
        raw = hmac.new(base64.b32decode(state['totp']), step, hashlib.sha1).digest()
        offset = raw[-1] & 15
        otp = str((struct.unpack('>I', raw[offset:offset + 4])[0] & 0x7fffffff) % 1000000).zfill(6)
        if otp != previous:
            break
        time.sleep(1)
    else:
        raise CheckFailure('等待新的双因素口令超时，不能重复使用已消费的口令')
    response = lab.request(node, 'POST', '/api/v1/users/labtwofactor/tokens',
        headers={'Authorization': '', 'X-Gitea-OTP': otp}, auth=('labtwofactor', state['passwords']['labtwofactor']),
        json={'name': '恢复后验证-' + str(time.time_ns()), 'scopes': ['read:user']})
    if not response.json().get('sha1'):
        raise CheckFailure('真实双因素认证未生成权限受限令牌')
    response = lab.api(node, 'POST', '/repos/labadmin/proof/issues',
        headers={'Authorization': 'token ' + state['scoped_token']},
        json={'title': '只读令牌不能创建事项'}, expected=None)
    if response.status_code != 403:
        raise CheckFailure('恢复后只读 Token 获得了写权限')


def challenge(expected):
    """独立验证器反向测试；每个变异都必须产生差异。"""
    import copy
    outputs = []
    for section in ('Git仓库', '业务表', '业务配置', '正文文件'):
        if not expected[section]:
            raise CheckFailure('反向测试缺少真实基线：' + section)
        value = copy.deepcopy(expected)
        key = next(iter(value[section]))
        del value[section][key]
        found = compare(expected, value)
        if not any(item['范围'] == section for item in found):
            raise CheckFailure('验证器漏检：' + section)
        outputs.append({'变异范围': section, '检出': True})
    return outputs
