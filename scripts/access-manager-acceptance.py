#!/usr/bin/env python3
"""通过真实 HTTPS、双 Worker、Gitea 与目录服务验证组织隔离；只允许隔离项目。"""
import base64
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import secrets
import time
import warnings
warnings.filterwarnings('ignore', message='urllib3 v2 only supports OpenSSL')
import requests

spec = importlib.util.spec_from_file_location('access_env',Path(__file__).with_name('access-env.py'))
env = importlib.util.module_from_spec(spec); spec.loader.exec_module(env)
CONFIG = env.values()
RUNTIME = json.loads((env.STATE/'manager/runtime.json').read_text())
BASE = 'https://localhost:'+str(RUNTIME['port'])
CA = str(env.STATE/'fixtures/ca.pem')
CONTAINER = CONFIG['ACCESS_PROJECT']+'-manager-backend'
RESULTS = []
RUN = secrets.token_hex(4)


def direct_request(method,url,**kwargs):
    with requests.Session() as session:
        session.trust_env=False
        return session.request(method,url,**kwargs)


def record(name, passed, detail=None):
    RESULTS.append({'用例':name,'结果':'通过' if passed else '失败','证据':detail})
    (env.STATE/'manager-results.json').write_text(json.dumps(RESULTS,ensure_ascii=False,indent=2)+'\n')
    print(('通过：' if passed else '失败：')+name,flush=True)
    return passed


class Client:
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = CA
        self.session.headers['Origin'] = BASE
        response = self.request('GET','/session')
        self.csrf(response)

    def csrf(self,response):
        data = response.json()
        if data.get('csrf_token'):
            self.session.headers['X-CSRF-Token'] = data['csrf_token']

    def request(self,method,path,data=None,**kwargs):
        return self.session.request(method,BASE+'/api'+path,json=data,timeout=40,**kwargs)

    def login(self,username='admin',source='local',password=None):
        response = self.request('POST','/login',{'username':username,'source':source,'password':password or (RUNTIME['password'] if source=='local' else CONFIG['DIRECTORY_PASSWORD'])})
        self.csrf(response)
        if response.status_code != 200:
            raise RuntimeError('验收登录失败：'+str(response.status_code)+' '+response.text[:200])
        return response.json()

    def org(self,binding):
        self.session.headers['X-Organization-ID'] = str(binding)
        return self

    def expect(self,name,method,path,expected=200,data=None,**kwargs):
        response = self.request(method,path,data,**kwargs)
        codes = expected if isinstance(expected,tuple) else (expected,)
        record(name,response.status_code in codes,{'状态码':response.status_code,'预期':codes,
            '错误':response.json().get('error') if response.headers.get('Content-Type','').startswith('application/json') and isinstance(response.json(),dict) else None})
        return response


def gitea(service,method,path,data=None,auth=None):
    port = CONFIG['GITEA_PORT' if service=='gitea' else 'ENHANCED_PORT']
    response = direct_request(method,f'http://127.0.0.1:{port}/api/v1'+path,json=data,auth=auth,
        headers={} if auth else {'Authorization':'token '+CONFIG[service.upper()+'_TOKEN']},timeout=30)
    if response.status_code not in (200,201,202,204):
        raise RuntimeError('Gitea 夹具操作失败：'+method+' '+path+' '+str(response.status_code)+' '+response.text[:200])
    return response.json() if response.content else None


def fixture_python(code,payload=None):
    info = json.loads(env.run(['docker','inspect',CONTAINER]))[0]
    if not CONTAINER.startswith(CONFIG['ACCESS_PROJECT']+'-') or not info['State']['Running']:
        raise RuntimeError('Manager 隔离容器身份不符。')
    raw = env.run(['docker','exec','-i','-e','DISABLE_BACKGROUND_TASKS=1',CONTAINER,'python','-c',code],input=json.dumps(payload or {}))
    return json.loads(raw.strip().splitlines()[-1])


def initialize_servers():
    payload=[{'name':'官方目录验收','url':'http://gitea:3000','token':CONFIG['GITEA_TOKEN'],'container':CONFIG['ACCESS_PROJECT']+'-gitea-1'},
             {'name':'增强目录验收','url':'http://enhanced:3000','token':CONFIG['ENHANCED_TOKEN'],'container':CONFIG['ACCESS_PROJECT']+'-enhanced-1'}]
    code='''import sys,json,os
from app import create_app
from models import db,GiteaServer
app=create_app()
with app.app_context():
 result=[]
 for value in json.load(sys.stdin):
  server=GiteaServer.query.filter_by(gitea_url=value['url']).first()
  if server is None:
   server=GiteaServer(name=value['name'],host=value['url'].split('//')[1].split(':')[0],ssh_user='git',gitea_port=3000,gitea_url=value['url'],api_token=value['token'],status='online',version='1.26.4',instance_identity='access-isolated-'+value['name'])
   db.session.add(server)
  server.is_local=True;server.gitea_container=value['container']
  db.session.flush();result.append({'id':server.id,'name':server.name})
 db.session.commit();print(json.dumps(result))'''
    return fixture_python(code,payload)


def age_session(account_id):
    code='''import sqlite3,json,sys,time,os
p=json.load(sys.stdin);c=sqlite3.connect('/access-data/gitea-manager.db')
c.execute('UPDATE access_sessions SET directory_checked_at=? WHERE account_id=? AND revoked_at IS NULL',(time.time()-301,p['account_id']));c.commit();print(json.dumps({'调整':'仅将测试会话目录检查时间推进到五分钟后'}))'''
    return fixture_python(code,{'account_id':account_id})


def approval_and_review(admin,A,B,a,b,target,foreign):
    policies=[]
    for client,binding,repo in [(A,a,target),(B,b,foreign)]:
        teams=gitea('gitea','GET','/orgs/'+binding['organization_name']+'/teams')
        owner=next(row for row in teams if row['name']=='Owners')
        rule={'name':'部门审批'+RUN,'branches':['main'],'total':1,'groups':[{'team_id':owner['id'],'required':1}],'reset_on_push':True}
        policy=client.expect('创建本组织审批规则','POST','/approval-rules/policies',201,data={'server_id':binding['server_id'],'repo_id':repo['id'],'rule':rule}).json()
        policies.append(policy)
    visible=A.expect('本组织审批规则列表','GET','/approval-rules/policies').json()['items']
    record('审批规则列表不包含其他组织',policies[0]['id'] in [r['id'] for r in visible] and policies[1]['id'] not in [r['id'] for r in visible])
    A.expect('越权创建他组织审批规则拒绝','POST','/approval-rules/policies',404,data={'server_id':a['server_id'],'repo_id':foreign['id'],'rule':policies[0]['rule']})
    for method,suffix,data in [('PUT','',{'rule':policies[1]['rule'],'revision':policies[1]['revision']}),('DELETE','',None),('GET','/audit',None),('GET','/status/1',None),('POST','/preview',{}),('POST','/enable',{})]:
        A.expect('替换他组织审批规则 '+method+suffix,method,'/approval-rules/policies/'+str(policies[1]['id'])+suffix,404,data=data)
    A.expect('本组织审批规则编辑','PUT','/approval-rules/policies/'+str(policies[0]['id']),data={'rule':policies[0]['rule'] | {'name':'更新部门审批'+RUN},'revision':policies[0]['revision']})
    audit=A.expect('本组织审批审计','GET','/approval-rules/policies/'+str(policies[0]['id'])+'/audit').json()
    person=A.request('GET','/session').json()['user']
    expected_actor=(person['display_name'] or person['username'])+'（账号 '+str(person['id'])+'）'
    record('审批审计记录实际个人身份',bool(audit.get('items')) and all(row['actor']==expected_actor for row in audit['items']),{'操作者':[row['actor'] for row in audit.get('items',[])]})
    A.expect('生产审批强制启用保持阻断','POST','/approval-rules/policies/'+str(policies[0]['id'])+'/enable',409,data={})
    A.expect('组织管理员不能导出组件安装包','POST','/approval-rules/policies/'+str(policies[0]['id'])+'/bundle',403,data={})
    bot='access-bot-'+RUN
    password=secrets.token_urlsafe(24)+'-Aa1'
    gitea('gitea','POST','/admin/users',{'username':bot,'email':bot+'@access.test','password':password,'must_change_password':False})
    token=gitea('gitea','POST','/users/'+bot+'/tokens',{'name':'隔离验收','scopes':['read:user','write:repository','write:issue']},auth=(bot,password))['sha1']
    reviews=[]
    for repo in [target,foreign]:
        gitea('gitea','PUT','/repos/'+repo['full_name']+'/collaborators/'+bot,{'permission':'read'})
        review=admin.expect('平台接入真实只读机器人仓库','POST','/ai-review/repositories',201,data={'server_id':a['server_id'],'repo_name':repo['full_name'],'bot_token':token,'enabled':False}).json()
        reviews.append(review)
    visible=A.expect('AI 仓库列表按组织过滤','GET','/ai-review/repositories').json()
    record('AI 列表不含他组织仓库和秘密',reviews[0]['id'] in [r['id'] for r in visible] and reviews[1]['id'] not in [r['id'] for r in visible] and all('bot_token' not in r and 'webhook_secret' not in r for r in visible))
    own='/ai-review/repositories/'+str(reviews[0]['id'])
    other='/ai-review/repositories/'+str(reviews[1]['id'])
    A.expect('组织管理员可启用已接入仓库','PUT',own,data={'enabled':True,'revision':reviews[0]['revision']})
    A.expect('组织管理员不能修改机器人令牌','PUT',own,403,data={'enabled':False,'revision':reviews[0]['revision']+1,'bot_token':'无效令牌'})
    A.expect('替换 AI 仓库开关 ID 拒绝','PUT',other,404,data={'enabled':True,'revision':reviews[1]['revision']})
    for suffix in ['/dataset','/dataset/export','/dataset/missing/history']:
        A.expect('替换 AI 仓库样本 ID '+suffix,'GET',other+suffix,404)
    A.expect('本组织样本列表读取','GET',own+'/dataset')
    A.expect('本组织业务样本导出','GET',own+'/dataset/export')
    A.expect('组织管理员不能安装 Webhook','POST',own+'/webhook',403,data={'manager_url':BASE})
    A.expect('组织管理员不能新增机器人接入','POST','/ai-review/repositories',403,data={'server_id':a['server_id'],'repo_name':target['full_name']})
    view=A.expect('组织管理员 AI 状态视图','GET','/ai-review/config').json()
    record('AI 状态视图不含模型凭据',view.get('config')=={'enabled':False} and view.get('health')=={})
    A.expect('他组织任务列表查询拒绝','GET','/ai-review/tasks?repository='+str(reviews[1]['id']),404)
    A.expect('原始审查诊断禁止组织管理员','GET','/ai-review/tasks/missing/raw-diagnostics',403)


def alert_isolation(A,B,binding,repo,template):
    preview=A.expect('提交告警回归策略预览','POST','/repository-policies/batches',202,data={'template_id':template['id'],'content_hash':template['content_hash'],'repo_ids':[repo['id']]}).json()
    for _ in range(45):
        batch=A.request('GET','/repository-policies/batches/'+str(preview['id'])).json().get('batch',{})
        if batch.get('status')=='ready':break
        time.sleep(1)
    record('告警回归预览完成',batch.get('status')=='ready')
    # 从真实 Gitea 变更预览依赖，后台应用必须识别变化并形成原有失败告警。
    gitea('gitea','PATCH','/repos/'+repo['full_name']+'/branch_protections/main',{'enable_push':False})
    A.expect('提交过期预览等待后台拒绝','POST','/repository-policies/batches/'+str(preview['id'])+'/apply',202,data={})
    for _ in range(45):
        batch=A.request('GET','/repository-policies/batches/'+str(preview['id'])).json().get('batch',{})
        if batch.get('status') not in ('apply_queued','applying'):break
        time.sleep(1)
    record('真实后台拒绝已变化仓库配置',batch.get('status')=='completed_with_errors',{'状态':batch.get('status')})
    alerts=A.expect('本组织告警列表','GET','/alerts').json()
    own=[row for row in alerts if row['source_id']==preview['id']]
    record('真实策略失败生成本组织告警',len(own)==1)
    if own:
        aid=own[0]['id']
        foreign=B.expect('其他组织告警列表','GET','/alerts').json()
        summary=B.expect('其他组织告警摘要','GET','/alerts/summary').json()
        record('告警列表与摘要不泄露其他组织',aid not in [r['id'] for r in foreign] and (summary.get('latest_alert') or {}).get('id')!=aid)
        B.expect('其他组织不能清除告警','POST','/alerts/'+str(aid)+'/clear',404,data={})
        A.expect('本组织可以清除告警','POST','/alerts/'+str(aid)+'/clear',data={})
    gitea('gitea','PATCH','/repos/'+repo['full_name']+'/branch_protections/main',{'enable_push':True})


def run():
    env.verify_owned()
    anon=Client()
    anon.expect('未登录不能读取实例','GET','/servers',401)
    no_csrf=direct_request('POST',BASE+'/api/login',json={'password':RUNTIME['password']},verify=CA,timeout=10)
    record('登录缺少 CSRF 被拒绝',no_csrf.status_code==403,{'状态码':no_csrf.status_code})
    admin=Client();session=admin.login()
    record('本地应急管理员正常登录',session['user']['is_platform_admin'] and not session['user']['must_change_password'])
    cookie_headers=admin.request('GET','/session').headers
    record('会话响应禁止缓存',cookie_headers.get('Cache-Control')=='no-store')
    admin.expect('配置隔离实例地址','POST','/settings',data={'host_ip':'127.0.0.1'})
    servers=initialize_servers()
    directory={'name':'隔离 OpenLDAP','type':'binddn','enabled':True,'host':'openldap','port':636,
        'security_protocol':'ldaps','skip_verify':False,'bind_dn':'cn=reader,dc=access,dc=test',
        'bind_password':CONFIG['DIRECTORY_PASSWORD'],'user_base':'ou=people,dc=access,dc=test','user_dn':'',
        'filter':'(uid=%s)','attribute_username':'uid','attribute_name':'givenName','attribute_surname':'sn','attribute_mail':'mail',
        'attributes_in_bind':False,'groups_enabled':False,'group_dn':'','group_filter':'','group_member_uid':'','user_uid':'',
        'paged_search_enabled':True,'paged_search_size':400,'ca_cert':Path(CA).read_text()}
    admin.expect('LDAP 设置保存','PUT','/access/directory',data=directory)
    stored=admin.expect('LDAP 设置刷新回读','GET','/access/directory').json()
    record('LDAP Bind Password 不回显',stored.get('bind_password_set') is True and 'bind_password' not in stored)
    admin.expect('真实 LDAPS 连接测试','POST','/access/directory/test-connection',data={'configuration':{}})
    admin.expect('真实 LDAP 用户测试','POST','/access/directory/test-login',data={'username':'user0000','password':CONFIG['DIRECTORY_PASSWORD'],'configuration':{}})
    all_users=admin.expect('LDAP HTTP 分页查询','GET','/access/directory/users').json()
    record('HTTP 查询 1501 用户无遗漏',len(all_users.get('items',[]))==1501,{'数量':len(all_users.get('items',[]))})
    accounts={};clients={}
    for name in ['user0000','user0001','user0003','user0004']:
        client=Client();state=client.login(name,'ldap');clients[name]=client;accounts[name]=state['user']['id']
        if name=='user0004':
            record('未授权用户仅有登录身份',state['organizations']==[] and state['capabilities']==[])
            client.expect('未授权用户不能访问业务','GET','/commit-rules',403)
    bindings={}
    repos={}
    for i,service in enumerate(['gitea','enhanced']):
        items=admin.expect(service+' 同步真实组织','POST','/access/organizations',data={'server_id':servers[i]['id']}).json()['items']
        for org in ['department-a','department-b']:
            bindings[(service,org)]=next(item for item in items if item['organization_name']==org)
            repo=gitea(service,'POST','/orgs/'+org+'/repos',{'name':'access-'+RUN,'auto_init':True,'default_branch':'main','private':True,'readme':'Default'})
            repos[(service,org)]=repo
            if org=='department-b':
                gitea(service,'POST','/repos/'+repo['full_name']+'/contents/sample.txt',{'content':base64.b64encode('部门乙测试数据\n'.encode()).decode(),'message':'部门乙验收提交','author':{'name':'department-b-author-'+RUN,'email':'dept-b@access.test'}})
    a=bindings[('gitea','department-a')];b=bindings[('gitea','department-b')];other=bindings[('enhanced','department-b')]
    for name,binding in [('user0000',a),('user0001',b),('user0003',a),('user0003',other)]:
        admin.expect(name+' 授予 '+str(binding['binding_id']),'POST','/access/grants',data={'account_id':accounts[name],'binding_id':binding['binding_id']})
    A=clients['user0000'].org(a['binding_id']);B=clients['user0001'].org(b['binding_id'])
    multiple=clients['user0003'].request('GET','/session').json()
    record('一个账号可以管理两个实例组织',len(multiple['organizations'])==2)
    actual=A.expect('组织管理员实例列表','GET','/servers').json()
    record('实例列表没有凭据与其他实例',len(actual)==1 and set(actual[0])<= {'id','name','version'} and actual[0]['id']==a['server_id'])
    A.expect('同名跨实例不能越权','GET','/repository-policies/repositories?server_id='+str(other['server_id']),404)
    A.expect('不能选择他人组织','GET','/commit-rules',404,headers={'X-Organization-ID':str(b['binding_id'])})
    for path in ['/settings','/access/accounts','/backups','/restore-tasks','/schedules','/mirrors','/servers/'+str(a['server_id']),'/ai-review/setup','/approval-rules/native-governance']:
        A.expect('平台功能隔离 '+path,'GET',path,403)
    A.expect('跨站请求拒绝','GET','/commit-rules',403,headers={'Origin':'https://unrelated.invalid'})
    rules=[]
    templates=[]
    for client,binding in [(A,a),(B,b)]:
        rule=client.expect('创建本组织提交规则','POST','/commit-rules',201,data={'server_id':binding['server_id'],'name':'组织规则'+RUN,'pattern':'^feat:'}).json();rules.append(rule)
        template=client.expect('创建本组织仓库策略','POST','/repository-policies/templates',201,data={'server_id':binding['server_id'],'name':'组织策略'+RUN,'organization':{'id':binding['organization_id'],'name':binding['organization_name'],'server_id':binding['server_id']},'rules':[{'rule_name':'main','enable_push':True}]}).json();templates.append(template)
    visible=A.expect('提交规则列表按组织过滤','GET','/commit-rules').json()['rules']
    record('列表不含其他组织规则',rules[0]['id'] in [r['id'] for r in visible] and rules[1]['id'] not in [r['id'] for r in visible])
    for method in ['PUT','DELETE']:
        A.expect('替换提交规则 ID '+method,method,'/commit-rules/'+str(rules[1]['id']),404,data={'name':'越权修改','pattern':'.*'})
    A.expect('替换策略模板 ID','DELETE','/repository-policies/templates/'+str(templates[1]['id']),404)
    target=repos[('gitea','department-a')];foreign=repos[('gitea','department-b')]
    A.expect('批量夹带其他组织仓库整批拒绝','POST','/repository-policies/batches',404,data={'template_id':templates[0]['id'],'content_hash':templates[0]['content_hash'],'repo_ids':[target['id'],foreign['id']]})
    A.expect('门禁批量夹带其他组织仓库拒绝','POST','/commit-gates/apply',404,data={'server_id':a['server_id'],'rule_id':rules[0]['id'],'repo_names':[target['full_name'],foreign['full_name']]})
    A.expect('直接读取他组织仓库保护拒绝','GET','/repository-policies/repositories/'+str(foreign['id'])+'/rules?server_id='+str(a['server_id']),404)
    preview=A.expect('提交本组织真实策略预览','POST','/repository-policies/batches',202,data={'template_id':templates[0]['id'],'content_hash':templates[0]['content_hash'],'repo_ids':[target['id']]}).json()
    for _ in range(45):
        state=A.request('GET','/repository-policies/batches/'+str(preview['id'])).json()
        batch=state.get('batch',state)
        if batch.get('status') not in ('pending','previewing','preview_queued','running'):
            break
        time.sleep(1)
    record('真实后台策略预览完成',batch.get('status')=='ready',{'状态':batch.get('status')})
    B.expect('其他组织不能读取批次详情','GET','/repository-policies/batches/'+str(preview['id']),404)
    if batch.get('status')=='ready':
        A.expect('应用本组织真实策略','POST','/repository-policies/batches/'+str(preview['id'])+'/apply',202,data={})
        for _ in range(45):
            state=A.request('GET','/repository-policies/batches/'+str(preview['id'])).json();batch=state.get('batch',state)
            if batch.get('status') not in ('apply_queued','applying','running'):
                break
            time.sleep(1)
        record('后台应用完成并记录个人身份',batch.get('status')=='completed' and '共享管理员' not in batch.get('actor',''),{'状态':batch.get('status'),'操作者':batch.get('actor')})
        actual=gitea('gitea','GET','/repos/'+target['full_name']+'/branch_protections')
        record('Gitea 回读保护规则',any(r['rule_name']=='main' for r in actual))
    # 仓库统计直接读取真实 Gitea 提交和文件数据，不注入统计结果。
    for client,binding in [(A,a),(B,b)]:
        before=client.request('GET','/statistics/'+str(binding['server_id'])+'/overview').json()
        record('未采集时不展示实例总数',before.get('ready') or before.get('total_commits') is None)
        client.expect('按组织启动真实统计采集','POST','/statistics/'+str(binding['server_id'])+'/refresh',202,data={})
        for _ in range(50):
            stats=client.request('GET','/statistics/'+str(binding['server_id'])+'/overview').json()
            if stats.get('ready'):break
            time.sleep(1)
        record('组织统计真实采集完成',stats.get('ready') is True,{'统计':stats})
        ranking=client.request('GET','/statistics/'+str(binding['server_id'])+'/repos').json()
        record('统计排行只有当前组织',isinstance(ranking,list) and all(row['repo_name'].startswith(binding['organization_name']+'/') for row in ranking))
    A.expect('其他组织作者详情不泄露','GET','/statistics/'+str(a['server_id'])+'/authors/department-b-author-'+RUN,404)
    approval_and_review(admin,A,B,a,b,target,foreign)
    alert_isolation(A,B,a,target,templates[0])
    # 删除授权后以多条独立 TCP 请求覆盖两个服务 Worker。
    admin.expect('撤销组织管理授权','DELETE',f"/access/grants/{accounts['user0000']}/{a['binding_id']}")
    cookie=dict(A.session.cookies);headers=dict(A.session.headers)
    def denied(_):
        response=direct_request('GET',BASE+'/api/commit-rules',headers=headers,cookies=cookie,verify=CA,timeout=20)
        return response.status_code
    with ThreadPoolExecutor(max_workers=8) as executor:
        codes=list(executor.map(denied,range(32)))
    record('撤权后并发 32 次请求全部拒绝',all(code==404 for code in codes),{'状态分布':{str(c):codes.count(c) for c in set(codes)}})
    admin.expect('恢复后续验收授权','POST','/access/grants',data={'account_id':accounts['user0000'],'binding_id':a['binding_id']})
    # 真实转移仓库之后，原组织不能继续读取原仓库或批次，新组织也不继承旧批次。
    target=gitea('gitea','PATCH','/repos/'+target['full_name'],{'name':'moved-'+target['name']})
    moved=gitea('gitea','POST','/repos/'+target['full_name']+'/transfer',{'new_owner':'department-b'})
    A.expect('仓库转移后旧组织读取拒绝','GET','/repository-policies/repositories/'+str(target['id'])+'/rules?server_id='+str(a['server_id']),404)
    A.expect('仓库转移后旧批次封存','GET','/repository-policies/batches/'+str(preview['id']),404)
    B.expect('新组织不继承旧组织批次','GET','/repository-policies/batches/'+str(preview['id']),404)
    B.expect('新组织可以读取转入仓库','GET','/repository-policies/repositories/'+str(target['id'])+'/rules?server_id='+str(b['server_id'])+'&org_id='+str(b['organization_id'])+'&org='+b['organization_name'])
    # 切换真实 AD；切换配置会失效所有旧目录会话。
    ad=directory | {'name':'隔离 AD','host':'samba','bind_dn':'Administrator@ACCESS.TEST','user_base':'CN=Users,DC=access,DC=test','filter':'(sAMAccountName=%s)','attribute_username':'sAMAccountName'}
    admin.expect('切换真实 AD 设置','PUT','/access/directory',data=ad)
    A.expect('目录配置变更使旧会话失效','GET','/commit-rules',401)
    ad_client=Client();ad_state=ad_client.login('user0001','ldap');ad_id=ad_state['user']['id']
    admin.expect('AD 账号授权','POST','/access/grants',data={'account_id':ad_id,'binding_id':a['binding_id']})
    ad_client.org(a['binding_id']).expect('AD 账号已有会话可以访问','GET','/commit-rules')
    env.compose('exec','-T','samba','samba-tool','user','disable','user0001')
    try:
        age_session(ad_id)
        ad_client.expect('AD 禁用后五分钟复核撤销会话','GET','/commit-rules',401)
    finally:
        env.compose('exec','-T','samba','samba-tool','user','enable','user0001')
    admin.expect('恢复 OpenLDAP 供浏览器验收','PUT','/access/directory',data=directory)
    result={'服务器':servers,'组织':list(bindings.values()),'仓库':[{'id':r['id'],'name':r['full_name']} for r in repos.values()], '账号':accounts}
    (env.STATE/'manager-fixtures.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result


def main():
    try:
        run()
    except Exception as exc:
        message=str(exc)
        for secret in [RUNTIME['password'],RUNTIME['session_key'],CONFIG['DIRECTORY_PASSWORD'],CONFIG['GITEA_PASSWORD']]:
            message=message.replace(secret,'[已隐藏]')
        record('验收执行异常',False,{'错误':message[:1000]})
        raise
    finally:
        lines=['# Manager 真实 HTTP 隔离验收','',f'执行 {len(RESULTS)} 项，通过 {sum(r["结果"]=="通过" for r in RESULTS)} 项。','',
            '入口为可信测试 CA 签发的 HTTPS；两个 Gunicorn Worker；真实 OpenLDAP、Samba AD 和两台 Gitea。','',
            'AD 五分钟检查通过只修改隔离测试会话的检查时间触发，其余认证、目录账号状态和请求链路均为真实执行。','',
            '| 用例 | 结果 |','|---|---|']
        lines += ['| '+r['用例']+' | '+r['结果']+' |' for r in RESULTS]
        (env.STATE/'manager-report.md').write_text('\n'.join(lines)+'\n')
    if any(r['结果']=='失败' for r in RESULTS):raise SystemExit(1)


if __name__=='__main__':main()
