#!/usr/bin/env python3
"""仅在已标记的 GitLab 来源补充现存保护分支与禁止合并的真实跨仓库请求。"""
import argparse,json,subprocess,time
from pathlib import Path
from urllib.parse import quote
import requests

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-state',type=Path,required=True);p.add_argument('--fixture',type=Path,required=True);a=p.parse_args();s=json.loads(a.source_state.read_text());f=json.loads(a.fixture.read_text())
    assert s['kind']=='gitlab' and f['project']['path'].startswith('advanced-')
    assert json.loads(subprocess.check_output(['docker','container','inspect',s['name']]))[0]['Config']['Labels']['io.gitea-manager.test']=='git-migration-source'
    c=requests.Session();c.trust_env=False;reply=c.post(s['base']+'/oauth/token',data={'grant_type':'password','username':'root','password':s['password'],'scope':'api'},timeout=60);reply.raise_for_status();c.headers['Authorization']='Bearer '+reply.json()['access_token']
    def api(method,path,data=None):
        r=c.request(method,s['base']+'/api/v4'+path,json=data,timeout=60);r.raise_for_status();return r.json() if r.content else None
    base='/projects/'+str(f['project']['id']);branch='private/migration-existing';pattern='private/*'
    saved=api('GET',base+'/protected_branches/'+quote(pattern,safe=''));assert saved['push_access_levels'][0]['access_level']==0 and saved['merge_access_levels'][0]['access_level']==0
    existing=c.get(s['base']+'/api/v4'+base+'/repository/branches/'+quote(branch,safe=''),timeout=30)
    if existing.status_code==404:
        api('DELETE',base+'/protected_branches/'+quote(pattern,safe=''))
        try:api('POST',base+'/repository/branches',{'branch':branch,'ref':'main'})
        finally:api('POST',base+'/protected_branches',{'name':pattern,'push_access_level':0,'merge_access_level':0,'allow_force_push':False})
    else:existing.raise_for_status()
    prs=api('GET',base+'/merge_requests?state=all');found=next((r for r in prs if r['target_branch']==branch),None)
    if not found:found=api('POST','/projects/'+str(f['fork']['id'])+'/merge_requests',{'source_branch':'cross-feature','target_branch':branch,'target_project_id':f['project']['id'],'title':'禁止任何人合并的跨仓库请求'})
    for _ in range(60):
        details=api('GET',base+'/merge_requests/'+str(found['iid']))
        if details.get('merge_status')=='can_be_merged':break
        time.sleep(1)
    else:raise RuntimeError('来源请求尚未确认无冲突，不能将其他拒绝原因计为权限验证')
    assert details.get('has_conflicts') is False and details['state']=='opened'
    denied=c.put(s['base']+'/api/v4'+base+'/merge_requests/'+str(found['iid'])+'/merge',json={'sha':details['sha']},timeout=60)
    assert api('GET','/user')['id']==1 and details.get('user',{}).get('can_merge') is False, '必须证明认证有效且来源明确拒绝合并权限'
    assert denied.status_code in (401,403,405,406), '来源禁止合并行为与预期不同'
    restored=api('GET',base+'/protected_branches/'+quote(pattern,safe=''));assert restored['push_access_levels'][0]['access_level']==restored['merge_access_levels'][0]['access_level']==0
    output=a.fixture.parent/(a.fixture.stem+'-rule-cases.json');output.write_text(json.dumps({'说明':'独立来源新增的现存保护分支，临时取消规则后已恢复并回读；来源管理员实际合并被拒绝','分支':branch,'请求编号':found['iid'],'源端拒绝状态':denied.status_code,'恢复规则':restored,'结果':'通过'},ensure_ascii=False,indent=2)+'\n');print('现存分支禁止合并夹具通过：'+str(output))

if __name__=='__main__':main()
