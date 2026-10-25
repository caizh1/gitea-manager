#!/usr/bin/env python3
"""两套产品共用的首次安装器：固定镜像、原子状态、幂等初始化与真实健康门禁。"""
import argparse
import base64
import fcntl
import hashlib
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend/services'))
from diagnostics import Report, register_secret, CommandFailure
REPORT = None


def run(args, *, data=None, timeout=180):
    env = {k: v for k, v in os.environ.items() if not k.startswith('COMPOSE_')}
    if REPORT is not None:
        sensitive = 'inspect' in args or 'admin' in args or ('load' not in args and any('token' in str(x) or 'password' in str(x) for x in args))
        return REPORT.command(args, label=REPORT.step + ' / ' + args[0], data=data,
                              timeout=timeout, env=env, sensitive=sensitive)[1]
    result = subprocess.run(args, input=data, text=True, capture_output=True, env=env, timeout=timeout)
    if result.returncode:
        raise RuntimeError('执行失败：' + args[0] + '；' + result.stderr[-3000:])
    return result.stdout


def private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def validate_host(value):
    try:
        address = ipaddress.IPv4Address(value)
        if address.is_unspecified or address.is_multicast or address == ipaddress.IPv4Address('255.255.255.255'):
            raise ValueError('不能使用未指定地址、组播或广播地址')
        if address.is_loopback and value != '127.0.0.1':
            raise ValueError('本机访问请使用 127.0.0.1')
        return value
    except ipaddress.AddressValueError:
        if re.fullmatch(r'[0-9.]+', value):
            raise ValueError('IPv4 地址无效') from None
        if len(value) <= 253 and all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in value.split('.')):
            return value.lower()
    raise ValueError('请输入 IPv4 或 ASCII 域名，不含协议、端口和路径')


def validate_port(value):
    if not str(value).isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError('端口必须为 1 至 65535 的整数')
    return int(value)


def prompt(label, default, validator=str):
    while True:
        try:
            value = input(f'{label} [{default}]：').strip() or str(default)
            return validator(value)
        except ValueError as exc:
            print(str(exc))
        except EOFError:
            raise RuntimeError('缺少交互输入；自动化验收请显式提供 --host 与 --port') from None


def default_host():
    if sys.platform == 'linux':
        try:
            for value in run(['hostname', '-I']).split():
                if isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address):
                    return value
        except (OSError, RuntimeError, ValueError):
            pass
    return '127.0.0.1'


def request(opener, method, url, value=None, headers=None):
    req = urllib.request.Request(url, data=None if value is None else json.dumps(value).encode(),
                                 headers={'Content-Type': 'application/json', **(headers or {})}, method=method)
    with opener.open(req, timeout=10) as response:
        body = response.read()
        return json.loads(body) if body else None


def wait_for(check, description, seconds=240):
    deadline = time.monotonic() + seconds
    last = None
    started=last_notice=time.monotonic()
    while time.monotonic() < deadline:
        now=time.monotonic()
        if now-last_notice>=10:
            print(f'{description}：已等待 {int(now-started)} 秒，剩余最多 {max(0,int(deadline-now))} 秒',flush=True)
            last_notice=now
        try:
            if check():
                return
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            last = str(exc)
            if REPORT: REPORT.event('等待', description, 原因=last)
        time.sleep(2)
    raise RuntimeError(description + '未就绪，最后状态：' + str(last) + '；保留现场，可修正原因后重跑')


def spec(image, **extra):
    return {'image': image, 'platform': 'linux/amd64', 'pull_policy': 'never',
            'restart': 'unless-stopped', 'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}, **extra}


def configuration(state, manifest, root):
    images = manifest['images']
    common = {'name': state['project'], 'services': {}, 'volumes': {}}
    services = common['services']
    binding = '127.0.0.1' if state['host'] in ('127.0.0.1', 'localhost') else '0.0.0.0'
    if state['product'] == 'gitea':
        services['database'] = spec(images['database']['tag'], environment={
            'POSTGRES_USER': 'gitea', 'POSTGRES_DB': 'gitea', 'POSTGRES_PASSWORD': state['database_password']},
            volumes=['database-data:/var/lib/postgresql/data'],
            healthcheck={'test': ['CMD-SHELL', 'pg_isready -U gitea -d gitea'], 'interval': '3s', 'timeout': '3s', 'retries': 60})
        services['gitea'] = spec(images['gitea']['tag'], environment={
            'GITEA__database__DB_TYPE': 'postgres', 'GITEA__database__HOST': 'database:5432',
            'GITEA__database__NAME': 'gitea', 'GITEA__database__USER': 'gitea', 'GITEA__database__PASSWD': state['database_password'],
            'GITEA__security__INSTALL_LOCK': 'true', 'GITEA__security__SECRET_KEY_URI': 'file:/data/gitea/conf/manager-secret-key',
            'GITEA__security__INTERNAL_TOKEN': state['internal_token'],
            'GITEA__server__DOMAIN': state['host'], 'GITEA__server__ROOT_URL': state['url'] + '/',
            'GITEA__server__SSH_DOMAIN': state['host'], 'GITEA__server__SSH_PORT': str(state['ssh_port']),
            'GITEA__service__DISABLE_REGISTRATION': 'true', 'GITEA__repository__DEFAULT_BRANCH': 'main'},
            ports=[f'{binding}:{state["port"]}:3000', f'{binding}:{state["ssh_port"]}:22'],
            volumes=['gitea-data:/data'], depends_on={'database': {'condition': 'service_healthy'}})
        common['volumes'] = {'database-data': {}, 'gitea-data': {}}
    else:
        env = {'DATABASE_URL': 'sqlite:////app/data/gitea-manager.db', 'BACKUP_DIR': '/app/data/backups',
               'SECRET_KEY': state['secret_key'], 'INIT_PASSWORD': state['admin_password'], 'INTEGRATION_ENABLED': 'false',
               'TRUSTED_PROXY_HOSTS': 'frontend',
               'AI_REVIEW_DB': '/app/review/review.db', 'AI_REVIEW_HOST_POLICY_FILE': '/app/review/hosts.json',
               'AI_REVIEW_MASTER_KEY_FILE': '/run/secrets/review_master',
               'AI_REVIEW_AUTHORIZATION_URL': 'http://backend:5000/api/internal/authorization/check',
               'AI_REVIEW_AUTHORIZATION_TOKEN_FILE': '/run/secrets/access_authorization',
               'AI_REVIEW_RUNTIME_STATUS': '/run/manager-runtime/status.json',
               'SSH_KEY_PATH': '/run/manager-ssh/id_rsa', 'SSH_KNOWN_HOSTS': '/run/manager-ssh/known_hosts'}
        mount = '/var/run/docker.sock:/var/run/docker.sock'
        services['backend'] = spec(images['backend']['tag'], environment=env,
            volumes=['manager-data:/app/data', 'diagnostic-data:/app/data/diagnostics', 'review-data:/app/review', mount,
                     f'{root}/state/ssh:/run/manager-ssh', f'{root}/state/runtime:/run/manager-runtime:ro',
                     f'{root}/state/diagnostics:/app/install-diagnostics:ro'], secrets=['review_master', 'access_authorization'])
        services['approval-view'] = spec(images['backend']['tag'],
            command=['gunicorn', '--bind', '0.0.0.0:5000', '--workers', '2', '--threads', '8', '--timeout', '30', 'approval.view_app:create_app()'],
            environment={k: env[k] for k in ('DATABASE_URL','SSH_KEY_PATH','SSH_KNOWN_HOSTS')}, volumes=['manager-data:/app/data', mount, f'{root}/state/ssh:/run/manager-ssh:ro'], depends_on=['backend'])
        services['frontend'] = spec(images['frontend']['tag'], ports=[f'{binding}:{state["port"]}:80'], depends_on=['backend'])
        if 'svn' in images:
            services['svn-migration-worker'] = spec(images['svn']['tag'], command=['python', '-m', 'svn_migration.worker'],
                environment={'DATABASE_URL': env['DATABASE_URL'], 'PYTHONDONTWRITEBYTECODE': '1'},
                volumes=['manager-data:/app/data'], read_only=True, tmpfs=['/tmp:size=128m,mode=1777'],
                cap_drop=['ALL'], security_opt=['no-new-privileges:true'], pids_limit=256, mem_limit='2g', cpus=2.0)
        if state.get('tls_port'):
            services['frontend']['ports'].append(f'{binding}:{state["tls_port"]}:443')
            services['frontend']['volumes'] = [f'{root}/state/tls/cert.pem:/etc/nginx/tls/cert.pem:ro',
                                               f'{root}/state/tls/key.pem:/etc/nginx/tls/key.pem:ro']

        for name, memory in [('worker', '1g'), ('progress', '256m'), ('feedback', '256m')]:
            worker_env = {k: v for k, v in env.items() if k.startswith('AI_REVIEW_')}
            worker_env['PYTHONDONTWRITEBYTECODE'] = '1'
            service = spec(images['backend']['tag'], command=['python', '-m', 'ai_review.' + name],
                environment=worker_env, volumes=['review-data:/app/review', f'{root}/state/runtime:/run/manager-runtime:ro'], secrets=['review_master', 'access_authorization'],
                read_only=True, tmpfs=['/tmp:size=128m,mode=1777'], cap_drop=['ALL'],
                security_opt=['no-new-privileges:true'], mem_limit=memory, cpus=1.0)
            if name == 'worker':
                worker_env.update(AI_REVIEW_EXECUTOR_TOKEN_FILE='/run/secrets/review_executor', AI_REVIEW_EXECUTOR_URL='http://review-executor:5052')
                service['volumes'].extend(['review-workspaces:/app/review-workspaces','diagnostic-data:/app/data/diagnostics'])
                worker_env['AI_REVIEW_WORKSPACE_ROOT']='/app/review-workspaces'
                service['secrets'].append('review_executor')
                service['networks'] = ['default', 'review-execution']
            if name == 'feedback':
                service['volumes'].append('manager-data:/app/data:ro')
            services['review-' + name] = service
        services['review-executor'] = spec(images[state.get('executor_key', 'executor')]['tag'], environment={
            'AI_REVIEW_EXECUTOR_TOKEN_FILE': '/run/secrets/review_executor',
            'AI_REVIEW_BROKER_URL': 'http://review-worker:5051/v1', 'PYTHONDONTWRITEBYTECODE': '1',
            'AI_REVIEW_WORKSPACE_ROOT':'/app/review-workspaces',
            'AI_REVIEW_NATIVE_ROOT': '/app/review-native'}, volumes=['executor-data:/app/review-native','review-workspaces:/app/review-workspaces:ro'],
            secrets=['review_executor'], networks=['review-execution'], dns=['127.0.0.1'],
            read_only=True, tmpfs=['/tmp:size=384m,mode=1777'], cap_drop=['ALL'],
            security_opt=['no-new-privileges:true'], pids_limit=128, mem_limit='2g', cpus=2.0)
        common['networks'] = {'review-execution': {'internal': True}}
        common['volumes'] = {'manager-data': {}, 'review-data': {}, 'executor-data': {}, 'review-workspaces': {}, 'diagnostic-data': {}}
        common['secrets'] = {'review_master': {'file': str(root / 'state/master.key')},
                             'review_executor': {'file': str(root / 'state/executor.token')},
                             'access_authorization': {'file': str(root / 'state/access-authorization.token')}}
        # 多个容器同时复制同一镜像目录到共享空卷会竞争；业务自行初始化空数据目录。
        for service in services.values():
            mounts = []
            for mount in service.get('volumes', []):
                parts = mount.split(':')
                if parts[0] in common['volumes']:
                    mount = {'type': 'volume', 'source': parts[0], 'target': parts[1],
                             'read_only': len(parts) > 2 and parts[2] == 'ro', 'volume': {'nocopy': True}}
                mounts.append(mount)
            if mounts:
                service['volumes'] = mounts
    return common


class Installer:
    def __init__(self, root, manifest):
        self.root, self.manifest = root, manifest
        self.state_file = root / 'state/installation.json'
        self.state = json.loads(self.state_file.read_text()) if self.state_file.exists() else None

    def compose(self, *args, data=None):
        return run(['docker', 'compose', '--project-directory', str(self.root), '--env-file', '/dev/null',
                    '-p', self.state['project'], '-f', str(self.root / 'compose.install.json'), *args], data=data)

    def initialize_state(self, args):
        manifest_hash = hashlib.sha256(json.dumps(self.manifest, sort_keys=True).encode()).hexdigest()
        if self.state:
            if self.state.get('manifest_hash') != manifest_hash:
                raise RuntimeError('现有实例与安装包版本不同，请使用有备份和回退能力的升级流程')
            if self.state['product'] != self.manifest['product'] or self.state['directory'] != str(self.root):
                raise RuntimeError('安装目录已移动或产品不匹配，请恢复原目录；禁止自动接管数据')
            for key in ('host', 'port', 'ssh_port', 'tls_port'):
                if getattr(args, key, None) is not None and getattr(args, key) != self.state.get(key):
                    raise RuntimeError('已有安装参数不同；安装器不会自动改地址或端口')
            for argument, stored in [('tls_cert', 'tls_cert_sha256'), ('tls_key', 'tls_key_sha256')]:
                supplied = getattr(args, argument, None)
                if supplied and hashlib.sha256(Path(supplied).read_bytes()).hexdigest() != self.state.get(stored):
                    raise RuntimeError('已有 HTTPS 证书参数不同，安装器不会自动替换')
            print('检测到原安装记录，继续使用原账号、密钥和数据。')
            return
        if (self.root / 'compose.install.json').exists():
            raise RuntimeError('发现配置但缺少安装记录，拒绝重建密钥，请恢复 state 备份')
        host = validate_host(args.host) if args.host else prompt('浏览器访问的服务器 IP 或域名', default_host(), validate_host)
        port = args.port or prompt('HTTP 端口', 3000 if self.manifest['product'] == 'gitea' else 5480, validate_port)
        state = {'说明': '首次安装持久状态，含凭据，禁止分享；重复运行不得重置。',
                 'product': self.manifest['product'], 'directory': str(self.root), 'manifest_hash': manifest_hash,
                 'project': 'gitea-' + self.manifest['product'] + '-' + secrets.token_hex(5),
                 'host': host, 'port': port, 'url': f'http://{host}:{port}',
                 'admin_password': secrets.token_urlsafe(32), 'secret_key': secrets.token_hex(32), 'complete': False}
        if state['product'] == 'gitea':
            state.update(ssh_port=args.ssh_port or prompt('Git SSH 端口', 2222, validate_port),
                         database_password=secrets.token_hex(32), internal_token=secrets.token_hex(64))
            if state['ssh_port'] == port:
                raise RuntimeError('HTTP 与 SSH 端口不能相同')
        else:
            state.update(master_key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(), executor_token=secrets.token_urlsafe(48),
                         access_authorization_token=secrets.token_urlsafe(48))

        tls_cert, tls_key = getattr(args, 'tls_cert', None), getattr(args, 'tls_key', None)
        if tls_cert or tls_key or getattr(args, 'tls_port', None):
            if state['product'] != 'manager' or not tls_cert or not tls_key:
                raise RuntimeError('Manager HTTPS 安装需要同时指定 --tls-cert 和 --tls-key')
            certificate, private_key = Path(tls_cert).resolve(), Path(tls_key).resolve()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(certificate), str(private_key), password=lambda: '')
            tls_port = getattr(args, 'tls_port', None) or 5443
            if tls_port == port:
                raise RuntimeError('HTTP 与 HTTPS 端口不能相同')
            state.update(tls_port=tls_port, tls_cert_source=str(certificate), tls_key_source=str(private_key),
                         tls_cert_sha256=hashlib.sha256(certificate.read_bytes()).hexdigest(),
                         tls_key_sha256=hashlib.sha256(private_key.read_bytes()).hexdigest(),
                         url=f'https://{host}:{tls_port}')
        self.state = state

    def prepare(self):
        state = self.state
        if state.get('tls_port'):
            for name, source, digest in [('cert.pem', 'tls_cert_source', 'tls_cert_sha256'),
                                         ('key.pem', 'tls_key_source', 'tls_key_sha256')]:
                target = self.root / 'state/tls' / name
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).hexdigest() != state[digest]:
                        raise RuntimeError('原 HTTPS 证书或私钥已变化，请核验并使用受控更新流程')
                elif state.get('config_hash'):
                    raise RuntimeError('原 HTTPS 证书或私钥丢失，未自动替换')
                else:
                    content = Path(state[source]).read_bytes()
                    if hashlib.sha256(content).hexdigest() != state[digest]:
                        raise RuntimeError('HTTPS 证书或私钥在安装期间发生变化')
                    private_write(target, content.decode())
        if state['product'] == 'manager':
            (self.root / 'state/runtime').mkdir(exist_ok=True, mode=0o755)
            if not (self.root / 'state/runtime/status.json').exists():
                private_write(self.root / 'state/runtime/status.json', {'ready':False,'reason':'尚未检测'})
            os.chmod(self.root / 'state/runtime/status.json',0o644)
        config = self.root / 'compose.install.json'
        if state.get('config_hash'):
            if not config.is_file() or hashlib.sha256(config.read_bytes()).hexdigest() != state['config_hash']:
                raise RuntimeError('安装配置缺失或已被修改，拒绝自动覆盖；请核对原配置')
            for name, key in [('master.key', 'master_key'), ('executor.token', 'executor_token'),
                              ('access-authorization.token', 'access_authorization_token')]:
                if state['product'] == 'manager' and (self.root / 'state' / name).read_text() != state[key]:
                    raise RuntimeError('原密钥缺失或不一致，拒绝重新生成')
            return
        if state['product'] == 'manager':
            private_write(self.root / 'state/master.key', state['master_key'])
            private_write(self.root / 'state/executor.token', state['executor_token'])
            private_write(self.root / 'state/access-authorization.token', state['access_authorization_token'])
            # 宿主机父目录为 0700；挂入非 root 执行器后需要可读。
            os.chmod(self.root / 'state/executor.token', 0o444)
        private_write(config, configuration(state, self.manifest, self.root))
        state['config_hash'] = hashlib.sha256(config.read_bytes()).hexdigest()
        private_write(self.state_file, state)

    def management_key(self):
        path=self.root/'state/ssh';path.mkdir(exist_ok=True,mode=0o700)
        code="from pathlib import Path; import paramiko,os; p=Path('/keys/id_rsa'); key=paramiko.RSAKey.from_private_key_file(str(p)) if p.exists() else paramiko.RSAKey.generate(3072); key.write_private_key_file(str(p)+'.new') if not p.exists() else None; os.replace(str(p)+'.new',p) if not p.exists() else None; os.chmod(p,0o600); Path(str(p)+'.pub').write_text(key.get_name()+' '+key.get_base64()+' gitea-manager'); Path('/keys/known_hosts').touch(mode=0o600,exist_ok=True)"
        run(['docker','run','--rm','--network','none','--user','0:0','-v',str(path)+':/keys','--entrypoint','python',self.manifest['images']['backend']['tag'],'-c',code])

    def select_runtime(self, legacy_only=False):
        cpu=Path('/proc/cpuinfo').read_text() if Path('/proc/cpuinfo').exists() else '桌面隔离测试'
        cpu='\n'.join(line for line in cpu.splitlines() if line.startswith(('model name','flags','vendor_id','cpu family','model\t')))
        signature=hashlib.sha256((cpu+json.dumps(self.manifest['images'],sort_keys=True)).encode()).hexdigest()
        if not legacy_only and self.state.get('runtime_signature')==signature and self.state.get('runtime') is not None and (self.root/'state/runtime/status.json').exists():
            return
        config_path=self.root/'compose.install.json'
        if self.state.get('config_hash') and (not config_path.exists() or hashlib.sha256(config_path.read_bytes()).hexdigest()!=self.state['config_hash']):
            raise RuntimeError('原 Compose 配置已改变，未覆盖手动配置')
        results=[];selected='executor';runtime={'ready':False,'reason':'两套执行器均未通过检测'}
        for key in (('executor_legacy',) if legacy_only else ('executor','executor_legacy')):
            if key not in self.manifest['images']:continue
            image=self.manifest['images'][key]['tag'];name=self.state['project']+'-probe'
            print('正在检测：'+('标准版 OpenCode' if key=='executor' else '兼容版 OpenCode 1.1.51'),flush=True)
            try:
                code,out,err=REPORT.command(['docker','run','--name',name,'--label','com.docker.compose.project='+self.state['project'],'--network','none',
                    '--read-only','--tmpfs','/tmp:size=384m,mode=1777','--cap-drop','ALL','--security-opt','no-new-privileges:true',
                    '--pids-limit','128','--memory','2g','--cpus','2','-e','PYTHONDONTWRITEBYTECODE=1',
                    '--entrypoint','python',image,'/opt/review/runtime_probe.py'],label='执行器自检 '+key,timeout=70,check=False)
                try:value=json.loads(out)
                except ValueError:
                    if code in (125,126,127):
                        raise RuntimeError('Docker 无法运行检测容器；退出码 '+str(code)+'；'+err[-1000:])
                    value={'ready':False,'reason':'检测进程未完成；退出码 '+str(code),'stderr':err[-12000:],'stdout':out[-12000:]}
                value['image']=image
                _,container_state,_=REPORT.command(['docker','inspect','--format','{{json .State}}',name],label='执行器检测容器状态',timeout=3,check=False)
                try:value['container_state']=json.loads(container_state)
                except ValueError:value['container_state']={'说明':'容器状态不可读取'}
                if value['container_state'].get('OOMKilled'):value['reason']='执行器检测发生容器内存不足；不能据此判断 CPU 不兼容'
                results.append(value)
                if value.get('ready') and code == 0:
                    selected=key;runtime=value;break
            finally:
                _,remaining,_=REPORT.command(['docker','ps','-aq','--filter','name=^/'+name+'$'],label='检查残留检测容器',timeout=5,check=False)
                if remaining.strip(): REPORT.command(['docker','rm','-f',name],label='回收检测容器',timeout=5,check=False)
        if selected=='executor_legacy': runtime['reason']='标准版检测或正式启动未通过，兼容版检测通过；'+results[0].get('reason','请查看检测证据')
        runtime['selected_image']=self.manifest['images'][selected]['tag']
        self.state.update(executor_key=selected,runtime=runtime,runtime_signature=signature)
        REPORT.write('runtime-probes-legacy-startup.json' if legacy_only else 'runtime-probes.json',results)
        folder=self.root/'state/runtime';folder.mkdir(exist_ok=True,mode=0o755)
        private_write(folder/'status.json',runtime);os.chmod(folder/'status.json',0o644)
        if self.state.get('config_hash'):
            config=configuration(self.state,self.manifest,self.root)
            private_write(self.root/'compose.install.json',config)
            self.state['config_hash']=hashlib.sha256((self.root/'compose.install.json').read_bytes()).hexdigest()
        private_write(self.state_file,self.state)

    def check_ports(self):
        own_ports = set()
        containers = run(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=' + self.state['project']]).split()
        if containers:
            for item in json.loads(run(['docker', 'inspect', *containers])):
                labels = item['Config'].get('Labels') or {}
                if labels.get('com.docker.compose.project.working_dir') != str(self.root):
                    raise RuntimeError('同名项目属于其他目录，拒绝接管')
                for bindings in item['NetworkSettings'].get('Ports', {}).values():
                    own_ports.update(int(row['HostPort']) for row in bindings or [])
        for port in [self.state['port']] + ([self.state['ssh_port']] if self.state['product'] == 'gitea' else []) + ([self.state['tls_port']] if self.state.get('tls_port') else []):
            if port not in own_ports:
                with socket.socket() as probe:
                    try:
                        probe.bind(('0.0.0.0', port))
                    except OSError:
                        raise RuntimeError(f'端口 {port} 已占用，未停止或替换其他服务') from None

    def load_images(self):
        available = set(run(['docker', 'image', 'ls', '--format', '{{.Repository}}:{{.Tag}}']).splitlines())
        def matches():
            for item in self.manifest['images'].values():
                if item['tag'] not in available:
                    return False
                info = json.loads(run(['docker', 'image', 'inspect', item['tag']]))[0]
                if info['Architecture'] != 'amd64' or info['Os'] != 'linux' or info['Id'] not in item['ids']:
                    return False
            return True
        if matches():
            print('本机已有与清单完全一致的镜像，直接复用。')
            return
        print('正在导入并核对离线镜像，不拉取网络镜像。')
        run(['docker', 'load', '-i', str(self.root / 'images/runtime.tar')], timeout=900)
        for item in self.manifest['images'].values():
            info = json.loads(run(['docker', 'image', 'inspect', item['tag']]))[0]
            if info['Architecture'] != 'amd64' or info['Os'] != 'linux' or info['Id'] not in item['ids']:
                raise RuntimeError('镜像身份或架构与交付清单不一致：' + item['tag'])

    def health(self):
        state = self.state
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        base = f'http://127.0.0.1:{state["port"]}'
        endpoint = '/api/healthz' if state['product'] == 'gitea' else '/api/session'
        wait_for(lambda: request(opener, 'GET', base + endpoint) is not None, 'HTTP 与数据库')
        if not state['complete']:
            if state['product'] == 'gitea':
                prefix = ['exec', '-T', '-u', 'git', 'gitea', 'gitea', '--config', '/data/gitea/conf/app.ini', 'admin', 'user']
                users = self.compose(*prefix, 'list')
                if not any(len(row.split()) > 1 and row.split()[1] == 'administrator' for row in users.splitlines()):
                    # 密码通过标准输入传递，宿主机进程参数不携带凭据。
                    self.compose('exec', '-T', '-u', 'git', 'gitea', 'sh', '-ec',
                        'read -r password; exec gitea --config /data/gitea/conf/app.ini admin user create --username administrator --email administrator@example.invalid --admin --must-change-password=false --password "$password"', data=state['admin_password'] + '\n')
                auth = base64.b64encode(('administrator:' + state['admin_password']).encode()).decode()
                account = request(opener, 'GET', base + '/api/v1/user', headers={'Authorization': 'Basic ' + auth})
                if account.get('login') != 'administrator' or not account.get('is_admin'):
                    raise RuntimeError('管理员身份验证失败')
            else:
                session = request(opener, 'GET', base + '/api/session')
                authenticated = request(opener, 'POST', base + '/api/login', {'password': state['admin_password']},
                                        headers={'X-CSRF-Token': session['csrf_token']})
                request(opener, 'POST', base + '/api/settings', {'host_ip': state['host']},
                        headers={'X-CSRF-Token': authenticated['csrf_token']})
                if request(opener, 'GET', base + '/api/settings')['host_ip'] != state['host']:
                    raise RuntimeError('Manager 初始化地址回读失败')
        if state['product'] == 'manager':
            if state.get('tls_port'):
                certificate = self.root/'state/tls/cert.pem'
                context = ssl.create_default_context(cafile=str(certificate))
                context.verify_flags |= getattr(ssl, 'VERIFY_X509_PARTIAL_CHAIN', 0)
                leaf_pem = re.search(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', certificate.read_text(), re.S).group()
                expected_certificate = ssl.PEM_cert_to_DER_cert(leaf_pem)
                def secure_endpoint():
                    with socket.create_connection(('127.0.0.1', state['tls_port']), timeout=5) as raw:
                        with context.wrap_socket(raw, server_hostname=state['host']) as secured:
                            if secured.getpeercert(binary_form=True) != expected_certificate:
                                raise RuntimeError('HTTPS 实际服务证书与提供的企业证书不一致')
                    return True
                wait_for(secure_endpoint, 'HTTPS 证书、主机名与加密入口', seconds=60)
            try:
                request(opener, 'GET', base + '/api/integrations/v1/health')
                raise RuntimeError('外部集成未按默认关闭')
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
            code = "import sqlite3,time,json; c=sqlite3.connect('/app/review/review.db'); rows=dict(c.execute('select name,updated from health')); print(json.dumps({n:time.time()-rows.get(n,0) for n in ('worker','progress','feedback')}))"
            def heartbeat():
                values=json.loads(self.compose('exec','-T','backend','python','-c',code))
                if REPORT: REPORT.event('检查','后台心跳',实际=values,预期='各进程小于 90 秒')
                return all(0 <= value < 90 for value in values.values())
            wait_for(heartbeat, '审查后台心跳')
            if 'svn' in self.manifest['images']:
                wait_for(lambda: bool(self.compose('exec', '-T', 'svn-migration-worker', 'python', '-m',
                                                  'svn_migration.worker', '--check')),
                         'SVN 迁移工具链与独立执行器', seconds=60)
            if state.get('runtime',{}).get('ready'):
                probe = "import requests,json; r=requests.get('http://review-executor:5052/health',timeout=(3,5)); print(json.dumps({'http':r.status_code,'result':r.json()}))"
                def executor_ready():
                    value=json.loads(self.compose('exec','-T','review-worker','python','-c',probe))
                    if REPORT: REPORT.event('检查','执行器健康',实际=value)
                    if value['http']!=200 or value['result'].get('ok') is not True:
                        raise RuntimeError('执行器 HTTP '+str(value['http'])+'：'+str(value['result'].get('reason','响应不满足就绪条件')))
                    return True
                try:
                    wait_for(executor_ready,'执行器启动自检',seconds=75)
                except RuntimeError as startup_error:
                    REPORT.event('降级','正式启动自检失败',原因=str(startup_error))
                    REPORT.write('runtime-startup-failure.json',{'说明':str(startup_error)})
                    if state.get('executor_key')!='executor_legacy':
                        self.select_runtime(legacy_only=True)
                        if state['runtime'].get('ready'):
                            self.compose('up','-d','--no-build','--pull','never','--force-recreate','review-executor')
                            try: wait_for(executor_ready,'兼容执行器启动自检',seconds=75)
                            except RuntimeError as legacy_error:
                                state['runtime'].update(ready=False,reason='兼容执行器正式启动失败：'+str(legacy_error))
                    else:
                        state['runtime'].update(ready=False,reason='兼容执行器正式启动失败：'+str(startup_error))
                    if not state['runtime'].get('ready'):
                        print('AI 执行器不可用，完成核心管理安装；请查看自动诊断。',flush=True)
                    private_write(self.root/'state/runtime/status.json',state['runtime']);os.chmod(self.root/'state/runtime/status.json',0o644)
                    private_write(self.state_file,state)
            if state.get('runtime',{}).get('ready'):
                auth_probe = "import requests,json; from pathlib import Path; s=requests.Session(); s.trust_env=False; key=Path('/run/secrets/review_executor').read_text().strip(); print(json.dumps({label:s.post('http://review-executor:5052/execute',json={},headers={'Authorization':'Bearer '+token},timeout=(3,5)).status_code for label,token in [('有效令牌',key),('无效令牌','invalid')]}))"
                actual=json.loads(self.compose('exec','-T','review-worker','python','-c',auth_probe))
                REPORT.event('检查','执行器鉴权',预期={'有效令牌':400,'无效令牌':401},实际=actual)
                if actual!={'有效令牌':400,'无效令牌':401}:raise RuntimeError('执行器鉴权结果不符：'+json.dumps(actual,ensure_ascii=False))
        rows = json.loads(run(['docker', 'inspect', *self.compose('ps', '-q').split()]))
        if len(rows) != len(configuration(state, self.manifest, self.root)['services']) or any(not row['State']['Running'] for row in rows):
            raise RuntimeError('仍有服务没有正常运行')
        state['complete'] = True
        private_write(self.state_file, state)
        private_write(self.root / 'state/access.txt', '安装完成\n访问地址：' + state['url'] +
                      ('\n管理员：administrator' if state['product'] == 'gitea' else '\n管理员：admin') +
                      '\n初始密码：' + state['admin_password'] + '\n密码修改后请使用新密码；安装器不会重置密码。\n')
        print('安装成功：' + state['url'])
        print('初始登录凭据已保存到：' + str(self.root / 'state/access.txt') + '（仅当前用户可读）')
        if state['product'] == 'manager':
            print('AI 审查状态：' + ('运行时就绪，模型通过页面配置' if state.get('runtime',{}).get('ready') else '不可用，核心管理与备份不受影响；请查看诊断页面'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desktop-test', action='store_true', help='仅本机独立验收，跳过 Ubuntu 依赖安装')
    parser.add_argument('--host', type=validate_host)
    parser.add_argument('--port', type=validate_port)
    parser.add_argument('--ssh-port', type=validate_port)
    parser.add_argument('--tls-cert', help='Manager 企业 HTTPS 证书路径')
    parser.add_argument('--tls-key', help='Manager 企业 HTTPS 私钥路径')
    parser.add_argument('--tls-port', type=validate_port, help='Manager HTTPS 端口，默认 5443')
    args = parser.parse_args()
    if args.desktop_test and sys.platform != 'darwin':
        raise RuntimeError('桌面验收开关仅用于 macOS 本机')
    if not args.desktop_test and (sys.platform != 'linux' or os.geteuid() != 0):
        raise RuntimeError('请使用 sudo bash install.sh')
    if os.environ.get('DOCKER_HOST') or os.environ.get('DOCKER_CONTEXT'):
        raise RuntimeError('请取消 DOCKER_HOST/DOCKER_CONTEXT，安装器只管理当前主机本地 Docker')
    endpoint = json.loads(run(['docker', 'context', 'inspect']))[0]['Endpoints']['docker']['Host']
    if not endpoint.startswith('unix://'):
        raise RuntimeError('不支持远端 Docker，避免把数据写入另一台机器')
    if not args.desktop_test and endpoint not in ('unix:///var/run/docker.sock', 'unix:///run/docker.sock'):
        raise RuntimeError('正式安装要求本机系统 Docker socket，不支持 rootless 或自定义 socket')
    info = json.loads(run(['docker', 'info', '--format', '{{json .}}']))
    if not args.desktop_test and (info['OSType'] != 'linux' or info['Architecture'] not in ('x86_64', 'amd64')):
        raise RuntimeError('Docker 服务端必须为本机 Linux amd64')
    run(['docker', 'compose', 'version'])
    with REPORT.stage('检查磁盘可用空间'):
        if shutil.disk_usage(ROOT).free < 5 * 1024 ** 3:
            raise RuntimeError('安装目录可用空间不足 5 GiB')
        if not args.desktop_test and shutil.disk_usage(info['DockerRootDir']).free < 5 * 1024 ** 3:
            raise RuntimeError('Docker 数据目录可用空间不足 5 GiB')
    (ROOT / 'state').mkdir(mode=0o700, exist_ok=True)
    os.chmod(ROOT / 'state', 0o700)
    with (ROOT / 'state/install.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('另一安装进程正在运行') from None
        manifest = json.loads((ROOT / 'installer-manifest.json').read_text())
        installer = Installer(ROOT, manifest)
        with REPORT.stage('检查安装参数与端口'):
            installer.initialize_state(args)
            installer.check_ports()
        if not installer.state_file.exists():
            private_write(installer.state_file, installer.state)
        for key in ('admin_password','secret_key','database_password','internal_token','master_key','executor_token','access_authorization_token'):
            register_secret(installer.state.get(key))
        with REPORT.stage('导入并核对离线镜像'): installer.load_images()
        with REPORT.stage('镜像创建与启动预检'):
            for key,image in manifest['images'].items():
                name=installer.state['project']+'-preflight-'+key.replace('_','-')
                try:
                    REPORT.command(['docker','run','--name',name,'--label','com.docker.compose.project='+installer.state['project'],'--network','none','--read-only','--entrypoint','/bin/true',image['tag']],label='预检镜像 '+key,timeout=30)
                finally:
                    _,remaining,_=REPORT.command(['docker','ps','-aq','--filter','name=^/'+name+'$'],label='检查预检容器',timeout=3,check=False)
                    if remaining.strip():REPORT.command(['docker','rm','-f',name],label='回收预检容器',timeout=3,check=False)
        if installer.state['product']=='manager':
            with REPORT.stage('生成专用 SSH 管理密钥'): installer.management_key()
            with REPORT.stage('检测并选择 OpenCode 执行器'): installer.select_runtime()
        with REPORT.stage('生成服务配置'): installer.prepare()
        installer.compose('config', '--quiet')
        if installer.state['product'] == 'manager':
            installer.compose('run', '--rm', '--no-deps', '-T', '--user', '0:0', '--cap-add', 'CHOWN', '--cap-add', 'FOWNER',
                              '--entrypoint', 'python', 'review-executor', '-c',
                              "import os; os.chown('/app/review-native',10001,10001); os.chmod('/app/review-native',0o700)")
        if installer.state['product'] == 'gitea':
            with REPORT.stage('初始化可随备份恢复的数据加密密钥'):
                installer.compose('run', '--rm', '--no-deps', '-T', '--user', '0:0', '--entrypoint', 'sh', 'gitea', '-ec',
                    'umask 077; mkdir -p /data/gitea/conf; if ! test -s /data/gitea/conf/manager-secret-key; then cat > /data/gitea/conf/manager-secret-key; fi; chown -R git:git /data/gitea/conf', data=installer.state['secret_key'])
        with REPORT.stage('启动并初始化服务'):
            installer.compose('up', '-d', '--no-build', '--pull', 'never')
        with REPORT.stage('验证核心服务与后台状态'): installer.health()



if __name__ == '__main__':
    REPORT = Report(ROOT / 'state/diagnostics', '一键离线安装', os.environ.get('INSTALL_RUN_ID'))
    status='失败'
    try:
        main()
        status='核心安装完成'
    except (Exception, KeyboardInterrupt) as exc:
        REPORT.fail(exc)
        print('安装未完成；步骤：'+REPORT.error['步骤']+'；错误编号：'+REPORT.error['错误编号']+'；'+REPORT.error['说明'],file=sys.stderr)
    finally:
        project=None
        try: project=json.loads((ROOT/'state/installation.json').read_text()).get('project')
        except (OSError,ValueError): pass
        try: REPORT.collect(project)
        except Exception as collection_error: REPORT.event('未采集', '附加诊断失败', 原因=str(collection_error))
        REPORT.finish(status)
    sys.exit(0 if status!='失败' else 1)
