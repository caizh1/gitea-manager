#!/usr/bin/env python3
"""隔离验收目录启动器；所有账号和密钥只读取临时夹具。"""
import base64
import hashlib
import os
from pathlib import Path
import subprocess

FIXTURES = Path('/fixtures')
MODE = os.environ.get('DIRECTORY_MODE', 'openldap')
PASSWORD = os.environ['DIRECTORY_PASSWORD']
CERT_NAME = os.environ.get('CERT_NAME', 'trusted')


def command(args):
    result = subprocess.run(args)
    if result.returncode:
        raise RuntimeError(f"目录初始化命令失败，退出码 {result.returncode}；参数已隐藏。")


def openldap():
    store = Path('/var/lib/ldap')
    store.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(8)
    hashed = '{SSHA}' + base64.b64encode(hashlib.sha1(PASSWORD.encode() + salt).digest() + salt).decode()
    config = '\n'.join([
        'include /etc/ldap/schema/core.schema',
        'include /etc/ldap/schema/cosine.schema',
        'include /etc/ldap/schema/inetorgperson.schema',
        'pidfile /tmp/slapd.pid', 'argsfile /tmp/slapd.args',
        'modulepath /usr/lib/ldap', 'moduleload back_mdb',
        'TLSCACertificateFile /fixtures/ca.pem',
        f'TLSCertificateFile /fixtures/{CERT_NAME}.pem',
        f'TLSCertificateKeyFile /fixtures/{CERT_NAME}.key',
        'TLSVerifyClient never', 'database mdb', 'maxsize 1073741824',
        'suffix "dc=access,dc=test"', 'rootdn "cn=admin,dc=access,dc=test"',
        f'rootpw {hashed}', 'directory /var/lib/ldap', 'index objectClass eq',
        'index uid,mail eq', 'sizelimit 1000',
        'limits dn.exact="cn=reader,dc=access,dc=test" size=unlimited',
        'access to attrs=userPassword by anonymous auth by self write by * none',
        'access to attrs=employeeNumber by dn.exact="cn=reader,dc=access,dc=test" read by * none',
        'access to * by * read', '',
    ])
    Path('/tmp/slapd.conf').write_text(config)
    if not (store / 'data.mdb').exists():
        command(['slapadd', '-f', '/tmp/slapd.conf', '-l', '/fixtures/seed.ldif'])
    os.execvp('slapd', ['slapd', '-d', '0', '-f', '/tmp/slapd.conf', '-h', 'ldap://0.0.0.0:389 ldaps://0.0.0.0:636'])


def samba():
    cfg = Path('/etc/samba/smb.conf')
    if not Path('/var/lib/samba/private/sam.ldb').exists():
        command(['samba-tool', 'domain', 'provision', '--realm=ACCESS.TEST', '--domain=ACCESS',
                 '--server-role=dc', '--dns-backend=SAMBA_INTERNAL', '--host-name=dc1',
                 '--adminpass=' + PASSWORD, '--use-rfc2307',
                 '--option=acl_xattr:security_acl_name=user.NTACL'])
        text = cfg.read_text()
        text = text.replace('[global]', '[global]\n\tldap server require strong auth = no\n\ttls enabled = yes\n\ttls ca file = /fixtures/ca.pem\n\ttls certfile = /fixtures/trusted.pem\n\ttls keyfile = /fixtures/trusted.key')
        cfg.write_text(text)
        # 使用 Samba 自身接口批量建立真实 AD 用户，保留 AD schema 和 objectGUID。
        from samba.auth import system_session
        from samba.param import LoadParm
        from samba.samdb import SamDB
        lp = LoadParm()
        lp.load(str(cfg))
        sam = SamDB(url='/var/lib/samba/private/sam.ldb', session_info=system_session(), lp=lp)
        for index in range(1501):
            name = f'user{index:04d}'
            sam.newuser(name, PASSWORD, givenname='测试', surname=str(index), mailaddress=name + '@access.test')
        sam.newgroup('department-a')
        sam.add_remove_group_members('department-a', ['user0000', 'user0001'], add_members_operation=True)
        sam.disable_account('(sAMAccountName=user0002)')
        print('真实 AD 目录已创建 1501 个验收账号。', flush=True)
    os.execvp('samba', ['samba', '-i', '--no-process-group'])


if MODE == 'samba':
    samba()
else:
    openldap()
