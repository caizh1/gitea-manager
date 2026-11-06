#!/usr/bin/env python3
"""为现有独立来源增加 npm 第二版本，故意区分注册表元数据和归档字段。"""
import argparse,base64,hashlib,io,json,shutil,subprocess,tarfile
from pathlib import Path
from urllib.parse import quote,urlsplit
import requests


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-state',type=Path,required=True);p.add_argument('--fixture',type=Path,required=True);a=p.parse_args();s=json.loads(a.source_state.read_text());f=json.loads(a.fixture.read_text())
    assert json.loads(subprocess.check_output(['docker','container','inspect',s['name']]))[0]['Config']['Labels']['io.gitea-manager.test']=='git-migration-source'
    c=requests.Session();c.trust_env=False;owner='root' if s['kind']=='gitlab' else 'source-admin'
    if s['kind']=='gitlab':
        token=c.post(s['base']+'/oauth/token',data={'grant_type':'password','username':owner,'password':s['password'],'scope':'api'},timeout=60).json();c.headers['Authorization']='Bearer '+token['access_token']
    else:c.auth=(owner,s['password'])
    package=next(v for v in f['packages'] if v['type']=='npm');name=package['name'];version='1.2.4';filename=name+'-'+version+'.tgz'
    archive_doc={'name':name,'version':version,'description':'归档中的原始说明','dependencies':{'fixture-dep':'^2.0.0'},'engines':{'node':'>=18'}}
    output=io.BytesIO()
    with tarfile.open(fileobj=output,mode='w:gz') as archive:
        data=json.dumps(archive_doc,ensure_ascii=False).encode();member=tarfile.TarInfo('package/package.json');member.size=len(data);member.mtime=1700000000;archive.addfile(member,io.BytesIO(data))
    raw=output.getvalue();readme='# 注册表版本说明\n\n此说明和安装依赖应以源注册表为准。\n'
    index_version={**archive_doc,'description':'注册表中的实际说明','dependencies':{'fixture-dep':'^3.0.0'},'readme':readme,'dist':{'shasum':hashlib.sha1(raw).hexdigest(),'integrity':'sha512-'+base64.b64encode(hashlib.sha512(raw).digest()).decode(),'tarball':s['base']+'/'+filename}}
    doc={'name':name,'readme':readme,'dist-tags':{'latest':version},'versions':{version:index_version},'_attachments':{filename:{'content_type':'application/octet-stream','data':base64.b64encode(raw).decode(),'length':len(raw)}}}
    route='/api/v4/projects/'+str(f['project']['id'])+'/packages/npm/'+quote(name,safe='') if s['kind']=='gitlab' else '/api/packages/'+owner+'/npm/'+quote(name,safe='')
    exists=c.get(s['base']+route,timeout=60)
    if exists.status_code==404 or version not in exists.json().get('versions',{}):
        result=c.put(s['base']+route,json=doc,timeout=60);result.raise_for_status()
    actual=c.get(s['base']+route,timeout=60);actual.raise_for_status();registry=actual.json();meta=registry['versions'][version]
    assert meta['dependencies']=={'fixture-dep':'^3.0.0'}
    actual_readme=meta.get('readme') or registry.get('readme')
    if s['kind']=='gitea':assert actual_readme==readme, '源端未保存说明，不得冒充验证'
    downloaded=c.get(s['base']+urlsplit(meta['dist']['tarball']).path,allow_redirects=False,timeout=60);downloaded.raise_for_status();raw=downloaded.content
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive:assert json.load(archive.extractfile('package/package.json'))['dependencies']=={'fixture-dep':'^2.0.0'}
    backup=a.fixture.with_name(a.fixture.stem+'-before-registry.json')
    if not backup.exists():shutil.copyfile(a.fixture,backup)
    if not any(v['type']=='npm' and v['version']==version for v in f['packages']):
        f['packages'].append({'type':'npm','name':name,'version':version,'files':{filename:hashlib.sha256(raw).hexdigest()}})
    f['npm_registry_cases']={'name':name,'version':version,'dependencies':meta['dependencies'],'description':meta.get('description'),'readme':actual_readme}
    a.fixture.write_text(json.dumps(f,ensure_ascii=False,indent=2)+'\n');print('多版本注册表差异夹具通过：'+str(a.fixture))

if __name__=='__main__':main()
