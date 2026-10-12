#!/usr/bin/env python3
"""HTTPS 会话、停用、撤销和 simple auth 到期的真实 HTTP 回归。"""
import importlib.util
import json
from pathlib import Path
import time

spec=importlib.util.spec_from_file_location('manager',Path(__file__).with_name('access-manager-acceptance.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
RESULTS=[]


def record(name,passed,detail=None):
    RESULTS.append({'用例':name,'结果':'通过' if passed else '失败','证据':detail})
    (m.env.STATE/'session-results.json').write_text(json.dumps(RESULTS,ensure_ascii=False,indent=2)+'\n')
    print(('通过：' if passed else '失败：')+name,flush=True)


m.record=record


def expire(account,field):
    if field not in ('last_seen_at','expires_at','directory_checked_at'):
        raise ValueError('测试时间字段不允许')
    code='''import sys,json,sqlite3,time
p=json.load(sys.stdin);connection=sqlite3.connect('/access-data/gitea-manager.db')
connection.execute('UPDATE access_sessions SET '+p['field']+'=? WHERE account_id=? AND revoked_at IS NULL',(time.time()-p['elapsed'],p['account']));connection.commit();print('{}')'''
    m.fixture_python(code,{'account':account,'field':field,'elapsed':301 if field=='directory_checked_at' else (1801 if field=='last_seen_at' else 1)})


def run():
    admin=m.Client();admin.login()
    bindings=admin.request('GET','/access/organizations').json()['items']
    binding=next(b for b in bindings if b['organization_name']=='department-a' and b['active'])
    source=admin.request('GET','/access/directory').json()
    source['bind_password']=m.CONFIG['DIRECTORY_PASSWORD']
    user=m.Client()
    response=user.request('POST','/login',{'source':'ldap','username':'user0010','password':m.CONFIG['DIRECTORY_PASSWORD']});user.csrf(response)
    if response.status_code!=200:raise RuntimeError('测试账号登录失败 '+str(response.status_code))
    identity=response.json()['user']['id']
    lifetime=m.fixture_python("import sys,json,sqlite3;p=json.load(sys.stdin);c=sqlite3.connect('/access-data/gitea-manager.db');v=c.execute('SELECT expires_at-created_at FROM access_sessions WHERE account_id=? ORDER BY created_at DESC LIMIT 1',(p['account'],)).fetchone()[0];print(json.dumps(v))",{'account':identity})
    record('数据库会话默认绝对期限八小时',lifetime==28800,{'秒数':lifetime})
    cookie=response.headers.get('Set-Cookie','')
    record('HTTPS Cookie 属性齐全',all(text in cookie for text in ['Secure','HttpOnly','SameSite=Lax']))
    admin.expect('会话测试账号授予组织','POST','/access/grants',data={'account_id':identity,'binding_id':binding['binding_id']})
    user.org(binding['binding_id']).expect('有效会话可以读取业务','GET','/commit-rules')
    fake=m.Client();fake.session.cookies.clear()
    for key,value in user.session.cookies.items():
        prefix,signature=value.rsplit('.',1)
        altered=prefix+'.'+('a' if signature[0]!='a' else 'b')+signature[1:]
        fake.session.cookies.set(key,altered)
    fake.org(binding['binding_id']).expect('篡改签名 Cookie 被拒绝','GET','/commit-rules',401)
    user.expect('写请求缺少 CSRF 被拒绝','POST','/commit-gates/test',403,data={'pattern':'.*','message':'测试'},headers={'X-CSRF-Token':''})
    admin.expect('平台停用目录账号','PATCH','/access/accounts/'+str(identity),data={'enabled':False})
    user.expect('停用账号后当前会话立即失效','GET','/commit-rules',401)
    admin.expect('恢复隔离测试账号','PATCH','/access/accounts/'+str(identity),data={'enabled':True})
    for field,name in [('last_seen_at','闲置三十分钟过期'),('expires_at','最长八小时期限过期')]:
        user=m.Client();user.login('user0010','ldap');user.org(binding['binding_id'])
        expire(identity,field)
        user.expect(name,'GET','/commit-rules',401)
    user=m.Client();user.login('user0010','ldap');user.org(binding['binding_id'])
    admin.expect('平台主动撤销会话','POST','/access/sessions/revoke',data={'account_id':identity})
    user.expect('会话撤销在下一次请求生效','GET','/commit-rules',401)
    user=m.Client();user.login('user0010','ldap');user.org(binding['binding_id'])
    user.expect('本人退出登录','POST','/logout',data={})
    user.expect('退出后业务请求拒绝','GET','/commit-rules',401)
    ldap_spec=importlib.util.spec_from_file_location('ldap',Path(__file__).with_name('access-ldap-acceptance.py'))
    ldap=importlib.util.module_from_spec(ldap_spec);ldap_spec.loader.exec_module(ldap)
    ldap.probe(source,action='session_fixture_rename')
    try:
        renamed=m.Client();same=renamed.login('renamed0010','ldap')
        record('真实目录改名保持管理账号与授权',same['user']['id']==identity and any(b['binding_id']==binding['binding_id'] for b in same['organizations']))
        renamed.org(binding['binding_id']).expect('改名账号仍可管理原组织','GET','/commit-rules')
    finally:
        ldap.probe(source,action='session_fixture_rename',restore=True)
    previous=m.Client();previous.login('user0010','ldap');previous.org(binding['binding_id'])
    ldap.probe(source,action='session_fixture_recreate')
    rebuilt=m.Client();new=rebuilt.login('user0010','ldap')
    record('同名删除重建不继承旧管理账号授权',new['user']['id']!=identity and new['organizations']==[] and new['capabilities']==[])
    rebuilt.expect('同名新账号仍需手动授权','GET','/commit-rules',403)
    expire(identity,'directory_checked_at')
    previous.expect('旧身份会话复核不得继承同名新账号','GET','/commit-rules',401)
    identity=new['user']['id']
    admin.expect('手动授予重建测试账号组织','POST','/access/grants',data={'account_id':identity,'binding_id':binding['binding_id']})
    # 仅调整隔离记录的时间戳，目录认证和后续请求没有替换实现。
    try:
        simple=source | {'type':'simple','user_dn':'uid=%s,ou=people,dc=access,dc=test','bind_dn':'','bind_password':''}
        admin.expect('保存无后台凭据 Simple auth','PUT','/access/directory',data=simple)
        user=m.Client();user.login('user0010','ldap');user.org(binding['binding_id'])
        user.expect('Simple auth 当次认证有效','GET','/commit-rules')
        expire(identity,'directory_checked_at')
        user.expect('Simple auth 五分钟后必须重新认证','GET','/commit-rules',401)
    finally:
        admin.expect('恢复 BindDN 目录设置','PUT','/access/directory',data=source)
    http='http://localhost:'+str(m.RUNTIME['http_port'])
    import requests
    session=requests.Session();session.trust_env=False
    bootstrap=session.get(http+'/api/session',timeout=10).json()
    response=session.post(http+'/api/login',json={'source':'ldap','username':'user0010','password':m.CONFIG['DIRECTORY_PASSWORD']},headers={'X-CSRF-Token':bootstrap['csrf_token'],'Origin':http},timeout=10)
    record('真实 HTTP 入口禁止 LDAP 登录',response.status_code==403,{'状态码':response.status_code})


if __name__=='__main__':
    try:
        run()
    except Exception as exc:
        record('验收执行异常',False,{'错误':str(exc)})
        raise
    finally:
        lines=['# 真实会话验收','',f'执行 {len(RESULTS)} 项，通过 {sum(r["结果"]=="通过" for r in RESULTS)} 项。','',
            '闲置、绝对到期与目录有效期通过推进隔离会话记录时间触发，不伪造目录返回或替换鉴权逻辑。','','| 用例 | 结果 |','|---|---|']
        lines+=['| '+r['用例']+' | '+r['结果']+' |' for r in RESULTS]
        (m.env.STATE/'session-report.md').write_text('\n'.join(lines)+'\n')
    if any(r['结果']=='失败' for r in RESULTS):raise SystemExit(1)
