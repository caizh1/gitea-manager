#!/usr/bin/env python3
"""逐项验收完整依赖闭包、原生制品协议、所有历史文件和 C 二进制，拒绝样本抽检。"""
import argparse, base64, hashlib, io, json, os, re, secrets, subprocess, time, zipfile
from pathlib import Path
from urllib.parse import quote, urlsplit
import requests


def docker(*args): return subprocess.check_output(['docker', *args], text=True).strip()
def read_run(name, ident):
    code="import sqlite3,json;c=sqlite3.connect('file:/app/data/gitea-manager.db?mode=ro',uri=True);c.row_factory=sqlite3.Row;r=dict(c.execute('SELECT * FROM git_migration_runs WHERE id=?',("+str(ident)+",)).fetchone());print(json.dumps(r))"
    return json.loads(docker('exec', name+'-worker', 'python', '-c', code))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('manager-state','target-state','fixture'): p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--run-id',type=int,required=True);p.add_argument('--merge',action='store_true');p.add_argument('--runner-image',default='gm-migration-ci-runner:20261026');a=p.parse_args()
    manager=json.loads(a.manager_state.read_text());target=json.loads(a.target_state.read_text());fixture=json.loads(a.fixture.read_text())
    for state,label in ((manager,'git-migration-manager'),(target,'git-migration-receiver')):
        assert json.loads(docker('container','inspect',state['name']+'-worker' if label=='git-migration-manager' else state['name']))[0]['Config']['Labels']['io.gitea-manager.test']==label
    run=read_run(manager['name'],a.run_id);assert run['status']=='succeeded', '执行尚未成功'
    checkpoint=json.loads(run['checkpoint_json']);root=checkpoint[str(fixture['project']['id'])];fork=checkpoint[str(fixture['fork']['id'])]
    projects=[v for k,v in checkpoint.items() if k.isdigit()];assert len(projects)==2+len(fixture.get('completeness',{}).get('merge_cases',[]))
    code="import sqlite3,json;c=sqlite3.connect('file:/app/data/gitea-manager.db?mode=ro',uri=True);print(c.execute('SELECT result_json FROM git_migration_inventories WHERE id=?',("+str(run['inventory_id'])+",)).fetchone()[0])"
    inventory=json.loads(docker('exec',manager['name']+'-worker','python','-c',code))
    manifests={item['project']['id']:item['source_manifest'] for item in inventory['projects']}
    client=requests.Session();client.trust_env=False;client.auth=('migration-admin',target['password']);base=target['base']
    client.get(base+'/user/login',timeout=30)
    login=client.post(base+'/user/login',headers={'Origin':base},data={'user_name':'migration-admin','password':target['password']},allow_redirects=False,timeout=30)
    assert login.status_code in (302,303), '隔离接收端网页登录失败'
    def get(path, binary=False):
        url=path if path.startswith('http') else base+path
        assert urlsplit(url).netloc==urlsplit(base).netloc, '原生索引生成了错误的服务地址'
        r=client.get(url,timeout=60);assert r.status_code==200, '读取失败 '+urlsplit(url).path+' HTTP '+str(r.status_code)
        return r.content if binary else r.json()
    def api(path):return get('/api/v1'+path)
    report={'说明':'固定真实来源的全部盘点项目、对象和文件验收，不代表生产或人工节省指标','来源':fixture['kind'],'执行':a.run_id,'项目':[],'制品协议':[],'历史文件':[],'C产物执行':[]}
    for row in projects:
        assert row['handed_off'] and row['code_verified'] and row['objects_verified'] and row['source_verified'] and not row['pending'] and not row['gaps']
        manifest=manifests[row['snapshot']['project_id']] if 'project_id' in row['snapshot'] else next(value for ident,value in manifests.items() if checkpoint[str(ident)] is row)
        expected_objects={kind+':'+ident for kind,ident in manifest['objects']}
        actual_objects={key for key in row['objects'] if not key.startswith(('git_ref:','merge_policy:'))}
        assert actual_objects==expected_objects,'目标回执与独立来源对象清单不一致'
        assert set(row.get('assets',{}))=={file['source_id'] for file in manifest['files']},'目标文件与独立来源清单不一致'
        repo=api('/repos/'+row['final_path']);assert repo['id']==row['repo_id'] and not repo['archived'] and not repo['has_actions']
        assert repo['default_branch']==row['snapshot']['default_ref'].removeprefix('refs/heads/')
        report['项目'].append({'路径':row['final_path'],'编号':repo['id'],'对象数':len(row['objects']),'文件数':len(row.get('assets',{})),'代码重新克隆核验':True,'Wiki核验':row.get('wiki_verified',False)})
        assert row.get('wiki_verified'), '依赖仓库 Wiki 未逐个核验'
        for ident, receipt in row.get('assets',{}).items():
            source=row['downloads'][ident]
            path='/attachments/'+receipt['uuid'] if receipt['parent_kind'] in ('issue','pull','comment','code_comment','release') else '/'+row['final_path']+'/migration-history/assets/'+str(receipt['id'])
            data=get(path,True)
            assert len(data)==source['size'] and hashlib.sha256(data).hexdigest()==source['sha256']==receipt['sha256']
            report['历史文件'].append({'来源编号':ident,'SHA256':receipt['sha256'],'字节':len(data)})
            if ident.startswith(('ci-artifact-','job-artifact-')) and zipfile.is_zipfile(io.BytesIO(data)):
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    names=[n for n in archive.namelist() if n.rsplit('/',1)[-1]=='fixture-app']
                    for name in names:
                        binary=archive.read(name)
                        if not binary.startswith(b'\x7fELF'):continue
                        folder=a.manager_state.parent/('binary-'+str(a.run_id)+'-'+str(receipt['id']));folder.mkdir(exist_ok=True)
                        executable=folder/'fixture-app';executable.write_bytes(binary);executable.chmod(0o755)
                        result=subprocess.run(['docker','run','--rm','--platform','linux/amd64','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','-v',str(folder.resolve())+':/fixture:ro','--entrypoint','/fixture/fixture-app',a.runner_image],capture_output=True)
                        assert result.returncode==0 and '迁移' in result.stdout.decode()
                        report['C产物执行'].append({'来源编号':ident,'SHA256':hashlib.sha256(binary).hexdigest(),'结果':'隔离执行通过'})
    pull=api('/repos/'+root['final_path']+'/pulls/1');assert pull['head']['repo']['id']==fork['repo_id'] and pull['base']['repo']['id']==root['repo_id']
    assert pull['head']['ref']=='cross-feature' and pull['base']['ref']=='main'
    report['跨仓库关系']={'来源仓库':fork['repo_id'],'目标仓库':root['repo_id'],'保持真实依赖':True}
    repo=api('/repos/'+root['final_path']);owner=repo['owner']['login'];registry='/api/packages/'+quote(owner,safe='')
    for package in fixture['packages']:
        kind,name,version=package['type'],package['name'],package['version'];downloads={};details={}
        if kind=='generic':
            for filename in package['files']:downloads[filename]=get(registry+'/generic/'+quote(name,safe='')+'/'+quote(version,safe='')+'/'+quote(filename,safe=''),True)
        elif kind=='npm':
            index=get(registry+'/npm/'+quote(name,safe=''));v=index['versions'][version]
            source_packages=[item['package'] for item in root['package_files'] if item['package']['type']=='npm' and item['package']['name']==name]
            source_package=next(item for item in source_packages if item['version']==version)
            expected_tags={tag:item['version'] for item in source_packages for tag in item['tags']}
            assert index['dist-tags']==expected_tags, 'npm 分发标签与固定来源索引不同'
            assert v['dependencies']==source_package['metadata']['dependencies']
            if source_package['metadata'].get('readme'):assert v['readme']==source_package['metadata']['readme']
            if 'engines' in source_package['metadata']:assert v['engines']==source_package['metadata']['engines']
            downloads[next(iter(package['files']))]=get(v['dist']['tarball'],True);details={'分发标签':index['dist-tags'],'依赖与运行时约束':'保留'}
        elif kind=='maven':
            group,artifact=name.split(':');prefix=registry+'/maven/'+group.replace('.','/')+'/'+artifact+'/'+version+'/'
            for filename in package['files']:downloads[filename]=get(prefix+filename,True)
        elif kind=='pypi':
            page=get(registry+'/pypi/simple/'+name,True).decode();assert '3.8' in page
            urls=re.findall(r'href="([^"]+)"',page)
            for url in urls:
                filename=urlsplit(url).path.rsplit('/',1)[-1]
                if filename in package['files']:downloads[filename]=get(url,True)
            details={'解释器版本约束':'保留'}
        elif kind=='nuget':
            index=get(registry+'/nuget/registration/'+name.lower()+'/index.json');assert 'Fixture.Dep' in json.dumps(index)
            for filename in package['files']:downloads[filename]=get(registry+'/nuget/package/'+name.lower()+'/'+version+'/'+filename,True)
            nuspec=get(registry+'/nuget/package/'+name.lower()+'/'+version+'/'+name.lower()+'.nuspec',True);assert b'Fixture.Dep' in nuspec;details={'依赖与清单':'保留'}
        elif kind=='helm':
            import yaml
            index=yaml.safe_load(get(registry+'/helm/index.yaml',True));entry=next(v for v in index['entries'][name] if v['version']==version)
            for url in entry['urls']:
                url=url if url.startswith('http') else base+registry+'/helm/'+url
                filename=urlsplit(url).path.rsplit('/',1)[-1]
                if filename in package['files']:downloads[filename]=get(url,True)
        assert set(downloads)==set(package['files']), '协议未能下载完整制品 '+kind
        assert all(hashlib.sha256(downloads[f]).hexdigest()==sha for f,sha in package['files'].items())
        report['制品协议'].append({'类型':kind,'名称':name,'文件数':len(downloads),'逐文件摘要':'全部一致',**details})
    if fixture.get('npm_registry_cases'):
        case=fixture['npm_registry_cases'];index=get(registry+'/npm/'+quote(case['name'],safe=''));actual=index['versions'][case['version']]
        assert (actual.get('description') or '')==(case.get('description') or '') and actual['dependencies']==case['dependencies']
        assert (actual.get('readme') or '')==(case.get('readme') or '') and (index.get('readme') or '')==(case.get('readme') or '')
        report['注册表元数据差异']='原生索引保留注册表说明与实际依赖，多版本标签和全部文件均核对'
    assert report['C产物执行'], '没有从目标下载并执行真实 C 编译产物'
    report['对象覆盖']={'依赖闭包项目':str(len(projects))+'/'+str(len(manifests)),'原生对象':sum(r['对象数'] for r in report['项目']),'全部文件':len(report['历史文件']),'协议':str(len({p['类型'] for p in report['制品协议']}))+'/6','独立来源对象文件清单':'逐项一致'}
    if a.merge:
        if not pull['merged']:
            deadline=time.monotonic()+60
            while time.monotonic()<deadline:
                pull=api('/repos/'+root['final_path']+'/pulls/1')
                if pull.get('mergeable'):break
                time.sleep(1)
            assert pull.get('mergeable'), '真实跨仓库 PR 不能合并'
            r=client.post(base+'/api/v1/repos/'+root['final_path']+'/pulls/1/merge',json={'Do':'merge','merge_when_checks_succeed':False},timeout=90)
            assert r.status_code in (200,201,204), '真实跨仓库 PR 合并失败 HTTP '+str(r.status_code)
        assert api('/repos/'+root['final_path']+'/pulls/1')['merged'];report['跨仓库合并']='真实原生 API 合并通过'
    output=a.manager_state.parent/('advanced-acceptance-'+str(a.run_id)+'.json');output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('完整高级基准验收通过：'+str(output))

if __name__=='__main__':main()
