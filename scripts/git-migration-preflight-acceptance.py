#!/usr/bin/env python3
"""真实隔离预迁移：附件、LFS、来源变化阻断、现场关闭及最终交接。"""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import requests

spec=importlib.util.spec_from_file_location('fixtures',Path(__file__).with_name('git-migration-account-acceptance.py'))
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manager-state',type=Path,required=True)
    parser.add_argument('--rich-fixture',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    state=json.loads(args.manager_state.read_text());source=json.loads(Path(state['source_state']).read_text());target=json.loads(Path(state['target_state']).read_text());rich=json.loads(args.rich_fixture.read_text())
    assert json.loads(fixtures.docker('inspect',state['name']+'-backend'))[0]['Config']['Labels']['io.gitea-manager.test']=='git-migration-mapping'
    for item,label in ((source,'git-migration-source'),(target,'git-migration-receiver')):
        assert json.loads(fixtures.docker('inspect',item['name']))[0]['Config']['Labels']['io.gitea-manager.test']==label
    session=requests.Session();session.trust_env=False
    def call(method,path,data=None,raw=False):
        response=session.request(method,state['base']+'/api'+path,json=data,timeout=90)
        if raw:return response
        if not 200<=response.status_code<300:raise RuntimeError('隔离管理接口失败：'+path+' '+response.text[:600])
        return response.json()
    session.headers['X-CSRF-Token']=call('GET','/session')['csrf_token']
    session.headers['X-CSRF-Token']=call('POST','/login',{'password':state['password']})['csrf_token']
    report={'说明':'固定接收镜像内真实隔离预迁移，不代表生产和全版本验证','断言':[]}
    def check(ok,name):
        if not ok:raise AssertionError(name)
        report['断言'].append(name);fixtures.save(args.output/'preflight-acceptance.json',report)
    def poll(path,statuses=('queued','running')):
        return fixtures.wait(lambda:(lambda x:x if x['status'] not in statuses else None)(call('GET',path)),'预迁移完整链路',1800)
    def target_api(method,path,data=None):return fixtures.api(target['base'],method,path,token=target['token'],body=data)
    source_api=lambda method,path,data=None:fixtures.api(source['base'],method,path,auth=('source-admin',source['password']),body=data)
    previous_inventory=call('GET','/git-migrations/inventories/'+str(state['inventory_id']))
    original_source=next(row for row in call('GET','/git-migrations/sources') if row['id']==previous_inventory['source_id'])
    # 向本次独立夹具补充真实 LFS 二进制，不接触用户业务仓库。
    directory=args.output/'lfs-source';remote=source['base']+'/'+rich['repo']['full_name']+'.git'
    env={**os.environ,'GIT_CONFIG_COUNT':'2','GIT_CONFIG_KEY_0':'http.extraHeader','GIT_CONFIG_VALUE_0':'Authorization: Basic '+base64.b64encode(('source-admin:'+source['password']).encode()).decode(),'GIT_CONFIG_KEY_1':'http.proxy','GIT_CONFIG_VALUE_1':''}
    def git(*cmd):
        r=subprocess.run(['git',*cmd],cwd=directory if directory.exists() else args.output,env=env,capture_output=True)
        if r.returncode:raise RuntimeError('隔离 LFS 夹具 Git 操作失败：'+cmd[0])
    if not directory.exists():git('clone',remote,str(directory.resolve()))
    git('config','user.name','迁移验收');git('config','user.email','migration@example.test');git('lfs','install','--local');git('lfs','track','firmware.bin')
    binary=b'\x01\x02\x03\x04'*262144
    (directory/'firmware.bin').write_bytes(binary)
    git('add','.gitattributes','firmware.bin')
    status=subprocess.run(['git','status','--porcelain'],cwd=directory,env=env,capture_output=True,text=True)
    if status.stdout.strip():git('commit','-m','添加隔离 LFS 验收二进制');git('push','origin','main')
    report['LFS文件摘要']=hashlib.sha256(binary).hexdigest()
    group=target_api('POST','/governance/groups',{'parent_id':0,'path':'preflight-'+secrets.token_hex(5),'name':'预迁移完整验收','visibility':2})
    def prepare():
        found=call('POST',f"/git-migrations/sources/{original_source['id']}/discoveries",{'source_version':original_source['version'],'request_key':secrets.token_hex(16)})
        found=poll('/git-migrations/discoveries/'+str(found['id']))
        check(found['status']=='succeeded','完整来源发现通过')
        project=next(p for p in found['result']['projects'] if p['path']==rich['repo']['full_name'])
        inventory=call('POST',f"/git-migrations/sources/{original_source['id']}/inventories",{'discovery_id':found['id'],'project_ids':[project['id']],'capture_code':True,'request_key':secrets.token_hex(16)})
        inventory=poll('/git-migrations/inventories/'+str(inventory['id']))
        check(inventory['status']=='succeeded','附件与 LFS 全量盘点通过')
        mapping=call('POST','/git-migrations/account-mappings',{'inventory_id':inventory['id'],'server_id':state['server_id'],'group_id':group['id'],'request_key':secrets.token_hex(16)})
        endpoint='/git-migrations/account-mappings/'+str(mapping['id']);mapping=poll(endpoint)
        admin=target_api('GET','/user')
        for row in mapping['rows']:
            mapping=call('PATCH',endpoint,{'revision':mapping['revision'],'action':'select','key':row['key'],'target_ids':[admin['id']]})
            mapping=call('PATCH',endpoint,{'revision':mapping['revision'],'action':'confirm','key':row['key']})
        check(mapping['ready'],'固定夹具的人员处理全部确认')
        return {'inventory_id':inventory['id'],'server_id':state['server_id'],'group_id':group['id'],'mapping_id':mapping['id'],'request_key':secrets.token_hex(16)}
    request=prepare()
    before={r['id'] for r in call('GET','/git-migrations/runs')}
    check(call('POST','/git-migrations/runs',{**request,'source_frozen':True},True).status_code==409,'不能绕过预迁移直接创建正式任务')
    trial=call('POST','/git-migrations/preflights',request);path='/git-migrations/preflights/'+str(trial['id'])
    check(call('POST',path+'/start',{'source_frozen':True,'request_key':secrets.token_hex(16)},True).status_code==409,'运行中的预迁移不能提前开始正式迁移')
    trial=poll(path);fixtures.save(args.output/'ready.json',trial)
    check(trial['can_start'] and trial['result']['rehearsal']=='passed','实际隔离预迁移与检查全部通过')
    check(before=={r['id'] for r in call('GET','/git-migrations/runs')},'预迁移不创建正式任务')
    check(call('POST',path+'/start',{'source_frozen':False,'request_key':secrets.token_hex(16)},True).status_code==400,'没有来源停写确认不能正式启动')
    checkpoint=json.loads(fixtures.docker('exec',state['name']+'-backend','python','-c',"import sqlite3;print(sqlite3.connect('/app/data/gitea-manager.db').execute('select checkpoint_json from git_migration_preflights where id=?',("+str(trial['id'])+",)).fetchone()[0])"))
    staged=next(p for k,p in checkpoint.items() if k.isdigit())
    repo=target_api('GET','/repositories/'+str(staged['repo_id']))
    check(repo['private'] and repo['owner']['login']=='migration-admin','实际预迁移副本仍在管理员私有空间')
    check(len(staged['assets'])>=3 and staged['snapshot']['lfs_objects'] and staged['wiki_verified'],'全部附件、通用制品、LFS 和 Wiki 已传输并回读')
    issue_path='/repos/'+rich['repo']['full_name']+'/issues/'+str(rich['issue']);old=source_api('GET',issue_path)['body']
    source_api('PATCH',issue_path,{'body':old+'\n预迁移后新增的真实来源内容'})
    call('POST',path+'/start',{'source_frozen':True,'request_key':secrets.token_hex(16)})
    blocked=poll(path);fixtures.save(args.output/'source-changed.json',blocked)
    check(blocked['status']=='blocked' and not blocked['run_id'],'来源变化时复检阻止正式任务创建')
    check(any(c['status']=='blocked' and c.get('code')=='source_changed' for c in blocked['result']['checks']),'报告准确标明来源变化阻断原因')
    check(before=={r['id'] for r in call('GET','/git-migrations/runs')},'复检失败没有遗漏创建正式任务')
    call('POST',path+'/cancel',{});cancelled=poll(path)
    check(cancelled['status']=='cancelled','结束预迁移后正确关闭接收会话')
    archived=target_api('GET','/repositories/'+str(staged['repo_id']))
    check(archived['archived'] and archived['private'],'结束后仅将本任务隔离副本归档保留')
    source_api('PATCH',issue_path,{'body':old})
    request=prepare();trial=call('POST','/git-migrations/preflights',request);path='/git-migrations/preflights/'+str(trial['id']);trial=poll(path)
    check(trial['can_start'],'新快照重新预迁移通过')
    launch={'source_frozen':True,'request_key':secrets.token_hex(16)}
    call('POST',path+'/start',launch);trial=poll(path)
    check(trial['status']=='consumed' and trial['run_id'],'全部复检通过后才创建正式任务')
    check(call('POST',path+'/start',launch)['run_id']==trial['run_id'],'重复启动同一预迁移不会重复创建任务')
    run=poll('/git-migrations/runs/'+str(trial['run_id']));fixtures.save(args.output/'completed.json',run)
    check(run['status']=='succeeded','附件、LFS、协作和群组交接全部完成')
    check(all(p['code_verified'] and p['objects_verified'] and p['source_verified'] and p['handed_off'] for p in run['checkpoint'].values()),'所有仓库完成代码与对象完整性核对')
    report.update(结果='通过',最终预迁移=trial['id'],阻断预迁移=blocked['id'],正式迁移=run['id'])
    fixtures.save(args.output/'preflight-acceptance.json',report)
    print('真实预迁移与失败门禁验收通过：'+str(args.output/'preflight-acceptance.json'),flush=True)

if __name__=='__main__':main()
