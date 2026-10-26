#!/usr/bin/env python3
"""创建真实三级及跨项目流水线，执行每个叶子作业并保存日志和 C 产物。"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import time
import zipfile
import requests


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-state',type=Path,required=True);p.add_argument('--fixture',type=Path,required=True)
    p.add_argument('--runner-image',default='gm-migration-ci-runner:20261026')
    p.add_argument('--resume',action='store_true',help='保留现有流水线，继续领取尚未执行的叶子作业')
    a=p.parse_args()
    source,fixture=json.loads(a.source_state.read_text()),json.loads(a.fixture.read_text())
    info=json.loads(subprocess.check_output(['docker','inspect',source['name']]))[0]
    assert source['kind']=='gitlab' and info['Config']['Labels'].get('io.gitea-manager.test')=='git-migration-source'
    assert 'ci_hierarchy' not in fixture, '层级夹具已建立，不能重复创建'
    base=source['base'];client=requests.Session();client.trust_env=False
    r=client.post(base+'/oauth/token',data={'grant_type':'password','username':'root','password':source['password'],'scope':'api'},timeout=60)
    r.raise_for_status();client.headers['Authorization']='Bearer '+r.json()['access_token']
    def api(method,path,**kw):
        r=client.request(method,base+'/api/v4'+path,timeout=90,**kw)
        if not 200<=r.status_code<300:raise RuntimeError('层级夹具接口失败：'+method+' '+path+' HTTP '+str(r.status_code))
        return r.json() if r.content else None
    root,fork=fixture['project'],fixture['fork'];tag='tree-'+str(root['id'])
    folder=a.fixture.parent/(a.fixture.stem+'-hierarchy');folder.mkdir(mode=0o700,exist_ok=a.resume)
    private=folder/'runner-private.json'
    runners=[]
    if a.resume:
        runners=json.loads(private.read_text())
    else:
        for project in (root,fork):
            details=api('GET','/projects/'+str(project['id']))
            r=client.post(base+'/api/v4/runners',data={'token':details['runners_token'],'description':'层级流水线隔离执行器','tag_list':tag,'run_untagged':'false','locked':'true'},timeout=60)
            r.raise_for_status();runners.append(r.json())
        private.write_text(json.dumps(runners));private.chmod(0o600)
    job=lambda name: name+':\n  tags: ['+tag+']\n  script: ["固定 C 编译验收"]\n  artifacts:\n    paths: [fixture-app]\n'
    root_config=job('tree-root')+'''tree-child-trigger:
  trigger:
    include: [.ci/child.yml]
    strategy: depend
tree-cross-trigger:
  trigger:
    project: PROJECT
    branch: main
    strategy: depend
'''.replace('PROJECT',fork['path_with_namespace'])
    child=job('tree-child')+'''tree-grandchild-trigger:
  trigger:
    include: [.ci/grandchild.yml]
    strategy: depend
'''
    for project,files in ((fork,{'.gitlab-ci.yml':job('tree-downstream')}),(root,{'.gitlab-ci.yml':root_config,'.ci/child.yml':child,'.ci/grandchild.yml':job('tree-grandchild')})):
        route='/projects/'+str(project['id'])
        actions=[]
        for name,content in files.items():
            response=client.get(base+'/api/v4'+route+'/repository/files/'+requests.utils.quote(name,safe=''),params={'ref':'main'},timeout=30)
            assert response.status_code in (200,404)
            if a.resume:
                assert response.status_code==200 and __import__('base64').b64decode(response.json()['content']).decode()==content, '来源配置已改变，不能恢复此夹具'
            actions.append({'action':'update' if response.status_code==200 else 'create','file_path':name,'content':content})
        if not a.resume: api('POST',route+'/repository/commits',json={'branch':'main','commit_message':'完整层级流水线验收','actions':actions})
    reports=[];deadline=time.monotonic()+480;idle=0
    while time.monotonic()<deadline:
        received=False
        for runner in runners:
            r=client.post(base+'/api/v4/jobs/request',json={'token':runner['token'],'info':{'name':'层级迁移验收','version':'16.1.0','platform':'linux','architecture':'amd64','executor':'shell','features':{'artifacts':True,'upload_multiple_artifacts':True,'trace_reset':True}}},timeout=40)
            if r.status_code==204:continue
            r.raise_for_status();job_data=r.json();received=True
            assert job_data['job_info']['name'] in ('tree-root','tree-child','tree-grandchild','tree-downstream'), '不能执行夹具以外的作业'
            ident,token=job_data['id'],job_data['token']
            build=folder/('build-'+str(ident));build.mkdir()
            (build/'main.c').write_text('#include <stdio.h>\nint main(void) { puts("层级迁移 C 验收"); return 0; }\n')
            result=subprocess.run(['docker','run','--rm','--platform','linux/amd64','--network','none','-v',str(build.resolve())+':/fixture','-w','/fixture','--entrypoint','sh',a.runner_image,'-c','gcc -std=c11 -Wall -Werror main.c -o fixture-app && ./fixture-app'],capture_output=True)
            assert result.returncode==0
            trace=result.stdout+'真实子流水线编译与运行通过\n'.encode()
            r=client.patch(base+'/api/v4/jobs/'+str(ident)+'/trace',headers={'JOB-TOKEN':token,'Content-Range':'0-'+str(len(trace)-1)},data=trace,timeout=30);r.raise_for_status()
            buffer=io.BytesIO()
            with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:archive.writestr('fixture-app',(build/'fixture-app').read_bytes())
            data=buffer.getvalue()
            r=client.post(base+'/api/v4/jobs/'+str(ident)+'/artifacts',headers={'JOB-TOKEN':token},params={'artifact_type':'archive','artifact_format':'zip','expire_in':'1 week'},files={'file':('artifacts.zip',data,'application/octet-stream')},timeout=90);r.raise_for_status()
            r=client.put(base+'/api/v4/jobs/'+str(ident),json={'token':token,'state':'success'},timeout=60);r.raise_for_status()
            reports.append({'作业编号':ident,'名称':job_data['job_info']['name'],'归档SHA256':hashlib.sha256(data).hexdigest()})
        idle=0 if received else idle+1
        if idle>=3 and reports:
            active=[]
            for project in (root,fork):
                active += [row for row in api('GET','/projects/'+str(project['id'])+'/pipelines') if row['status'] in ('created','waiting_for_resource','pending','running')]
            if not active:break
        time.sleep(1)
    else:raise RuntimeError('真实层级流水线未全部完成')
    names={r['名称'] for r in reports}
    assert {'tree-root','tree-child','tree-grandchild','tree-downstream'}<=names, '未实际执行全部层级'
    for runner in runners:api('DELETE','/runners/'+str(runner['id']))
    fixture['ci_hierarchy']={'说明':'真实三级和跨项目触发，每个叶子均编译并运行 C 程序','jobs':reports}
    a.fixture.write_text(json.dumps(fixture,ensure_ascii=False,indent=2)+'\n')
    print('真实层级流水线全部执行完成，叶子作业：'+str(len(reports)))


if __name__=='__main__':main()
