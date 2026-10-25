"""一键升级的离线控制器，由包内 Python 镜像运行；敏感状态仅保存在私有备份目录。"""
import argparse
import base64
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import secrets
import shutil
import sys
import tarfile
import tempfile
import time

import docker
from docker.types import Mount

SERVICES = ('backend', 'frontend', 'review-worker', 'review-executor', 'review-progress', 'review-feedback', 'approval-view', 'svn-migration-worker', 'git-migration-worker')
WRITERS = ('backend', 'review-worker', 'review-progress', 'review-feedback', 'approval-view', 'svn-migration-worker', 'git-migration-worker')
STATE = Path('/state')
CHECK_KEY = '''
from pathlib import Path
from cryptography.fernet import Fernet
import json, sqlite3, sys
key=Path(sys.argv[1]).read_bytes().strip(); cipher=Fernet(key)
path=Path(sys.argv[2])
def verify(value):
    if isinstance(value,dict):
        for item in value.values(): verify(item)
    elif isinstance(value,list):
        for item in value: verify(item)
    elif isinstance(value,str) and value.startswith('gAAAA'):
        cipher.decrypt(value.encode())
if path.is_file():
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as c:
        names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('settings','repositories','setup_jobs','setup_resources'):
            if table in names:
                for row in c.execute('SELECT value FROM '+table): verify(json.loads(row[0]))
sys.stdout.buffer.write(key)
'''


CHECK_ACCESS_KEYS = r'''
from pathlib import Path
from cryptography.fernet import Fernet
import hashlib, json, sqlite3, sys
path=Path(sys.argv[1]); key=Path(sys.argv[2]) if sys.argv[2] else path.parent/'access.key'
files=[key,path.parent/'.session-key',path.parent/'svn-migration.key',path.parent/'git-migration.key']; encrypted=[]
if path.is_file():
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as c:
        names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'access_directory_sources' in names:
            encrypted=[r[0] for r in c.execute("SELECT password_ciphertext FROM access_directory_sources WHERE password_ciphertext != ''")]
        if 'svn_sources' in names:
            migration_values=[r[0] for r in c.execute("SELECT password_ciphertext FROM svn_sources WHERE password_ciphertext != ''")]
            if migration_values:
                migration_cipher=Fernet((path.parent/'svn-migration.key').read_bytes().strip())
                for value in migration_values: migration_cipher.decrypt(value.encode())
        if 'git_migration_sources' in names:
            values=[r[0] for r in c.execute("SELECT secret_ciphertext FROM git_migration_sources WHERE secret_ciphertext != ''")]
            if values:
                git_cipher=Fernet((path.parent/'git-migration.key').read_bytes().strip())
                for value in values: git_cipher.decrypt(value.encode())
if encrypted:
    cipher=Fernet(key.read_bytes().strip())
    for value in encrypted: cipher.decrypt(value.encode())
result={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
print(json.dumps(result))
'''


def atomic_text(path, value, mode=0o600):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.chmod(temporary, mode); stream.write(value); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally: temporary.unlink(missing_ok=True)


def write(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): result.update(block)
    return result.hexdigest()


def extract():
    archive = Path('/package/gitea-manager.tar.gz')
    expected = Path('/package/gitea-manager.tar.gz.sha256').read_text().split()[0]
    if digest(archive) != expected: raise RuntimeError('安装包校验失败')
    root = Path('/release')
    with tarfile.open(archive) as bundle:
        names = set()
        for member in bundle.getmembers():
            if member.name in names or not (member.isfile() or member.isdir()) or not (root / member.name).resolve().is_relative_to(root):
                raise RuntimeError('安装包包含重复文件或不安全路径')
            names.add(member.name)
        bundle.extractall(root)
    manifest = json.loads((root / 'docs/AIReview交付清单.json').read_text())
    for row in manifest['文件']:
        path = (root / row['文件']).resolve()
        if not path.is_relative_to(root) or digest(path) != row['SHA256']:
            raise RuntimeError('文件级校验失败：' + row['文件'])
    print('安装包与文件清单校验通过')


def environment(info):
    return dict(v.split('=', 1) for v in info['Config'].get('Env', []) if '=' in v)


def read_secret_file(container, path):
    """读取原容器的文件与挂载，容器已停止时也不需要启动业务进程。"""
    limit = 2 * 1024 * 1024
    for _ in range(40):
        try:
            stream, _details = container.get_archive(path)
        except docker.errors.NotFound:
            return b''
        raw = bytearray()
        try:
            for chunk in stream:
                raw.extend(chunk)
                if len(raw) > limit + 65536:
                    raise RuntimeError('原服务密钥文件超过允许大小')
        finally:
            if hasattr(stream, 'close'): stream.close()
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            members = archive.getmembers()
            if len(members) != 1:
                raise RuntimeError('原服务密钥路径必须指向单个文件')
            member = members[0]
            if member.issym():
                path = posixpath.normpath(posixpath.join(posixpath.dirname(path), member.linkname))
                continue
            if not member.isfile() or member.size > limit:
                raise RuntimeError('原服务密钥路径必须指向普通小文件')
            return archive.extractfile(member).read()
    raise RuntimeError('原服务密钥符号链接层级过多或存在循环')


def mount_value(item):
    kind = item['Type']
    if kind not in ('bind', 'volume'): raise RuntimeError('数据目录必须使用持久化 bind 或 volume 挂载')
    return {'type': kind, 'source': item.get('Name') if kind == 'volume' else item['Source'],
            'target': item['Destination'], 'read_only': not item.get('RW', True)}


def volume_at(info, path):
    rows = [r for r in info['Mounts'] if path == r['Destination'] or path.startswith(r['Destination'].rstrip('/') + '/')]
    if not rows: return None, None
    actual = max(rows, key=lambda r: len(r['Destination']))
    return mount_value(actual), path.removeprefix(actual['Destination']).lstrip('/')


def escaped(value):
    """已解析的配置再次交给 Compose 时，禁止把原始密码中的美元符号二次插值。"""
    if isinstance(value, str): return value.replace('$', '$$')
    if isinstance(value, list): return [escaped(v) for v in value]
    if isinstance(value, dict): return {k: escaped(v) for k, v in value.items()}
    return value


def approval_view_spec(backend, image, networks):
    """仅继承展示实际需要的原挂载和环境，保留 Manager 数据库与维护标记。"""
    env=backend['environment']
    default_networks=networks
    paths=[env['DATABASE_URL'].removeprefix('sqlite:///'),env.get('SSH_KEY_PATH','/root/.ssh/id_rsa'),
           '/var/run/docker.sock',env['GM_UPGRADE_MAINTENANCE_FILE']]
    view_mounts=[]
    for path in paths:
        matches=[m for m in backend['volumes'] if path==m['target'] or path.startswith(m['target'].rstrip('/')+'/')]
        if matches:
            mount=max(matches,key=lambda m:len(m['target']))
            if mount not in view_mounts:view_mounts.append(copy.deepcopy(mount))
    view_env={k:env[k] for k in ('DATABASE_URL','SSH_KEY_PATH','SSH_KEY_PASSPHRASE','GM_UPGRADE_MAINTENANCE_FILE') if k in env}
    spec=dict(image=image,platform='linux/amd64',pull_policy='never',restart='unless-stopped',
        command=['gunicorn','--bind','0.0.0.0:5000','--workers','2','--threads','8','--timeout','30','approval.view_app:create_app()'],
        environment=view_env,volumes=view_mounts,networks=copy.deepcopy(default_networks),
        mem_limit='512m',cpus=1.0,logging={'driver':'json-file','options':{'max-size':'10m','max-file':'3'}})
    return spec


class Upgrade:
    def __init__(self):
        self.client = docker.from_env(timeout=180)
        self.info_path = STATE / '升级状态.json'
        self.info = json.loads(self.info_path.read_text()) if self.info_path.exists() else {}
        self.backend = None
        with contextlib.suppress(docker.errors.NotFound): self.backend = self.client.containers.get(os.environ['GM_TARGET_CONTAINER'])
        self.project = self.info.get('project') or self.backend.labels['com.docker.compose.project']
        self.release_host = os.environ['GM_RELEASE_HOST']
        self.state_host = os.environ['GM_STATE_HOST']
        self.work_host = os.environ['GM_DEPLOYMENT_HOST']
        self.image = os.environ.get('GM_CONTROLLER_IMAGE') or self.client.images.get('gitea-review-backend:offline').id

    def save(self):
        events = self.info.setdefault('events', [])
        if not events or events[-1]['阶段'] != self.info.get('phase'):
            events.append({'阶段': self.info.get('phase'), '时间': time.time()})
        write(self.info_path, self.info)

    def containers(self):
        rows = self.client.containers.list(all=True, filters={'label': 'com.docker.compose.project=' + self.project})
        result = {}
        for c in rows:
            service = c.labels.get('com.docker.compose.service')
            if service in SERVICES:
                if service in result: raise RuntimeError('同一服务存在多个容器，请先核对部署实例')
                result[service] = c
        return result

    def volume(self, action, name, mount, parameters=None):
        mounts = [Mount('/volume', mount['source'], type=mount['type'], read_only=action not in ('restore', 'maintenance')),
                  Mount('/state', self.state_host, type='bind'),
                  Mount('/upgrade_volume.py', self.release_host + '/scripts/upgrade_volume.py', type='bind', read_only=True)]
        container = self.client.containers.create(self.image, ['python', '/upgrade_volume.py', action, name], platform='linux/amd64',
                    mounts=mounts, network_disabled=True, read_only=True, tmpfs={'/tmp': 'size=256m,mode=1777'},
                    environment={'VOLUME_PARAMETERS': json.dumps(parameters or {}, ensure_ascii=False), 'UPGRADE_TEMP_DIR': '/state', 'SQLITE_TMPDIR': '/state'},
                    labels={'io.gitea-manager.upgrade-helper': self.project})
        try:
            container.start(); result = container.wait(timeout=600)
            if result['StatusCode']:
                # 本助手仅输出受控中文错误；不输出数据库内容、配置或容器环境。
                output = container.logs().decode(errors='replace')
                message = next((line.split(': ', 1)[-1] for line in reversed(output.splitlines()) if line.startswith('RuntimeError: ')), None)
                raise RuntimeError(message or ('数据卷操作未完成：' + action + '/' + name))
            output = container.logs(stdout=True, stderr=False).decode()
            return json.loads(output) if action == 'inspect' else None
        finally:
            with contextlib.suppress(Exception): container.remove(force=True)

    def preflight(self):
        rows = self.containers()
        if 'frontend' not in rows or not self.backend.attrs['State']['Running']:
            raise RuntimeError('未找到正在运行的完整 Manager 实例')
        if any(c.attrs['State'].get('Paused') for c in rows.values()):
            raise RuntimeError('原服务处于暂停状态，未自动改变其状态')
        loaded_images = {}
        manifest = json.loads(Path('/release/offline/镜像清单.json').read_text())['镜像']
        components = ['backend', 'frontend', 'executor', 'svn', 'git']
        if any(r['镜像'] == 'gitea-review-executor-legacy:offline' for r in manifest): components.append('executor-legacy')
        for image in components:
            actual = self.client.images.get('gitea-review-' + image + ':offline')
            if actual.attrs['Architecture'] != 'amd64' or actual.attrs['Os'] != 'linux': raise RuntimeError('应用镜像架构不匹配')
            manifest = json.loads(Path('/release/offline/镜像清单.json').read_text())['镜像']
            expected = next(r for r in manifest if r['镜像'] == 'gitea-review-' + image + ':offline')
            if actual.id not in (expected['ID'], expected['配置ID']): raise RuntimeError('导入后的镜像与交付清单不一致')
            loaded_images[image] = actual.id
        probes = []
        for component in ('executor', 'executor-legacy'):
            if component not in loaded_images: continue
            probe = self.client.containers.create(loaded_images[component],
                ['python', '/opt/review/runtime_probe.py'], platform='linux/amd64', network_disabled=True,
                read_only=True, tmpfs={'/tmp':'size=384m,mode=1777'}, mem_limit='2g', pids_limit=128,
                cap_drop=['ALL'], security_opt=['no-new-privileges:true'])
            try:
                probe.start(); code = probe.wait(timeout=75)['StatusCode']
                try: result = json.loads(probe.logs(stdout=True, stderr=False))
                except ValueError:
                    # 隔离探针不挂载凭据；保留有限标准错误，避免缺文件被隐藏成空输出。
                    result = {'ready': False, 'reason': '执行器检测输出无效',
                              'stderr': probe.logs(stdout=False, stderr=True)[-4096:].decode(errors='replace')}
                probes.append({'component':component, 'exit_code':code, 'result':result})
                write(STATE / 'runtime-probes.json', probes)
                if code == 0 and result.get('ready'):
                    loaded_images['executor'] = loaded_images[component]
                    break
            finally: probe.remove(force=True)
        else: raise RuntimeError('执行器真实启动检测失败，原 Manager 未停止；请查看 runtime-probes.json')
        probe = self.client.containers.create(loaded_images['svn'], ['python', '-c',
            "import subprocess,svn_migration.worker; subprocess.run(['git','svn','--version'],check=True); subprocess.run(['svn','--version','--quiet'],check=True)"],
            platform='linux/amd64', network_disabled=True, read_only=True,
            environment={'PYTHONDONTWRITEBYTECODE': '1'}, tmpfs={'/tmp': 'size=32m,mode=1777'})
        try:
            probe.start()
            if probe.wait(timeout=60)['StatusCode']:
                raise RuntimeError('SVN 迁移镜像的工具链不完整，原 Manager 未停止')
        finally:
            probe.remove(force=True)
        probe = self.client.containers.create(loaded_images['git'], ['python', '-c',
            "import subprocess,git_migration.worker; subprocess.run(['git','--version'],check=True); subprocess.run(['git','lfs','version'],check=True)"],
            platform='linux/amd64', network_disabled=True, read_only=True,
            environment={'PYTHONDONTWRITEBYTECODE': '1'}, tmpfs={'/tmp': 'size=32m,mode=1777'})
        try:
            probe.start()
            if probe.wait(timeout=60)['StatusCode']:
                raise RuntimeError('Git 迁移镜像的工具链不完整，原 Manager 未停止')
        finally:
            probe.remove(force=True)
        env = environment(self.backend.attrs)
        uri = env.get('DATABASE_URL', '')
        if not uri.startswith('sqlite:////') or '?' in uri:
            raise RuntimeError('当前一键升级支持使用本机持久卷的 SQLite 部署')
        db_path = uri.removeprefix('sqlite:///')
        mount, relative = volume_at(self.backend.attrs, db_path)
        if not mount or mount['read_only']: raise RuntimeError('原 Manager 数据库没有可写持久挂载，未创建空库替代')
        access_key_probe = self.backend.exec_run(['python', '-c', CHECK_ACCESS_KEYS, db_path, env.get('ACCESS_KEY_FILE', '')])
        if access_key_probe.exit_code:
            raise RuntimeError('原目录密码无法用持久密钥解密，禁止生成替代密钥或继续升级')
        access_keys = json.loads(access_key_probe.output)

        backup_path = env.get('BACKUP_DIR', '/app/data/backups')
        excluded = backup_path.removeprefix(mount['target'] + '/') if backup_path.startswith(mount['target'] + '/') else ''
        if excluded and len(Path(excluded).parts) != 1:
            raise RuntimeError('嵌套备份目录需要先明确独立挂载，避免回退时遗漏中间目录')
        if self.state_host == mount['source'] or self.state_host.startswith(mount['source'].rstrip('/') + '/'):
            raise RuntimeError('升级备份目录不能放在被备份的数据卷中')
        parameters = {'database': relative, 'excluded': excluded}
        status = self.volume('inspect', 'manager', mount, parameters)
        if not status['database']['exists']: raise RuntimeError('原 Manager 数据库不存在，已停止升级')
        if status['database']['busy']: raise RuntimeError('Manager 存在运行中的任务，请完成后再次执行升级')
        review_path = env.get('AI_REVIEW_DB', '/app/review/review.db')
        review_mount, review_relative = volume_at(self.backend.attrs, review_path)
        if review_mount and (self.state_host == review_mount['source'] or self.state_host.startswith(review_mount['source'].rstrip('/') + '/')):
            raise RuntimeError('升级备份目录不能放在 Review 数据卷内部，已避免递归备份')
        if not review_mount:
            probe = self.backend.exec_run(['python', '-c', "from pathlib import Path; import sqlite3,json,sys; p=Path(sys.argv[1]); meaningful=False\nif p.is_file():\n with sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True) as c:\n  names={r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")}\n  meaningful=any(c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('repositories','jobs') if t in names)\n  if 'settings' in names: meaningful=meaningful or any(json.loads(r[0]).get('api_key') for r in c.execute('SELECT value FROM settings'))\nsys.exit(2 if meaningful else 0)", review_path])
            if probe.exit_code:
                raise RuntimeError('容器内存在未持久挂载的 Review 数据，请先迁出并挂载原库，未创建空库替代')
        review_status = self.volume('inspect', 'review', review_mount, {'database': review_relative}) if review_mount else None
        if review_status and review_status['database']['busy']: raise RuntimeError('AI 审查或初始化任务正在执行，请完成后再次升级')
        master_path = env.get('AI_REVIEW_MASTER_KEY_FILE')
        master_mount, _ = volume_at(self.backend.attrs, master_path) if master_path else (None, None)
        if review_status and review_status['database']['exists'] and not master_mount:
            raise RuntimeError('已有 Review 数据但缺少原主密钥挂载，不能生成新密钥替代')
        if master_mount:
            check = self.backend.exec_run(['python', '-c', CHECK_KEY, master_path, review_path])
            if check.exit_code or len(check.output) != 44: raise RuntimeError('原主密钥不可读取或不能解密现有 Review 凭据，已停止升级')
            original_key = STATE / '原主密钥.key'; original_key.write_bytes(check.output); original_key.chmod(0o600)
        # 备份实际运行的密钥文件，不把明文输出到终端或日志。
        saved_secrets = []
        for service, container in rows.items():
            for key, path in environment(container.attrs).items():
                if key != 'SSH_KEY_PATH' and not key.endswith(('_KEY_FILE', '_TOKEN_FILE')): continue
                content = read_secret_file(container, path)
                if not content:
                    if key == 'SSH_KEY_PATH': continue
                    raise RuntimeError('原服务密钥文件无法读取：' + service + '/' + key)
                saved_secrets.append({'service': service, 'variable': key, 'path': path, 'value': base64.b64encode(content).decode()})
        write(STATE / '原密钥备份.json', saved_secrets)
        self.info = {'project': self.project, 'phase': 'checked', 'started_at': float(os.environ.get('GM_UPGRADE_STARTED_AT', time.time())), 'loaded_images': loaded_images, 'original': {name: c.attrs for name, c in rows.items()},
                     'manager_mount': mount, 'manager_parameters': parameters, 'access_keys': access_keys, 'review_mount': review_mount,
                     'review_parameters': {'database': review_relative or 'review.db'}, 'review_existed': bool(review_status and review_status['database']['exists']),
                     'master_mount': master_mount, 'release_host': self.release_host, 'state_host': self.state_host,
                     'work_host': self.work_host, 'paused': [], 'running': [name for name, c in rows.items() if c.attrs['State']['Running']]}
        self.save()
        needed = 2 * (status['files']['backup_bytes'] + (review_status['files']['backup_bytes'] if review_status else 0))
        needed += sum(self.client.images.get(i).attrs['Size'] for i in {c.attrs['Image'] for c in rows.values()})
        if shutil.disk_usage(STATE).free < needed * 1.2 + 512 * 1024 * 1024:
            raise RuntimeError('升级备份空间不足；原备份文件会保留原位，无需重复复制')
        print('已识别原部署、持久数据与旧镜像；当前没有在途任务')

    def freeze(self):
        self.info['phase'] = 'freezing'; self.save()
        try:
            for name, c in self.containers().items():
                if c.attrs['State']['Running']:
                    # 先记录意图，即使 pause 后控制器退出也能恢复原服务。
                    self.info['paused'].append(c.id); self.save(); c.pause()
            for name in ('manager', 'review'):
                mount = self.info.get(name + '_mount')
                if mount:
                    result = self.volume('inspect', name, mount, self.info[name + '_parameters'])
                    if result['database']['busy']: raise RuntimeError('冻结检查发现新进入的任务，已恢复原服务，请稍后升级')
            self.info['phase'] = 'frozen'; self.save()
        except BaseException:
            self.unpause(); raise

    def unpause(self):
        for ident in self.info.get('paused', []):
            with contextlib.suppress(docker.errors.NotFound):
                c = self.client.containers.get(ident)
                if c.attrs['State']['Paused']: c.unpause()
        self.info['paused'] = []; self.save()

    def backup(self):
        if self.info['phase'] != 'stopped': raise RuntimeError('必须先完全停止原服务再备份')
        if any(c.attrs['State']['Running'] for c in self.containers().values()):
            raise RuntimeError('原服务尚未完全停止，拒绝生成升级快照')
        for name in ('manager', 'review'):
            mount = self.info.get(name + '_mount')
            if mount and self.volume('inspect', name, mount, self.info[name + '_parameters'])['database']['busy']:
                raise RuntimeError('停机后发现新进入的任务，将恢复原服务，待任务完成后再升级')
        for name in ('manager', 'review'):
            mount = self.info.get(name + '_mount')
            if mount: self.volume('backup', name, mount, self.info[name + '_parameters'])
        for index, image in enumerate(sorted({c['Image'] for c in self.info['original'].values()})):
            with (STATE / ('旧镜像-' + str(index) + '.tar')).open('wb') as output:
                for block in self.client.images.get(image).save(): output.write(block)
        self.info['phase'] = 'backed_up'; self.save()
        print('原数据库、运行配置和旧镜像已备份；现有 Gitea 备份文件保留原路径')

    def prepare(self):
        original = json.loads((STATE / '原Compose.json').read_text())
        current = copy.deepcopy(original)
        images = self.info['loaded_images']
        for service, attrs in self.info['original'].items():
            if service not in current['services']: raise RuntimeError('原 Compose 与运行容器不一致')
            spec = current['services'][service]
            spec.pop('build', None); spec.pop('develop', None); spec.pop('secrets', None)
            spec['image'] = attrs['Image']; spec['pull_policy'] = 'never'
            spec['environment'] = environment(attrs)
            spec['volumes'] = [mount_value(m) for m in attrs['Mounts'] if m['Type'] in ('bind', 'volume')]
            spec['container_name'] = attrs['Name'].lstrip('/')
            spec['ports'] = [{'target': int(port.split('/')[0]), 'published': row['HostPort'], 'host_ip': row.get('HostIp') or '0.0.0.0', 'protocol': port.split('/')[1]}
                             for port, bindings in attrs['HostConfig'].get('PortBindings', {}).items() for row in bindings or []]
            for mount in spec['volumes']:
                if mount['type'] == 'volume': current.setdefault('volumes', {})[mount['source']] = {'external': True, 'name': mount['source']}
        # 保存按实际运行参数固定的回退配置，避免旧 .env 的默认值发生漂移。
        write(STATE / '回退Compose.json', escaped(current))
        backend = current['services']['backend']; frontend = current['services']['frontend']
        backend['image'] = images['backend']; frontend['image'] = images['frontend']
        backend['platform'] = frontend['platform'] = 'linux/amd64'
        for spec in (backend, frontend):
            spec.pop('command', None); spec.pop('entrypoint', None)
        # 源码或静态文件挂载会遮挡新镜像；常规离线部署没有这些挂载。
        if any(m['target'] in ('/app', '/usr/share/nginx/html') for spec in (backend, frontend) for m in spec.get('volumes', [])):
            raise RuntimeError('原部署直接挂载应用源码，请先改用已构建镜像，再执行一键升级')
        env = backend['environment']
        # 新功能所需卷必须共享；旧部署尚无这些目录时由 Compose 新建。
        def runtime_mount(service, target, name, read_only=False):
            attrs = self.info['original'].get(service, {})
            old = next((m for m in attrs.get('Mounts', []) if m['Destination'] == target), None)
            mount = mount_value(old) if old else {'type': 'volume', 'source': self.project + '-' + name, 'target': target}
            mount['read_only'] = read_only
            if mount['type'] == 'volume':
                current.setdefault('volumes', {})[mount['source']] = {'name': mount['source'], **({'external': True} if old else {})}
            return mount
        workspace = runtime_mount('review-worker', '/app/review-workspaces', 'review-workspaces')
        native = runtime_mount('review-executor', '/app/review-native', 'review-native')
        for key in ('AI_REVIEW_GITEA_HOSTS', 'AI_REVIEW_MODEL_HOSTS', 'AI_REVIEW_WEBHOOK_HOSTS'):
            hosts = {h.strip().lower() for attrs in self.info['original'].values() for h in environment(attrs).get(key, '').split(',') if h.strip()}
            env[key] = ','.join(sorted(hosts))
        env.setdefault('TRUSTED_PROXY_HOSTS', 'frontend')
        for key in ('SECRET_KEY', 'INIT_PASSWORD'):
            if not env.get(key) or env[key] in ('change-me-in-production', 'admin123') or (key == 'SECRET_KEY' and len(env[key]) < 32):
                env[key] = secrets.token_urlsafe(40)
        secret_dir = Path('/deployment/.gitea-manager-upgrades/secrets'); secret_dir.mkdir(parents=True, exist_ok=True); secret_dir.chmod(0o700)
        def secret(name, content, target, mode=0o600):
            file = secret_dir / name
            if not file.exists(): atomic_text(file, content, mode)
            return {'type': 'bind', 'source': self.work_host + '/.gitea-manager-upgrades/secrets/' + name, 'target': target, 'read_only': True}
        original_key = STATE / '原主密钥.key'
        key = original_key.read_text() if original_key.exists() else base64.urlsafe_b64encode(os.urandom(32)).decode()
        if (secret_dir / 'master.key').exists() and original_key.exists() and (secret_dir / 'master.key').read_text().strip() != key.strip():
            raise RuntimeError('管理目录中的主密钥与原运行密钥不同，未覆盖任一密钥')
        master = secret('master.key', key, '/run/secrets/review_master')
        from cryptography.fernet import Fernet
        try: Fernet((secret_dir / 'master.key').read_bytes().strip())
        except ValueError: raise RuntimeError('部署目录中的主密钥格式无效，请保留原密钥与升级记录') from None
        saved = json.loads((STATE / '原密钥备份.json').read_text())
        tokens = {row['value'] for row in saved if row['variable'] == 'AI_REVIEW_EXECUTOR_TOKEN_FILE'}
        if len(tokens) > 1: raise RuntimeError('原执行器与 Worker 的认证令牌不一致')
        token = base64.b64decode(next(iter(tokens))).decode().strip() if tokens else secrets.token_urlsafe(40)
        executor_path = secret_dir / 'executor.token'
        if executor_path.exists() and tokens and executor_path.read_text().strip() != token:
            raise RuntimeError('管理目录中的执行令牌与原运行令牌不同')
        executor = secret('executor.token', token, '/run/secrets/review_executor', 0o444)
        if len(executor_path.read_text().strip()) < 32: raise RuntimeError('执行令牌格式无效，请保留升级记录')
        authorization_tokens = {row['value'] for row in saved if row['variable'] == 'AI_REVIEW_AUTHORIZATION_TOKEN_FILE'}
        if len(authorization_tokens) > 1:
            raise RuntimeError('原组织授权服务令牌不一致，未覆盖现有令牌')
        authorization_token = (base64.b64decode(next(iter(authorization_tokens))).decode().strip()
                               if authorization_tokens else secrets.token_urlsafe(48))
        authorization_path = secret_dir / 'access-authorization.token'
        if authorization_path.exists() and authorization_tokens and authorization_path.read_text().strip() != authorization_token:
            raise RuntimeError('管理目录中的组织授权令牌与原运行令牌不同')
        authorization = secret('access-authorization.token', authorization_token, '/run/secrets/access_authorization')
        if len(authorization_path.read_text().strip()) < 32:
            raise RuntimeError('组织授权令牌格式无效，请保留升级记录')
        env.update(AI_REVIEW_AUTHORIZATION_URL='http://backend:5000/api/internal/authorization/check',
                   AI_REVIEW_AUTHORIZATION_TOKEN_FILE='/run/secrets/access_authorization')

        review = self.info['review_mount']
        if not review:
            name = self.project + '-review-data'
            try: self.client.volumes.get(name)
            except docker.errors.NotFound: self.client.volumes.create(name, labels={'io.gitea-manager.review-data': self.project})
            review = {'type': 'volume', 'source': name, 'target': '/app/review', 'read_only': False}
            self.info['review_mount'] = review
        review_root = self.info['review_parameters']['database']
        # 原 Review 库在其卷内的相对位置也必须保留。
        review = dict(review, target='/app/review', read_only=False)
        if review['type'] == 'volume': current.setdefault('volumes', {})[review['source']] = {'external': True, 'name': review['source']}
        env.update(AI_REVIEW_DB='/app/review/' + review_root, AI_REVIEW_HOST_POLICY_FILE='/app/review/hosts.json',
                   AI_REVIEW_MASTER_KEY_FILE='/run/secrets/review_master', GM_UPGRADE_MAINTENANCE_FILE='/app/review/.upgrade-maintenance')
        gate_mounts = []
        diagnostic_mounts = []
        if env.get('AI_REVIEW_RUNTIME_STATUS'):
            status_path = env['AI_REVIEW_RUNTIME_STATUS']
            candidates = [m for m in backend['volumes'] if status_path == m['target'] or status_path.startswith(m['target'].rstrip('/') + '/')]
            if not candidates:
                raise RuntimeError('原执行器安装状态缺少持久化挂载，未绕过运行时门禁')
            gate_mounts = [dict(max(candidates, key=lambda m: len(m['target'])), read_only=True)]
            diagnostic_path = env.get('MANAGEMENT_DIAGNOSTICS', '/app/data/diagnostics')
            diagnostic_mounts = [dict(m, read_only=False) for m in backend['volumes'] if m['target'] == diagnostic_path]
            if not diagnostic_mounts:
                # 不为诊断把整个 Manager 数据卷暴露给 Worker，使用已有共享 Review 卷。
                diagnostic_path = '/app/review/runtime-diagnostics'
            env['MANAGEMENT_DIAGNOSTICS'] = diagnostic_path
        common = {k: v for k, v in env.items() if k.startswith('AI_REVIEW_') and (k.endswith('_HOSTS') or k in ('AI_REVIEW_DB', 'AI_REVIEW_HOST_POLICY_FILE', 'AI_REVIEW_MASTER_KEY_FILE', 'AI_REVIEW_RUNTIME_STATUS', 'AI_REVIEW_AUTHORIZATION_URL', 'AI_REVIEW_AUTHORIZATION_TOKEN_FILE'))}
        common.update(GM_UPGRADE_MAINTENANCE_FILE=env['GM_UPGRADE_MAINTENANCE_FILE'], PYTHONDONTWRITEBYTECODE='1')
        backend['volumes'] = [m for m in backend.get('volumes', []) if m['target'] not in (self.info.get('review_mount', {}).get('target'), '/app/review', master['target'], authorization['target'])] + [review, master, authorization]
        # 保留原业务网络并复用实际网络名，避免另起项目导致数据或连接失联。
        default_networks = backend.get('networks') or {'default': None}
        for key in default_networks:
            actual = next((n for n in self.info['original']['backend']['NetworkSettings']['Networks'] if n == current.get('networks', {}).get(key, {}).get('name') or n == self.project + '_' + key), None)
            if actual: current.setdefault('networks', {})[key] = {'external': True, 'name': actual}
        execution_key = 'gm-review-execution'
        execution = current.setdefault('networks', {}).get(execution_key, {})
        if not execution:
            try:
                network = self.client.networks.get(self.project + '_' + execution_key)
                if not network.attrs.get('Internal'): raise RuntimeError('执行器专用网络必须禁止外部出口')
                execution = {'external': True, 'name': network.name}
            except docker.errors.NotFound: pass
        if execution.get('external'):
            network = self.client.networks.get(execution.get('name', execution_key))
            if not network.attrs.get('Internal'): raise RuntimeError('执行器专用网络必须禁止外部出口')
        else:
            execution['internal'] = True
        current['networks'][execution_key] = execution
        for name, module, memory in [('review-worker', 'worker', '1g'), ('review-progress', 'progress', '256m'), ('review-feedback', 'feedback', '256m')]:
            service_env = dict(common); mounts = [review, master, authorization] + gate_mounts
            networks = dict(default_networks) if isinstance(default_networks, dict) else {k: None for k in default_networks}
            if name == 'review-worker':
                mounts += [executor, workspace]; networks[execution_key] = None
                if env.get('AI_REVIEW_RUNTIME_STATUS'):
                    mounts += diagnostic_mounts
                    service_env['MANAGEMENT_DIAGNOSTICS'] = env['MANAGEMENT_DIAGNOSTICS']
                service_env['AI_REVIEW_WORKSPACE_ROOT'] = '/app/review-workspaces'
                service_env.update(AI_REVIEW_EXECUTOR_TOKEN_FILE='/run/secrets/review_executor', AI_REVIEW_EXECUTOR_URL='http://review-executor:5052')
            if name == 'review-feedback':
                mounts += [dict(self.info['manager_mount'], target='/app/data', read_only=True)]
                service_env['AI_REVIEW_MANAGER_DB'] = '/app/data/' + self.info['manager_parameters']['database']
            spec = dict(image=images['backend'], platform='linux/amd64', pull_policy='never', restart='unless-stopped',
                        command=['python', '-m', 'ai_review.' + module], environment=service_env, volumes=mounts,
                        networks=networks, read_only=True, tmpfs=['/tmp:size=128m,mode=1777'], cap_drop=['ALL'],
                        security_opt=['no-new-privileges:true'], mem_limit=memory, cpus=1.0 if module == 'worker' else 0.5)
            if name in self.info['original']: spec['container_name'] = self.info['original'][name]['Name'].lstrip('/')
            current['services'][name] = spec
        spec = dict(image=images['executor'], platform='linux/amd64', pull_policy='never', restart='unless-stopped',
                    environment={'AI_REVIEW_EXECUTOR_TOKEN_FILE': '/run/secrets/review_executor', 'AI_REVIEW_BROKER_URL': 'http://review-worker:5051/v1', 'PYTHONDONTWRITEBYTECODE': '1',
                                 'AI_REVIEW_WORKSPACE_ROOT': '/app/review-workspaces', 'AI_REVIEW_NATIVE_ROOT': '/app/review-native'},
                    volumes=[executor, native, dict(workspace, read_only=True)], networks=[execution_key], dns=['127.0.0.1'], read_only=True,
                    tmpfs=['/tmp:size=384m,mode=1777'], cap_drop=['ALL'], security_opt=['no-new-privileges:true'], pids_limit=128, mem_limit='2g', cpus=2.0)
        if 'review-executor' in self.info['original']: spec['container_name'] = self.info['original']['review-executor']['Name'].lstrip('/')
        current['services']['review-executor'] = spec
        # 展示复用实际 Manager 数据卷，参与升级冻结和恢复。
        spec=approval_view_spec(backend,images['backend'],default_networks)
        if 'approval-view' in self.info['original']:spec['container_name']=self.info['original']['approval-view']['Name'].lstrip('/')
        current['services']['approval-view']=spec
        migration_env = {'DATABASE_URL': env['DATABASE_URL'],
                         'GM_UPGRADE_MAINTENANCE_FILE': env['GM_UPGRADE_MAINTENANCE_FILE'],
                         'PYTHONDONTWRITEBYTECODE': '1'}
        # 转换现场与密钥固定保存在 Manager 数据卷；不向迁移执行器挂载宿主机控制接口。
        migration_mounts = [dict(self.info['manager_mount'], read_only=False), dict(review, read_only=True)]
        spec = dict(image=images['svn'], platform='linux/amd64', pull_policy='never', restart='unless-stopped',
                    command=['python', '-m', 'svn_migration.worker'], environment=migration_env,
                    volumes=migration_mounts, networks=copy.deepcopy(default_networks), read_only=True,
                    tmpfs=['/tmp:size=128m,mode=1777'], cap_drop=['ALL'], security_opt=['no-new-privileges:true'],
                    pids_limit=256, mem_limit='2g', cpus=2.0, stop_grace_period='30s')
        if 'svn-migration-worker' in self.info['original']:
            spec['container_name'] = self.info['original']['svn-migration-worker']['Name'].lstrip('/')
        current['services']['svn-migration-worker'] = spec
        git_spec = copy.deepcopy(spec)
        git_spec.update(image=images['git'], command=['python', '-m', 'git_migration.worker'],
                        mem_limit='512m', cpus=1.0, pids_limit=128, stop_grace_period='45s')
        git_spec.pop('container_name', None)
        if 'git-migration-worker' in self.info['original']:
            git_spec['container_name'] = self.info['original']['git-migration-worker']['Name'].lstrip('/')
        current['services']['git-migration-worker'] = git_spec
        write(STATE / '新Compose.json', escaped(current))
        self.info['phase'] = 'prepared'; self.info['new_images'] = images; self.save()

    def maintenance(self, enabled):
        self.volume('maintenance', 'review', self.info['review_mount'], {'enabled': enabled})

    def stop_old(self):
        if self.info['phase'] != 'prepared': raise RuntimeError('必须先固定升级和回退配置')
        self.info['phase'] = 'stopping'; self.save()
        # 优雅停机可能继续提交已进入的请求；必须等其完成后才能生成数据快照。
        for ident in self.info['paused']:
            c = self.client.containers.get(ident); c.stop(timeout=30)
        self.info['paused'] = []; self.info['phase'] = 'stopped'; self.save()

    def activate(self):
        if self.info['phase'] != 'backed_up': raise RuntimeError('离线快照未完成，不能启动新版本')
        self.info['phase'] = 'replacing'; self.save()
        self.maintenance(True)

    def verify(self):
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            rows = self.containers()
            if all(name in rows and rows[name].attrs['State']['Running'] for name in SERVICES):
                result = rows['backend'].exec_run(['python', '-c', "import requests,re; r=requests.get('http://127.0.0.1:5000/api/session',timeout=3); assert r.status_code==200; r=requests.get('http://frontend/api/session',timeout=3); assert r.status_code==200 and 'authenticated' in r.json(); page=requests.get('http://frontend/',timeout=3); assert page.status_code==200; asset=re.search(r'src=\"(/assets/[^\"]+\\.js)\"',page.text); assert asset and requests.get('http://frontend'+asset[1],timeout=3).status_code==200"])
                health = rows['review-worker'].exec_run(['python', '-c', "import requests,time; from ai_review.store import Store; h=Store().health_status(); assert all(any(x['name']==n and time.time()-x['updated']<30 for x in h) for n in ('worker','progress','feedback')); r=requests.get('http://review-executor:5052/health',timeout=3); assert r.status_code==200 and r.json().get('ready')"])
                view_health=rows['approval-view'].exec_run(['python','-c',"import requests; r=requests.get('http://127.0.0.1:5000/health',timeout=3); assert r.status_code==200 and r.json().get('role')=='approval-view'; blocked=requests.get('http://127.0.0.1:5000/api/session',timeout=3); assert blocked.status_code==503 and blocked.json().get('error')=='正在升级并核对数据，请稍后访问'; from approval.view_app import create_app; assert '/api/session' not in {r.rule for r in create_app().url_map.iter_rules()}"])
                gate_health = rows['backend'].exec_run(['python', '-c', "from ai_review.runtime_status import available,status; assert available(), status().get('reason')"])
                svn_health = rows['svn-migration-worker'].exec_run(['python', '-m', 'svn_migration.worker', '--check'])
                git_health = rows['git-migration-worker'].exec_run(['python', '-m', 'git_migration.worker', '--check'])
                if git_health.exit_code == 0 and result.exit_code == 0 and health.exit_code == 0 and view_health.exit_code == 0 and gate_health.exit_code == 0 and svn_health.exit_code == 0: break
            time.sleep(2)
        else: raise RuntimeError('新服务未通过健康检查，将恢复旧版本')
        new_env = environment(rows['backend'].attrs)
        access_key_probe = rows['backend'].exec_run(['python', '-c', CHECK_ACCESS_KEYS,
            new_env['DATABASE_URL'].removeprefix('sqlite:///'), new_env.get('ACCESS_KEY_FILE', '')])
        if access_key_probe.exit_code:
            raise RuntimeError('升级后目录密钥解密检查失败，将恢复旧版本')
        actual_keys = json.loads(access_key_probe.output)
        if any(actual_keys.get(path) != value for path, value in self.info.get('access_keys', {}).items()):
            raise RuntimeError('升级后目录或会话持久密钥发生变化，将恢复旧版本')
        paused = []
        try:
            for name in WRITERS:
                c = rows[name]; c.pause(); paused.append(c)
            for name in ('manager', 'review'):
                if (STATE / (name + '.json')).exists():
                    self.volume('verify', name, self.info[name + '_mount'], self.info[name + '_parameters'])
        finally:
            for c in paused:
                with contextlib.suppress(Exception): c.unpause()
        self.info['phase'] = 'verified'; self.save()
        print('全部服务、独立审批展示、模型执行器和历史数据核对通过')

    def commit(self):
        if self.info['phase'] != 'verified': raise RuntimeError('校验未完成，不能解除维护状态')
        managed = Path('/deployment/compose.managed.json')
        atomic_text(managed, (STATE / '新Compose.json').read_text())
        self.info['phase'] = 'committing'; self.save()
        self.maintenance(False)
        self.info['phase'] = 'complete'; self.save()
        print('升级成功：原数据和配置已保留，首次 AI 接入请打开管理页面；用时 ' + str(round(time.time() - self.info['started_at'])) + ' 秒')

    def rollback(self):
        if not self.info: return
        phase = self.info.get('phase')
        if phase in ('checked', 'freezing', 'frozen', 'prepared', 'stopping', 'stopped', 'backed_up'):
            # 新版本尚未运行，原库没有被替换；备份中途失败也不得恢复半份快照。
            self.unpause(); return
        if phase in ('complete', 'committing'):
            raise RuntimeError('维护窗口已结束；为保护升级后的新数据，不自动覆盖数据库。请先检查在途任务并使用回退检查流程')
        if phase in ('replacing', 'verified'):
            for name, c in self.containers().items():
                if c.attrs['State']['Running']: c.stop(timeout=30)
                if name not in self.info['original']: c.remove()
            for name in ('manager', 'review'):
                if (STATE / (name + '.tar')).exists():
                    self.volume('restore', name, self.info[name + '_mount'], self.info[name + '_parameters'])
                    self.volume('verify', name, self.info[name + '_mount'], self.info[name + '_parameters'])
            if not (STATE / 'review.tar').exists(): self.maintenance(False)
            self.info['phase'] = 'restored'; self.save()
        print('回退数据已恢复，正在恢复原容器配置')

    def rollback_health(self):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            rows = self.containers()
            if all(name in rows and rows[name].attrs['State']['Running'] and not rows[name].attrs['State'].get('Paused') for name in self.info['running']):
                result = rows['backend'].exec_run(['python', '-c', "import requests; assert requests.get('http://127.0.0.1:5000/api/session',timeout=3).status_code==200"])
                git_health = rows['git-migration-worker'].exec_run(['python', '-m', 'git_migration.worker', '--check'])
                if git_health.exit_code == 0 and result.exit_code == 0:
                    print('原服务恢复并通过健康检查'); return
            time.sleep(2)
        raise RuntimeError('原服务启动后未通过健康检查，请保留升级记录')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['extract', 'preflight', 'freeze', 'backup', 'prepare', 'stop', 'activate', 'verify', 'commit', 'rollback', 'rollback_health'])
    args = parser.parse_args()
    if args.action == 'extract': extract(); return
    tool = Upgrade()
    try:
        function = {'stop': tool.stop_old}.get(args.action) or getattr(tool, args.action)
        function()
    finally: tool.client.close()


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        if isinstance(exc, RuntimeError): print('升级未完成：' + str(exc), file=sys.stderr)
        else: print('升级操作未完成，请检查 Docker、持久目录和私有升级记录（' + type(exc).__name__ + '）', file=sys.stderr)
        sys.exit(1)
