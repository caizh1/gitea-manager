"""从真实 HTTP API 驱动独立 Worker；所有断言以隔离实例真实状态为准。"""
import json
from pathlib import Path
import secrets
import time
import requests

state=json.loads(Path('/evidence/resources.json').read_text())
session=requests.Session();session.trust_env=False
checks=[]
base='http://manager:5000/api'


def check(name, value):
    checks.append({'检查':name,'通过':bool(value)})
    Path('/evidence/acceptance.json').write_text(json.dumps({'说明':'三个隔离 SVN 与真实增强版 Gitea 的 HTTP 迁移验收','检查':checks},ensure_ascii=False,indent=2))
    assert value,name
    print('通过：'+name,flush=True)


def call(method,path,data=None,expected=200):
    response=session.request(method,base+path,json=data,timeout=60)
    assert response.status_code==expected,(path,response.status_code,response.text[:600])
    return response.json()


def wait(path,key='status',done=('success','failed','partial','blocked','uncertain'),timeout=1200):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        result=call('GET',path)
        if result.get(key) in done:return result
        time.sleep(1)
    raise AssertionError('等待超时：'+path)


for _ in range(90):
    try:
        initial=call('GET','/session');break
    except (AssertionError,requests.RequestException):time.sleep(1)
session.headers['X-CSRF-Token']=initial['csrf_token']
login=call('POST','/login',{'username':'admin','password':state.get('manager_password_new',state['manager_password'])})
session.headers['X-CSRF-Token']=login.get('csrf_token') or call('GET','/session')['csrf_token']
if login.get('user',{}).get('must_change_password'):
    state['manager_password_new']=secrets.token_urlsafe(24)
    login=call('POST','/access/password',{'current_password':state['manager_password'],'new_password':state['manager_password_new']})
    session.headers['X-CSRF-Token']=login.get('csrf_token') or call('GET','/session')['csrf_token']
    Path('/evidence/resources.json').write_text(json.dumps(state,ensure_ascii=False,indent=2))
call('POST','/settings',{'host_ip':'127.0.0.1'})
target=call('POST','/servers',{'name':'SVN 隔离目标','host':'gitea','ssh_user':'git','gitea_port':3000,
    'gitea_url':'http://gitea:3000','api_token':state['gitea_token'],'is_local':False},201)
sources=[];repositories=[]
for label in ('a','b','c'):
    source=call('POST','/svn-migrations/sources',{'name':'来源 '+label,'slug':'fixture-'+label+'-'+secrets.token_hex(3),
        'url':'svn://source-'+label,'username':'migration','password':state['svn_password']},201)
    sources.append(source)
    layouts=[{'trunk':'trunk','branches':'branches','tags':'tags'}, {'trunk':'mainline','branches':'devlines','tags':'releases'}, {'trunk':'src','branches':'','tags':''}]
    rows=call('POST',f'/svn-migrations/sources/{source["id"]}/repositories',{'repositories':[
        {'name':'project-'+str(i),'url':'svn://source-'+label+'/project-'+str(i),'layout':layouts[i]} for i in range(3)]},201)
    repositories.extend(rows)
check('登记三个独立 SVN 来源与九个仓库',len(repositories)==9)
plan=call('POST','/svn-migrations/plans',{'name':'三个 SVN 九仓库验收','server_id':target['id'],'repositories':[{'id':r['id']} for r in repositories]},201)
call('POST',f'/svn-migrations/plans/{plan["id"]}/preflight',{},202)
preview=wait(f'/svn-migrations/plans/{plan["id"]}',done=('ready','blocked'))
check('九仓库只读预检通过',preview['status']=='ready')
check('同名 SVN 作者按来源隔离',len({a['email'] for a in preview['preview']['authors'] if a['svn_user']=='alice'})==3)
key=secrets.token_hex(16)
body={'plan_id':plan['id'],'preview_hash':preview['preview_hash'],'request_key':key,'freeze_confirmed':True,'full_read_confirmed':True}
run=call('POST','/svn-migrations/runs',body,202)
again=call('POST','/svn-migrations/runs',body,202)
check('重复请求返回同一执行任务',again['id']==run['id'])
started=wait(f'/svn-migrations/runs/{run["id"]}',done=('running','failed'))
check('工作由独立 Worker 领取',started['status']=='running')
call('DELETE',f'/servers/{target["id"]}',expected=409)
call('POST','/backups',{'source_server_id':target['id']},expected=409)
check('迁移期间阻止删除实例及停机备份',True)
result=wait(f'/svn-migrations/runs/{run["id"]}')
Path('/evidence/run.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
check('独立 Worker 完成九个仓库迁移',result['status']=='success' and result['completed']==9)
check('每个仓库全部提交经过内容和元数据校验',all(i['result']['提交数']==i['result']['已验证提交数'] and i['result']['提交数']>0 for i in result['items']))
gitea=requests.Session();gitea.trust_env=False;gitea.headers['Authorization']='token '+state['gitea_token']
for item,spec in zip(result['items'],result['snapshot']['repositories']):
    url='http://gitea:3000/api/v1/repos/'+spec['owner']+'/'+spec['name']
    repository=gitea.get(url).json()
    check('私有仓库 '+spec['owner']+'/'+spec['name'],repository['private'] is True)
    check('导入工作流未自动启用 '+spec['owner']+'/'+spec['name'],repository['has_actions'] is False)
    check('匿名拒绝读取 '+spec['owner']+'/'+spec['name'],requests.get(url).status_code in (401,403,404))
check('普通只读用户与业务授权未被自动创建',len(gitea.get('http://gitea:3000/api/v1/admin/users').json())==1)

# 在同一来源再次计划新仓库，检验后续授权不会泄漏给新的迁入仓库。
old_group=result['snapshot']['repositories'][0]['owner']
group=gitea.get('http://gitea:3000/api/v1/orgs/'+old_group).json()
created=gitea.post('http://gitea:3000/api/v1/admin/users',json={'username':'business-user','email':'business@example.test',
    'password':secrets.token_urlsafe(24),'must_change_password':False,'send_notify':False})
assert created.status_code==201,created.text
user=created.json()
group_state=gitea.get(f'http://gitea:3000/api/v1/governance/groups/{group["id"]}').json()
response=gitea.put(f'http://gitea:3000/api/v1/governance/groups/{group["id"]}/members/{user["id"]}',json={'role':20,'revision':group_state['revision']})
assert response.status_code==204,response.text
extra=call('POST',f'/svn-migrations/sources/{sources[0]["id"]}/repositories',{'repositories':[
    {'name':name,'url':'svn://source-a/'+name,'layout':{'trunk':'src','branches':'','tags':''}}
    for name in ('project-later-a','project-later-b')]},201)
later=call('POST','/svn-migrations/plans',{'name':'后续批次与重启恢复','server_id':target['id'],'repositories':[{'id':r['id']} for r in extra]},201)
call('POST',f'/svn-migrations/plans/{later["id"]}/preflight',{},202)
later=wait(f'/svn-migrations/plans/{later["id"]}',done=('ready','blocked'))
check('旧群组人工授权后后续批次选择新的私有群组',later['status']=='ready' and later['preview']['repositories'][0]['owner']!=old_group)
body={'plan_id':later['id'],'preview_hash':later['preview_hash'],'request_key':secrets.token_hex(16),'freeze_confirmed':True,'full_read_confirmed':True}
next_run=call('POST','/svn-migrations/runs',body,202)
wait(f'/svn-migrations/runs/{next_run["id"]}',done=('running','failed'))
Path('/evidence/worker-restart.request').write_text('仅重启本次隔离迁移执行器\n')
interrupted=wait(f'/svn-migrations/runs/{next_run["id"]}',done=('uncertain','success','failed'))
check('Worker 被强制终止后状态明确标为待确认',interrupted['status']=='uncertain')
call('POST',f'/svn-migrations/runs/{next_run["id"]}/reconcile',{},202)
resumed=wait(f'/svn-migrations/runs/{next_run["id"]}')
check('原快照核对并恢复后续批次成功',resumed['status']=='success' and resumed['completed']==2)
for item in resumed['items']:
    response=gitea.get('http://gitea:3000/api/v1/repos/'+item['result']['目标'].split('http://gitea:3000/')[1],auth=None)
    assert response.status_code==200

for name in ('external-current','external-history'):
    rows=call('POST',f'/svn-migrations/sources/{sources[0]["id"]}/repositories',{'repositories':[
        {'name':name,'url':'svn://source-a/'+name,'layout':{'trunk':'src','branches':'','tags':''}}]},201)
    negative=call('POST','/svn-migrations/plans',{'name':name,'server_id':target['id'],'repositories':[{'id':rows[0]['id']}]},201)
    call('POST',f'/svn-migrations/plans/{negative["id"]}/preflight',{},202)
    blocked=wait(f'/svn-migrations/plans/{negative["id"]}',done=('blocked','ready'))
    check(name+' 被预检明确阻断',blocked['status']=='blocked' and blocked['preview']['issues'][0]['code']=='externals')
duplicate=call('POST','/svn-migrations/plans',{'name':'重复目标检测','server_id':target['id'],'repositories':[{'id':repositories[3]['id']}]},201)
call('POST',f'/svn-migrations/plans/{duplicate["id"]}/preflight',{},202)
blocked=wait(f'/svn-migrations/plans/{duplicate["id"]}',done=('blocked','ready'))
check('已有目标被阻断而没有覆盖',blocked['status']=='blocked' and blocked['preview']['issues'][0]['code']=='conflict')
call('POST',f'/svn-migrations/runs/{run["id"]}/cleanup',{})
report=call('GET',f'/svn-migrations/runs/{run["id"]}/report')
check('清理成功工作区后迁移报告和提交映射仍保留',report['结果']['completed']==9 and bool(report['结果']['items'][0]['result']['提交映射']))
check('每个源修订均有提交映射或明确排除说明',all(len(item['result']['修订覆盖'])>0 and all(row['已映射路径'] or row['未生成独立提交的原因'] for row in item['result']['修订覆盖']) for item in report['结果']['items']))
Path('/evidence/acceptance.json').write_text(json.dumps({'状态':'通过','说明':'隔离 Linux 固定基准；不代表生产 SVN 或人工节省指标已验收','检查':checks},ensure_ascii=False,indent=2))
