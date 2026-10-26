#!/usr/bin/env python3
"""建立跨仓库、重叠规则及五种原生制品的真实隔离来源，保留全部证据。"""
import argparse, base64, hashlib, io, json, secrets, subprocess, tarfile, time, zipfile
from pathlib import Path
from urllib.parse import quote
import requests


def tgz(files):
    out=io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz') as tar:
        for name,content in files.items():
            entry=tarfile.TarInfo(name);entry.size=len(content);entry.mtime=1700000000;tar.addfile(entry,io.BytesIO(content))
    return out.getvalue()


def zip_bytes(files):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for name,content in files.items():z.writestr(name,content)
    return out.getvalue()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source-state',type=Path,required=True)
    args=parser.parse_args();state=json.loads(args.source_state.read_text());kind=state['kind'];base=state['base']
    info=json.loads(subprocess.check_output(['docker','inspect',state['name']]))[0]
    if info['Config']['Labels'].get('io.gitea-manager.test')!='git-migration-source':raise RuntimeError('只能向本任务独立来源创建验收资源')
    session=requests.Session();session.trust_env=False;owner='root' if kind=='gitlab' else 'source-admin'
    if kind=='gitlab':
        r=session.post(base+'/oauth/token',data={'grant_type':'password','username':owner,'password':state['password'],'scope':'api'},timeout=60);r.raise_for_status();session.headers['Authorization']='Bearer '+r.json()['access_token']
        # NuGet 发布不接受普通 OAuth 凭据；夹具使用独立且短期的管理令牌。
        from datetime import datetime, timedelta, timezone
        r=session.post(base+'/api/v4/users/1/personal_access_tokens',json={'name':'迁移隔离夹具-'+secrets.token_hex(4),'scopes':['api'],
                       'expires_at':(datetime.now(timezone.utc)+timedelta(days=1)).date().isoformat()},timeout=60)
        r.raise_for_status()
        session.headers.pop('Authorization',None);session.headers['PRIVATE-TOKEN']=r.json()['token']
    else:session.auth=(owner,state['password'])
    def request(method,url,**kwargs):
        r=session.request(method,base+url,timeout=90,**kwargs)
        if not 200<=r.status_code<300:raise RuntimeError('夹具操作失败 '+method+' '+url+' HTTP '+str(r.status_code)+' '+r.text[:200])
        return r.json() if r.content and r.headers.get('Content-Type','').startswith('application/json') else None
    api='/api/v4' if kind=='gitlab' else '/api/v1';key=secrets.token_hex(4);name='advanced-'+key;version='1.2.3'
    if kind=='gitlab':
        project=request('POST',api+'/projects',json={'name':name,'visibility':'private','initialize_with_readme':True,'default_branch':'main'})
        group=request('POST',api+'/groups',json={'name':'依赖群组 '+key,'path':'fork-'+key,'visibility':'private'})
        fork=request('POST',api+'/projects/'+str(project['id'])+'/fork',json={'namespace_id':group['id'],'path':name+'-fork'})
        for _ in range(90):
            fork=request('GET',api+'/projects/'+str(fork['id']))
            if fork.get('import_status')=='finished':break
            time.sleep(1)
        root=api+'/projects/'+str(project['id']);head=api+'/projects/'+str(fork['id'])
        request('POST',head+'/repository/commits',json={'branch':'cross-feature','start_branch':'main','commit_message':'跨仓库变更','actions':[{'action':'create','file_path':'cross.txt','content':'跨仓库迁移内容\n'}]})
        pull=request('POST',head+'/merge_requests',json={'source_branch':'cross-feature','target_branch':'main','target_project_id':project['id'],'title':'真实跨仓库合并请求','description':'保留来源仓库关系与历史讨论'})
        for route in (root,head):request('POST',route+'/wikis',json={'title':'Home','content':'依赖仓库百科\n'})
        for pattern,level,force in [('release/*',40,False),('release/open*',30,True),('private/*',0,False)]:
            request('POST',root+'/protected_branches',json={'name':pattern,'push_access_level':level,'merge_access_level':level,'allow_force_push':force})
    else:
        project=request('POST',api+'/user/repos',json={'name':name,'private':True,'auto_init':True,'default_branch':'main'})
        fork_user='fork-'+key;fork_password=secrets.token_urlsafe(26)
        request('POST',api+'/admin/users',json={'username':fork_user,'password':fork_password,'email':fork_user+'@example.invalid','must_change_password':False})
        request('PUT',api+'/repos/'+owner+'/'+name+'/collaborators/'+fork_user,json={'permission':'read'})
        fork_session=requests.Session();fork_session.trust_env=False;fork_session.auth=(fork_user,fork_password)
        r=fork_session.post(base+api+'/repos/'+owner+'/'+name+'/forks',json={'name':name+'-fork'},timeout=60);r.raise_for_status();fork=r.json()
        root=api+'/repos/'+owner+'/'+name;head=api+'/repos/'+fork['full_name']
        r=fork_session.post(base+head+'/contents/cross.txt',json={'content':base64.b64encode('跨仓库迁移内容\n'.encode()).decode(),'branch':'main','new_branch':'cross-feature','message':'跨仓库变更'},timeout=60);r.raise_for_status()
        pull=request('POST',root+'/pulls',json={'base':'main','head':fork_user+':cross-feature','title':'真实跨仓库合并请求','body':'保留真实依赖仓库'})
        for route in (root,head):request('PATCH',route,json={'has_wiki':True})
        for route in (root,head):request('POST',route+'/wiki/new',json={'title':'Home','content_base64':base64.b64encode('依赖仓库百科\n'.encode()).decode(),'message':'创建百科'})
        for pattern,priority,push in [('release/*',2,False),('release/open*',1,True),('private/*',3,False)]:
            request('POST',root+'/branch_protections',json={'rule_name':pattern,'priority':priority,'enable_push':push,'enable_merge_whitelist':True,'merge_whitelist_usernames':[owner]})
    request('POST',root+('/protected_tags' if kind=='gitlab' else '/tag_protections'),json={'name':'v*','create_access_level':40} if kind=='gitlab' else {'name_pattern':'v*','whitelist_usernames':[owner]})
    packages=[]
    npm_name='adv-'+key+'-npm';npm_json={'name':npm_name,'version':version,'description':'原生制品迁移验收','dependencies':{'fixture-dep':'^2.0.0'},'license':'MIT','engines':{'node':'>=18'}}
    npm_data=tgz({'package/package.json':json.dumps(npm_json,ensure_ascii=False).encode(),'package/README.md':'制品中文说明\n'.encode()});npm_file=npm_name+'-'+version+'.tgz'
    doc={'name':npm_name,'dist-tags':{'latest':version,'stable':version},'versions':{version:{**npm_json,'dist':{'shasum':hashlib.sha1(npm_data).hexdigest(),'integrity':'sha512-'+base64.b64encode(hashlib.sha512(npm_data).digest()).decode(),'tarball':base+'/'+npm_file}}},'_attachments':{npm_file:{'content_type':'application/octet-stream','data':base64.b64encode(npm_data).decode(),'length':len(npm_data)}}}
    request('PUT',root+'/packages/npm/'+npm_name if kind=='gitlab' else '/api/packages/'+owner+'/npm/'+npm_name,json=doc)
    packages.append({'type':'npm','name':npm_name,'version':version,'files':{npm_file:hashlib.sha256(npm_data).hexdigest()}})
    artifact='adv-'+key+'-maven';group_id='local.migration';pom=('<project><modelVersion>4.0.0</modelVersion><groupId>'+group_id+'</groupId><artifactId>'+artifact+'</artifactId><version>'+version+'</version><name>迁移验收</name></project>').encode()
    jar=zip_bytes({'evidence.txt':'原生构建文件\n'.encode()});maven_files={artifact+'-'+version+'.pom':pom,artifact+'-'+version+'.jar':jar}
    for filename,data in maven_files.items():request('PUT',(root+'/packages/maven/' if kind=='gitlab' else '/api/packages/'+owner+'/maven/')+group_id.replace('.','/')+'/'+artifact+'/'+version+'/'+filename,data=data)
    packages.append({'type':'maven','name':group_id+':'+artifact,'version':version,'files':{f:hashlib.sha256(b).hexdigest() for f,b in maven_files.items()}})
    py_name='adv_'+key+'_py';wheel_name=py_name+'-'+version+'-py3-none-any.whl';meta='Metadata-Version: 2.1\nName: '+py_name+'\nVersion: '+version+'\nSummary: 迁移验收\nRequires-Python: >=3.8\n\n保留长说明\n'
    wheel=zip_bytes({py_name+'-'+version+'.dist-info/METADATA':meta.encode(),py_name+'-'+version+'.dist-info/WHEEL':b'Wheel-Version: 1.0\nGenerator: migration-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n',py_name+'-'+version+'.dist-info/RECORD':b'',py_name+'.py':"value = '迁移验收'\n".encode()})
    request('POST',root+'/packages/pypi' if kind=='gitlab' else '/api/packages/'+owner+'/pypi',data={':action':'file_upload','protocol_version':'1','name':py_name,'version':version,'summary':'迁移验收','requires_python':'>=3.8','sha256_digest':hashlib.sha256(wheel).hexdigest()},files={'content':(wheel_name,wheel,'application/octet-stream')})
    packages.append({'type':'pypi','name':py_name.replace('_','-'),'version':version,'files':{wheel_name:hashlib.sha256(wheel).hexdigest()}})
    nuget_name='Adv.'+key;nuget_file=(nuget_name+'.'+version+'.nupkg').lower();nuspec=('<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd"><metadata><id>'+nuget_name+'</id><version>'+version+'</version><authors>迁移验收</authors><description>保留依赖和包内容</description><dependencies><dependency id="Fixture.Dep" version="[1.0.0]" /></dependencies></metadata></package>').encode();nuget_data=zip_bytes({nuget_name+'.nuspec':nuspec,'lib/net8.0/evidence.txt':'制品内容\n'.encode()})
    nuget_headers = {}
    if kind == 'gitlab':
        credential = request('POST',api+'/users/1/personal_access_tokens',json={'name':'隔离制品验收 '+key,'scopes':['api'],'expires_at':(datetime.now(timezone.utc)+timedelta(days=1)).date().isoformat()})
        nuget_headers={'Authorization':None,'X-NuGet-ApiKey':credential['token']}
    request('PUT',root+'/packages/nuget/' if kind=='gitlab' else '/api/packages/'+owner+'/nuget',headers=nuget_headers,files={'package':(nuget_file,nuget_data,'application/octet-stream')})
    packages.append({'type':'nuget','name':nuget_name,'version':version,'files':{nuget_file:hashlib.sha256(nuget_data).hexdigest()}})
    helm_name='adv-'+key+'-chart';helm_file=helm_name+'-'+version+'.tgz';helm_data=tgz({helm_name+'/Chart.yaml':('apiVersion: v2\nname: '+helm_name+'\nversion: '+version+'\ndescription: 迁移验收\n').encode(),helm_name+'/values.yaml':'说明: 保留原始配置\n'.encode()})
    request('POST',root+'/packages/helm/api/stable/charts' if kind=='gitlab' else '/api/packages/'+owner+'/helm/api/charts',auth=(owner,credential['token']) if kind=='gitlab' else None,headers={'Authorization':None} if kind=='gitlab' else {},files={'chart':(helm_file,helm_data,'application/gzip')})
    packages.append({'type':'helm','name':helm_name,'version':version,'files':{helm_file:hashlib.sha256(helm_data).hexdigest()}})
    if kind=='gitlab':request('DELETE',api+'/personal_access_tokens/'+str(credential['id']))
    if kind=='gitea':
        for package in packages:request('POST',api+'/packages/'+owner+'/'+package['type']+'/'+quote(package['name'],safe='')+'/-/link/'+name)
    if kind=='gitlab':
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            rows=request('GET',root+'/packages')
            if any(v['package_type']=='nuget' and v['name']==nuget_name and v.get('status')=='default' for v in rows):break
            time.sleep(1)
        else:raise RuntimeError('来源 NuGet 未处理为可安装版本，夹具不计为通过')
    output=args.source_state.parent/(name+'.json');output.write_text(json.dumps({'说明':'本任务真实高级迁移基准，凭据不在此清单中','kind':kind,'project':project,'fork':fork,'pull':pull,'packages':packages},ensure_ascii=False,indent=2)+'\n')
    print('高级真实夹具已创建：'+str(output),flush=True)

if __name__=='__main__':main()
