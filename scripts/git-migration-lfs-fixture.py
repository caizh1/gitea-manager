#!/usr/bin/env python3
"""在隔离来源增加两版历史 LFS、分支和标签，并实际重新下载核对。"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import subprocess
import sys
import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from git_migration.providers import Provider
from git_migration.transport import Transport
from git_migration.git_snapshot import GitSnapshot


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-state',type=Path,required=True);p.add_argument('--fixture',type=Path,required=True)
    p.add_argument('--inside-fixture',action='store_true',help='由宿主机核对测试标签后，在无管理挂载的隔离生产器中运行')
    p.add_argument('--resume',action='store_true',help='继续已保留的本地提交，不重新创建版本')
    a=p.parse_args()
    state,fixture=json.loads(a.source_state.read_text()),json.loads(a.fixture.read_text())
    if a.inside_fixture:
        assert os.environ.get('GM_MIGRATION_FIXTURE_SOURCE')==state['name'], '缺少宿主机核对的来源标识'
        state['base']=state['base'].replace('127.0.0.1','host.docker.internal')
    else:
        info=json.loads(subprocess.check_output(['docker','inspect',state['name']]))[0]
        assert info['Config']['Labels'].get('io.gitea-manager.test')=='git-migration-source'
    assert 'lfs' not in fixture, '已建立 LFS 夹具，不能重复推送'
    http=Transport(state['base']);kind=state['kind'];owner='root' if kind=='gitlab' else 'source-admin'
    producer=Provider(http,kind,owner,state['password']);producer.authenticate();credential=None
    if kind=='gitlab':
        credential,_=http.request('POST','/oauth/token',data={'grant_type':'password','username':owner,'password':state['password'],'scope':'api'})
        r=requests.post(state['base']+'/api/v4/users/1/personal_access_tokens',headers={'Authorization':'Bearer '+credential['access_token']},
                        json={'name':'隔离 LFS 写入-'+secrets.token_hex(3),'scopes':['api'],'expires_at':(datetime.now(timezone.utc)+timedelta(days=1)).date().isoformat()},timeout=60)
        r.raise_for_status();credential=r.json();producer=Provider(http,kind,owner,credential['token'],'token');producer.authenticate()
    project={'id':fixture['project']['id'],'path':fixture['project'].get('path_with_namespace') or fixture['project']['full_name']}
    folder=a.fixture.parent/(a.fixture.stem+'-lfs');snapshot=GitSnapshot(producer,project,folder/('retry-'+secrets.token_hex(3)) if a.resume else folder)
    checkout=folder/'checkout'
    if not a.resume: snapshot.git('clone','--',snapshot.url,str(checkout))
    else: snapshot.git('remote','set-url','origin',snapshot.url,cwd=checkout)
    snapshot.git('config','user.name','迁移验收作者',cwd=checkout);snapshot.git('config','user.email','migration@example.invalid',cwd=checkout)
    if not a.resume: snapshot.git('checkout','-b','migration-lfs-'+secrets.token_hex(3),cwd=checkout)
    snapshot.git('lfs','install','--local',cwd=checkout)
    (checkout/'.gitattributes').write_text('*.bin filter=lfs diff=lfs merge=lfs -text\n')
    hashes=[]
    for index in (1,2):
        data=bytes(range(256))*256+('历史 LFS 版本 '+str(index)).encode();hashes.append(hashlib.sha256(data).hexdigest())
        if not a.resume:
            (checkout/'historical-build.bin').write_bytes(data);snapshot.git('add','.',cwd=checkout)
            snapshot.git('commit','-m','历史构建 '+str(index)+' [skip ci]',cwd=checkout)
    if not a.resume: snapshot.git('tag','-a','migration-lfs-tag-'+secrets.token_hex(3),'-m','历史构建标记',cwd=checkout)
    try:
        snapshot.git('push','--all','origin',cwd=checkout);snapshot.git('push','--tags','origin',cwd=checkout)
    except Exception as error:
        from urllib.parse import urlsplit
        message=str(error.__context__ or error)
        message=re.sub(r'https?://[^\s"<>]+',lambda m:urlsplit(m.group()).scheme+'://'+(urlsplit(m.group()).hostname or '')+(':'+str(urlsplit(m.group()).port) if urlsplit(m.group()).port else '')+urlsplit(m.group()).path,message)
        message=re.sub(r'(?i)authorization[^\n]*','认证头已隐藏',message)
        message=re.sub(r'[A-Za-z0-9_-]{30,}','[长标识已隐藏]',message)
        print('隔离 LFS 诊断：'+message,flush=True)
        raise
    captured=GitSnapshot(producer,project,folder/'verify').capture()
    assert set(hashes)<={r['oid'] for r in captured['lfs_objects']}
    fixture['lfs']={'说明':'两版实际上传并重新下载核对的历史 LFS','oids':hashes}
    a.fixture.write_text(json.dumps(fixture,ensure_ascii=False,indent=2)+'\n')
    if kind=='gitlab':
        r=requests.delete(state['base']+'/api/v4/personal_access_tokens/'+str(credential['id']),headers={'PRIVATE-TOKEN':credential['token']},timeout=30);r.raise_for_status()
    http.close();print('历史 LFS、分支与标签真实核验通过')


if __name__=='__main__':main()
