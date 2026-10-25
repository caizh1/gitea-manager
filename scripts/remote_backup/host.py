"""宿主编排只操作本轮标签资源；不用宿主 Docker 承载被测 Gitea。"""
import argparse
import datetime
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import tarfile
import time

from .catalog import CASES, verdict

ROOT = Path(__file__).resolve().parents[2]
LABEL = 'gitea.manager.remote-lab'


def run(args, *, cwd=None, capture=False, timeout=None, stdin=None):
    environment = os.environ.copy()
    docker_bin = Path('/Applications/Docker.app/Contents/Resources/bin')
    if docker_bin.is_dir():
        environment['PATH'] = str(docker_bin) + os.pathsep + environment.get('PATH', '')
    return subprocess.run(args, cwd=cwd, input=stdin, text=True, check=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None, timeout=timeout, env=environment)


def output(args, **kwargs):
    return run(args, capture=True, **kwargs).stdout.strip()


def write_json(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.chmod(mode)
    temp.replace(path)


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def select_networks(existing):
    occupied = [ipaddress.ip_network(item, strict=False) for item in existing if ':' not in item]
    pools = [['10.%d.%d.0/24' % (second, third) for third in (10, 20, 30)] for second in range(231, 250)]
    pools += [['172.%d.%d.0/24' % (second, third) for third in (10, 20, 30)] for second in range(16, 32)]
    pools += [['192.168.%d.0/24' % (base + offset) for offset in range(3)] for base in range(200, 251, 3)]
    for group in pools:
        candidate = [ipaddress.ip_network(value) for value in group]
        if not any(a.overlaps(b) for a in candidate for b in occupied):
            return [str(item) for item in candidate]
    raise RuntimeError('没有找到与现有路由隔离的三段测试网段')


def route_network(value):
    """兼容 macOS 路由表里的 10/8、172.16/12 等省略零写法。"""
    if not value or value in ('default', 'Destination') or ':' in value:
        return None
    address, slash, prefix = value.partition('/')
    parts = address.split('.')
    if not all(part.isdigit() and 0 <= int(part) <= 255 for part in parts) or len(parts) > 4:
        return None
    if not slash:
        prefix = str(8 * len(parts))
    address = '.'.join(parts + ['0'] * (4 - len(parts)))
    try:
        return str(ipaddress.ip_network(address + '/' + prefix, strict=False))
    except ValueError:
        return None


def preflight(root):
    info = json.loads(output(['docker', 'info', '--format', '{{json .}}']))
    ids = output(['docker', 'network', 'ls', '-q']).splitlines()
    networks = json.loads(output(['docker', 'network', 'inspect', *ids])) if ids else []
    occupied = [item['Subnet'] for network in networks for item in (network.get('IPAM', {}).get('Config') or []) if item.get('Subnet')]
    routes = output(['netstat', '-rn', '-f', 'inet'])
    for line in routes.splitlines():
        value = line.split()[0] if line.split() else ''
        normalized = route_network(value)
        if normalized:
            occupied.append(normalized)
    ram = info.get('MemTotal', 0)
    free = shutil.disk_usage(ROOT).free
    status = '通过' if ram >= 6 * 1024**3 and free >= 30 * 1024**3 else '环境阻塞'
    report = {'说明': '只检查资源，不停止既有容器；16GiB为建议值，实际峰值持续记录',
              '状态': status, 'Docker内存字节': ram, '宿主可用空间字节': free,
              '建议内存字节': 16 * 1024**3, '最低部署内存字节': 6 * 1024**3,
              '网段': select_networks(occupied), '宿主路由': routes,
              '既有容器': [json.loads(line) for line in output(['docker', 'ps', '-a', '--format', '{{json .}}']).splitlines()]}
    write_json(root / 'preflight.json', report)
    print(json.dumps({k: v for k, v in report.items() if k not in ('既有容器', '宿主路由')}, ensure_ascii=False), flush=True)
    return report


def image_id(name):
    return json.loads(output(['docker', 'image', 'inspect', name]))[0]['Id']


def freeze(root):
    destination = root / 'source'
    if (root / 'source-manifest.json').exists():
        return
    records = []
    for folder in ('backend', 'frontend'):
        for path in sorted((ROOT / folder).rglob('*')):
            rel = path.relative_to(ROOT)
            if any(part in {'node_modules', '__pycache__', '.git', 'data', '.cache'} for part in rel.parts):
                continue
            if not path.is_file() or path.is_symlink() or path.suffix in {'.pyc', '.db', '.log', '.key'} or path.name == '.env':
                continue
            target = destination / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            records.append({'文件': str(rel), 'SHA256': hashlib.sha256(path.read_bytes()).hexdigest(), '字节': path.stat().st_size})
    write_json(root / 'source-manifest.json', {'说明': '实施前冻结的工作区构建基线，包含未提交业务代码',
               '提交': output(['git', 'rev-parse', 'HEAD'], cwd=ROOT), '文件': records})


def prepare_seeds(root, openssd):
    destination = root / 'seeds'
    destination.mkdir(exist_ok=True)
    rows = []
    for name, source in [('qemu', 'https://gitlab.com/qemu-project/qemu.git'), ('openssd', str(openssd))]:
        path = destination / (name + '.git')
        if not path.exists():
            run(['git', 'clone', '--mirror', '--no-local', source, str(path)])
        if output(['git', '--git-dir', str(path), 'rev-parse', '--is-shallow-repository']) != 'false':
            raise RuntimeError('真实样本不得使用浅克隆：' + name)
        run(['git', '--git-dir', str(path), 'fsck', '--full'], capture=True)
        refs = output(['git', '--git-dir', str(path), 'show-ref'])
        rows.append({'仓库': name, '来源': source, 'HEAD': output(['git', '--git-dir', str(path), 'rev-parse', 'HEAD']),
                     '引用数量': len(refs.splitlines()), '全部引用SHA256': hashlib.sha256(refs.encode()).hexdigest(),
                     '引用': refs.splitlines(), '历史完整性': 'fsck通过且非浅克隆'})
    write_json(root / 'seed-manifest.json', {'说明': '首次完整克隆固定，后续不自动更新上游', '仓库': rows})


def compose(root, *arguments, capture=False):
    return run(['docker', 'compose', '-f', str(root / 'compose.json'), *arguments], capture=capture)


def service_id(root, name):
    return compose(root, 'ps', '-q', name, capture=True).stdout.strip()


def assert_owned(root, identifier):
    attrs = json.loads(output(['docker', 'inspect', identifier]))[0]
    if attrs['Config']['Labels'].get(LABEL) != root.name:
        raise RuntimeError('拒绝操作不属于本轮的容器')
    return attrs


def exec_service(root, name, command, capture=False, stdin=None):
    identifier = service_id(root, name)
    assert_owned(root, identifier)
    return run(['docker', 'exec', '-i', identifier, *command], capture=capture, stdin=stdin)


def generate(root, pre):
    images = {'gitea': image_id('gitea-enhanced:enhanced-20261020-r9'),
              'postgres': image_id('postgres:16.8-bookworm')}
    secret_file = root / 'private.json'
    if secret_file.exists():
        return json.loads(secret_file.read_text())
    nets = pre['网段']
    addresses = {name: str(ipaddress.ip_network(net).network_address + 10)
                 for name, net in zip(('manager', 'a', 'b'), nets)}
    state = {'run_id': root.name, 'images': images, 'addresses': addresses, 'networks': nets,
             'http_port': free_port(), 'manager_password': secrets.token_urlsafe(24),
             'manager_secret': secrets.token_hex(32), 'nodes': {}}
    for name in ('a', 'b'):
        state['nodes'][name] = {'password': secrets.token_urlsafe(24), 'db_password': secrets.token_urlsafe(24),
                               'secret': secrets.token_hex(32), 'admin': 'labadmin', 'token': ''}
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run(['ssh-keygen', '-q', '-t', 'rsa', '-b', '3072', '-N', '', '-f', str(root / 'id_rsa')])
    (root / 'authorized_keys').write_text((root / 'id_rsa.pub').read_text())
    (root / 'authorized_keys').chmod(0o644)
    (root / 'known_hosts').touch(mode=0o600)
    write_json(secret_file, state)
    return state


def build(root):
    freeze(root)
    suffix = 'remote-' + root.name
    manager = 'gitea-review-backend:' + suffix
    run(['docker', 'build', '--platform', 'linux/amd64', '--build-arg', 'BASE_IMAGE=python:3.11-slim',
         '-t', manager, str(root / 'source' / 'backend')])
    front = root / 'source' / 'frontend'
    if not (front / 'node_modules').exists():
        (front / 'node_modules').symlink_to(ROOT / 'frontend' / 'node_modules', target_is_directory=True)
    run(['npm', 'run', 'build'], cwd=front)
    frontend = 'gitea-review-frontend:' + suffix
    run(['docker', 'build', '--platform', 'linux/amd64', '--build-arg', 'BASE_IMAGE=nginx:1.31.1', '-t', frontend, str(front)])
    host_image = 'gitea-remote-host:' + root.name
    run(['docker', 'build', '-f', str(ROOT / 'deployment' / 'remote-backup' / 'host.Dockerfile'),
         '-t', host_image, str(ROOT / 'deployment' / 'remote-backup')])
    driver_image = 'gitea-remote-driver:' + root.name
    # 驱动镜像仅包含测试工具；被测镜像来自冻结源码且不挂载工作区。
    context = root / 'driver-build'
    shutil.copytree(ROOT / 'scripts' / 'remote_backup', context / 'remote_backup', dirs_exist_ok=True)
    run(['docker', 'build', '--platform', 'linux/amd64', '--build-arg', 'MANAGER_IMAGE=' + manager,
         '-f', str(ROOT / 'deployment' / 'remote-backup' / 'driver.Dockerfile'), '-t', driver_image, str(context)])
    write_json(root / 'images.json', {key: {'名称': name, '摘要': image_id(name)} for key, name in
               [('manager', manager), ('frontend', frontend), ('host', host_image), ('driver', driver_image)]})


def topology(root, state):
    built = json.loads((root / 'images.json').read_text())
    labels = {LABEL: root.name}
    net_names = ('management', 'source', 'target')
    nets = state['networks']
    routes = {name: str(ipaddress.ip_network(net).network_address + 254) for name, net in zip(net_names, nets)}
    def base(image):
        return {'image': image, 'pull_policy': 'never', 'labels': labels, 'restart': 'no'}
    router = dict(base(built['host']['摘要']), entrypoint=['sleep', 'infinity'], cap_add=['NET_ADMIN'],
                  sysctls={'net.ipv4.ip_forward': '1'}, networks={name: {'ipv4_address': routes[name]} for name in net_names})
    services = {'router': router}
    volumes = {}
    for node, network in [('a', 'source'), ('b', 'target')]:
        volumes['host-' + node + '-engine'] = {'labels': labels}
        volumes['host-' + node + '-files'] = {'labels': labels}
        services['host-' + node] = dict(base(built['host']['摘要']), privileged=True,
            environment={'DOCKER_TLS_CERTDIR': ''}, networks={network: {'ipv4_address': state['addresses'][node]}},
            volumes=['host-' + node + '-engine:/var/lib/docker', 'host-' + node + '-files:/lab',
                     str(root / 'authorized_keys') + ':/keys/authorized_keys:ro'], mem_limit='3200m')
    volumes['manager-data'] = {'labels': labels}
    services['backend'] = dict(base(built['manager']['摘要']), platform='linux/amd64',
        environment={'DATABASE_URL': 'sqlite:////app/data/gitea-manager.db', 'BACKUP_DIR': '/app/data/backups',
            'INIT_PASSWORD': state['manager_password'], 'SECRET_KEY': state['manager_secret'],
            'SSH_KEY_PATH': '/keys/id_rsa', 'SSH_KNOWN_HOSTS': '/keys/known_hosts',
            'MANAGEMENT_DIAGNOSTICS_ROOT': '/app/data/diagnostics'},
        networks={'management': {'ipv4_address': state['addresses']['manager']}},
        volumes=['manager-data:/app/data', str(root / 'id_rsa') + ':/keys/id_rsa:ro',
                 str(root / 'known_hosts') + ':/keys/known_hosts:ro'], mem_limit='640m')
    services['frontend'] = dict(base(built['frontend']['摘要']), platform='linux/amd64', networks=['management'],
                               ports=['127.0.0.1:%d:80' % state['http_port']], mem_limit='128m')
    services['driver'] = dict(base(built['driver']['摘要']), platform='linux/amd64',
        entrypoint=['sleep', 'infinity'], networks=['management'], volumes=[str(root) + ':/evidence'],
        environment={'PYTHONPATH': '/checks'}, mem_limit='1200m')
    volumes['cold-manager-data'] = {'labels': labels}
    cold = json.loads(json.dumps(services['backend']))
    cold['profiles'] = ['cold']
    cold['networks'] = ['management']
    cold['volumes'][0] = 'cold-manager-data:/app/data'
    cold['environment']['SECRET_KEY'] = state['manager_secret'] + '-cold'
    services['cold-backend'] = cold
    config = {'name': 'gm-remote-' + root.name, 'services': services,
              'networks': {name: {'labels': labels, 'ipam': {'config': [{'subnet': net}]}}
                           for name, net in zip(net_names, nets)}, 'volumes': volumes}
    write_json(root / 'compose.json', config)
    return routes


def build_runner(root):
    context = root / 'runner-build'
    context.mkdir(exist_ok=True)
    action = context / 'upload-artifact'
    if not action.exists():
        run(['git', 'clone', '--depth', '1', '--branch', 'v3', 'https://github.com/actions/upload-artifact.git', str(action)])
    image = 'gitea-remote-runner:' + root.name
    run(['docker', 'build', '--platform', 'linux/amd64', '-f', str(ROOT / 'deployment' / 'remote-backup' / 'runner.Dockerfile'),
         '-t', image, str(context)])
    write_json(root / 'runner-image.json', {'镜像': image, '摘要': image_id(image),
               '说明': '固定官方 Runner 与本地预置 upload-artifact v3，执行阶段不访问公网',
               '动作提交': output(['git', '-C', str(action), 'rev-parse', 'HEAD'])})
    archive = root / 'runner-image.tar'
    run(['docker', 'image', 'save', '--platform', 'linux/amd64', '-o', str(archive), image])


def refresh_host(root):
    guard = (root / 'test-run.lock').open('a')
    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    built = json.loads((root / 'images.json').read_text())
    image = built['host']['名称']
    run(['docker', 'build', '-f', str(ROOT / 'deployment' / 'remote-backup' / 'host.Dockerfile'),
         '-t', image, str(ROOT / 'deployment' / 'remote-backup')])
    built['host']['摘要'] = image_id(image)
    write_json(root / 'images.json', built)
    config = json.loads((root / 'compose.json').read_text())
    for service in ('host-a', 'host-b'):
        config['services'][service]['image'] = built['host']['摘要']
    write_json(root / 'compose.json', config)
    compose(root, 'up', '-d', '--no-deps', 'host-a', 'host-b')
    state = json.loads((root / 'private.json').read_text())
    routes = {name: str(ipaddress.ip_network(net).network_address + 254)
              for name, net in zip(('management', 'source', 'target'), state['networks'])}
    configure_routes(root, state, routes)
    guard.close()


def freeze_driver_source(root, context, digest):
    if not (context / 'remote_backup').is_dir():
        return
    version = digest.split(':')[-1]
    destination = root / 'test-versions' / version
    if not destination.exists():
        shutil.copytree(context / 'remote_backup', destination, ignore=shutil.ignore_patterns('__pycache__'))
    records = [{'文件': str(path.relative_to(destination)), 'SHA256': hashlib.sha256(path.read_bytes()).hexdigest()}
               for path in sorted(destination.glob('*.py'))]
    write_json(root / 'test-manifests' / (version + '.json'),
               {'说明': '该测试驱动镜像对应的独立保存源码摘要', '镜像': digest, '文件': records})


def refresh_driver(root):
    guard = (root / 'test-run.lock').open('a')
    try:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('已有验收正在运行，拒绝替换其驱动容器') from None
    built = json.loads((root / 'images.json').read_text())
    context = root / 'driver-build'
    freeze_driver_source(root, context, built['driver']['摘要'])
    for cache in (context / 'remote_backup').rglob('__pycache__'):
        shutil.rmtree(cache)
    shutil.copytree(ROOT / 'scripts' / 'remote_backup', context / 'remote_backup', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
    image = built['driver']['名称']
    run(['docker', 'build', '--platform', 'linux/amd64', '--build-arg', 'MANAGER_IMAGE=' + built['manager']['名称'],
         '-f', str(ROOT / 'deployment' / 'remote-backup' / 'driver.Dockerfile'), '-t', image, str(context)])
    deadline = time.monotonic() + 5
    while True:
        try:
            built['driver']['摘要'] = image_id(image)
            break
        except subprocess.CalledProcessError:
            # Docker Desktop 刚提交的标签短暂不可读；只重试查询，不重复构建或替换实例。
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)
    write_json(root / 'images.json', built)
    freeze_driver_source(root, context, built['driver']['摘要'])
    if (root / 'compose.json').exists():
        config = json.loads((root / 'compose.json').read_text())
        for network in config['networks'].values():
            network.pop('internal', None)
        config['services']['driver']['image'] = built['driver']['摘要']
        config['services']['driver']['environment']['REMOTE_DRIVER_VERSION'] = built['driver']['摘要']
        if 'cold-backend' not in config['services']:
            cold = json.loads(json.dumps(config['services']['backend']))
            cold.update(profiles=['cold'], networks=['management'])
            cold['volumes'][0] = 'cold-manager-data:/app/data'
            cold['environment']['SECRET_KEY'] += '-cold'
            config['services']['cold-backend'] = cold
            config['volumes']['cold-manager-data'] = {'labels': {LABEL: root.name}}
        write_json(root / 'compose.json', config)
        compose(root, 'up', '-d', '--no-deps', 'driver')
        state = json.loads((root / 'private.json').read_text())
        identifier = service_id(root, 'driver')
        assert_owned(root, identifier)
        gateway = str(ipaddress.ip_network(state['networks'][0]).network_address + 254)
        run(['docker', 'run', '--rm', '--network', 'container:' + identifier, '--cap-add', 'NET_ADMIN',
             '--label', LABEL + '=' + root.name, '--entrypoint', 'sh', built['host']['摘要'],
             '-ec', 'ip route del default 2>/dev/null || true'])
        for subnet in state['networks'][1:]:
            run(['docker', 'run', '--rm', '--network', 'container:' + identifier, '--cap-add', 'NET_ADMIN',
                 '--label', LABEL + '=' + root.name, '--entrypoint', 'ip', built['host']['摘要'],
                 'route', 'replace', subnet, 'via', gateway])
    guard.close()


def load_runner(root):
    image = json.loads((root / 'runner-image.json').read_text())
    for node in ('a', 'b'):
        identifier = service_id(root, 'host-' + node)
        assert_owned(root, identifier)
        run(['docker', 'cp', str(root / 'runner-image.tar'), identifier + ':/lab/runner-image.tar'])
        exec_service(root, 'host-' + node, ['docker', 'load', '-i', '/lab/runner-image.tar'])
        exec_service(root, 'host-' + node, ['rm', '/lab/runner-image.tar'])
    state = json.loads((root / 'private.json').read_text())
    state['runner_image'] = image['镜像']
    write_json(root / 'private.json', state)


def load_pg_variant(root):
    """只读取本机已有 PG17 镜像，加载到专属 B 引擎，供真实主版本准入验证。"""
    guard = (root / 'test-run.lock').open('a')
    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if shutil.disk_usage(root).free < 6 * 1024**3:
        raise RuntimeError('现有镜像导出空间不足，未操作其他任务或拉取外网镜像')
    tag = 'postgres:17.11-bookworm'
    original = json.loads(output(['docker', 'image', 'inspect', '--platform', 'linux/amd64', tag]))[0]
    archive = root / ('postgres-variant-' + original['Id'].split(':')[-1][:16] + '.tar')
    if not archive.exists():
        run(['docker', 'image', 'save', '--platform', 'linux/amd64', '-o', str(archive), tag])
    with tarfile.open(archive) as stream:
        manifest = json.load(stream.extractfile('manifest.json'))
        candidates = [stream.extractfile(entry['Config']).read() for entry in manifest]
    matches = [raw for raw in candidates if json.loads(raw).get('rootfs', {}).get('diff_ids') == original['RootFS']['Layers']
               and json.loads(raw).get('architecture') == 'amd64']
    if len(matches) != 1:
        raise RuntimeError('PG17 导出配置不能与固定本机镜像 RootFS 对应，拒绝继续')
    target = service_id(root, 'host-b')
    assert_owned(root, target)
    run(['docker', 'cp', str(archive), target + ':/lab/postgres-variant.tar'])
    exec_service(root, 'host-b', ['docker', 'load', '-i', '/lab/postgres-variant.tar'])
    exec_service(root, 'host-b', ['rm', '/lab/postgres-variant.tar'])
    loaded = 'sha256:' + hashlib.sha256(matches[0]).hexdigest()
    state = json.loads((root / 'private.json').read_text())
    state['pg_variant_image'] = loaded
    write_json(root / 'private.json', state)
    write_json(root / 'postgres-variant-identity.json', {'说明': '本机已有镜像，经导出配置和 RootFS 核对；仅用于独立 B 空白目标',
        '原镜像': original['Id'], '导出配置镜像': loaded, '固定标签': tag})


def build_integrations(root):
    guard = (root / 'test-run.lock').open('a')
    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    state = json.loads((root / 'private.json').read_text())
    built = json.loads((root / 'images.json').read_text())
    context = root / 'integration-build'
    context.mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'scripts/remote_backup/integration_server.py', context)
    image = 'gitea-remote-integrations:' + root.name
    run(['docker', 'build', '--platform', 'linux/amd64', '--build-arg', 'MANAGER_IMAGE=' + built['manager']['名称'],
         '-f', str(ROOT / 'deployment/remote-backup/integrations.Dockerfile'), '-t', image, str(context)])
    digest = image_id(image)
    built['integrations'] = {'名称': image, '摘要': digest}
    write_json(root / 'images.json', built)
    address = str(ipaddress.ip_network(state['networks'][0]).network_address + 20)
    state['addresses']['integration'] = address
    if 'integrations' not in state:
        state['integrations'] = {key: secrets.token_urlsafe(24) for key in ('ldap_password', 'oauth_secret', 'webhook_secret')}
    state['integrations']['callbacks'] = ['http://' + state['addresses'][node] + ':3000/user/oauth2/lab-oauth/callback' for node in ('a', 'b')]
    write_json(root / 'private.json', state)
    write_json(root / 'integrations' / 'integration-private.json', state['integrations'])
    config = json.loads((root / 'compose.json').read_text())
    config['services']['integrations'] = {'image': digest, 'platform': 'linux/amd64',
        'labels': {LABEL: root.name}, 'volumes': [str(root / 'integrations') + ':/state'],
        'networks': {'management': {'ipv4_address': address}}, 'mem_limit': '256m',
        'restart': 'unless-stopped'}
    write_json(root / 'compose.json', config)
    compose(root, 'up', '-d', '--no-deps', 'integrations')
    identifier = service_id(root, 'integrations')
    assert_owned(root, identifier)
    gateway = str(ipaddress.ip_network(state['networks'][0]).network_address + 254)
    commands = ['ip route del default 2>/dev/null || true']
    commands.extend('ip route replace %s via %s' % (subnet, gateway) for subnet in state['networks'][1:])
    run(['docker', 'run', '--rm', '--network', 'container:' + identifier, '--cap-add', 'NET_ADMIN',
         '--label', LABEL + '=' + root.name, '--entrypoint', 'sh', built['host']['摘要'], '-ec', '; '.join(commands)])
    for node in ('a', 'b'):
        exec_service(root, 'router', ['iptables', '-I', 'FORWARD', '1', '-s', state['addresses'][node],
             '-d', address, '-p', 'tcp', '-m', 'multiport', '--dports', '1389,1025,8081', '-j', 'ACCEPT'])
    guard.close()


def configure_routes(root, state, routes):
    tool = json.loads((root / 'images.json').read_text())['host']['摘要']
    for service in ('backend', 'driver', 'host-a', 'host-b'):
        identifier = service_id(root, service)
        assert_owned(root, identifier)
        index = 1 if service == 'host-a' else 2 if service == 'host-b' else 0
        gateway = list(routes.values())[index]
        commands = ['ip route del default 2>/dev/null || true']
        for n, net in enumerate(state['networks']):
            if n != index:
                commands.append('ip route replace %s via %s' % (net, gateway))
        run(['docker', 'run', '--rm', '--network', 'container:' + identifier, '--cap-add', 'NET_ADMIN',
             '--label', LABEL + '=' + root.name, '--entrypoint', 'sh', tool, '-ec', '; '.join(commands)])
    exec_service(root, 'router', ['sh', '-ec', 'ip route del default 2>/dev/null || true'])
    a, b = state['addresses']['a'], state['addresses']['b']
    mgmt = state['networks'][0]
    firewall = ['iptables -P FORWARD DROP', 'iptables -F FORWARD', 'iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT']
    for address in (a, b):
        firewall.append('iptables -A FORWARD -s %s -d %s -p tcp -m multiport --dports 22,2222,3000 -j ACCEPT' % (mgmt, address))
        firewall.append('iptables -A FORWARD -s %s -d %s -p icmp -j ACCEPT' % (mgmt, address))
        if state['addresses'].get('integration'):
            firewall.append('iptables -A FORWARD -s %s -d %s -p tcp -m multiport --dports 1389,1025,8081 -j ACCEPT' %
                            (address, state['addresses']['integration']))
    exec_service(root, 'router', ['sh', '-ec', '; '.join(firewall)])
    for node in ('a', 'b'):
        deadline = time.monotonic() + 90
        while True:
            try:
                exec_service(root, 'host-' + node, ['docker', 'info'], capture=True)
                break
            except subprocess.CalledProcessError:
                if time.monotonic() > deadline:
                    raise RuntimeError('独立 Docker 引擎启动超时')
                time.sleep(1)
    known = []
    for node in ('a', 'b'):
        pub = exec_service(root, 'host-' + node, ['cat', '/etc/ssh/ssh_host_ed25519_key.pub'], capture=True).stdout.strip()
        known.append(state['addresses'][node] + ' ' + ' '.join(pub.split()[:2]))
    (root / 'known_hosts').write_text('\n'.join(known) + '\n')


def copy_images(root, state):
    archive = root / 'node-images.tar'
    if not archive.exists():
        original = state.get('outer_images', state['images'])
        run(['docker', 'image', 'save', '--platform', 'linux/amd64', '-o', str(archive), *original.values()])
    for node in ('a', 'b'):
        identifier = service_id(root, 'host-' + node)
        run(['docker', 'cp', str(archive), identifier + ':/lab/node-images.tar'])
        exec_service(root, 'host-' + node, ['docker', 'load', '-i', '/lab/node-images.tar'])
        exec_service(root, 'host-' + node, ['rm', '/lab/node-images.tar'])
    # Docker Desktop 的 OCI 索引 ID 与经典引擎加载后的配置 ID 不同。
    # 以实际导出配置及 RootFS 匹配，避免把索引 ID 当作远端存在的镜像 ID。
    loaded = {}
    with tarfile.open(archive) as stream:
        records = json.load(stream.extractfile('manifest.json'))
        configs = [stream.extractfile(item['Config']).read() for item in records]
        fixed = state.get('outer_images', state['images'])
        for name, identifier in fixed.items():
            original = json.loads(output(['docker', 'image', 'inspect', '--platform', 'linux/amd64', identifier]))[0]
            matches = [raw for raw in configs if json.loads(raw).get('rootfs', {}).get('diff_ids') == original['RootFS']['Layers']]
            if len(matches) != 1 or json.loads(matches[0]).get('architecture') != 'amd64':
                raise RuntimeError('导出镜像配置无法与固定源镜像一一对应')
            loaded[name] = 'sha256:' + hashlib.sha256(matches[0]).hexdigest()
    state['outer_images'] = dict(fixed)
    state['images'] = loaded
    write_json(root / 'private.json', state)
    write_json(root / 'node-image-identity.json', {'说明': 'OCI 索引和经典 Docker 配置 ID 映射，RootFS 一致',
               '源索引': state['outer_images'], '加载配置': loaded})


def driver(root, phase, extra=()):
    try:
        result = exec_service(root, 'driver', ['python', '-m', 'remote_backup.driver', phase, *extra], capture=True)
    except subprocess.CalledProcessError as exc:
        raw = (exc.stdout or '') + '\n' + (exc.stderr or '')
        private = json.loads((root / 'private.json').read_text())
        def redact(value, key=''):
            nonlocal raw
            if isinstance(value, dict):
                for name, entry in value.items():
                    redact(entry, name if key not in ('passwords', 'tokens') else key)
            elif isinstance(value, str) and value and any(item in key for item in ('password', 'secret', 'token', 'totp')):
                raw = raw.replace(value, '[已脱敏]')
        redact(private)
        path = root / ('driver-error-' + str(time.time_ns()) + '.log')
        path.write_text('驱动原始错误（已脱敏）：\n' + raw)
        print(raw[-6000:], flush=True)
        raise RuntimeError('验收阶段失败，证据：' + str(path)) from None
    else:
        print(result.stdout, end='', flush=True)


def topology_proof(root, state):
    driver(root, 'probe')
    try:
        exec_service(root, 'router', ['iptables', '-I', 'FORWARD', '1', '-j', 'DROP'])
        driver(root, 'probe', ('--unreachable',))
    finally:
        exec_service(root, 'router', ['iptables', '-D', 'FORWARD', '-j', 'DROP'])
    driver(root, 'probe')
    engines = {node: exec_service(root, 'host-' + node, ['docker', 'info', '--format', '{{.ID}}'], capture=True).stdout.strip()
               for node in ('a', 'b')}
    if len(set(engines.values())) != 2:
        raise RuntimeError('两个模拟主机错误地共用 Docker 引擎')
    evidence = {'类别': 'topology', '用例': '独立引擎和强制跨路由器通信', '状态': '通过',
                '说明': '关闭路由时两台主机均不可达，恢复后可达；Docker引擎ID不同',
                '引擎': engines, '路由器计数': exec_service(root, 'router', ['iptables', '-vnL', 'FORWARD'], capture=True).stdout}
    with (root / 'results.jsonl').open('a') as stream:
        stream.write(json.dumps(evidence, ensure_ascii=False) + '\n')


def disruption_rules(address, mode):
    """由固定类型生成隔离规则，不接受驱动提供任意 iptables 参数。"""
    address = str(ipaddress.IPv4Address(address))
    if mode in ('to-node', 'from-node', 'both'):
        directions = ('-d', '-s') if mode == 'both' else ('-d',) if mode == 'to-node' else ('-s',)
        return [['FORWARD', direction, address, '-j', 'DROP'] for direction in directions]
    if mode == 'reset':
        return [['FORWARD', direction, address, '-p', 'tcp', '-j', 'REJECT', '--reject-with', 'tcp-reset']
                for direction in ('-d', '-s')]
    if mode in ('ssh-only', 'api-only'):
        return [['FORWARD', '-d', address, '-p', 'tcp', '--dport', '22' if mode == 'ssh-only' else '3000', '-j', 'DROP']]
    raise RuntimeError('网络中断类型不在测试清单')


def manager_storage_mount(mode):
    """只允许故障覆盖本轮 Manager 的备份目录或临时盘，禁止任意挂载。"""
    mounts = {'backup-full': '/app/data/backups:rw,size=16m,mode=1777',
              'backup-inodes': '/app/data/backups:rw,size=16m,nr_inodes=128,mode=1777',
              'backup-readonly': '/app/data/backups:ro,size=16m,mode=1777',
              'temporary-full': '/tmp:rw,size=16m,mode=1777'}
    if mode not in mounts:
        raise RuntimeError('Manager 存储故障类型不在测试清单')
    return mounts[mode]


def manager_storage(root, request):
    """短暂遮蔽专属目录，保留原卷；退出时用原配置重建同一 Manager。"""
    path, saved = root / 'compose.json', root / 'manager-storage-original.json'
    mode = request['mode']
    mount = manager_storage_mount(mode)
    if request['enabled']:
        if saved.exists():
            raise RuntimeError('前次存储故障尚未还原，拒绝覆盖原配置')
        original = json.loads(path.read_text())
        write_json(saved, original)
        changed = json.loads(json.dumps(original))
        changed['services']['backend'].setdefault('tmpfs', []).append(mount)
        write_json(path, changed)
    else:
        if not saved.exists():
            return {'说明': '没有待还原的本轮存储故障配置'}
        write_json(path, json.loads(saved.read_text()))
    compose(root, 'up', '-d', '--force-recreate', 'backend', capture=True)
    identifier = service_id(root, 'backend')
    assert_owned(root, identifier)
    state = json.loads((root / 'private.json').read_text())
    gateway = str(ipaddress.ip_network(state['networks'][0]).network_address + 254)
    script = 'ip route del default 2>/dev/null || true; ' + '; '.join(
        'ip route replace %s via %s' % (network, gateway) for network in state['networks'][1:])
    tool = json.loads((root / 'images.json').read_text())['host']['摘要']
    run(['docker', 'run', '--rm', '--network', 'container:' + identifier, '--cap-add', 'NET_ADMIN',
         '--label', LABEL + '=' + root.name, '--entrypoint', 'sh', tool, '-ec', script], capture=True)
    evidence = {'说明': '原数据卷保持，故障只落在有界临时盘', '类型': mode, '启用': request['enabled'],
                '挂载': json.loads(output(['docker', 'inspect', '-f', '{{json .HostConfig.Tmpfs}}', identifier]))}
    if request['enabled'] and mode == 'backup-inodes':
        fill = ('import errno,json,os\n'
                'for i in range(4096):\n'
                ' try: open("/app/data/backups/.inode-%s"%i,"wb").close()\n'
                ' except OSError as exc:\n'
                '  print(json.dumps({"errno":exc.errno,"created":i})); break\n'
                'else: raise RuntimeError("inode 故障没有命中")')
        evidence['inode填充'] = json.loads(output(['docker', 'exec', identifier, 'python', '-c', fill]))
        if evidence['inode填充']['errno'] != 28:
            raise RuntimeError('inode 填充并非空间耗尽，拒绝声称命中')
    if not request['enabled']:
        saved.unlink()
    return evidence


def network_queues(rows, interfaces):
    """仅比较队列配置，计数器不作为配置；拒绝附加子队列或不完整接口。"""
    selected = [row for row in rows if row.get('dev') in interfaces]
    if len(selected) != len(interfaces) or {row['dev'] for row in selected} != set(interfaces):
        raise RuntimeError('路由器队列不是每接口唯一根队列，拒绝覆盖')
    if any(not row.get('root') for row in selected):
        raise RuntimeError('路由器存在未登记子队列，拒绝覆盖')
    return {row['dev']: {key: row.get(key) for key in ('kind', 'handle', 'root', 'options')}
            for row in selected}


def verify_network_profile(queues, profile):
    """按真实 tc JSON 的秒、字节每秒和丢包比例核对参数，不能只凭 netem 名称通过。"""
    delay, jitter, rate, loss = (0.05, 0.01, 12500000, 0) if profile == 'wan' else (0.1, 0.02, 2500000, 0.005)
    for row in queues.values():
        options = row.get('options') or {}
        actual_delay = options.get('delay') or {}
        if (row.get('kind') != 'netem' or options.get('rate', {}).get('rate') != rate
                or abs(actual_delay.get('delay', -1) - delay) > 1e-7
                or abs(actual_delay.get('jitter', -1) - jitter) > 1e-7
                or abs(options.get('loss-random', {}).get('loss', 0) - loss) > 1e-7):
            raise RuntimeError('实际队列延迟、抖动、速率或丢包与剖面不一致')


def network_profile(root, request):
    """只还原当前所有者实际建立的队列；异常或身份变化留下明确活动标记。"""
    profile, owner = request['profile'], request.get('profile_owner', '')
    if profile not in ('normal', 'wan', 'lossy') or not re.fullmatch('[a-f0-9]{32}', owner):
        raise RuntimeError('网络剖面或本轮所有者无效')
    router = service_id(root, 'router')
    attrs = assert_owned(root, router)
    state = json.loads((root / 'private.json').read_text())
    addresses = json.loads(exec_service(root, 'router', ['ip', '-j', '-4', 'addr', 'show'], capture=True).stdout)
    expected = {str(ipaddress.ip_network(network).network_address + 254) for network in state['networks']}
    interfaces = {row['ifname']: sorted(info['local'] for info in row['addr_info'])
                  for row in addresses if row['ifname'] != 'lo'}
    if len(interfaces) != 3 or {ip for values in interfaces.values() for ip in values} != expected:
        raise RuntimeError('路由接口与本轮三个网段不完全对应，未修改队列')
    def read():
        return network_queues(json.loads(exec_service(root, 'router', ['tc', '-j', 'qdisc', 'show'], capture=True).stdout), interfaces)
    current = read()
    path = root / 'network-profile-active.json'
    active = json.loads(path.read_text()) if path.exists() else None
    if active:
        if (active['所有者'] != owner or active['路由器'] != router or active['镜像'] != attrs['Image']
                or active['接口'] != interfaces or active['当前队列'] != current):
            raise RuntimeError('已有队列所有者、实际配置或路由身份不同，拒绝覆盖或还原')
        if profile != 'normal':
            raise RuntimeError('先还原本轮剖面，再启用下一种；不覆盖活动标记')
    elif any(row['kind'] != 'noqueue' or row['handle'] != '0:' or row['options'] for row in current.values()):
        raise RuntimeError('路由器已有非默认队列，拒绝覆盖未知设置')
    if profile == 'normal':
        if active:
            for interface in interfaces:
                if active['当前队列'][interface] != active['原队列'][interface]:
                    exec_service(root, 'router', ['tc', 'qdisc', 'del', 'dev', interface, 'root'], capture=True)
                    active['当前队列'] = read(); write_json(path, active)
            if read() != active['原队列']:
                raise RuntimeError('本轮原队列未完整还原，活动标记保留')
            active.update(说明='本轮网络剖面已还原，历史记录保留', 状态='已还原')
            write_json(root / ('network-profile-restored-' + str(time.time_ns()) + '.json'), active)
            path.unlink()
        return {'说明': '默认队列只读确认或本轮自有队列完整还原', '队列配置': read()}
    active = {'说明': '仅本轮所有者可还原的队列配置；未知变化必须停止', '所有者': owner,
              '路由器': router, '镜像': attrs['Image'], '接口': interfaces,
              '原队列': current, '当前队列': current, '剖面': profile, '状态': '应用中'}
    write_json(path, active)
    parameters = ['delay', '50ms', '10ms', 'rate', '100mbit'] if profile == 'wan' else ['delay', '100ms', '20ms', 'loss', '0.5%', 'rate', '20mbit']
    try:
        for interface in interfaces:
            active['正在应用接口'] = interface; write_json(path, active)
            exec_service(root, 'router', ['tc', 'qdisc', 'replace', 'dev', interface, 'root', 'netem', *parameters], capture=True)
            active['当前队列'] = read()
            if active['当前队列'][interface]['kind'] != 'netem':
                raise RuntimeError('实际队列不是netem，不能认定剖面已生效')
            active.pop('正在应用接口', None); write_json(path, active)
        verify_network_profile(active['当前队列'], profile)
        active['状态'] = '已应用'; write_json(path, active)
        return {'说明': '三接口自有队列已实际应用', '剖面': profile, '队列配置': read(), '所有者': owner}
    except Exception:
        # 只有实际配置仍与已登记配置相同才允许还原；不删除未知队列。
        network_profile(root, {'profile': 'normal', 'profile_owner': owner})
        raise


def backup_identity(root, backup_id):
    """只读取指定备份的持久诊断关联和进程身份，不导出日志或凭据。"""
    if type(backup_id) is not int or backup_id <= 0:
        raise RuntimeError('诊断读取必须指定唯一有效备份编号')
    identifier = service_id(root, 'backend')
    attrs = assert_owned(root, identifier)
    code = r'''from pathlib import Path
import json,os,re,sqlite3,sys,time
identifier=int(sys.argv[1])
with sqlite3.connect('file:/app/data/gitea-manager.db?mode=ro',uri=True) as db:
 db.row_factory=sqlite3.Row
 row=dict(db.execute('SELECT id,source_server_id,diagnostic_id FROM backups WHERE id=?',(identifier,)).fetchone())
diagnostic=row['diagnostic_id'] or ''
assert re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]{8}',diagnostic)
directory=Path('/app/data/diagnostics')/diagnostic
assert directory.is_dir() and not directory.is_symlink()
raw=json.loads((directory/'status.json').read_text())
status={key:raw[key] for key in ('状态','开始时间','进程') if key in raw}
events=directory/'events.jsonl'
assert events.stat().st_size <= 2*1024*1024
links=[item for line in events.read_text().splitlines() if (item:=json.loads(line)).get('状态')=='关联' and item.get('说明')=='业务操作']
assert len(links)==1
link={key:links[0][key] for key in ('操作编号','业务类型','业务编号')}
process=status.get('进程')
born=None
if type(process) is int and process>0:
 try:
  ticks=int(Path('/proc/%d/stat'%process).read_text().rsplit(')',1)[1].split()[19])
  boot=next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
  born=boot+ticks/os.sysconf('SC_CLK_TCK')
 except FileNotFoundError:pass
print(json.dumps({'任务ID':row['id'],'源ID':row['source_server_id'],'诊断编号':diagnostic,
 '诊断状态':status,'业务关联':link,'原诊断进程存活':born is not None and born<=float(status['开始时间']),
 '当前同PID进程启动秒':born,'采集时间':time.time()},ensure_ascii=False))
'''
    value = json.loads(exec_service(root, 'backend', ['python', '-', str(backup_id)],
                                   capture=True, stdin=code).stdout)
    value.update({'后端容器': identifier, '后端镜像': attrs['Image'],
                  '后端启动时间': attrs['State']['StartedAt']})
    return value


def fault_controller(root, stop):
    """控制通道只能改本轮路由器与容器，拒绝任意命令或未登记的操作。"""
    requests_dir = root / 'control'
    requests_dir.mkdir(exist_ok=True)
    state = json.loads((root / 'private.json').read_text())
    while not stop.wait(0.2):
        for request_path in requests_dir.glob('*.request.json'):
            response_path = request_path.with_name(request_path.name.replace('.request.json', '.response.json'))
            if response_path.exists():
                continue
            request = json.loads(request_path.read_text())
            try:
                evidence = {}
                operation = request['operation']
                node = request.get('node', 'a')
                if node not in ('a', 'b'):
                    raise RuntimeError('非法测试节点')
                if operation == 'disconnect':
                    seconds = request['seconds']
                    if seconds not in (5, 30, 180, 600):
                        raise RuntimeError('断网时长不在测试清单')
                    address = state['addresses'][node]
                    mode = request.get('mode', 'to-node')
                    rules = disruption_rules(address, mode)
                    installed = []
                    begin = time.monotonic()
                    try:
                        for rule in rules:
                            exec_service(root, 'router', ['iptables', '-I', *rule])
                            installed.append(rule)
                        write_json(response_path.with_suffix('.active'), {'说明': '断网已生效', '模式': mode})
                        stop.wait(seconds)
                        evidence.update(模式=mode, 节点=node, 请求秒数=seconds,
                                        实际秒数=round(time.monotonic() - begin, 3), 规则=rules)
                        evidence['丢包计数'] = exec_service(root, 'router', ['iptables', '-vnL', 'FORWARD'], capture=True).stdout
                    finally:
                        for rule in reversed(installed):
                            exec_service(root, 'router', ['iptables', '-D', *rule])
                elif operation == 'profile':
                    evidence.update(network_profile(root, request))
                    evidence['实际队列参数'] = exec_service(root, 'router', ['tc', '-s', 'qdisc', 'show'], capture=True).stdout
                    network_namespace = service_id(root, 'driver')
                    tool = json.loads((root / 'images.json').read_text())['host']['摘要']
                    for target_node in ('a', 'b'):
                        evidence['往返测量-' + target_node] = output(['docker', 'run', '--rm', '--network', 'container:' + network_namespace,
                            '--label', LABEL + '=' + root.name, '--entrypoint', 'ping', tool, '-c', '5', '-W', '3', state['addresses'][target_node]])
                elif operation == 'health-block':
                    rule = ['FORWARD', '-s', state['addresses']['manager'], '-d', state['addresses'][node],
                            '-p', 'tcp', '--dport', '3000', '-j', 'DROP']
                    exec_service(root, 'router', ['iptables', '-I' if request['enabled'] else '-D', *rule])
                elif operation == 'tls-port':
                    rule = ['FORWARD', '-s', state['networks'][0], '-d', state['addresses']['a'],
                            '-p', 'tcp', '--dport', '3443', '-j', 'ACCEPT']
                    exec_service(root, 'router', ['iptables', '-I' if request['enabled'] else '-D', *rule])
                elif operation == 'manager-storage':
                    evidence = manager_storage(root, request)
                elif operation == 'manager-space':
                    evidence = json.loads(exec_service(root, 'backend', ['python', '-c',
                        'import json,shutil,tempfile; p=tempfile.gettempdir(); print(json.dumps({"临时目录":p,"空闲字节":shutil.disk_usage(p).free}))'], capture=True).stdout)
                elif operation == 'manager-role-tls':
                    from .manager_role_host import configure
                    if type(request.get('enabled')) is not bool:
                        raise RuntimeError('角色 TLS 开关必须是布尔值')
                    evidence = configure(root, request['enabled'])
                elif operation == 'cold-manager':
                    if request['enabled']:
                        compose(root, 'stop', 'backend', capture=True)
                        compose(root, '--profile', 'cold', 'up', '-d', 'cold-backend', capture=True)
                        target = service_id(root, 'cold-backend')
                    else:
                        compose(root, '--profile', 'cold', 'stop', 'cold-backend', capture=True)
                        compose(root, 'start', 'backend', capture=True)
                        target = service_id(root, 'backend')
                    assert_owned(root, target)
                    tool = json.loads((root / 'images.json').read_text())['host']['摘要']
                    gateway = str(ipaddress.ip_network(state['networks'][0]).network_address + 254)
                    script = 'ip route del default 2>/dev/null || true; ' + '; '.join(
                        'ip route replace %s via %s' % (network, gateway) for network in state['networks'][1:])
                    run(['docker', 'run', '--rm', '--network', 'container:' + target, '--cap-add', 'NET_ADMIN',
                         '--label', LABEL + '=' + root.name, '--entrypoint', 'sh', tool, '-ec', script], capture=True)
                elif operation == 'source-power':
                    service = 'host-' + node
                    if request['enabled']:
                        compose(root, 'start', service, capture=True)
                        identifier = service_id(root, service)
                        assert_owned(root, identifier)
                        index = 1 if node == 'a' else 2
                        gateway = str(ipaddress.ip_network(state['networks'][index]).network_address + 254)
                        script = 'ip route del default 2>/dev/null || true; ' + '; '.join(
                            'ip route replace %s via %s' % (network, gateway)
                            for i, network in enumerate(state['networks']) if i != index)
                        exec_service(root, service, ['sh', '-ec', script], capture=True)
                    else:
                        compose(root, 'stop', service, capture=True)
                elif operation == 'restart-host':
                    identifier = service_id(root, 'host-' + node)
                    assert_owned(root, identifier)
                    run(['docker', 'restart', identifier], capture=True)
                    index = 1 if node == 'a' else 2
                    gateway = str(ipaddress.ip_network(state['networks'][index]).network_address + 254)
                    script = 'ip route del default 2>/dev/null || true; ' + '; '.join(
                        'ip route replace %s via %s' % (network, gateway)
                        for i, network in enumerate(state['networks']) if i != index)
                    exec_service(root, 'host-' + node, ['sh', '-ec', script], capture=True)
                elif operation == 'kill-manager':
                    identifier = service_id(root, 'backend')
                    assert_owned(root, identifier)
                    run(['docker', 'kill', '--signal=KILL', identifier], capture=True)
                    compose(root, 'start', 'backend', capture=True)
                    gateway = str(ipaddress.ip_network(state['networks'][0]).network_address + 254)
                    script = 'ip route del default 2>/dev/null || true; ' + '; '.join(
                        'ip route replace %s via %s' % (network, gateway) for network in state['networks'][1:])
                    tool = json.loads((root / 'images.json').read_text())['host']['摘要']
                    run(['docker', 'run', '--rm', '--network', 'container:' + identifier, '--cap-add', 'NET_ADMIN',
                        '--label', LABEL + '=' + root.name, '--entrypoint', 'sh', tool, '-ec', script], capture=True)
                elif operation == 'backup-identity':
                    evidence = backup_identity(root, request.get('backup_id'))
                elif operation == 'finish-backup':
                    backup_id = request.get('backup_id')
                    if type(backup_id) is not int or backup_id <= 0:
                        raise RuntimeError('运维收尾必须指定唯一有效备份编号')
                    identifier = service_id(root, 'backend')
                    assert_owned(root, identifier)
                    source = (ROOT / 'scripts/finish-interrupted-backup.py').read_text()
                    digest = hashlib.sha256(source.encode()).hexdigest()
                    saved = root / ('maintenance-tool-' + digest + '.py')
                    if not saved.exists():
                        saved.write_text(source); saved.chmod(0o600)
                    result = run(['docker', 'exec', '-i', identifier, 'python', '-', '--backup-id', str(backup_id)],
                        stdin=source, capture=True, timeout=30)
                    evidence = {'说明': '既有命名收尾工具；工具本身验证 API 启动晚于原诊断，不删除文件或操作远端',
                        '工具SHA256': digest, '结果': json.loads(result.stdout)}
                else:
                    raise RuntimeError('控制请求不在白名单')
                write_json(response_path, {'状态': '完成', '证据': evidence})
            except Exception as exc:
                write_json(response_path, {'状态': '失败', '说明': str(exc)[:1000]})


def controlled_driver(root, phase, extra=()):
    begin = time.monotonic()
    path = root / 'phase-runs' / (str(time.time_ns()) + '-' + phase + '.json')
    source = Path(__file__).read_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    controller = root / 'controller-versions' / (source_hash + '.py')
    if not controller.exists():
        controller.parent.mkdir(exist_ok=True, mode=0o700)
        controller.write_bytes(source)
        controller.chmod(0o600)
    record = {'说明': '执行完成只表示命令结束，功能判定仍以逐项断言为准', '阶段': phase,
        '状态': '执行中', '开始时间UTC': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        '宿主控制器SHA256': source_hash,
        '镜像': json.loads((root / 'images.json').read_text())}
    write_json(path, record)
    stop = threading.Event()
    controller = threading.Thread(target=fault_controller, args=(root, stop), daemon=True)
    observer = threading.Thread(target=resource_observer, args=(root, stop), daemon=True)
    awake = subprocess.Popen(['caffeinate', '-i', '-w', str(os.getpid())], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL) if shutil.which('caffeinate') else None
    controller.start()
    observer.start()
    try:
        if phase == 'manager-ui':
            browser_driver(root)
        else:
            driver(root, phase, extra)
    except Exception as exc:
        record.update(状态='执行异常', 错误=str(exc))
        raise
    else:
        record['状态'] = '执行完成'
    finally:
        if awake is not None:
            awake.terminate()
            awake.wait(timeout=5)
        stop.set()
        controller.join(timeout=15)
        observer.join(timeout=15)
        record.update(耗时秒=round(time.monotonic() - begin, 3),
            结束时间UTC=datetime.datetime.now(datetime.timezone.utc).isoformat())
        write_json(path, record)


def browser_driver(root):
    """复用执行锁，只让宿主浏览器访问本轮带标签的回环入口。"""
    with (root / 'test-run.lock').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if shutil.disk_usage(root).free < 6 * 1024**3:
            raise RuntimeError('浏览器只保留元数据与截图，仍要求六 GiB 余量，未操作')
        actual = {name: assert_owned(root, service_id(root, name)) for name in ('frontend', 'backend')}
        expected = json.loads((root / 'images.json').read_text())
        if any(actual[name]['Image'] != expected[image]['摘要'] for name, image in (('frontend', 'frontend'), ('backend', 'manager'))):
            raise RuntimeError('实际被测前后端不匹配冻结镜像，拒绝浏览器验证')
        state = json.loads((root / 'private.json').read_text())
        bindings = actual['frontend']['NetworkSettings']['Ports'].get('80/tcp', [])
        if bindings != [{'HostIp': '127.0.0.1', 'HostPort': str(state['http_port'])}]:
            raise RuntimeError('浏览器入口不是本轮前端的唯一回环端口，拒绝访问')
        raw = (ROOT / 'scripts/remote-backup-ui.cjs').read_bytes()
        saved = root / 'controller-versions' / (hashlib.sha256(raw).hexdigest() + '.cjs')
        if not saved.exists():
            saved.write_bytes(raw)
            saved.chmod(0o600)
        run([os.environ.get('REMOTE_BACKUP_NODE', 'node'), str(saved), str(root)], timeout=240)


def resource_observer(root, stop):
    """只采样本轮容器与宿主可用空间，记录采样峰值而非推断瞬时峰值。"""
    while not stop.is_set():
        row = {'说明': '三十秒资源采样，采样间隔内瞬时峰值未知',
               '时间': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               '宿主可用字节': shutil.disk_usage(ROOT).free}
        try:
            identifiers = output(['docker', 'ps', '-q', '--filter', 'label=' + LABEL + '=' + root.name], timeout=10).splitlines()
            values = output(['docker', 'stats', '--no-stream', '--format', '{{json .}}', *identifiers], timeout=15) if identifiers else ''
            row['容器'] = [json.loads(line) for line in values.splitlines()]
        except Exception as exc:
            row['采样错误'] = type(exc).__name__
        with (root / 'resources.jsonl').open('a') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        stop.wait(30)


def report(root):
    from .metrics import collect
    records = [json.loads(line) for line in (root / 'results.jsonl').read_text().splitlines()] if (root / 'results.jsonl').exists() else []
    summary = verdict(records)
    summary.update({'说明': '未执行项不能算作通过；历史首次失败保留', '记录': records})
    write_json(root / 'report.json', summary)
    metrics = collect(root)
    write_json(root / 'performance.json', metrics)
    lines = ['# 跨网段灾备验收报告', '', '**功能验收：%s**' % summary['功能验收'], '',
             '准备阶段首败：%d；功能检查失败：%d。准备错误保留，但不能据此捏造产品缺陷。' % (summary['准备阶段首败数'], summary['功能检查失败数']),
             '', '| 类别 | 状态 |', '|---|---|']
    lines.extend('| %s | %s |' % (CASES[key], value) for key, value in summary['类别'].items())
    lines.extend(['', '## 任务实测', '', '| 任务 | 状态 | 总耗时秒 |', '|---|---|---:|'])
    lines.extend('| %s | %s | %s |' % (row['任务'], row['状态'], row['总耗时秒']) for row in metrics['任务'])
    lines.extend(['', '可用性采样、Manager 响应、原始阶段与资源统计见同目录 performance.json；不是生产 RTO 承诺。'])
    lines.extend(['', '## 逐项记录', ''])
    for record in records:
        lines.append('- %s：%s。%s' % (record.get('用例', record.get('类别')), record['状态'], record.get('说明', '')))
    lines.extend(['', '## 验收边界', '', '本机 Docker 子网仿真不能替代生产机房、物理存储或实际人工计时。',
                  '测试首次失败和未执行项目均保留；修复后追加复验记录，不删除旧结论。'])
    (root / 'report.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'功能验收': summary['功能验收'], '报告': str(root / 'report.md')}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['preflight', 'prepare', 'build', 'build-host', 'build-driver', 'build-runner', 'build-integrations', 'load-runner', 'load-pg17', 'deploy', 'bootstrap', 'seed', 'seed-integrations', 'validate-source', 'validate-target', 'integrations', 'integrations-check', 'checkpoint', 'checkpoint-full', 'baseline', 'normal', 'secrets', 'offline', 'fault', 'oracle', 'process', 'process-source-snapshot', 'process-source-dump', 'process-source-transfer', 'database', 'database-source', 'database-import', 'archive', 'archive-paths', 'archive-truncated', 'settings', 'ssh', 'ssh-keys', 'manager', 'manager-ui', 'manager-roles', 'manager-permissions', 'collaboration', 'address', 'storage', 'storage-source', 'storage-manager', 'storage-preflight', 'unsupported', 'unsupported-pg', 'concurrent', 'schedule', 'post-publish', 'network', 'network-stable', 'soak', 'report', 'cleanup'])
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--hours', type=float, default=24)
    parser.add_argument('--openssd-source', type=Path, default=Path('/Users/archer/Work/cosmos-plus-openssd'))
    args = parser.parse_args()
    latest = ROOT / 'work' / 'remote-backup' / 'latest'
    root = args.run_dir or (Path(latest.read_text().strip()) if latest.exists() else latest.parent / datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    root = root.resolve()
    if root.parent != latest.parent.resolve():
        raise RuntimeError('运行目录必须属于 work/remote-backup，拒绝任意路径')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(str(root) + '\n')
    if args.phase == 'preflight':
        preflight(root)
    elif args.phase == 'prepare':
        prepare_seeds(root, args.openssd_source)
    elif args.phase == 'build':
        build(root)
    elif args.phase == 'build-runner':
        build_runner(root)
    elif args.phase == 'build-integrations':
        build_integrations(root)
    elif args.phase == 'build-host':
        refresh_host(root)
    elif args.phase == 'build-driver':
        refresh_driver(root)
    elif args.phase == 'load-runner':
        load_runner(root)
    elif args.phase == 'load-pg17':
        load_pg_variant(root)
    elif args.phase == 'bootstrap':
        state = json.loads((root / 'private.json').read_text())
        topology_proof(root, state)
        driver(root, 'deploy')
    elif args.phase == 'deploy':
        pre = preflight(root)
        if pre['状态'] != '通过':
            raise RuntimeError('资源预检未通过，未部署环境')
        state = generate(root, pre)
        routes = topology(root, state)
        compose(root, 'up', '-d')
        configure_routes(root, state, routes)
        copy_images(root, state)
        topology_proof(root, state)
        driver(root, 'deploy')
    elif args.phase in ('seed', 'seed-integrations', 'validate-source', 'validate-target', 'integrations', 'integrations-check', 'checkpoint', 'checkpoint-full', 'baseline', 'normal', 'secrets', 'offline', 'fault', 'oracle', 'process', 'process-source-snapshot', 'process-source-dump', 'process-source-transfer', 'database', 'database-source', 'database-import', 'archive', 'archive-paths', 'archive-truncated', 'settings', 'ssh', 'ssh-keys', 'manager', 'manager-ui', 'manager-roles', 'manager-permissions', 'collaboration', 'address', 'storage', 'storage-source', 'storage-manager', 'storage-preflight', 'unsupported', 'unsupported-pg', 'concurrent', 'schedule', 'post-publish', 'network', 'network-stable', 'soak'):
        controlled_driver(root, args.phase, ('--hours', str(args.hours)) if args.phase == 'soak' else ())
    elif args.phase == 'cleanup':
        ids = output(['docker', 'ps', '-aq', '--filter', 'label=' + LABEL + '=' + root.name]).splitlines()
        for identifier in ids:
            assert_owned(root, identifier)
        compose(root, 'down')  # 数据卷和失败证据保留，清除必须是另外的明确操作。
    else:
        report(root)


if __name__ == '__main__':
    main()
