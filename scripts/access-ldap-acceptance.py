#!/usr/bin/env python3
"""真实 OpenLDAP、Samba AD 与两版 Gitea 的 LDAP 差分验收。"""
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location('access_env', Path(__file__).with_name('access-env.py'))
env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(env)
CONFIG = env.values()
RECORDS = []
AUTH_SOURCES = {}


def probe(config, action='authenticate', **kwargs):
    payload = {'config': config, 'action': action, 'username': 'user0000', 'password': CONFIG['DIRECTORY_PASSWORD']} | kwargs
    raw = env.compose('exec', '-T', 'probe', 'python3', '/acceptance/access-ldap-probe.py', input=json.dumps(payload))
    return json.loads(raw.strip().splitlines()[-1])


def base(directory='openldap', **updates):
    cfg = {'name': '隔离目录', 'type': 'binddn', 'enabled': True, 'host': directory, 'port': 636,
           'security_protocol': 'ldaps', 'bind_dn': 'cn=reader,dc=access,dc=test',
           'bind_password': CONFIG['DIRECTORY_PASSWORD'], 'user_base': 'ou=people,dc=access,dc=test',
           'filter': '(uid=%s)', 'attribute_username': 'uid', 'attribute_name': 'givenName',
           'attribute_surname': 'sn', 'attribute_mail': 'mail',
           'ca_cert': (env.STATE/'fixtures/ca.pem').read_text()}
    if directory == 'samba':
        cfg.update(bind_dn='Administrator@ACCESS.TEST', user_base='CN=Users,DC=access,DC=test',
                   filter='(sAMAccountName=%s)', attribute_username='sAMAccountName')
    return cfg | updates


def record(name, passed, detail):
    RECORDS.append({'用例': name, '结果': '通过' if passed else '失败', '证据': detail})
    print(('通过：' if passed else '失败：')+name, flush=True)
    (env.STATE/'ldap-results.json').write_text(json.dumps(RECORDS, ensure_ascii=False, indent=2)+'\n')


def api(service, method, path, data=None):
    port = CONFIG['GITEA_PORT' if service == 'gitea' else 'ENHANCED_PORT']
    req = urllib.request.Request(f'http://127.0.0.1:{port}/api/v1'+path, method=method,
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={'Authorization':'token '+CONFIG[service.upper()+'_TOKEN'], 'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def source(service, cfg):
    direct = cfg.get('type') == 'simple'
    kind = 'ldap-simple' if direct else 'ldap'
    name = 'access-'+secrets.token_hex(5)
    cmd = ['admin', 'auth', 'add-'+kind]
    cmd += ['--name', name, '--active', '--host', cfg['host'], '--port', str(cfg['port']),
            '--security-protocol', {'ldaps':'LDAPS', 'starttls':'StartTLS', 'unencrypted':'Unencrypted'}[cfg['security_protocol']],
            '--skip-tls-verify='+str(cfg.get('skip_verify', False)).lower(),
            '--user-search-base', cfg.get('user_base',''), '--user-filter', cfg['filter'],
            '--username-attribute', cfg.get('attribute_username',''), '--firstname-attribute', cfg.get('attribute_name',''),
            '--surname-attribute', cfg.get('attribute_surname',''), '--email-attribute', cfg.get('attribute_mail','')]
    if not direct:
        cmd += ['--enable-groups='+str(cfg.get('groups_enabled', False)).lower(),
            '--group-search-base-dn', cfg.get('group_dn',''), '--group-member-attribute', cfg.get('group_member_uid',''),
            '--group-user-attribute', cfg.get('user_uid',''), '--group-filter', cfg.get('group_filter','')]
    if direct:
        cmd += ['--user-dn', cfg['user_dn']]
    else:
        cmd += ['--bind-dn', cfg.get('bind_dn',''), '--bind-password', cfg.get('bind_password',''),
                '--attributes-in-bind='+str(cfg.get('attributes_in_bind', False)).lower(),
                '--page-size', str(cfg.get('paged_search_size',500) if cfg.get('paged_search_enabled') else 0)]
    env.gitea_cli(service, *cmd)
    listing = env.gitea_cli(service, 'admin', 'auth', 'list')
    return int(re.search(r'(?m)^(\d+)\s+'+name+r'\s',listing).group(1))


def gitea_login(service, cfg, username, password):
    source_id = source(service, cfg)
    # 每用例显式绑定来源，避免已有增强版来源编辑缺陷或其他来源回退污染认证结果。
    account = 'ldapcase'+secrets.token_hex(5)
    status, body = api(service, 'POST', '/admin/users', {'username':account, 'email':account+'@access.test',
        'source_id': source_id, 'login_name':username, 'password':CONFIG['GITEA_PASSWORD'], 'must_change_password':False})
    if status != 201:
        raise RuntimeError('创建独立目录对照账号失败：'+str(status)+' '+body.decode()[:300])
    import base64
    port = CONFIG['GITEA_PORT' if service == 'gitea' else 'ENHANCED_PORT']
    token = base64.b64encode((account+':'+password).encode()).decode()
    req = urllib.request.Request(f'http://127.0.0.1:{port}/api/v1/user', headers={'Authorization':'Basic '+token})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return {'ok': response.status == 200, 'status': response.status, 'user': json.load(response).get('login')}
    except urllib.error.HTTPError as exc:
        return {'ok': False, 'status': exc.code}


def compare(name, cfg, expected=True, username='user0000', password=None):
    password = CONFIG['DIRECTORY_PASSWORD'] if password is None else password
    result = probe(cfg, username=username, password=password)
    peers = {service:gitea_login(service,cfg,username,password) for service in ['gitea','enhanced']}
    passed = result['ok'] is expected and all(p['ok'] is expected for p in peers.values())
    detail = {'预期': expected, 'Manager':result, 'Gitea':peers}
    record(name, passed, detail)
    return result


def wait_directories():
    for directory in ['openldap','samba']:
        started = time.monotonic()
        while time.monotonic()-started < 600:
            result = probe(base(directory))
            if result['ok']:
                break
            time.sleep(2)
        else:
            raise RuntimeError(directory+' 真实服务未就绪：'+result.get('message',''))


def certificates():
    try:
        for label in ['expired','wrong-host','untrusted','trusted']:
            os.environ['OPENLDAP_CERT'] = label
            env.compose('up','-d','--no-deps','--force-recreate','openldap')
            for _ in range(30):
                ready = probe(base(security_protocol='unencrypted',port=389))
                if ready['ok']:
                    break
                time.sleep(1)
            for protocol in ['ldaps','starttls']:
                cfg = base(security_protocol=protocol,port=636 if protocol=='ldaps' else 389)
                compare('证书 '+label+' '+protocol+' 正常校验',cfg,label=='trusted')
                compare('证书 '+label+' '+protocol+' SkipVerify',cfg | {'skip_verify':True},True)
    finally:
        os.environ.pop('OPENLDAP_CERT',None)
        env.compose('up','-d','--no-deps','openldap')


def identities():
    result = probe(base(),action='identity_lifecycle')
    record('OpenLDAP 改名稳定及同名重建新身份',result.get('ok') and all(result['result'].values()),result)
    cfg = base('samba')
    before = probe(cfg,username='user1499')
    env.compose('exec','-T','samba','samba-tool','user','rename','user1499','--samaccountname=renamed1499')
    try:
        renamed = probe(cfg,username='renamed1499')
    finally:
        env.compose('exec','-T','samba','samba-tool','user','rename','renamed1499','--samaccountname=user1499')
    env.compose('exec','-T','samba','samba-tool','user','delete','user1499')
    env.compose('exec','-T','samba','samba-tool','user','create','user1499',CONFIG['DIRECTORY_PASSWORD'],'--given-name=测试','--surname=1499','--mail-address=user1499@access.test')
    rebuilt = probe(cfg,username='user1499')
    passed = before['ok'] and renamed['ok'] and rebuilt['ok'] and before['result']['subject']==renamed['result']['subject'] and before['result']['subject']!=rebuilt['result']['subject']
    record('AD 改名稳定及同名重建新身份',passed,{'原身份':before,'改名':renamed,'重建':rebuilt})


def account_expiry():
    cfg = base('samba')
    before = probe(cfg,username='user1500')
    env.compose('exec','-T','samba','samba-tool','user','setexpiry','user1500','--days=-1')
    try:
        compare('AD 账号已过期拒绝密码登录',cfg,False,username='user1500')
        result = probe(cfg,action='revalidate',username='user1500',subject=before['result']['subject'])
        record('AD 账号已过期拒绝已有会话复核',not result['ok'],result)
    finally:
        env.compose('exec','-T','samba','samba-tool','user','setexpiry','user1500','--noexpiry')


def main():
    env.verify_owned()
    wait_directories()
    for directory in ['openldap','samba']:
        for protocol in ['unencrypted','ldaps','starttls']:
            cfg = base(directory, security_protocol=protocol, port=636 if protocol=='ldaps' else 389)
            compare(directory+' BindDN '+protocol,cfg)
            if directory == 'openldap':
                compare('OpenLDAP 匿名查询 '+protocol,cfg | {'bind_dn':'','bind_password':''})
            dn = 'uid=%s,ou=people,dc=access,dc=test' if directory == 'openldap' else '%s@ACCESS.TEST'
            compare(directory+' Simple auth '+protocol,cfg | {'type':'simple','user_dn':dn})
        compare(directory+' 错误密码',base(directory),False,password='错误密码')
        compare(directory+' 空密码',base(directory),False,password='')
        compare(directory+' 不存在用户',base(directory),False,username='user9999')
        multi = '(|(uid=%s)(uid=user0001))' if directory=='openldap' else '(|(sAMAccountName=%s)(sAMAccountName=user0001))'
        compare(directory+' 多条用户命中拒绝',base(directory,filter=multi),False)
        compare(directory+' 注入字符',base(directory),False,username='user0000*)(uid=*)')
        compare(directory+' 重复占位符与邮箱登录',base(directory,filter='(|('+('uid' if directory=='openldap' else 'sAMAccountName')+'=%[1]s)(mail=%[1]s))'),username='user0000@access.test')
        compare(directory+' 用户名属性留空',base(directory,attribute_username=''))
        group = {'groups_enabled':True,'group_dn':'ou=groups,dc=access,dc=test' if directory=='openldap' else 'CN=Users,DC=access,DC=test',
                 'group_filter':'cn=department-a','group_member_uid':'member','user_uid':'dn'}
        compare(directory+' 群组允许',base(directory,**group))
        compare(directory+' 群组拒绝',base(directory,**group),False,username='user0003')
        compare(directory+' 空群组过滤器不限制登录',base(directory,**(group | {'group_filter':''})),username='user0003')
        paging = base(directory,paged_search_enabled=True,paged_search_size=400)
        if directory == 'samba':
            paging['filter'] = '(&(objectClass=user)(sAMAccountName=user*)(sAMAccountName=%s))'
        result = probe(paging, action='search_users')
        record(directory+' 分页精确覆盖有效测试用户',result.get('ok') and result['result']['count']==(1500 if directory=='samba' else 1501) and result['result']['subjects']==(1500 if directory=='samba' else 1501),result)
    compare('OpenLDAP BindDN 上下文私有属性',base(attribute_name='employeeNumber',attributes_in_bind=True))
    result = compare('OpenLDAP 用户上下文无法读取私有属性',base(attribute_name='employeeNumber',attributes_in_bind=False))
    record('属性读取上下文实际生效',result.get('ok') and result['result']['display_name']=='0',{'实际显示名称':result.get('result',{}).get('display_name')})
    compare('OpenLDAP 禁用过滤器',base(filter='(&(uid=%s)(description=enabled))'),False,username='user0002')
    compare('AD 禁用账号',base('samba'),False,username='user0002')
    disabled = probe(base('samba'),action='revalidate_disabled')
    record('AD 禁用账号既有会话复核拒绝',not disabled['ok'],disabled)
    paging = probe(base(bind_dn='',bind_password=''),action='search_users')
    record('OpenLDAP 未分页超过服务端上限时拒绝部分结果',not paging['ok'],paging)
    identities()
    account_expiry()
    certificates()
    try:
        env.compose('stop','openldap')
        compare('目录中断拒绝登录',base(),False)
    finally:
        env.compose('start','openldap')
    for _ in range(30):
        if probe(base())['ok']:
            break
        time.sleep(1)
    compare('目录恢复后正常登录',base())
    lines = ['# 真实目录差分验收报告','',f'共 {len(RECORDS)} 项，通过 {sum(r["结果"]=="通过" for r in RECORDS)} 项。','',
             '真实环境：OpenLDAP、Samba AD DC、官方 Gitea 1.26.4、当前增强版镜像。各目录 1501 个账号；不代表公司目录验收。','',
             '| 用例 | 结果 |','|---|---|']
    lines += ['| '+r['用例']+' | '+r['结果']+' |' for r in RECORDS]
    (env.STATE/'ldap-report.md').write_text('\n'.join(lines)+'\n')
    if any(r['结果']=='失败' for r in RECORDS):
        raise SystemExit(1)


if __name__=='__main__':
    main()
