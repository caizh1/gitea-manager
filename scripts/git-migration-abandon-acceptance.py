#!/usr/bin/env python3
"""验证真实未完成任务安全终止、目标永久封锁和实例互斥释放。"""
import argparse
import json
from pathlib import Path
import time
import requests


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manager-state',type=Path,required=True);p.add_argument('--target-state',type=Path,required=True);p.add_argument('--run-id',type=int,required=True);a=p.parse_args()
    manager,target=json.loads(a.manager_state.read_text()),json.loads(a.target_state.read_text())
    c=requests.Session();c.trust_env=False;base=manager['base']+'/api'
    r=c.get(base+'/session',timeout=30);r.raise_for_status();c.headers['X-CSRF-Token']=r.json()['csrf_token']
    r=c.post(base+'/login',json={'password':manager['password']},timeout=30);r.raise_for_status();c.headers['X-CSRF-Token']=r.json()['csrf_token']
    def call(method,path,**kw):
        r=c.request(method,base+path,timeout=90,**kw);r.raise_for_status();return r.json()
    path='/git-migrations/runs/'+str(a.run_id)
    for _ in range(60):
        run=call('GET',path)
        if run['status'] in ('interrupted','uncertain','quarantined'):break
        time.sleep(0.5)
    else:raise RuntimeError('任务未到达停止状态，不能进行终止验收')
    server=call('GET','/servers/'+str(run['server_id']))
    before=c.put(base+'/servers/'+str(run['server_id']),json={'name':server['name']},timeout=30)
    assert before.status_code==409, '未完成任务没有保护实例操作'
    result=call('POST',path+'/abandon',json={'reason':'隔离验收：保留未完成现场并核对安全终止'})
    assert result['status']=='abandoned'
    assert call('POST',path+'/abandon',json={'reason':'幂等核对'})['id']==a.run_id
    after=c.put(base+'/servers/'+str(run['server_id']),json={'name':server['name']},timeout=30)
    assert after.status_code==200, '终止后没有释放实例互斥'
    native=requests.Session();native.trust_env=False;native.headers['Authorization']='token '+target['token']
    repos=[]
    for value in run['checkpoint'].values():
        if not value.get('repo_id') or value.get('handed_off'):continue
        r=native.get(target['base']+'/api/v1/repositories/'+str(value['repo_id']),timeout=30);r.raise_for_status()
        assert r.json()['archived'] and r.json()['private']
        # 接收标识在固定计划的私有检查点中，只在隔离卷内读取，避免输出凭据。
        import subprocess
        code="import sqlite3,json;c=sqlite3.connect('/app/data/gitea-manager.db');r=c.execute('SELECT checkpoint_json FROM git_migration_runs WHERE id=?',("+str(a.run_id)+",)).fetchone();print(r[0])"
        checkpoint=json.loads(subprocess.check_output(['docker','exec',manager['name']+'-worker','python','-c',code],text=True))
        project=next(v for k,v in checkpoint.items() if k.isdigit() and v.get('repo_id')==value['repo_id'])
        endpoint=target['base']+'/api/v1/governance/migration-sessions/'+project['key']
        status=native.get(endpoint+'/status',timeout=30);status.raise_for_status();assert status.json()['state']=='abandoned'
        rejected=native.post(endpoint+'/objects',json={'objects':[{'kind':'label','source_id':'after-abandon','title':'不应写入','color':'#ff0000'}]},timeout=30)
        assert rejected.status_code==409
        repos.append(value['repo_id'])
    assert repos, '没有真实目标写入，不能计为有现场终止通过'
    report={'说明':'真实检查点终止，未删除数据或卷','结果':'通过','任务':a.run_id,'保留归档仓库':repos,'终止前实例操作拒绝':True,'终止后实例互斥释放':True,'接收端拒绝继续写入':True}
    output=a.manager_state.parent/('abandon-acceptance-'+str(a.run_id)+'.json');output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print('真实终止验收通过：'+str(output))


if __name__=='__main__':main()
