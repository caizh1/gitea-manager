#!/usr/bin/env python3
"""真实目录与组织权限验收环境：创建、执行、停止均限制到本脚本的项目标记。"""
import argparse
import base64
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'work' / 'access-acceptance'
ENV = STATE / '.env'
COMPOSE = ROOT / 'compose.access-test.yml'


def run(args, capture=True, check=True, **kwargs):
    result = subprocess.run(args, cwd=ROOT, capture_output=capture, text=True, **kwargs)
    if check and result.returncode:
        detail = (result.stderr or '')[-2500:]
        if ENV.exists():
            for key, value in values().items():
                if 'PASSWORD' in key or 'TOKEN' in key:
                    detail = detail.replace(value, '[已隐藏]')
        raise RuntimeError('命令执行失败：' + str(args[0]) + '\n' + detail)
    return result.stdout if capture else result.returncode


def values():
    return dict(line.split('=', 1) for line in ENV.read_text().splitlines() if '=' in line and not line.startswith('#'))


def reserve_ports(number):
    sockets = []
    try:
        for _ in range(number):
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def free_subnet():
    ids = run(['docker', 'network', 'ls', '-q']).split()
    existing = json.loads(run(['docker', 'network', 'inspect', *ids])) if ids else []
    used = [ipaddress.ip_network(c['Subnet']) for n in existing for c in n.get('IPAM', {}).get('Config', []) if c.get('Subnet')]
    for index in range(1, 255):
        candidate = ipaddress.ip_network(f'10.242.{index}.0/24')
        if all(candidate.version != net.version or not candidate.overlaps(net) for net in used):
            return str(candidate)
    raise RuntimeError('未找到独立验收子网。')


def write_secrets(path, text):
    path.write_text(text)
    path.chmod(0o600)


def prepare():
    STATE.mkdir(parents=True, exist_ok=True)
    if ENV.exists():
        return values()
    ports = reserve_ports(6)
    config = {'ACCESS_PROJECT': 'gitea-access-' + secrets.token_hex(5), 'ACCESS_STATE': str(STATE), 'ACCESS_SUBNET': free_subnet(),
              'DIRECTORY_IMAGE': 'gitea-manager-access-directory:20261011',
              'GITEA_IMAGE': 'gitea/gitea@sha256:ebe815a4e594bb9a180dc7b3a7946e11cb7389f8ae2daa2beb31287416c94849',
              'ENHANCED_IMAGE': 'sha256:01d658c9c852636c3a048035f1d97ca2f42f86e60a9ddd119608b0582b28bdad',
              'DIRECTORY_PASSWORD': 'Aa1!' + secrets.token_urlsafe(24),
              'GITEA_PASSWORD': 'Aa1!' + secrets.token_urlsafe(24)}
    config.update(zip(['LDAP_PORT', 'LDAPS_PORT', 'AD_PORT', 'AD_LDAPS_PORT', 'GITEA_PORT', 'ENHANCED_PORT'], map(str, ports)))
    write_secrets(ENV, '# 独立验收凭据；禁止提交或打包。\n' + '\n'.join(k + '=' + v for k, v in config.items()) + '\n')
    fixtures(config)
    return config


def fixtures(config):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    target = STATE / 'fixtures'
    target.mkdir(exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Access Acceptance CA')])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(days=1))
          .not_valid_after(now+datetime.timedelta(days=365)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .sign(ca_key, hashes.SHA256()))
    (target/'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (target/'ca-bundle.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    for label in ['trusted', 'expired', 'wrong-host', 'untrusted']:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        before = now-datetime.timedelta(days=10)
        after = now-datetime.timedelta(days=1) if label == 'expired' else now+datetime.timedelta(days=180)
        hosts = ['unrelated.invalid'] if label == 'wrong-host' else ['openldap', 'samba', 'dc1', 'dc1.access.test', 'localhost']
        alt = [x509.DNSName(host) for host in hosts]
        if label != 'wrong-host':
            alt.append(x509.IPAddress(ipaddress.ip_address('127.0.0.1')))
        signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if label == 'untrusted' else ca_key
        cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
                .issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(before).not_valid_after(after).add_extension(x509.SubjectAlternativeName(alt), critical=False)
                .sign(signing_key, hashes.SHA256()))
        (target/(label+'.pem')).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (target/(label+'.key')).write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
        (target/(label+'.key')).chmod(0o600)
    pwd = config['DIRECTORY_PASSWORD']
    encoded_pwd = base64.b64encode(pwd.encode()).decode()
    records = ['dn: dc=access,dc=test\nobjectClass: domain\ndc: access',
               'dn: ou=people,dc=access,dc=test\nobjectClass: organizationalUnit\nou: people',
               'dn: ou=groups,dc=access,dc=test\nobjectClass: organizationalUnit\nou: groups',
               'dn: cn=reader,dc=access,dc=test\nobjectClass: organizationalRole\nobjectClass: simpleSecurityObject\ncn: reader\nuserPassword:: '+encoded_pwd]
    for index in range(1501):
        uid = f'user{index:04d}'
        given = base64.b64encode('测试'.encode()).decode()
        records.append(f'dn: uid={uid},ou=people,dc=access,dc=test\nobjectClass: inetOrgPerson\nuid: {uid}\ncn: {uid}\nsn: {index}\ngivenName:: {given}\nmail: {uid}@access.test\nemployeeNumber: {uid}\ndescription: '+('disabled' if index == 2 else 'enabled')+f'\nuserPassword:: {encoded_pwd}')
    records.append('dn: cn=department-a,ou=groups,dc=access,dc=test\nobjectClass: groupOfNames\ncn: department-a\nmember: uid=user0000,ou=people,dc=access,dc=test\nmember: uid=user0001,ou=people,dc=access,dc=test')
    write_secrets(target/'seed.ldif', '\n\n'.join(records)+'\n')


def compose(*args, **kwargs):
    config = values()
    if not config['ACCESS_PROJECT'].startswith('gitea-access-') or config['ACCESS_STATE'] != str(STATE):
        raise RuntimeError('项目标记不匹配，拒绝操作。')
    return run(['docker', 'compose', '-p', config['ACCESS_PROJECT'], '--env-file', str(ENV), '-f', str(COMPOSE), *args], **kwargs)


def verify_owned():
    config = values()
    ids = compose('ps', '-aq').split()
    for cid in ids:
        labels = json.loads(run(['docker', 'inspect', '--format', '{{json .Config.Labels}}', cid]))
        if labels.get('gitea-manager.access-test') != config['ACCESS_PROJECT']:
            raise RuntimeError('容器所有权标记不匹配，拒绝操作。')


def wait_http(port):
    for _ in range(180):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/v1/version', timeout=2) as res:
                if res.status == 200:
                    return
        except Exception:
            time.sleep(1)
    raise RuntimeError('Gitea 启动未就绪。')


def gitea_cli(service, *args):
    return compose('exec', '-T', '--user', 'git', service, 'gitea', '--config', '/data/gitea/conf/app.ini', *args)


def initialize():
    config = values()
    for service, port_key in [('gitea', 'GITEA_PORT'), ('enhanced', 'ENHANCED_PORT')]:
        wait_http(config[port_key])
        if 'access-admin' not in gitea_cli(service, 'admin', 'user', 'list'):
            gitea_cli(service, 'admin', 'user', 'create', '--username', 'access-admin', '--password', config['GITEA_PASSWORD'], '--email', 'admin@access.test', '--admin', '--must-change-password=false')
        token_key = service.upper()+'_TOKEN'
        if token_key not in config:
            token = gitea_cli(service, 'admin', 'user', 'generate-access-token', '--username', 'access-admin', '--token-name', 'access-test', '--scopes', 'all', '--raw').strip().splitlines()[-1]
            with ENV.open('a') as handle:
                handle.write(token_key+'='+token+'\n')
            config[token_key] = token
        for org in ['department-a', 'department-b']:
            request = urllib.request.Request(f'http://127.0.0.1:{config[port_key]}/api/v1/orgs/{org}', headers={'Authorization': 'token '+config[token_key]})
            try:
                urllib.request.urlopen(request, timeout=10).close()
                continue
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
            payload = json.dumps({'username': org, 'full_name': '部门甲' if org.endswith('a') else '部门乙', 'visibility': 'private'}).encode()
            request = urllib.request.Request(f'http://127.0.0.1:{config[port_key]}/api/v1/orgs', data=payload, headers={'Authorization':'token '+config[token_key], 'Content-Type':'application/json'})
            urllib.request.urlopen(request, timeout=10).close()
    records = []
    for service in ['openldap', 'samba', 'gitea', 'enhanced']:
        cid = compose('ps', '-q', service).strip()
        info = json.loads(run(['docker', 'inspect', cid]))[0]
        records.append({'服务':service, '镜像摘要':info['Image'], '项目':config['ACCESS_PROJECT']})
    (STATE/'images.json').write_text(json.dumps(records, ensure_ascii=False, indent=2)+'\n')
    print('隔离环境已初始化；真实 AD 数据加载状态请查看 status。凭据只保存在 work/access-acceptance/.env。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['up', 'init', 'test', 'status', 'down'])
    options = parser.parse_args()
    if options.action == 'up':
        prepare()
        image_name = values()['DIRECTORY_IMAGE']
        if image_name.startswith('sha256:'):
            run(['docker', 'image', 'inspect', image_name])
        else:
            run(['docker', 'build', '--platform', 'linux/amd64', '-t', image_name, 'deployment/access-test'], capture=False)
            image_id = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image_name]).strip()
            write_secrets(ENV, ENV.read_text().replace('DIRECTORY_IMAGE='+image_name+'\n', 'DIRECTORY_IMAGE='+image_id+'\n'))
        compose('up', '-d', capture=False)
        initialize()
    else:
        if not ENV.exists():
            raise RuntimeError('尚未建立隔离验收环境。')
        verify_owned()
        if options.action == 'init':
            initialize()
        elif options.action == 'test':
            run([sys.executable, str(ROOT/'scripts/access-ldap-acceptance.py')], capture=False)
            run([sys.executable, str(ROOT/'scripts/access-ldap-attributes.py')], capture=False)
        elif options.action == 'status':
            print(compose('ps'))
        else:
            if (STATE/'manager/runtime.json').exists():
                run([sys.executable, str(ROOT/'scripts/access-manager-env.py'), 'down'], capture=False)
            compose('down', capture=False)
            print('仅停止并移除本项目容器和网络；目录卷及验收证据保留。')


if __name__ == '__main__':
    main()
