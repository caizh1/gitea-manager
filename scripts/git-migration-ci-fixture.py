#!/usr/bin/env python3
"""通过真实 Runner 协议与工作流创建 CI 历史、日志、报告和构建产物，不改来源数据库。"""
import argparse,base64,gzip,hashlib,io,json,os,secrets,subprocess,sys,time,zipfile
from pathlib import Path
import requests


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-state',type=Path,required=True);p.add_argument('--fixture',type=Path,required=True);p.add_argument('--runner-image',default='gm-migration-ci-runner:20261025');a=p.parse_args()
    s=json.loads(a.source_state.read_text());fixture=json.loads(a.fixture.read_text());kind=s['kind'];base=s['base'];project=fixture['project'];folder=a.fixture.parent/(a.fixture.stem+'-ci');folder.mkdir(mode=0o700,exist_ok=True)
    info=json.loads(subprocess.check_output(['docker','inspect',s['name']]))[0]
    if info['Config']['Labels'].get('io.gitea-manager.test')!='git-migration-source':raise RuntimeError('拒绝修改非隔离来源')
    client=requests.Session();client.trust_env=False
    if kind=='gitlab':
        reply=client.post(base+'/oauth/token',data={'grant_type':'password','username':'root','password':s['password'],'scope':'api'},timeout=60);reply.raise_for_status();client.headers['Authorization']='Bearer '+reply.json()['access_token']
    else:client.auth=('source-admin',s['password'])
    def api(method,path,**kw):
        r=client.request(method,base+('/api/v4' if kind=='gitlab' else '/api/v1')+path,timeout=90,**kw)
        if not 200<=r.status_code<300:raise RuntimeError('CI 夹具接口失败 '+path+' HTTP '+str(r.status_code))
        return r.json() if r.content else None
    c_source='#include <stdio.h>\nint main(void) { puts("C 迁移端到端验收"); return 0; }\n'
    if kind=='gitlab':
        route='/projects/'+str(project['id']);tag='migration-'+str(project['id'])
        config='''stages: [verify]
verify-fixture:
  stage: verify
  tags: [TAG]
  script: ['gcc -std=c11 -Wall -Werror main.c -o fixture-app', './fixture-app']
  artifacts:
    paths: [fixture-app, main.c]
    reports:
      junit: report.xml
    expire_in: 1 week
manual-fixture:
  stage: verify
  when: manual
  script: ['echo 保留未执行状态']
'''.replace('TAG',tag)
        api('POST',route+'/repository/commits',json={'branch':'main','commit_message':'建立真实 CI 验收','actions':[{'action':'update' if api('GET',route+'/repository/tree?ref=main') and any(v['name']=='.gitlab-ci.yml' for v in api('GET',route+'/repository/tree?ref=main')) else 'create','file_path':'.gitlab-ci.yml','content':config},{'action':'create','file_path':'main.c','content':c_source}]})
        details=api('GET',route);registration=details.get('runners_token')
        if not registration:raise RuntimeError('来源未提供测试项目 Runner 注册令牌')
        r=client.post(base+'/api/v4/runners',data={'token':registration,'description':'迁移历史隔离执行器','tag_list':tag,'run_untagged':'false','locked':'true'},timeout=60);r.raise_for_status();runner=r.json()
        private=folder/'runner-private.json';private.write_text(json.dumps(runner));private.chmod(0o600)
        artifacts=[]
        for attempt in range(2):
            deadline=time.monotonic()+180;job=None
            while time.monotonic()<deadline:
                r=client.post(base+'/api/v4/jobs/request',json={'token':runner['token'],'info':{'name':'隔离迁移验收','version':'16.1.0','platform':'linux','architecture':'amd64','executor':'shell','features':{'artifacts':True,'upload_multiple_artifacts':True,'trace_reset':True}}},timeout=40)
                if r.status_code==201:job=r.json();break
                if r.status_code not in (204,):raise RuntimeError('领取隔离作业失败 HTTP '+str(r.status_code))
                time.sleep(1)
            if not job:raise RuntimeError('真实 CI 作业未进入可领取状态')
            job_id=job['id'];token=job['token']
            compilation=folder/('build-'+str(job_id));compilation.mkdir(exist_ok=True);(compilation/'main.c').write_text(c_source)
            executed=subprocess.run(['docker','run','--rm','--platform','linux/amd64','--network','none','-v',str(compilation.resolve())+':/fixture','-w','/fixture','--entrypoint','sh',a.runner_image,'-c','gcc -std=c11 -Wall -Werror main.c -o fixture-app && ./fixture-app'],capture_output=True)
            if executed.returncode:raise RuntimeError('真实 C 夹具编译执行失败')
            log=executed.stdout+('真实隔离作业\n第 '+str(attempt+1)+' 次执行\n').encode()
            r=client.patch(base+'/api/v4/jobs/'+str(job_id)+'/trace',headers={'JOB-TOKEN':token,'Content-Range':'0-'+str(len(log)-1)},data=log,timeout=30);r.raise_for_status()
            raw=('迁移构建产物 '+str(attempt+1)+'\n').encode();report=('<testsuites><testsuite name="迁移验收" tests="1"><testcase name="历史文件"/></testsuite></testsuites>').encode();z=io.BytesIO()
            with zipfile.ZipFile(z,'w',zipfile.ZIP_DEFLATED) as archive:archive.writestr('fixture-app',(compilation/'fixture-app').read_bytes());archive.writestr('main.c',c_source.encode())
            for typ,fmt,name,data in [('archive','zip','artifacts.zip',z.getvalue()),('junit','gzip','report.xml.gz',gzip.compress(report,mtime=0))]:
                r=client.post(base+'/api/v4/jobs/'+str(job_id)+'/artifacts',headers={'JOB-TOKEN':token},params={'artifact_type':typ,'artifact_format':fmt,'expire_in':'1 week'},files={'file':(name,data,'application/octet-stream')},timeout=90);r.raise_for_status()
                artifacts.append({'作业':job_id,'类型':typ,'SHA256':hashlib.sha256(data).hexdigest(),'字节':len(data)})
            r=client.put(base+'/api/v4/jobs/'+str(job_id),json={'token':token,'state':'failed' if attempt==0 else 'success','failure_reason':'script_failure' if attempt==0 else None},timeout=60);r.raise_for_status()
            if attempt==0:api('POST',route+'/pipeline',json={'ref':'main'})
        api('DELETE','/runners/'+str(runner['id']))
        output={'说明':'真实 GitLab Runner 接口领取、日志上传、原始报告和归档上传，保留失败、后续成功流水线与手动作业','制品':artifacts,'作业':api('GET',route+'/jobs?include_retried=true')}
    else:
        route='/repos/'+project['full_name'];api('PATCH',route,json={'has_actions':True})
        source_file=client.get(base+'/api/v1'+route+'/contents/main.c',timeout=30)
        source_body={'branch':'main','content':base64.b64encode(c_source.encode()).decode(),'message':'增加 C 编译基准'}
        if source_file.status_code==200:source_body['sha']=source_file.json()['sha']
        api('PUT' if source_file.status_code==200 else 'POST',route+'/contents/main.c',json=source_body)
        runner_dir=folder/'runner';runner_dir.mkdir(exist_ok=True)
        import shutil
        shutil.copyfile(Path(__file__).resolve().parents[1]/'work/git-migration/runner/act_runner',runner_dir/'act_runner');(runner_dir/'act_runner').chmod(0o755)
        (runner_dir/'config.yaml').write_text('log:\n  level: info\nrunner:\n  file: /runner/.runner\n  capacity: 1\n  timeout: 5m\n  labels: [local-host:host]\ncache:\n  enabled: false\nhost:\n  workdir_parent: /runner/jobs\n')
        if (runner_dir/'.runner').exists(): (runner_dir/'.runner').rename(runner_dir/('runner-previous-'+secrets.token_hex(4)+'.json'))
        generated=subprocess.check_output(['docker','exec','--user','git',s['name'],'gitea','actions','generate-runner-token','--scope',project['full_name']],text=True)
        registration=generated.strip().splitlines()[-1]
        runner_name='gm-git-ci-runner-'+secrets.token_hex(5);mount=str(runner_dir.resolve())+':/runner'
        cmd=['docker','run','--rm','--platform','linux/amd64','--network','container:'+s['name'],'-v',mount,'-w','/runner','--entrypoint','/runner/act_runner',a.runner_image,'register','--config','/runner/config.yaml','--no-interactive','--instance','http://127.0.0.1:3000','--token',registration,'--name',runner_name,'--labels','local-host:host']
        result=subprocess.run(cmd,capture_output=True,text=True)
        if result.returncode or not (runner_dir/'.runner').exists():
            safe=(result.stdout+result.stderr).replace(registration,'[已隐藏注册令牌]')
            (folder/'registration-error.txt').write_text('隔离 Runner 注册诊断：\n'+safe)
            raise RuntimeError('真实 Gitea Runner 注册未落盘，已保留诊断')
        subprocess.run(['docker','run','-d','--platform','linux/amd64','--name',runner_name,'--label','io.gitea-manager.test=git-migration-ci','--network','container:'+s['name'],'--cpus','1','--memory','768m','-v',mount,'-w','/runner','--entrypoint','/runner/act_runner',a.runner_image,'daemon','--config','/runner/config.yaml'],check=True,stdout=subprocess.DEVNULL)
        # 仅执行本脚本定义的内联步骤；不依赖外部 Actions 仓库或源端业务代码。
        job_python='''import os,json,urllib.request,hashlib,base64,subprocess
base=os.environ['ACTIONS_RUNTIME_URL'].rstrip('/')
token=os.environ['ACTIONS_RUNTIME_TOKEN']
run=os.environ['GITHUB_RUN_ID']
url=base+'/_apis/pipelines/workflows/'+run+'/artifacts?api-version=6.0-preview'
def req(method,url,data,headers={}):
 r=urllib.request.Request(url,data=data,method=method,headers={'Authorization':'Bearer '+token,**headers})
 with urllib.request.urlopen(r,timeout=30) as response: return response.read()
a=json.loads(req('POST',url,json.dumps({'Type':'actions_storage','Name':'fixture'}).encode(),{'Content-Type':'application/json'}))
u=a['fileContainerResourceUrl']
if u.startswith('/'): u='http://127.0.0.1:3000'+u
source=base64.b64decode('C_SOURCE_BASE64')
open('main.c','wb').write(source)
subprocess.run(['gcc','-std=c11','-Wall','-Werror','main.c','-o','fixture-app'],check=True)
subprocess.run(['./fixture-app'],check=True)
body=open('fixture-app','rb').read()
u=u+'?itemPath=fixture/fixture-app'
req('PUT',u,body,{'Content-Type':'application/octet-stream','Content-Range':'bytes 0-'+str(len(body)-1)+'/'+str(len(body)),'x-tfs-filelength':str(len(body)),'x-actions-results-md5':base64.b64encode(hashlib.md5(body).digest()).decode()})
req('PATCH',url+'&artifactName=fixture',json.dumps({'Size':len(body),'Name':'fixture'}).encode(),{'Content-Type':'application/json'})
print('真实 Actions 日志与制品上传完成')
'''
        job_python=job_python.replace('C_SOURCE_BASE64',base64.b64encode(c_source.encode()).decode())
        encoded=base64.b64encode(job_python.encode()).decode();workflow='name: 历史迁移验收\non: [push]\njobs:\n  verify:\n    runs-on: local-host\n    steps:\n      - name: 创建真实产物\n        run: python3 -c "import base64; exec(base64.b64decode(\''+encoded+'\'))"\n'
        file_url=route+'/contents/.gitea/workflows/migration.yaml'
        existing=client.get(base+'/api/v1'+file_url,timeout=30)
        body={'branch':'main','content':base64.b64encode(workflow.encode()).decode(),'message':'建立真实 Actions 历史'}
        if existing.status_code==200:body['sha']=existing.json()['sha']
        committed=api('PUT' if existing.status_code==200 else 'POST',file_url,json=body)
        expected_commit=committed['commit']['sha']
        deadline=time.monotonic()+240;runs=None
        while time.monotonic()<deadline:
            runs=[r for r in api('GET',route+'/actions/runs')['workflow_runs'] if r['head_sha']==expected_commit]
            if runs and runs[0].get('status')=='completed':break
            time.sleep(2)
        if not runs or runs[0].get('conclusion')!='success':
            subprocess.run(['docker','logs','--tail','80',runner_name],stdout=(folder/'runner-log.txt').open('w'),stderr=subprocess.STDOUT)
            raise RuntimeError('真实 Gitea CI 执行未成功，请读取隔离日志')
        subprocess.run(['docker','stop',runner_name],check=True,stdout=subprocess.DEVNULL)
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
        from git_migration.providers import Provider
        from git_migration.transport import Transport
        from git_migration.gitea_web import read_run
        provider=Provider(Transport(base),'gitea','source-admin',s['password']);provider.authenticate()
        view=read_run(provider,project['full_name'],runs[0]['run_number'])
        assert any(v['name']=='fixture' and v['status']=='completed' for v in view['artifacts'])
        result=provider.http.session.get(base+'/'+project['full_name']+'/actions/runs/'+str(runs[0]['run_number'])+'/artifacts/fixture',timeout=60);result.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(result.content)) as archive:binary=archive.read('fixture-app')
        assert binary.startswith(b'\x7fELF'), '流水线成功但来源没有可下载的 C 二进制，不能计为完成'
        provider.http.close()
        output={'说明':'官方 Runner 实际编译并执行 C 程序，已通过网页旧协议重新下载二进制','流水线':runs,'作业':api('GET',route+'/actions/jobs'),'构建产物':api('GET',route+'/actions/artifacts'),'旧版制品':view['artifacts'],'C产物SHA256':hashlib.sha256(binary).hexdigest()}
    (folder/'ci-evidence.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n');print('真实 CI 历史夹具已建立：'+str(folder/'ci-evidence.json'),flush=True)

if __name__=='__main__':main()
