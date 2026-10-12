#!/usr/bin/env python3
"""持久化后台任务在领取前撤权的真实验收；利用现有实例锁控制合法调度时序。"""
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import time

spec=importlib.util.spec_from_file_location('manager',Path(__file__).with_name('access-manager-acceptance.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
RESULTS=[]


def record(name,passed,detail=None):
    RESULTS.append({'用例':name,'结果':'通过' if passed else '失败','证据':detail})
    (m.env.STATE/'task-results.json').write_text(json.dumps(RESULTS,ensure_ascii=False,indent=2)+'\n')
    print(('通过：' if passed else '失败：')+name,flush=True)


m.record=record


def run():
    admin=m.Client();admin.login()
    person=m.Client();identity=person.login('user0000','ldap')['user']['id']
    bindings=admin.request('GET','/access/organizations').json()['items']
    server=next(s for s in m.initialize_servers() if s['name']=='官方目录验收')
    binding=next(b for b in bindings if b['server_id']==server['id'] and b['organization_name']=='department-a' and b['active'])
    person.org(binding['binding_id'])
    repo=m.gitea('gitea','POST','/orgs/department-a/repos',{'name':'queue-'+secrets.token_hex(5),'auto_init':True,'default_branch':'main','private':True})
    template=person.expect('创建撤权测试策略','POST','/repository-policies/templates',201,data={'server_id':server['id'],'name':'后台撤权测试','organization':{'id':binding['organization_id'],'name':binding['organization_name'],'server_id':server['id']},'rules':[{'rule_name':'main','enable_push':True}]}).json()
    batch=person.expect('创建真实后台预览','POST','/repository-policies/batches',202,data={'template_id':template['id'],'content_hash':template['content_hash'],'repo_ids':[repo['id']]}).json()
    path='/repository-policies/batches/'+str(batch['id'])
    for _ in range(60):
        state=person.request('GET',path).json()['batch']
        if state['status']=='ready':break
        time.sleep(.5)
    record('真实预览已经就绪',state['status']=='ready')
    # 与生产 Worker 共用同一个实例锁，保证测试能够确定性地在领取前撤权。
    code="import fcntl,sys;f=open('/access-data/.policy-locks/"+str(server['id'])+".lock','a');fcntl.flock(f,fcntl.LOCK_EX);print('测试实例锁已领取',flush=True);sys.stdin.readline()"
    lock=subprocess.Popen(['docker','exec','-i',m.CONTAINER,'python','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if lock.stdout.readline().strip()!='测试实例锁已领取':raise RuntimeError('测试实例锁未领取')
    revoked=False
    try:
        person.expect('锁定实例时提交真实应用任务','POST',path+'/apply',202,data={})
        queued=admin.request('GET',path).json()['batch']
        record('数据库任务保持待领取状态',queued['status']=='apply_queued',{'状态':queued['status']})
        admin.expect('任务领取前撤销操作者授权','DELETE',f"/access/grants/{identity}/{binding['binding_id']}")
        revoked=True
    finally:
        lock.communicate('\n',timeout=10)
    try:
        for _ in range(60):
            state=admin.request('GET',path).json()
            if state['batch']['status'] not in ('apply_queued','applying'):break
            time.sleep(.5)
        record('真实 Worker 领取后拒绝已撤权操作者',state['batch']['status']=='completed_with_errors' and all(r['status']=='blocked' for r in state['items']),{'状态':state['batch']['status'],'仓库结果':state['items']})
        actual=m.gitea('gitea','GET','/repos/'+repo['full_name']+'/branch_protections')
        record('撤权任务没有产生外部写入',actual==[],{'Gitea保护规则数量':len(actual)})
        other=m.Client();other.login('user0003','ldap');other.org(binding['binding_id'])
        other.expect('撤销个人不影响同组织其他管理员','GET','/commit-rules')
    finally:
        if revoked:
            admin.expect('恢复隔离操作者授权','POST','/access/grants',data={'account_id':identity,'binding_id':binding['binding_id']})


if __name__=='__main__':
    try:
        run()
    except Exception as exc:
        record('验收执行异常',False,{'错误':str(exc)})
        raise
    finally:
        lines=['# 后台任务真实撤权验收','',f'执行 {len(RESULTS)} 项，通过 {sum(r["结果"]=="通过" for r in RESULTS)} 项。','',
            '使用现有实例文件锁控制领取时序；授权变更、持久任务、后台 Worker 和 Gitea 均实际执行。','','| 用例 | 结果 |','|---|---|']
        lines+=['| '+r['用例']+' | '+r['结果']+' |' for r in RESULTS]
        (m.env.STATE/'task-report.md').write_text('\n'.join(lines)+'\n')
    if any(r['结果']=='失败' for r in RESULTS):raise SystemExit(1)
