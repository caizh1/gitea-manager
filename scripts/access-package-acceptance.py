#!/usr/bin/env python3
"""从最终安装包验收 HTTPS、目录密钥与重复安装；仅新建独立桌面测试项目。"""
import argparse
import hashlib
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import tarfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            value.update(block)
    return value.hexdigest()


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError('隔离命令失败：' + str(command[0]) + '，退出码 ' + str(result.returncode))
    return result.stdout


def ports():
    with socket.socket() as one, socket.socket() as two:
        one.bind(('127.0.0.1', 0)); two.bind(('127.0.0.1', 0))
        return one.getsockname()[1], two.getsockname()[1]


def free_subnets():
    ids = run(['docker', 'network', 'ls', '-q']).split()
    networks = json.loads(run(['docker', 'network', 'inspect', *ids]))
    used = [ipaddress.ip_network(value['Subnet']) for network in networks
            for value in (network.get('IPAM', {}).get('Config') or []) if value.get('Subnet')]
    values = [str(n) for n in ipaddress.ip_network('10.252.224.0/20').subnets(new_prefix=28)
              if not any(other.version == 4 and n.overlaps(other) for other in used)]
    if len(values) < 2:
        raise RuntimeError('没有可用的独立验收网段，未清理既有网络')
    return values[:2]


class Client:
    def __init__(self, base, ca):
        self.base, self.csrf = base, ''
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(ca))))
        self.request('GET', '/session')

    def request(self, method, path, value=None, expected=200, csrf=True):
        headers = {'Content-Type': 'application/json', 'Origin': self.base}
        if csrf:
            headers['X-CSRF-Token'] = self.csrf
        request = urllib.request.Request(self.base + '/api' + path, method=method, headers=headers,
            data=json.dumps(value).encode() if value is not None else None)
        try:
            response = self.opener.open(request, timeout=35)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            payload = json.load(response)
            if response.code != expected:
                raise RuntimeError(f'最终包接口 {method} {path} 返回 {response.code}，预期 {expected}')
        if isinstance(payload, dict) and payload.get('csrf_token'):
            self.csrf = payload['csrf_token']
        return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installer', required=True, type=Path)
    parser.add_argument('--directory-state', type=Path, default=ROOT/'work/access-acceptance')
    args = parser.parse_args()
    archive = args.installer.resolve()
    expected = Path(str(archive) + '.sha256').read_text().split()[0]
    if digest(archive) != expected:
        raise RuntimeError('最终安装包摘要不一致')
    folder = ROOT/'work'/('access-package-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3))
    folder.mkdir(mode=0o700)
    installation = folder/'installation'; installation.mkdir()
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            if not (member.isfile() or member.isdir()) or not (installation/member.name).resolve().is_relative_to(installation):
                raise RuntimeError('安装包包含非普通文件或越界路径')
        bundle.extractall(installation)
    certificate, private_key = folder/'cert.pem', folder/'key.pem'
    run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2','-subj','/CN=localhost',
         '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1','-keyout',str(private_key),'-out',str(certificate)])
    private_key.chmod(0o600)
    http_port, tls_port = ports()
    command = ['bash', str(installation/'install.sh'), '--desktop-test', '--host', '127.0.0.1',
               '--port', str(http_port), '--tls-port', str(tls_port), '--tls-cert', str(certificate), '--tls-key', str(private_key)]
    report = {'说明':'使用最终包和独立新建项目；桌面模式只跳过宿主 APT 与 systemd 配置，不代表真实 amd64 宿主验收。',
              '安装包SHA256':expected,'结果':[]}
    def record(name, detail):
        report['结果'].append({'场景':name,'结果':'通过','证据':detail})
        (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(name+'：通过',flush=True)
    state_path = installation/'state/installation.json'
    project = None
    try:
        subnets = free_subnets()
        with (folder/'first-install.log').open('w') as log:
            process = subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
            deadline = time.monotonic()+120
            while not state_path.exists() and process.poll() is None and time.monotonic()<deadline:
                time.sleep(.1)
            if not state_path.exists():
                raise RuntimeError('一键安装未生成状态，原日志已保留')
            state = json.loads(state_path.read_text()); project=state['project']
            # 测试机保留大量旧现场；只给本次项目预留网段，不改变安装包配置。
            for name,subnet in zip(('default','review-execution'),subnets):
                run(['docker','network','create',*(['--internal'] if name=='review-execution' else []),
                     '--subnet',subnet,'--label','com.docker.compose.project='+project,
                     '--label','com.docker.compose.network='+name,project+'_'+name])
            if process.wait(timeout=600):
                raise RuntimeError('最终包首次安装失败，原日志已保留')
        state=json.loads(state_path.read_text())
        report.update(项目=project,安装目录=str(installation),HTTPS入口=state['url'])
        compose=['docker','compose','-p',project,'-f',str(installation/'compose.install.json')]
        def services():
            ids=run(['docker','ps','-q','--filter','label=com.docker.compose.project='+project]).split()
            return {r['Config']['Labels']['com.docker.compose.service']:r for r in json.loads(run(['docker','inspect',*ids]))}
        active=services()
        assert len(active)==7 and state['runtime']['ready']
        assert all(m['Destination'] not in ('/app','/usr/share/nginx/html') for s in active.values() for m in s['Mounts'])
        record('最终包七服务与执行器',{'服务':sorted(active),'无源码挂载':True,'执行器版本':state['runtime']['version']})
        admin=Client(state['url'],certificate)
        admin.request('POST','/login',{'password':state['admin_password']},expected=403,csrf=False)
        session=admin.request('POST','/login',{'password':state['admin_password']})
        assert session['authenticated'] and session['user']['is_platform_admin']
        record('真实HTTPS登录与CSRF',{'证书校验':True,'无CSRF拒绝':True})
        values=dict(line.split('=',1) for line in (args.directory_state/'.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
        network=values['ACCESS_PROJECT']+'_default'
        backend=active['backend']['Id']
        run(['docker','network','connect',network,backend])
        config={'name':'最终包隔离目录','type':'binddn','enabled':True,'host':'openldap','port':636,
                'security_protocol':'ldaps','bind_dn':'cn=reader,dc=access,dc=test','bind_password':values['DIRECTORY_PASSWORD'],
                'user_base':'ou=people,dc=access,dc=test','filter':'(uid=%s)','attribute_username':'uid',
                'attribute_name':'givenName','attribute_surname':'sn','attribute_mail':'mail',
                'ca_cert':(args.directory_state/'fixtures/ca.pem').read_text()}
        saved=admin.request('PUT','/access/directory',config)
        assert saved['bind_password_set'] and 'bind_password' not in saved
        admin.request('POST','/access/directory/test-connection',{})
        ldap=Client(state['url'],certificate)
        logged=ldap.request('POST','/login',{'source':'ldap','username':'user0000','password':values['DIRECTORY_PASSWORD']})
        assert logged['authenticated'] and not logged['capabilities'] and not logged['organizations']
        record('最终包真实LDAPS认证',{'稳定账号ID':logged['user']['id'],'未授权账号无管理能力':True,'目录密码不回显':True})
        inspect_code="""import hashlib,json,sqlite3
from pathlib import Path
from cryptography.fernet import Fernet
p=Path('/app/data/access.key'); c=sqlite3.connect('/app/data/gitea-manager.db')
rows=c.execute('select password_ciphertext from access_directory_sources').fetchall()
assert rows and all(Fernet(p.read_bytes()).decrypt(row[0].encode()) for row in rows)
print(json.dumps({'密钥摘要':hashlib.sha256(p.read_bytes()).hexdigest(),'账号数':c.execute('select count(*) from access_accounts').fetchone()[0],'目录密文':hashlib.sha256(rows[0][0].encode()).hexdigest()}))
"""
        before=json.loads(run(['docker','exec',backend,'python','-c',inspect_code]))
        private_files={str(p.relative_to(installation)):digest(p) for p in (installation/'state').rglob('*')
                       if p.is_file() and p.suffix in ('.key','.token','.pem')}
        run(compose+['up','-d','--no-build','--pull','never','--force-recreate','backend'])
        with (folder/'repeat-install.log').open('w') as log:
            result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=600)
        assert result.returncode==0,'重复安装失败，已保留日志'
        backend=services()['backend']['Id']
        assert before==json.loads(run(['docker','exec',backend,'python','-c',inspect_code]))
        assert all(digest(installation/path)==value for path,value in private_files.items())
        run(['docker','network','connect',network,backend])
        ldap=Client(state['url'],certificate)
        again=ldap.request('POST','/login',{'source':'ldap','username':'user0000','password':values['DIRECTORY_PASSWORD']})
        assert again['user']['id']==logged['user']['id']
        record('容器重建与重复安装保留目录身份和密钥',{'持久数据摘要':before,'TLS和服务密钥保持':True})
        report['状态']='全部通过'
    except Exception as exc:
        report.update(状态='失败，保留首次现场',首次失败=str(exc))
        raise
    finally:
        (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print('最终包验收证据：'+str(folder/'report.json'),flush=True)
        # 保留本次项目供整包升级回退验收，不删除卷、凭据及首次失败现场。


if __name__=='__main__':
    main()
