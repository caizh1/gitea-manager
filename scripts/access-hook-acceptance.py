#!/usr/bin/env python3
"""真实 Git 推送与 PR 路径的组织门禁验收；凭据仅由进程环境传给 Git。"""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess

spec=importlib.util.spec_from_file_location('manager',Path(__file__).with_name('access-manager-acceptance.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
RESULTS=[]


def record(name,passed,detail=None):
    RESULTS.append({'用例':name,'结果':'通过' if passed else '失败','证据':detail})
    (m.env.STATE/'hook-results.json').write_text(json.dumps(RESULTS,ensure_ascii=False,indent=2)+'\n')
    print(('通过：' if passed else '失败：')+name,flush=True)


m.record=record


def run():
    m.env.verify_owned()
    admin=m.Client();admin.login()
    bindings=admin.request('GET','/access/organizations').json()['items']
    official=next(s for s in m.initialize_servers() if s['name']=='官方目录验收')
    binding=next(b for b in bindings if b['server_id']==official['id'] and b['organization_name']=='department-a')
    other=next(b for b in bindings if b['server_id']==official['id'] and b['organization_name']=='department-b')
    A=m.Client();A.login('user0000','ldap');A.org(binding['binding_id'])
    B=m.Client();B.login('user0001','ldap');B.org(other['binding_id'])
    suffix=secrets.token_hex(4)
    repo=m.gitea('gitea','POST','/orgs/department-a/repos',{'name':'hook-'+suffix,'auto_init':True,'default_branch':'main','private':True})
    rule=A.expect('本组织创建真实推送门禁规则','POST','/commit-rules',201,data={'server_id':official['id'],'name':'真实门禁'+suffix,'pattern':'^feat:'}).json()
    applied=A.expect('本组织安装提交 Hook','POST','/commit-gates/apply',data={'server_id':official['id'],'rule_id':rule['id'],'repo_names':[repo['full_name']]}).json()['results']
    record('Hook 安装回读成功',len(applied)==1 and applied[0]['install_status']=='success',{'状态':applied[0].get('install_status'),'诊断':applied[0].get('install_log')})
    B.expect('其他组织不能移除门禁','POST','/commit-gates/remove',404,data={'server_id':official['id'],'repo_names':[repo['full_name']]})
    work=m.env.STATE/'git'/suffix;work.mkdir(parents=True)
    askpass=work/'askpass.sh'
    askpass.write_text('#!/bin/sh\ncase "$1" in *sername*) printf "%s\\n" "access-admin" ;; *) printf "%s\\n" "$ACCESS_GIT_PASSWORD" ;; esac\n')
    askpass.chmod(0o700)
    environment=dict(os.environ,GIT_ASKPASS=str(askpass),GIT_TERMINAL_PROMPT='0',ACCESS_GIT_PASSWORD=m.CONFIG['GITEA_PASSWORD'],NO_PROXY='*',no_proxy='*')
    def git(*args,check=True):
        result=subprocess.run(['git',*args],cwd=work,env=environment,capture_output=True,text=True,timeout=30)
        if check and result.returncode:
            raise RuntimeError('真实 Git 命令失败：'+args[0]+' '+result.stderr[-1200:])
        return result
    url='http://127.0.0.1:'+m.CONFIG['GITEA_PORT']+'/'+repo['full_name']+'.git'
    git('clone',url,'repo')
    work=work/'repo'
    git('config','user.name','部门甲验收')
    git('config','user.email','department-a@access.test')
    (work/'sample.txt').write_text('真实门禁验收\n')
    git('add','sample.txt');git('commit','-m','非法提交说明')
    bad=git('push','origin','main',check=False)
    record('实际 Git push 拒绝不合规提交',bad.returncode!=0 and 'rejected commit' in bad.stderr,{'退出码':bad.returncode})
    git('commit','--amend','-m','feat: 合规提交说明')
    good=git('push','origin','main',check=False)
    record('实际 Git push 允许合规提交',good.returncode==0,{'退出码':good.returncode})
    git('switch','-c','feature-acceptance')
    (work/'feature.txt').write_text('真实 PR 验收\n')
    git('add','feature.txt');git('commit','-m','feat: 审批候选改动');git('push','origin','feature-acceptance')
    pr=m.gitea('gitea','POST','/repos/'+repo['full_name']+'/pulls',{'title':'feat: 真实 PR 合并','head':'feature-acceptance','base':'main'})
    port=m.CONFIG['GITEA_PORT']
    import time
    for _ in range(20):
        current=m.gitea('gitea','GET','/repos/'+repo['full_name']+'/pulls/'+str(pr['number']))
        if current.get('mergeable'):break
        time.sleep(.5)
    response=m.direct_request('POST','http://127.0.0.1:'+port+'/api/v1/repos/'+repo['full_name']+'/pulls/'+str(pr['number'])+'/merge',json={'Do':'merge','MergeTitleField':'不合规合并说明'},headers={'Authorization':'token '+m.CONFIG['GITEA_TOKEN']},timeout=30)
    record('真实 PR 合并拒绝不合规合并说明',response.status_code>=400,{'状态码':response.status_code})
    if response.status_code>=400:
        m.gitea('gitea','POST','/repos/'+repo['full_name']+'/pulls/'+str(pr['number'])+'/merge',{'Do':'merge','MergeTitleField':'feat: 合规合并说明'})
        result=m.gitea('gitea','GET','/repos/'+repo['full_name']+'/pulls/'+str(pr['number']))
        record('真实 PR 合规合并完成',result.get('merged') is True)
    removed=A.expect('本组织移除提交 Hook','POST','/commit-gates/remove',data={'server_id':official['id'],'repo_names':[repo['full_name']]}).json()
    record('Hook 移除回读成功',all(r.get('status')=='success' for r in removed.get('results',[])),{'结果':removed})


if __name__=='__main__':
    try:
        run()
    except Exception as exc:
        message=str(exc).replace(m.CONFIG['GITEA_PASSWORD'],'[已隐藏]').replace(m.CONFIG['GITEA_TOKEN'],'[已隐藏]')
        record('验收执行异常',False,{'错误':message})
        raise
    finally:
        lines=['# 真实 Git 与 PR 组织门禁验收','',f'执行 {len(RESULTS)} 项，通过 {sum(r["结果"]=="通过" for r in RESULTS)} 项。','','| 用例 | 结果 |','|---|---|']
        lines+=['| '+r['用例']+' | '+r['结果']+' |' for r in RESULTS]
        (m.env.STATE/'hook-report.md').write_text('\n'.join(lines)+'\n')
    if any(r['结果']=='失败' for r in RESULTS):raise SystemExit(1)
