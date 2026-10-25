#!/usr/bin/env python3
"""仅升级标记的隔离 Worker 并恢复原任务，核对任务与仓库编号不变。"""
import argparse,json,secrets,subprocess,time
from pathlib import Path
import requests


def docker(*args):return subprocess.check_output(['docker',*args],text=True).strip()
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manager-state',type=Path,required=True);p.add_argument('--worker-image',required=True);p.add_argument('--run-id',type=int,required=True);a=p.parse_args()
    state=json.loads(a.manager_state.read_text());name=state['name'];worker=name+'-worker';info=json.loads(docker('inspect',worker))[0]
    if info['Config']['Labels'].get('io.gitea-manager.test')!='git-migration-manager':raise RuntimeError('只允许升级隔离验收 Worker')
    c=requests.Session();c.trust_env=False;base=state['base']+'/api'
    def call(method,path,data=None):
        r=c.request(method,base+path,json=data,timeout=40);r.raise_for_status();return r.json()
    c.headers['X-CSRF-Token']=call('GET','/session')['csrf_token'];c.headers['X-CSRF-Token']=call('POST','/login',{'password':state['password']})['csrf_token']
    before=call('GET','/git-migrations/runs/'+str(a.run_id));assert before['status'] in ('uncertain','interrupted','quarantined')
    ids={k:v['repo_id'] for k,v in before['checkpoint'].items() if v.get('repo_id')}
    docker('stop','-t','20',worker);docker('rename',worker,worker+'-before-'+secrets.token_hex(3))
    docker('run','-d','--platform','linux/amd64','--label','io.gitea-manager.test=git-migration-manager','--network',name,'--memory','512m','--cpus','1','--pids-limit','128','--name',worker,'--env-file',str(a.manager_state.parent/'manager.env'),'-v',name+':/app/data','--read-only','--tmpfs','/tmp:size=128m,mode=1777','--cap-drop','ALL','--security-opt','no-new-privileges:true',a.worker_image,'python','-m','git_migration.worker')
    call('POST','/git-migrations/runs/'+str(a.run_id)+'/resume',{})
    deadline=time.monotonic()+900
    while time.monotonic()<deadline:
        row=call('GET','/git-migrations/runs/'+str(a.run_id))
        if row['status'] in ('uncertain','interrupted','quarantined','failed'):raise RuntimeError('恢复未通过：'+row['stage']+'；'+row['error']+'；'+json.dumps(row['checkpoint'],ensure_ascii=False))
        if row['status']=='succeeded':break
        time.sleep(1)
    else:raise RuntimeError('恢复迁移超时')
    assert all(row['checkpoint'][k]['repo_id']==v for k,v in ids.items())
    out=a.manager_state.parent/('recovery-'+str(a.run_id)+'.json');out.write_text(json.dumps({'说明':'真实隔离写入缺陷修复后使用原检查点继续','结果':'通过','原仓库编号':ids,'迁移执行':row,'执行器镜像':json.loads(docker('image','inspect',a.worker_image))[0]['Id']},ensure_ascii=False,indent=2)+'\n');print('原任务恢复通过：'+str(out))

if __name__=='__main__':main()
