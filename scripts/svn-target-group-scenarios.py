"""真实增强版 Gitea 与 SVN 的目标群组迁移验收，只用于标记隔离环境。"""
import hashlib
import json
from pathlib import Path
import secrets
import time
import requests

state = json.loads(Path('/evidence/resources.json').read_text())
base = 'http://manager:5000/api'
session = requests.Session(); session.trust_env = False
gitea = requests.Session(); gitea.trust_env = False
gitea.headers['Authorization'] = 'token ' + state['gitea_token']
checks = []


def check(name, value):
    checks.append({'检查': name, '通过': bool(value)})
    Path('/evidence/target-group-acceptance.json').write_text(json.dumps({'说明': '真实隔离实例、已有成员子群组、完整 SVN 迁移与权限变化阻断', '检查': checks}, ensure_ascii=False, indent=2))
    print(('通过：' if value else '失败：') + name, flush=True)
    assert value, name


def call(method, path, data=None, expected=200):
    response = session.request(method, base + path, json=data, timeout=90)
    assert response.status_code == expected, (path, response.status_code, response.text[:1000])
    return response.json()


def remote(method, path, data=None, expected=200):
    response = gitea.request(method, 'http://gitea:3000/api/v1' + path, json=data, timeout=30)
    assert response.status_code == expected, (path, response.status_code, response.text[:1000])
    return response.json() if response.content else None


def wait(path, done):
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        result = call('GET', path)
        if result['status'] in done: return result
        time.sleep(1)
    raise AssertionError('隔离任务超时：' + path)


for _ in range(90):
    try:
        initial = call('GET', '/session'); break
    except (AssertionError, requests.RequestException): time.sleep(1)
session.headers['X-CSRF-Token'] = initial['csrf_token']
login = call('POST', '/login', {'username': 'admin', 'password': state.get('manager_password_new', state['manager_password'])})
session.headers['X-CSRF-Token'] = login.get('csrf_token') or call('GET', '/session')['csrf_token']
if login.get('user', {}).get('must_change_password'):
    state['manager_password_new'] = secrets.token_urlsafe(24)
    login = call('POST', '/access/password', {'current_password': state['manager_password'], 'new_password': state['manager_password_new']})
    session.headers['X-CSRF-Token'] = login.get('csrf_token') or call('GET', '/session')['csrf_token']
call('POST', '/settings', {'host_ip': '127.0.0.1'})
target = call('POST', '/servers', {'name': '研发 Gitea', 'host': 'gitea', 'ssh_user': 'git', 'gitea_port': 3000,
    'gitea_url': 'http://gitea:3000', 'api_token': state['gitea_token'], 'is_local': False}, 201)
target_base = '/svn-migrations/targets/' + str(target['id'])
parent = call('POST', target_base + '/groups', {'name': '固件研发', 'path': 'firmware'}, 201)
child = call('POST', target_base + '/groups', {'name': '存储固件', 'path': 'storage', 'parent_id': parent['id']}, 201)
check('用户可显式新建并选择易读的根群组和子群组', child['full_path'] == 'firmware/storage')
root_rows = call('POST', target_base + '/groups/query', {})
children = call('POST', target_base + '/groups/query', {'parent_id': parent['id']})
check('群组选择读取真实层级和建库权限', root_rows['groups'][0]['id'] == parent['id'] and children['groups'][0]['id'] == child['id'] and child['selectable'])
business_password = secrets.token_urlsafe(24)
user = remote('POST', '/admin/users', {'username': 'firmware-reader', 'email': 'reader@example.test', 'password': business_password,
    'must_change_password': False, 'send_notify': False}, 201)
parent_state = remote('GET', '/governance/groups/' + str(parent['id']))
remote('PUT', f"/governance/groups/{parent['id']}/members/{user['id']}", {'role': 20, 'revision': parent_state['revision']}, 204)
members_before = remote('GET', f"/governance/groups/{child['id']}/members")
source = call('POST', '/svn-migrations/sources', {'name': '研发 SVN', 'slug': 'target-group-source', 'url': 'svn://source-a',
    'username': 'migration', 'password': state['svn_password']}, 201)
repos = call('POST', f"/svn-migrations/sources/{source['id']}/repositories", {'repositories': [
    {'name': 'emmc_production', 'url': 'svn://source-a/project-0', 'layout': {'project': True, 'trunk': '.', 'branches': '', 'tags': ''}},
    {'name': 'project_a', 'url': 'svn://source-a/project-1', 'layout': {'project': True, 'trunk': '.', 'branches': '', 'tags': ''}}]}, 201)
paths = call('POST', target_base + '/paths', {'group_id': child['id'], 'names': [repo['name'] for repo in repos]})
check('保存前返回完整目标路径及可用状态', all(row['available'] for row in paths['repositories']) and paths['repositories'][0]['path'] == 'firmware/storage/emmc_production')
plan = call('POST', '/svn-migrations/plans', {'name': '已有成员群组迁移', 'server_id': target['id'], 'target_group': {'id': child['id']}, 'repositories': repos}, 201)
call('POST', f"/svn-migrations/plans/{plan['id']}/preflight", {}, 202)
preview = wait(f"/svn-migrations/plans/{plan['id']}", ('ready', 'blocked'))
check('已有成员的子群组通过只读预检', preview['status'] == 'ready')
check('预检及快照使用显式群组，不再生成随机前缀', all(row['owner'] == 'firmware/storage' for row in preview['preview']['repositories']))
run = call('POST', '/svn-migrations/runs', {'plan_id': plan['id'], 'preview_hash': preview['preview_hash'], 'request_key': secrets.token_hex(16), 'freeze_confirmed': True, 'full_read_confirmed': True}, 202)
finished = wait(f"/svn-migrations/runs/{run['id']}", ('success', 'failed', 'partial', 'uncertain'))
Path('/evidence/target-group-run.json').write_text(json.dumps(finished, ensure_ascii=False, indent=2))
check('两个真实 SVN 项目迁移到同一已选子群组', finished['status'] == 'success' and finished['completed'] == 2)
members_after = remote('GET', f"/governance/groups/{child['id']}/members")
check('原群组成员和授权保持不变', members_before == members_after)
for repo in repos:
    path = '/repos/firmware/storage/' + repo['name']
    result = remote('GET', path)
    check('目标私有且自动执行关闭：' + repo['name'], result['private'] and not result['has_actions'])
    reader = requests.Session(); reader.trust_env = False; reader.auth = ('firmware-reader', business_password)
    response = reader.get('http://gitea:3000/api/v1' + path, timeout=30)
    check('原有群组成员可按既有权限读取：' + repo['name'], response.status_code == 200)
conflicts = call('POST', target_base + '/paths', {'group_id': child['id'], 'names': ['emmc_production', 'new', 'NEW']})
check('已存在目标与计划内大小写重名均被阻断', [row['available'] for row in conflicts['repositories']] == [False, True, False])
extra = call('POST', f"/svn-migrations/sources/{source['id']}/repositories", {'repositories': [
    {'name': 'change_guard', 'url': 'svn://source-a/project-2', 'layout': {'project': True, 'trunk': '.', 'branches': '', 'tags': ''}}]}, 201)
changed_plan = call('POST', '/svn-migrations/plans', {'name': '权限变化阻断', 'server_id': target['id'], 'target_group': {'id': child['id']}, 'repositories': extra}, 201)
call('POST', f"/svn-migrations/plans/{changed_plan['id']}/preflight", {}, 202)
before = wait(f"/svn-migrations/plans/{changed_plan['id']}", ('ready', 'blocked'))
check('权限变化测试的预检先通过', before['status'] == 'ready')
parent_state = remote('GET', '/governance/groups/' + str(parent['id']))
remote('PUT', f"/governance/groups/{parent['id']}/members/{user['id']}", {'role': 30, 'revision': parent_state['revision']}, 204)
changed_run = call('POST', '/svn-migrations/runs', {'plan_id': changed_plan['id'], 'preview_hash': before['preview_hash'], 'request_key': secrets.token_hex(16), 'freeze_confirmed': True, 'full_read_confirmed': True}, 202)
changed = wait(f"/svn-migrations/runs/{changed_run['id']}", ('success', 'failed', 'partial', 'uncertain'))
check('预检后群组权限变化阻止写入', changed['status'] == 'failed' and '权限已变化' in changed['error'])
response = gitea.get('http://gitea:3000/api/v1/repos/firmware/storage/change_guard', timeout=30)
check('权限变化阻断后未创建目标仓库', response.status_code == 404)
state.update(target_server_id=target['id'], target_group_id=child['id'], root_group_id=parent['id'], source_id=source['id'], repository_ids=[repo['id'] for repo in repos])
Path('/evidence/resources.json').write_text(json.dumps(state, ensure_ascii=False, indent=2))
print('真实群组迁移验收全部通过', flush=True)
