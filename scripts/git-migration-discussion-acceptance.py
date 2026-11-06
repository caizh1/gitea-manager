"""仅在已有标记隔离实例创建新样本，验证非行内未解决讨论的迁移门禁。"""
import json
import secrets
import subprocess
import time
from pathlib import Path

import requests

import argparse
parser=argparse.ArgumentParser(description=__doc__)
for key in ('source-state','manager-state','target-state','output'):parser.add_argument('--'+key,type=Path,required=True)
a=parser.parse_args()
ROOT=Path(__file__).resolve().parents[1]
OUT=a.output;OUT.mkdir(parents=True,exist_ok=True)
source=json.loads(a.source_state.read_text())
manager=json.loads(a.manager_state.read_text())
target=json.loads(a.target_state.read_text())
for state, name, label in [(source,source['name'],'git-migration-source'),(target,target['name'],'git-migration-receiver'),(manager,manager['name']+'-backend','git-migration-manager')]:
    info = json.loads(subprocess.check_output(['docker','inspect',name]))[0]
    assert info['Config']['Labels']['io.gitea-manager.test'] == label

def session():
    value=requests.Session();value.trust_env=False;return value

src, dst, mgr = session(), session(), session()
token=src.post(source['base']+'/oauth/token',data={'grant_type':'password','username':'root','password':source['password'],'scope':'api'},timeout=60)
assert token.status_code==200
src.headers['Authorization']='Bearer '+token.json()['access_token']
dst.headers['Authorization']='token '+target['token']

def api(client, base, method, path, body=None, raw=False):
    response=client.request(method,base+path,json=body,timeout=90)
    if raw:return response
    if not 200<=response.status_code<300:raise RuntimeError('隔离接口失败：'+method+' '+path+' HTTP '+str(response.status_code))
    return response.json() if response.content else None

def s(method,path,body=None,raw=False):return api(src,source['base']+'/api/v4',method,path,body,raw)
def t(method,path,body=None,raw=False):return api(dst,target['base']+'/api/v1',method,path,body,raw)
def m(method,path,body=None,raw=False):return api(mgr,manager['base']+'/api',method,path,body,raw)

report={'说明':'本轮独立新增的真实样本；不修改既有验收项目，不连接生产环境','步骤':[]}
def save(stage,**values):
    report.update(values);report['步骤'].append(stage)
    (OUT/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(stage,flush=True)

previous=json.loads((OUT/'initial-fixture-preparation.json').read_text()) if (OUT/'initial-fixture-preparation.json').exists() else None
if previous:
    project=s('GET','/projects/'+str(previous['来源项目']));name=project['path']
    base='/projects/'+str(project['id'])
    pull=s('GET',base+'/merge_requests/'+str(previous['来源请求']))
    discussion=s('GET',base+'/merge_requests/'+str(pull['iid'])+'/discussions/'+previous['原始讨论']['id'])
else:
    name='review-discussion-'+secrets.token_hex(4)
    project=s('POST','/projects',{'name':name,'visibility':'private','initialize_with_readme':True,'default_branch':'main','only_allow_merge_if_all_discussions_are_resolved':True})
    base='/projects/'+str(project['id'])
    s('POST',base+'/repository/commits',{'branch':'feature','start_branch':'main','commit_message':'新增独立讨论门禁复核样本','actions':[{'action':'create','file_path':'review.txt','content':'讨论必须先解决\n'}]})
    pull=s('POST',base+'/merge_requests',{'source_branch':'feature','target_branch':'main','title':'非行内讨论门禁复核'})
    discussion=s('POST',base+'/merge_requests/'+str(pull['iid'])+'/discussions',{'body':'必须处理的普通线程，无文件位置'})
    s('POST',base+'/wikis',{'title':'Home','content':'独立复核样本','format':'markdown'})
    command="p=Project.find("+str(project['id'])+"); raise '独立样本身份不符' unless p.path == "+repr(name)+"; mr=p.merge_requests.find_by!(iid:1); mr.reload_diff(User.find(1)); mr.check_mergeability; puts '独立差异准备完成'"
    subprocess.run(['docker','exec',source['name'],'gitlab-rails','runner',command],check=True,stdout=subprocess.DEVNULL)

save('已创建独立来源样本',来源项目=project['id'],来源路径=project['path_with_namespace'],来源请求=pull['iid'],原始讨论=discussion)
assert discussion['notes'][0]['resolvable'] is True and discussion['notes'][0]['resolved'] is False
assert not discussion['notes'][0].get('position')
for _ in range(60):
    details=s('GET',base+'/merge_requests/'+str(pull['iid']))
    if details.get('diff_refs') and details.get('detailed_merge_status')=='discussions_not_resolved':break
    time.sleep(1)
assert details.get('diff_refs') and details.get('has_conflicts') is False and details.get('detailed_merge_status')=='discussions_not_resolved' and details.get('blocking_discussions_resolved') is False
denied=s('PUT',base+'/merge_requests/'+str(pull['iid'])+'/merge',{'sha':details['sha']},True)
assert denied.status_code in (405,406) and s('GET',base+'/merge_requests/'+str(pull['iid']))['state']=='opened'
save('来源实际拒绝未解决普通线程的合并',来源拒绝状态=denied.status_code,来源拒绝正文=denied.json(),来源合并状态=details.get('detailed_merge_status'))

mgr.headers['X-CSRF-Token']=m('GET','/session')['csrf_token']
mgr.headers['X-CSRF-Token']=m('POST','/login',{'password':manager['password']})['csrf_token']
registered=next(x for x in m('GET','/git-migrations/sources') if x['url']==source['base'].replace('127.0.0.1','host.docker.internal'))
server=next(x for x in m('GET','/servers') if x['gitea_url'].rstrip('/')==target['base'].replace('127.0.0.1','host.docker.internal').rstrip('/'))
group=t('POST','/governance/groups',{'parent_id':0,'path':name+'-'+secrets.token_hex(3),'name':'普通讨论门禁独立复核','visibility':2})

def poll(path):
    deadline=time.monotonic()+1800
    while time.monotonic()<deadline:
        value=m('GET',path)
        if value['status'] not in ('queued','running'):return value
        time.sleep(.5)
    raise RuntimeError('等待隔离任务超时')

if previous:
    old='/git-migrations/preflights/'+str(previous['预检查编号'])
    if m('GET',old)['status'] in ('ready','blocked','interrupted'):
        m('POST',old+'/cancel',{});assert poll(old)['status']=='cancelled'
found=m('POST',f"/git-migrations/sources/{registered['id']}/discoveries",{'source_version':registered['version'],'request_key':secrets.token_hex(16)})
found=poll('/git-migrations/discoveries/'+str(found['id']));assert found['status']=='succeeded'
inv=m('POST',f"/git-migrations/sources/{registered['id']}/inventories",{'discovery_id':found['id'],'project_ids':[project['id']],'capture_code':True,'request_key':secrets.token_hex(16)})
inv=poll('/git-migrations/inventories/'+str(inv['id']));assert inv['status']=='succeeded'
save('独立样本盘点成功',盘点编号=inv['id'])
mapping=m('POST','/git-migrations/account-mappings',{'inventory_id':inv['id'],'server_id':server['id'],'group_id':group['id'],'request_key':secrets.token_hex(16)})
mp='/git-migrations/account-mappings/'+str(mapping['id']);mapping=poll(mp);assert mapping['status']=='succeeded'
actor=t('GET','/user')
for row in mapping['rows']:
    mapping=m('PATCH',mp,{'revision':mapping['revision'],'action':'select','key':row['key'],'target_ids':[actor['id']]})
    mapping=m('PATCH',mp,{'revision':mapping['revision'],'action':'confirm','key':row['key']})
assert mapping['ready']
trial=m('POST','/git-migrations/preflights',{'inventory_id':inv['id'],'server_id':server['id'],'group_id':group['id'],'mapping_id':mapping['id'],'request_key':secrets.token_hex(16)})
pp='/git-migrations/preflights/'+str(trial['id']);trial=poll(pp)
save('预检查等待差异确认',预检查编号=trial['id'],预检查状态=trial['status'],预检查=trial)
assert trial['status']=='blocked' and not trial['can_start'] and trial['compatibility']['differences']
# 留给后台浏览器完成真实页面确认；也可用专门的 API 验收覆盖幂等与伪造摘要。
while not (OUT/'ui-confirmed.json').exists():time.sleep(1)
trial=poll(pp)
assert trial['status']=='ready' and trial['can_start'] and trial['compatibility']['acknowledged']
save('真实页面确认后预检查通过',预检查=trial)

m('POST',pp+'/start',{'source_frozen':True,'request_key':secrets.token_hex(16)})
trial=poll(pp);assert trial['run_id']
run=poll('/git-migrations/runs/'+str(trial['run_id']))
save('正式迁移返回结果',正式任务编号=run['id'],正式状态=run['status'],正式任务=run)
assert run['status']=='succeeded'
progress=run['checkpoint'][str(project['id'])]
repo=t('GET','/repositories/'+str(progress['repo_id']))
route='/repos/'+repo['full_name']+'/pulls/'+str(pull['iid'])
comments=t('GET','/repos/'+repo['full_name']+'/issues/'+str(pull['iid'])+'/comments')
save('目标普通讨论回读完成',目标仓库编号=repo['id'],目标路径=repo.get('full_path'),目标评论=comments)
for _ in range(90):
    current=t('GET',route)
    response=t('POST',route+'/merge',{'Do':'merge','head_commit_id':current['head']['sha'],'MergeMessageField':'独立审查：检查普通讨论门禁'},True)
    if response.status_code in (200,201):break
    if response.status_code not in (405,409,422):break
    time.sleep(1)
after=t('GET',route)
save('目标真实合并验证结束',目标合并HTTP=response.status_code,目标实际已合并=after.get('merged'),目标合并响应=response.text[:1000],结论='确认采用原生合并行为后成功，来源状态另行核验' if after.get('merged') else '合并未完成')

assert after.get('merged')
raw=subprocess.check_output(['docker','exec',target['name'],'sqlite3','-json','/data/gitea/gitea.db',"SELECT id,data FROM governance_migration_history WHERE repo_id="+str(repo['id'])+" AND kind='discussion_snapshot'"])
rows=json.loads(raw);assert rows
snapshot=next(x for x in rows if json.loads(x['data'])['thread_id']==discussion['id'])
history=json.loads(snapshot['data']);assert history['notes'][0]['resolved'] is False and history['notes'][0]['resolvable'] is True
save('合并后仍完整保留来源待解决状态',历史记录编号=snapshot['id'],来源快照=history,目标原生仓库名=repo['full_name'],结果='通过')
