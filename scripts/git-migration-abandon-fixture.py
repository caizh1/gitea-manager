#!/usr/bin/env python3
"""从成功计划新建一条独立验收任务，在真实写入后停止，不修改数据库状态。"""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import time
import requests
from git_migration_acceptance_helpers import create_verified_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manager-state', type=Path, required=True)
    parser.add_argument('--completed-run-id', type=int, required=True)
    parser.add_argument('--target-state', type=Path, required=True)
    args = parser.parse_args()
    state = json.loads(args.manager_state.read_text())
    worker = state['name'] + '-worker'
    info = json.loads(subprocess.check_output(['docker', 'inspect', worker], text=True))[0]
    assert info['Config']['Labels'].get('io.gitea-manager.test') == 'git-migration-manager'
    client = requests.Session()
    client.trust_env = False
    base = state['base'] + '/api'

    def call(method, path, data=None):
        r = client.request(method, base + path, json=data, timeout=45)
        r.raise_for_status()
        return r.json()

    client.headers['X-CSRF-Token'] = call('GET', '/session')['csrf_token']
    client.headers['X-CSRF-Token'] = call('POST', '/login', {'password': state['password']})['csrf_token']
    original = call('GET', '/git-migrations/runs/' + str(args.completed_run_id))
    assert original['status'] == 'succeeded'
    request = {k:v for k,v in original['plan']['request'].items() if k!='preflight_id'}
    # 已交接路径不能复用；建立继承同一授权的新测试群组和新人员确认记录。
    target=json.loads(args.target_state.read_text())
    native = requests.Session()
    native.trust_env = False
    def target_call(method,path,data=None):
        reply=native.request(method,target['base']+'/api/v1'+path,headers={'Authorization':'token '+target['token']},json=data,timeout=60)
        reply.raise_for_status();return reply.json()
    group=target_call('POST','/governance/groups',{'parent_id':request['group_id'],'path':'abort-'+secrets.token_hex(5),'name':'终止验收隔离群组','visibility':2})
    request.update(group_id=group['id'],request_key=secrets.token_hex(20))
    if request.get('mapping_id'):
        mapping=call('POST','/git-migrations/account-mappings',{k:request[k] for k in ('inventory_id','server_id','group_id','request_key')})
        mapping_path='/git-migrations/account-mappings/'+str(mapping['id'])
        for _ in range(1200):
            mapping=call('GET',mapping_path)
            if mapping['status'] not in ('queued','running'):break
            time.sleep(.3)
        assert mapping['status']=='succeeded'
        for row in mapping['rows']:
            names={u['login'] for key in row['principal_keys'] for u in original['plan']['principal_map'][key]}
            ids=[target_call('GET','/users/'+name)['id'] for name in sorted(names)]
            mapping=call('PATCH',mapping_path,{'revision':mapping['revision'],'action':'select','key':row['key'],'target_ids':ids})
            mapping=call('PATCH',mapping_path,{'revision':mapping['revision'],'action':'confirm','key':row['key']})
        assert mapping['ready']
        request['mapping_id']=mapping['id']
    created = create_verified_run(call, request)
    path = '/git-migrations/runs/' + str(created['id'])
    for _ in range(1800):
        row = call('GET', path)
        if any(p.get('repo_id') for p in row['checkpoint'].values()):
            break
        assert row['status'] in ('queued', 'running'), '未形成真实写入，不能视为有现场终止验收'
        time.sleep(0.1)
    else:
        raise RuntimeError('等待实际写入超时')
    subprocess.run(['docker', 'stop', '-t', '20', worker], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(['docker', 'start', worker], check=True, stdout=subprocess.DEVNULL)
    for _ in range(120):
        row = call('GET', path)
        if row['status'] == 'interrupted':
            break
        assert row['status'] in ('queued', 'running'), '任务未在预期检查点停止'
        time.sleep(0.5)
    else:
        raise RuntimeError('执行器重启没有回收停止任务')
    assert not any(p.get('handed_off') for p in row['checkpoint'].values()), '终止夹具不应触及业务交接'
    print('创建终止验收任务：' + str(created['id']))


if __name__ == '__main__':
    main()
