#!/usr/bin/env python3
"""从成功计划新建一条独立验收任务，在真实写入后停止，不修改数据库状态。"""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import time
import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manager-state', type=Path, required=True)
    parser.add_argument('--completed-run-id', type=int, required=True)
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
    request = dict(original['plan']['request'], request_key=secrets.token_hex(20))
    created = call('POST', '/git-migrations/runs', request)
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
