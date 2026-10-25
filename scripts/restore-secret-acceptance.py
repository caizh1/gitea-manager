#!/usr/bin/env python3
"""独立 Compose 实例实测恢复、两步验证解密、重启、Compose 重建及回退。"""
import base64
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import secrets
import struct
import socket
import subprocess
import sys
import time


def host_main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='gitea-review-backend:offline')
    parser.add_argument('--modes', default='ini,environment,uri,file,ssh,rollback')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    root = repo / 'work' / ('restore-secret-' + secrets.token_hex(4))
    root.mkdir(parents=True, mode=0o700)
    project = root.name
    password = secrets.token_hex(16)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        fixed_port = listener.getsockname()[1]
    (root/'external-secret').write_text(secrets.token_hex(32))
    (root/'external-secret').chmod(0o644)
    env = {'GITEA__database__DB_TYPE': 'postgres', 'GITEA__database__HOST': 'db:5432',
           'GITEA__database__NAME': 'gitea', 'GITEA__database__USER': 'gitea',
           'GITEA__database__PASSWD': password, 'GITEA__security__INSTALL_LOCK': 'true',
           'GITEA__repository__DEFAULT_BRANCH': 'main'}
    modes = args.modes.split(',')
    if not set(modes) <= {'ini', 'environment', 'uri', 'file', 'ssh', 'rollback', 'url'}:
        raise RuntimeError('验收模式不受支持')
    runner = project + '-runner'
    def run(*cmd):
        subprocess.run(cmd, check=True)
    try:
        run('docker', 'run', '-d', '--platform', 'linux/amd64', '--name', runner,
            '-v', '/var/run/docker.sock:/var/run/docker.sock', '-v', str(root)+':/evidence',
            '-v', str(repo/'scripts')+':/checks:ro', '-e', 'ACCEPTANCE_ROOT=/evidence/state',
            '-e', 'ACCEPTANCE_RESULTS=results.json', '-e', 'PYTHONPATH=/app',
            args.image, 'sleep', 'infinity')
        for mode in modes:
            entry = dict(env)
            if mode in ('environment', 'rollback', 'ssh', 'url'):
                entry['GITEA__security__SECRET_KEY'] = secrets.token_hex(32)
            if mode == 'uri':
                entry['GITEA__security__SECRET_KEY_URI'] = 'file:/run/secrets/external-secret'
            if mode == 'file':
                entry['GITEA__security__SECRET_KEY__FILE'] = '/run/secrets/external-secret'
            config = {'name': project, 'services': {
                'db': {'image': 'postgres:17.11-bookworm', 'platform': 'linux/amd64',
                       'environment': {'POSTGRES_USER': 'gitea', 'POSTGRES_DB': 'gitea', 'POSTGRES_PASSWORD': password},
                       'healthcheck': {'test': ['CMD', 'pg_isready', '-U', 'gitea'], 'interval': '1s', 'retries': 60},
                       'volumes': ['database:/var/lib/postgresql/data']},
                'gitea': {'image': 'gitea/gitea:1.26.4', 'platform': 'linux/amd64', 'environment': entry,
                          'depends_on': {'db': {'condition': 'service_healthy'}},
                          'volumes': ['gitea:/data', str(root/'external-secret')+':/run/secrets/external-secret:ro'],
                          'ports': [f'127.0.0.1:{fixed_port}:3000'], 'restart': 'unless-stopped'}},
                'volumes': {'database': {}, 'gitea': {}},
                'networks': {'default': {'ipam': {'config': [{'subnet': '10.254.213.0/24'}]}}}}
            compose = root/'compose.yaml'
            compose.write_text(json.dumps(config, ensure_ascii=False))
            cli = ['docker', 'compose', '-p', project, '-f', str(compose)]
            run(*cli, 'up', '-d', '--pull', 'never')
            run('docker', 'network', 'connect', project+'_default', runner)
            run('docker', 'exec', runner, 'python', '/checks/restore-secret-acceptance.py',
                'inside', mode, project+'-gitea-1', project+'-db-1')
            if mode != 'rollback':
                run(*cli, 'up', '-d', '--no-deps', '--force-recreate', '--pull', 'never', 'gitea')
                run('docker', 'exec', runner, 'python', '/checks/restore-secret-acceptance.py',
                    'inside', 'recreated', project+'-gitea-1', project+'-db-1')
            run('docker', 'network', 'disconnect', project+'_default', runner)
            run(*cli, 'down', '-v')
        print('真实恢复验收完成，证据目录：' + str(root), flush=True)
    finally:
        subprocess.run(['docker', 'network', 'disconnect', project+'_default', runner], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker', 'compose', '-p', project, '-f', str(root/'compose.yaml'), 'down', '-v'])
        subprocess.run(['docker', 'rm', '-f', runner])


def inside_main():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    import requests
    from services.restore_secrets import read_file, security_values, uri_path
    spec = importlib.util.spec_from_file_location('acceptance', '/checks/operation-progress-acceptance.py')
    a = importlib.util.module_from_spec(spec)
    Path('/evidence/state').mkdir(exist_ok=True)
    spec.loader.exec_module(a)
    import access.models  # 注册当前权限表，使用完整模型元数据创建隔离数据库。
    import access.scopes
    mode, target_name, db_name = sys.argv[2:]
    def record(label):
        with Path('/evidence/checks.jsonl').open('a') as f:
            f.write(json.dumps({'模式': mode, '检查': label, '结果': '通过'}, ensure_ascii=False)+'\n')
        print(label + '：通过', flush=True)
    def health(container):
        container.reload()
        network = next(iter(container.attrs['NetworkSettings']['Networks'].values()))
        url = 'http://' + network['IPAddress'] + ':3000'
        end = time.monotonic()+120
        while time.monotonic() < end:
            try:
                if requests.get(url+'/api/v1/version', timeout=2).status_code == 200:
                    return url
            except requests.RequestException:
                pass
            time.sleep(1)
        raise RuntimeError('隔离 Gitea 未就绪')
    def key_of(container):
        key, uri = security_values(read_file(container, '/data/gitea/conf/app.ini')[0])
        return read_file(container, uri_path(uri))[0].decode().strip() if uri else key or '!#@FDEWREWR&*('
    def sql(pg, query):
        result = pg.exec_run(['psql', '-U', 'gitea', '-d', 'gitea', '-v', 'ON_ERROR_STOP=1', '-Atc', query], user='postgres')
        if result.exit_code: raise RuntimeError('隔离样本数据库操作失败')
        return result.output.decode().strip()
    totp_secret = 'JBSWY3DPEHPK3PXP'
    def enroll(container, pg, password, api_token):
        base = health(container)
        for index in range(4):
            result = requests.post(base+'/api/v1/admin/users', headers={'Authorization': 'token '+api_token},
                json={'username': 'secretuser'+str(index), 'password': password,
                      'email': f'secret{index}@example.test', 'must_change_password': False}, timeout=30)
            if result.status_code != 201: raise RuntimeError('隔离两步验证用户创建失败：'+str(result.status_code))
            iv = secrets.token_bytes(16)
            cipher = Cipher(algorithms.AES(hashlib.md5(key_of(container).encode()).digest()), modes.CFB(iv)).encryptor()
            encrypted = base64.b64encode(iv + cipher.update(base64.b64encode(totp_secret.encode())) + cipher.finalize()).decode()
            sql(pg, f"INSERT INTO two_factor (uid,secret) SELECT id,'{encrypted}' FROM \"user\" WHERE name='secretuser{index}'")
    def login(url, password, index):
        step = struct.pack('>Q', int(time.time())//30)
        digest = hmac.new(base64.b32decode(totp_secret), step, hashlib.sha1).digest()
        offset = digest[-1] & 15
        code = str((struct.unpack('>I', digest[offset:offset+4])[0] & 0x7fffffff) % 1000000).zfill(6)
        result = requests.post(url+f'/api/v1/users/secretuser{index}/tokens', auth=('secretuser'+str(index), password),
            headers={'X-Gitea-OTP': code}, json={'name': '验收'+secrets.token_hex(4), 'scopes': ['read:user']}, timeout=10)
        if result.status_code != 201: raise RuntimeError('真实两步验证解密及认证失败：'+str(result.status_code))
    try:
        with a.app.app_context():
            a.db.create_all()
            target_container = a.client.containers.get(target_name)
            pg = a.client.containers.get(db_name)
            url = health(target_container)
            if mode == 'recreated':
                credentials = json.loads(Path('/evidence/login.json').read_text())
                login(url, credentials['口令'], 3)
                assert key_of(target_container) == credentials['源密钥']
                record('Compose 强制重建后源密钥及真实两步验证仍有效')
                return
            original_create = a.create
            def source_create(image, suffix, **kwargs):
                if suffix == 'source':
                    entry = kwargs['environment']
                    if mode in ('environment', 'url'):
                        entry.pop('GITEA__security__SECRET_KEY_URI')
                        entry['GITEA__security__SECRET_KEY'] = secrets.token_hex(32)
                    elif mode in ('uri', 'file'):
                        entry['GITEA__security__SECRET_KEY_URI'] = 'file:/tmp/source-secret'
                    elif mode == 'ini':
                        entry.pop('GITEA__security__SECRET_KEY_URI')
                container = original_create(image, suffix, **kwargs)
                if suffix == 'source':
                    if mode in ('uri', 'file'):
                        a.upload(container, '/tmp', 'source-secret', secrets.token_hex(32), mode=0o644)
                    elif mode == 'ini':
                        # 官方入口不会为已有 ini 补模板；显式使用持久目录，避免把样本放进容器可写层。
                        a.upload(container, '/data', 'gitea/conf/app.ini', '[server]\nAPP_DATA_PATH = /data/gitea\n'
                            '[repository]\nROOT = /data/git/repositories\n[security]\nSECRET_KEY = '+secrets.token_hex(32)+'\n')
                return container
            a.create = source_create
            source, source_container, password = a.node('source')
            source_pg = a.client.containers.get(source.pg_container)
            enroll(source_container, source_pg, password, source.api_token)
            login(source.gitea_url, password, 0)
            r = requests.post(source.gitea_url+'/api/v1/user/repos', headers={'Authorization': 'token '+source.api_token},
                json={'name': 'secret-sample', 'auto_init': True, 'private': True}, timeout=30)
            r.raise_for_status()
            created = target_container.exec_run(['gitea', '-c', '/data/gitea/conf/app.ini', 'admin', 'user', 'create',
                '--username', 'progressadmin', '--password', password, '--email', 'target@example.test',
                '--admin', '--must-change-password=false'], user='git')
            if created.exit_code: raise RuntimeError('隔离目标管理员创建失败')
            token = requests.post(url+'/api/v1/users/progressadmin/tokens', auth=('progressadmin', password),
                json={'name': '恢复验收', 'scopes': ['all']}, timeout=10)
            token.raise_for_status()
            target = a.GiteaServer(name='密钥兼容隔离目标', host='127.0.0.1', ssh_user='root',
                gitea_url=url.removeprefix('http://') if mode == 'url' else url,
                gitea_port=3000, api_token=token.json()['sha1'], is_local=True, connection_mode='local', gitea_container=target_name, pg_container=db_name,
                pg_dbname='gitea', pg_user='gitea', role='backup')
            a.db.session.add(target); a.db.session.commit()
            old_id = target_container.id
            old_key = key_of(target_container)
            target_container.reload()
            old_ports = target_container.attrs['NetworkSettings']['Ports']
            old_mounts = sorted((m['Type'], m.get('Name', m.get('Source')), m['Destination'], m['RW']) for m in target_container.attrs['Mounts'])
            external_before = Path('/evidence/external-secret').read_bytes()
            old_compose = Path('/evidence/compose.yaml').read_bytes()
            if mode == 'rollback':
                enroll(target_container, pg, password, target.api_token)
                original_repo = requests.post(url+'/api/v1/user/repos', headers={'Authorization': 'token '+target.api_token},
                    json={'name': 'target-only', 'auto_init': True, 'private': True}, timeout=30)
                original_repo.raise_for_status()
            if mode == 'ssh':
                import paramiko
                gateway = a.create('gitea-manager-restore-ssh:20261013', 'ssh', entrypoint=['sleep'], command=['infinity'],
                    volumes={'/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}})
                gateway.start(); gateway.reload()
                result = gateway.exec_run(['sh', '-ec', 'ssh-keygen -A; mkdir -p /root/.ssh; chmod 700 /root/.ssh'])
                if result.exit_code: raise RuntimeError('隔离 SSH 网关初始化失败')
                key = paramiko.RSAKey.generate(2048)
                key.write_private_key_file(str(a.root/'ssh-key'))
                a.upload(gateway, '/root/.ssh', 'authorized_keys', key.get_name()+' '+key.get_base64()+'\n')
                result = gateway.exec_run(['/usr/sbin/sshd', '-o', 'PermitRootLogin=prohibit-password', '-o', 'PasswordAuthentication=no'])
                if result.exit_code: raise RuntimeError('隔离 SSH 网关启动失败')
                host = gateway.attrs['NetworkSettings']['Networks']['bridge']['IPAddress']
                pub = gateway.exec_run(['cat', '/etc/ssh/ssh_host_ed25519_key.pub']).output.decode().split()
                (a.root/'known-hosts').write_text(host+' '+pub[0]+' '+pub[1]+'\n')
                for server in (source, target):
                    server.is_local = False; server.connection_mode = 'ssh'; server.host = host
                a.db.session.commit()
            sample = a.backup(source, mode+'.zip')
            if mode == 'ini':
                # 去掉 Manager 新增快照，实测历史 dump 的原密钥及 API 校验兼容路径。
                import zipfile
                original = Path(sample.file_path)
                temporary = original.with_suffix('.legacy.zip')
                with zipfile.ZipFile(original) as before, zipfile.ZipFile(temporary, 'w') as after:
                    for member in before.infolist():
                        if not member.filename.endswith(('manager-secrets.json', 'manager-snapshot.json', 'manager-manifest.json')):
                            after.writestr(member, before.read(member.filename))
                temporary.replace(original)
            if mode == 'rollback':
                from models import BackupRepoCommit
                row = BackupRepoCommit.query.filter_by(backup_id=sample.id).first()
                row.commit_ids_hash = '0'*64
                a.db.session.commit()
            a.restore(sample, target, expected='failed' if mode == 'rollback' else 'success')
            if mode == 'url':
                record('无协议 Gitea URL 的真实恢复与接口校验通过')
            target_container = a.client.containers.get(target_name)
            url = health(target_container)
            assert target_container.attrs['NetworkSettings']['Ports'] == old_ports
            actual_mounts = sorted((m['Type'], m.get('Name', m.get('Source')), m['Destination'], m['RW']) for m in target_container.attrs['Mounts'])
            assert actual_mounts == old_mounts, '恢复前后实际挂载、来源或权限不一致'
            assert Path('/evidence/external-secret').read_bytes() == external_before
            source_key = key_of(source_container).encode()
            for path in (a.root/'diagnostics').rglob('*'):
                if path.is_file() and path.suffix in ('.json', '.txt', '.log'):
                    raw = path.read_bytes()
                    assert source_key not in raw and old_key.encode() not in raw, '诊断记录泄露密钥'
            if mode == 'rollback':
                assert target_container.id == old_id
                assert key_of(target_container) == old_key
                assert Path('/evidence/compose.yaml').read_bytes() == old_compose
                login(url, password, 1)
                original_repo = requests.get(url+'/api/v1/repos/progressadmin/target-only', headers={'Authorization': 'token '+target.api_token}, timeout=10)
                original_repo.raise_for_status()
                record('真实校验失败后容器、Compose、原密钥和两步验证全部回退')
                return
            assert key_of(target_container) == key_of(source_container)
            login(url, password, 1)
            git = source_container.exec_run(['git', 'ls-remote', url.replace('http://', 'http://progressadmin:'+password+'@')+'/progressadmin/secret-sample.git'])
            assert git.exit_code == 0, '恢复后真实私有 Git 访问失败'
            record('真实备份恢复、私有 Git 和两步验证解密通过')
            target_container.restart()
            login(health(target_container), password, 2)
            record('目标重启后两步验证仍有效')
            Path('/evidence/login.json').write_text(json.dumps({'口令': password, '源密钥': key_of(source_container)}, ensure_ascii=False))
            Path('/evidence/login.json').chmod(0o600)
    finally:
        for container in reversed(a.owned):
            container.remove(force=True, v=True)
        a.client.close()


if __name__ == '__main__':
    inside_main() if len(sys.argv) > 1 and sys.argv[1] == 'inside' else host_main()
