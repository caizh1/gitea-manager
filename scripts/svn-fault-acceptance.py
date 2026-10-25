"""在指定的本任务隔离实例上注入暂停、取消、外部引用变化与来源变化。"""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import time
import requests

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('prefix')
args=parser.parse_args()
assert args.prefix.startswith('gm-svn-') and '/' not in args.prefix
root=Path(__file__).resolve().parents[1]
folder=root/'work/svn-acceptance'/args.prefix
state=json.loads((folder/'resources.json').read_text())
info=json.loads(subprocess.check_output(['docker','inspect',args.prefix+'-manager']))[0]
assert info['Config']['Labels'].get('io.gitea-manager.test')=='svn'
base='http://127.0.0.1:'+info['NetworkSettings']['Ports']['5000/tcp'][0]['HostPort']+'/api'
gitea_info=json.loads(subprocess.check_output(['docker','inspect',args.prefix+'-gitea']))[0]
gitea_base='http://127.0.0.1:'+gitea_info['NetworkSettings']['Ports']['3000/tcp'][0]['HostPort']+'/api/v1'
session=requests.Session();session.trust_env=False
git=requests.Session();git.trust_env=False;git.headers['Authorization']='token '+state['gitea_token']
checks=[]


def check(name,value):
    checks.append({'检查':name,'通过':bool(value)})
    (folder/'fault-acceptance.json').write_text(json.dumps({'检查':checks},ensure_ascii=False,indent=2))
    assert value,name
    print('通过：'+name,flush=True)


def call(method,path,data=None,status=200):
    r=session.request(method,base+path,json=data,timeout=60)
    assert r.status_code==status,(path,r.status_code,r.text[:300])
    return r.json()


def wait(run_id,statuses,timeout=1200):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        row=call('GET',f'/svn-migrations/runs/{run_id}')
        if row['status'] in statuses:return row
        time.sleep(.25)
    raise AssertionError('等待任务状态超时')


session.headers['X-CSRF-Token']=call('GET','/session')['csrf_token']
login=call('POST','/login',{'username':'admin','password':state.get('manager_password_new',state['manager_password'])})
session.headers['X-CSRF-Token']=login.get('csrf_token') or call('GET','/session')['csrf_token']
target=call('GET','/svn-migrations/targets')[0]['id']


def prepare(label,source_label,paths):
    src=call('POST','/svn-migrations/sources',{'name':'故障验收 '+label,'slug':'fault-'+label+'-'+secrets.token_hex(3),
        'url':'svn://source-'+source_label,'username':'migration','password':state['svn_password']},201)
    repos=call('POST',f'/svn-migrations/sources/{src["id"]}/repositories',{'repositories':[
        {'name':'project-'+str(i),'url':'svn://source-'+source_label+'/'+path,
         'layout':{'trunk':'src','branches':'','tags':''}} for i,path in enumerate(paths)]},201)
    return repos


def plan(repos):
    row=call('POST','/svn-migrations/plans',{'name':'故障注入','server_id':target,'repositories':[{'id':x['id']} for x in repos]},201)
    call('POST',f'/svn-migrations/plans/{row["id"]}/preflight',{},202)
    for _ in range(1200):
        row=call('GET',f'/svn-migrations/plans/{row["id"]}')
        if row['status'] in ('ready','blocked'):break
        time.sleep(.5)
    assert row['status']=='ready',row.get('error')
    return row


def start(row):
    return call('POST','/svn-migrations/runs',{'plan_id':row['id'],'preview_hash':row['preview_hash'],
        'request_key':secrets.token_hex(16),'freeze_confirmed':True,'full_read_confirmed':True},202)['id']


repos=prepare('controls','a',['project-2','project-later-a'])
run_id=start(plan(repos))
wait(run_id,('running','failed'))
# 领取任务与进入首仓转换是两个状态；必须等首仓实际开始后才测试“完成当前仓库”。
deadline=time.monotonic()+1200
while time.monotonic()<deadline:
    active=call('GET',f'/svn-migrations/runs/{run_id}')
    if active['items'][0]['status']=='running':break
    assert active['status']=='running',active['status']
    time.sleep(.1)
else:raise AssertionError('首仓未进入执行状态')
call('POST',f'/svn-migrations/runs/{run_id}/pause',{},202)
paused=wait(run_id,('paused','failed','success'))
check('暂停完成当前仓库且未启动下一仓库',paused['status']=='paused' and paused['completed']==1 and paused['items'][1]['status']=='pending')
spec=paused['snapshot']['repositories'][0]
repo_path='/repos/'+spec['owner']+'/'+spec['name']
original_refs=git.get(gitea_base+repo_path+'/branches',timeout=30).json()
response=git.post(gitea_base+repo_path+'/branches',json={'new_branch_name':'external-change','old_branch_name':'main'},timeout=30)
check('外部引用变化已注入',response.status_code==201)
call('POST',f'/svn-migrations/runs/{run_id}/resume',{},202)
changed=wait(run_id,('uncertain','failed','success'))
check('恢复发现外部引用后暂停并保留外部改动',changed['status']=='uncertain' and git.get(gitea_base+repo_path+'/branches/external-change').status_code==200)
response=git.delete(gitea_base+repo_path+'/branches/external-change',timeout=30)
assert response.status_code==204,response.text
call('POST',f'/svn-migrations/runs/{run_id}/reconcile',{},202)
wait(run_id,('running','failed'))
call('POST',f'/svn-migrations/runs/{run_id}/cancel',{},202)
cancelled=wait(run_id,('cancelled','failed','success'))
check('取消停止当前执行且保留已完成仓库',cancelled['status']=='cancelled' and cancelled['completed']==1)
call('POST',f'/svn-migrations/runs/{run_id}/resume',{},202)
resumed=wait(run_id,('success','failed','uncertain','partial'))
check('取消后核对原快照并继续成功',resumed['status']=='success' and resumed['completed']==2)
current_refs=git.get(gitea_base+repo_path+'/branches',timeout=30).json()
check('已完成仓库未被重复转换或覆盖',current_refs==original_refs)

repos=prepare('source-change','c',['project-2'])
preview=plan(repos)
subprocess.run(['docker','exec',args.prefix+'-source-c','svn','mkdir','file:///svn/project-2/src/after-preview','-m','预览后来源变化'],check=True,stdout=subprocess.DEVNULL)
run_id=start(preview)
changed=wait(run_id,('failed','partial','success'))
check('预览后来源继续提交被阻断',changed['status']=='failed' and '变化' in changed['error'])
spec=preview['preview']['repositories'][0]
check('来源变化未创建目标仓库',git.get(gitea_base+'/repos/'+spec['owner']+'/'+spec['name']).status_code==404)
preview=plan(repos)
subprocess.run(['docker','exec',args.prefix+'-source-c','svnadmin','setuuid','/svn/project-2'],check=True)
changed=wait(start(preview),('failed','partial','success'))
check('来源 UUID 改变被阻断',changed['status']=='failed' and 'UUID' in changed['error'])

# 同一物理 SVN 中登记两个独立项目，覆盖含中文和空格的项目 URL。
paths=[]
for name in ('项目一','项目二'):
    relative='project-2/projects/中文 空间/'+name
    subprocess.run(['docker','exec',args.prefix+'-source-a','svn','copy','--parents',
        'file:///svn/project-2/src','file:///svn/'+relative+'/src','-m','新增多项目验收范围'],check=True,stdout=subprocess.DEVNULL)
    paths.append(relative)
repos=prepare('multiple-projects','a',paths)
completed=wait(start(plan(repos)),('success','failed','partial','uncertain'))
check('同一 SVN 的两个独立项目含中文空格路径均迁移成功',completed['status']=='success' and completed['completed']==2)
check('两个项目保持独立仓库与完整提交校验',len({item['result']['仓库ID'] for item in completed['items']})==2 and all(item['result']['提交数']==item['result']['已验证提交数'] for item in completed['items']))
(folder/'fault-acceptance.json').write_text(json.dumps({'状态':'通过','检查':checks},ensure_ascii=False,indent=2))
