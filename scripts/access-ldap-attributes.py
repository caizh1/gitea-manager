#!/usr/bin/env python3
"""通过真实 Gitea 首次网页登录，比较目录属性及读取身份，而不是预创建账号的既有属性。"""
import importlib.util
import json
from pathlib import Path
import secrets
import warnings
warnings.filterwarnings('ignore', message='urllib3 v2 only supports OpenSSL')
import requests

spec=importlib.util.spec_from_file_location('ldap',Path(__file__).with_name('access-ldap-acceptance.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
results=[]
for context in [False,True]:
    uid='attrs'+secrets.token_hex(5)
    login='lookup'+secrets.token_hex(5)
    password=secrets.token_urlsafe(24)+'-Aa1'
    cfg=m.base(filter='(&(cn=%s)(uid='+uid+'))',attribute_name='employeeNumber',attributes_in_bind=context)
    m.probe(cfg,action='attribute_fixture_create',uid=uid,username=login,user_password=password)
    try:
        manager=m.probe(cfg,username=login,password=password)['result']
        expected={'姓名':manager['display_name'],'邮箱':manager['email'],'用户名':manager['username']}
        for service in ['gitea','enhanced']:
            m.source(service,cfg)
            port=m.CONFIG['GITEA_PORT' if service=='gitea' else 'ENHANCED_PORT']
            session=requests.Session();session.trust_env=False
            url='http://127.0.0.1:'+port
            session.get(url+'/user/login',timeout=20)
            response=session.post(url+'/user/login',data={'user_name':login,'password':password},allow_redirects=False,timeout=60)
            status,body=m.api(service,'GET','/users/'+uid)
            user=json.loads(body)
            actual={'姓名':user.get('full_name'),'邮箱':user.get('email'),'用户名':user.get('login')}
            passed=response.status_code==303 and status==200 and actual==expected
            results.append({'用例':service+' 首次登录属性对照（BindDN上下文='+str(context)+'）','结果':'通过' if passed else '失败','证据':{'Manager':expected,'Gitea':actual,'登录状态码':response.status_code,'跳转':response.headers.get('Location'),'账号查询':status}})
            print(json.dumps(results[-1],ensure_ascii=False),flush=True)
    finally:
        m.probe(cfg,action='attribute_fixture_delete',uid=uid)
        (m.env.STATE/'ldap-attribute-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
if len(results)!=4 or any(r['结果']=='失败' for r in results):raise SystemExit(1)
