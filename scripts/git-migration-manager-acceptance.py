#!/usr/bin/env python3
"""在固定镜像与独立卷中验证来源登记、发现、盘点和维护恢复，不连接生产实例。"""
import argparse
import importlib.util
import json
from pathlib import Path
import secrets
import time
import requests

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('source_acceptance', Path(__file__).with_name('git-migration-source-acceptance.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
docker = module.docker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-tag', required=True)
    parser.add_argument('--interrupt-once', action='store_true', help='写入期间停止执行器并核对同一任务恢复')
    parser.add_argument('--target-state', type=Path, help='可选的独立接收端凭据；提供后验证完整迁移执行')
    parser.add_argument('--source-state', type=Path, required=True)
    parser.add_argument('--subnet', required=True, help='经检查未占用的独立容器网段')
    parser.add_argument('--resume-state', type=Path, help='恢复本脚本已创建的隔离环境')
    parser.add_argument('--project-name', default='source-project', help='本次核验的固定样本仓库名')
    parser.add_argument('--fixture', type=Path, help='完整固定清单，包括全部合并方式矩阵')
    parser.add_argument('--rotate-source-credential', action='store_true', help='真实验证来源凭据失效、轮换和同任务恢复')
    args = parser.parse_args()
    source = json.loads(args.source_state.read_text())
    fixture = json.loads(args.fixture.read_text()) if args.fixture else None
    details = json.loads(docker('inspect', source['name']))[0]
    if details['Config']['Labels'].get('io.gitea-manager.test') != 'git-migration-source':
        raise RuntimeError('仅接受独立来源夹具')
    name = 'gm-git-manager-' + time.strftime('%Y%m%d%H%M%S') + '-' + secrets.token_hex(2)
    previous = json.loads(args.resume_state.read_text()) if args.resume_state else None
    if previous:
        name = previous['name']
    folder = ROOT / 'work' / 'git-migration' / name
    folder.mkdir(parents=True, mode=0o700, exist_ok=bool(previous))
    password = previous['password'] if previous else secrets.token_urlsafe(28)
    label = 'io.gitea-manager.test=git-migration-manager'
    if not previous:
        docker('network', 'create', '--label', label, '--subnet', args.subnet, name)
        docker('volume', 'create', '--label', label, name)
    env = folder / 'manager.env'
    if not previous:
        env.write_text('DATABASE_URL=sqlite:////app/data/gitea-manager.db\nINIT_PASSWORD=' + password +
                       '\nSECRET_KEY=' + secrets.token_urlsafe(48) +
                       '\nDISABLE_BACKGROUND_TASKS=1\nGM_UPGRADE_MAINTENANCE_FILE=/app/data/.git-maintenance\n')
    env.chmod(0o600)
    backend = 'gitea-review-backend:' + args.image_tag
    frontend = 'gitea-review-frontend:' + args.image_tag
    git_image = 'gitea-review-git:' + args.image_tag
    common = ['--platform', 'linux/amd64', '--label', label, '--network', name,
              '--memory', '512m', '--cpus', '1', '--pids-limit', '128']
    if not previous:
        docker('run', '-d', *common, '--name', name + '-backend', '--network-alias', 'backend',
               '--env-file', str(env), '-v', name + ':/app/data', backend)
        docker('run', '-d', *common, '--name', name + '-frontend', '-p', '127.0.0.1::80', frontend)
    else:
        for suffix in ('-backend', '-frontend'):
            info = json.loads(docker('inspect', name + suffix))[0]
            if info['Config']['Labels'].get('io.gitea-manager.test') != 'git-migration-manager':
                raise RuntimeError('恢复对象不是本脚本的隔离资源')
            expected = backend if suffix == '-backend' else frontend
            if info['Image'] != json.loads(docker('image', 'inspect', expected))[0]['Id']:
                raise RuntimeError('恢复环境镜像与请求的固定镜像不一致')
    port = docker('port', name + '-frontend', '80/tcp').rsplit(':', 1)[1]
    base = 'http://127.0.0.1:' + port
    state = {'说明': '隔离验收凭据，不得发布', 'name': name, 'base': base, 'password': password,
             'backend_image': json.loads(docker('image', 'inspect', backend))[0]['Id'],
             'frontend_image': json.loads(docker('image', 'inspect', frontend))[0]['Id']}
    path = folder / 'resources.json'
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    path.chmod(0o600)
    session = requests.Session()
    session.trust_env = False

    def call(method, path, data=None):
        response = session.request(method, base + '/api' + path, json=data, timeout=30)
        if not 200 <= response.status_code < 300:
            raise RuntimeError('隔离 Manager 请求失败：' + path + ' HTTP ' + str(response.status_code))
        return response.json()

    def wait(predicate, description, seconds=120):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                result = predicate()
                if result:
                    return result
            except requests.RequestException:
                pass
            time.sleep(1)
        raise RuntimeError('等待超时：' + description)

    wait(lambda: session.get(base + '/api/session', timeout=3).status_code == 200, '管理端启动')
    session.headers['X-CSRF-Token'] = call('GET', '/session')['csrf_token']
    login = call('POST', '/login', {'password': password})
    session.headers['X-CSRF-Token'] = login['csrf_token']
    call('POST', '/settings', {'host_ip': '127.0.0.1'})
    existing_worker = docker('ps', '-aq', '--filter', 'name=^/' + name + '-worker$') if previous else ''
    if existing_worker:
        info = json.loads(docker('inspect', name + '-worker'))[0]
        if info['Config']['Labels'].get('io.gitea-manager.test') != 'git-migration-manager' or info['Image'] != json.loads(docker('image', 'inspect', git_image))[0]['Id']:
            raise RuntimeError('既有执行器不属于本次固定镜像验收')
    else:
        docker('run', '-d', *common, '--name', name + '-worker', '--env-file', str(env),
               '-v', name + ':/app/data', '--read-only', '--tmpfs', '/tmp:size=128m,mode=1777',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', git_image,
               'python', '-m', 'git_migration.worker')
    wait(lambda: call('GET', '/git-migrations/status')['worker']['online'], '发现执行器')
    source_url = source['base'].replace('127.0.0.1', 'host.docker.internal')
    registered = next((s for s in call('GET', '/git-migrations/sources') if s['url'] == source_url and s['kind'] == source['kind']), None)
    if registered is None:
        registered = call('POST', '/git-migrations/sources', {'name': '真实隔离来源 ' + source['kind'], 'kind': source['kind'],
            'url': source_url, 'username': 'source-admin' if source['kind'] == 'gitea' else 'root',
            'auth_method': 'password', 'secret': source['password']})
    discovery = call('POST', '/git-migrations/sources/' + str(registered['id']) + '/discoveries',
                     {'source_version': registered['version'], 'request_key': secrets.token_hex(20)})

    def completed(kind, ident):
        result = call('GET', '/git-migrations/' + kind + '/' + str(ident))
        if result['status'] in ('failed', 'interrupted'):
            raise RuntimeError('任务未完成：' + result['error'])
        return result if result['status'] == 'succeeded' else None

    discovery = wait(lambda: completed('discoveries', discovery['id']), '真实来源发现')
    project = next(p for p in discovery['result']['projects'] if p['path'].endswith('/' + args.project_name))
    selected_ids = [project['id']] + ([row['project_id'] for row in fixture['completeness']['merge_cases']] if fixture else [])
    inventory = call('POST', '/git-migrations/sources/' + str(registered['id']) + '/inventories',
                     {'discovery_id': discovery['id'], 'project_ids': selected_ids, 'request_key': secrets.token_hex(20), 'capture_code': True})
    inventory = wait(lambda: completed('inventories', inventory['id']), '真实元数据盘点', seconds=1800)
    migration = None
    alias_gate = False
    if args.target_state:
        target = json.loads(args.target_state.read_text())
        target_info = json.loads(docker('inspect', target['name']))[0]
        if target_info['Config']['Labels'].get('io.gitea-manager.test') != 'git-migration-receiver':
            raise RuntimeError('仅允许使用独立接收端验收')
        target_url = target['base'].replace('127.0.0.1', 'host.docker.internal')
        server = next((row for row in call('GET', '/servers') if row['gitea_url'].rstrip('/') == target_url.rstrip('/')), None)
        server = server or call('POST', '/servers', {'name': '迁移接收验收 ' + secrets.token_hex(4),
            'host': '127.0.0.1', 'ssh_user': 'root', 'gitea_port': 3000,
            'gitea_url': target_url, 'api_token': target['token']})
        reply = requests.post(target['base'] + '/api/v1/governance/groups',
            headers={'Authorization': 'token ' + target['token']}, timeout=30,
            json={'parent_id': 0, 'path': 'worker-test-' + secrets.token_hex(5), 'name': '完整执行器验收', 'visibility': 2})
        if reply.status_code not in (200, 201):
            raise RuntimeError('创建隔离接收群组失败')
        group = reply.json()
        if fixture:
            def target_api(method,path,**kwargs):
                r = requests.request(method,target['base']+'/api/v1'+path,headers={'Authorization':'token '+target['token']},timeout=60,**kwargs)
                if not 200 <= r.status_code < 300: raise RuntimeError('准备目标测试账号失败：'+path+' HTTP '+str(r.status_code))
                return r.json() if r.content else None
            target.setdefault('test_users',{})
            for login_name in ('migration-assignee','migration-reviewer','migration-ci'):
                existing = requests.get(target['base']+'/api/v1/users/'+login_name,headers={'Authorization':'token '+target['token']},timeout=30)
                if existing.status_code == 404:
                    user_password = secrets.token_urlsafe(28)
                    user = target_api('POST','/admin/users',json={'username':login_name,'password':user_password,'email':login_name+'@example.invalid','must_change_password':False})
                    target['test_users'][login_name] = {'id':user['id'],'password':user_password}
                else:
                    existing.raise_for_status();user=existing.json()
                    if login_name not in target['test_users']: raise RuntimeError('同名测试账号没有本夹具的创建回执')
                current_group = target_api('GET','/governance/groups/'+str(group['id']))
                target_api('PUT','/governance/groups/'+str(group['id'])+'/members/'+str(user['id']),json={'role':30,'revision':current_group['revision']})
            args.target_state.write_text(json.dumps(target,ensure_ascii=False,indent=2)+'\n');args.target_state.chmod(0o600)
        required = call('GET', '/git-migrations/inventories/' + str(inventory['id']) + '/principals')
        principal_map = {key: ['migration-admin'] for key in required}
        if fixture:
            principal_map.update(fixture['completeness']['principal_map'])
            if 'gitlab:ci:issuer' in required: principal_map['gitlab:ci:issuer']=['migration-ci']
        run_request = {'principal_map': principal_map, 'inventory_id': inventory['id'],
            'server_id': server['id'], 'group_id': group['id'], 'source_frozen': True, 'request_key': secrets.token_hex(20)}
        alias_data = {'name': '重复连接门禁验收', 'host': 'alias.invalid', 'ssh_user': 'root', 'gitea_port': 3000,
                      'gitea_url': target_url + '/', 'api_token': target['token']}
        alias = call('POST', '/servers', alias_data)
        try:
            blocked = session.post(base + '/api/git-migrations/runs', json=run_request, timeout=30)
            if blocked.status_code != 409 or blocked.json().get('code') != 'instance_alias':
                raise RuntimeError('重复连接记录没有在迁移写入前阻断')
        finally:
            call('DELETE', '/servers/' + str(alias['id']))
        migration = call('POST', '/git-migrations/runs', run_request)
        blocked = session.post(base + '/api/servers', json=alias_data, timeout=30)
        if blocked.status_code != 409 or blocked.json().get('code') != 'instance_alias':
            raise RuntimeError('迁移期间允许新增实例别名')
        alias_gate = True
        if args.interrupt_once:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                observed = call('GET', '/git-migrations/runs/' + str(migration['id']))
                if any(value.get('repo_id') for value in observed.get('checkpoint', {}).values()):
                    break
                if observed['status'] != 'running' and observed['status'] != 'queued':
                    raise RuntimeError('未能在写入阶段触发中断，不能计为恢复通过')
                time.sleep(0.1)
            else:
                raise RuntimeError('等待写入中断点超时')
            before_repos = {key: value['repo_id'] for key, value in observed['checkpoint'].items() if value.get('repo_id')}
            docker('stop', '-t', '20', name + '-worker')
            if args.rotate_source_credential:
                if source['kind'] != 'gitea': raise RuntimeError('真实失效验收当前使用 Gitea 密码路径')
                new_password = secrets.token_urlsafe(28)
                profile = requests.get(source['base']+'/api/v1/user',auth=('source-admin',source['password']),timeout=30)
                profile.raise_for_status()
                reply = requests.patch(source['base']+'/api/v1/admin/users/source-admin',auth=('source-admin',source['password']),json={'login_name':profile.json()['login_name'],'password':new_password,'must_change_password':False},timeout=30)
                reply.raise_for_status()
                source['password']=new_password
                args.source_state.write_text(json.dumps(source,ensure_ascii=False,indent=2)+'\n');args.source_state.chmod(0o600)
            docker('start', name + '-worker')
            wait(lambda: call('GET', '/git-migrations/runs/' + str(migration['id']))['status'] == 'interrupted', '执行器中断回收')
            resumed = call('POST', '/git-migrations/runs/' + str(migration['id']) + '/resume', {})
            if args.rotate_source_credential:
                wait(lambda:call('GET','/git-migrations/runs/'+str(migration['id']))['status']=='uncertain','真实凭据失效回收')
                call('POST','/git-migrations/sources/'+str(registered['id'])+'/credentials',{'version':registered['version'],'secret':new_password})
                resumed=call('POST','/git-migrations/runs/'+str(migration['id'])+'/resume',{})
                # 再独立验证目标令牌失效，不能依靠实例编辑绕过迁移互斥。
                target_auth=('migration-admin',target['password'])
                token_list=requests.get(target['base']+'/api/v1/users/migration-admin/tokens',auth=target_auth,timeout=30)
                token_list.raise_for_status()
                old_tokens=[token for token in token_list.json() if token.get('token_last_eight')==target['token'][-8:]]
                if len(old_tokens)!=1: raise RuntimeError('不能唯一核对待撤销的当前目标令牌')
                replacement=requests.post(target['base']+'/api/v1/users/migration-admin/tokens',auth=target_auth,
                    json={'name':'凭据恢复验收-'+secrets.token_hex(4),'scopes':['all']},timeout=30)
                replacement.raise_for_status();new_target_token=replacement.json()['sha1']
                for token in old_tokens:
                    revoked=requests.delete(target['base']+'/api/v1/users/migration-admin/tokens/'+str(token['id']),auth=target_auth,timeout=30);revoked.raise_for_status()
                # 停止后重新领取，保证下一次目标检查使用已经失效的原令牌。
                docker('stop','-t','20',name+'-worker');docker('start',name+'-worker')
                wait(lambda:call('GET','/git-migrations/runs/'+str(migration['id']))['status'] in ('interrupted','uncertain'),'目标令牌失效回收')
                stopped=call('GET','/git-migrations/runs/'+str(migration['id']))
                if stopped['status']=='interrupted':call('POST','/git-migrations/runs/'+str(migration['id'])+'/resume',{})
                wait(lambda:call('GET','/git-migrations/runs/'+str(migration['id']))['status']=='uncertain','目标令牌真实拒绝')
                call('POST','/git-migrations/runs/'+str(migration['id'])+'/target-credentials',{'token':new_target_token})
                target['token']=new_target_token
                args.target_state.write_text(json.dumps(target,ensure_ascii=False,indent=2)+'\n');args.target_state.chmod(0o600)
                resumed=call('POST','/git-migrations/runs/'+str(migration['id'])+'/resume',{})
            if resumed['id'] != migration['id'] or any(resumed['checkpoint'][key]['repo_id'] != value for key, value in before_repos.items()):
                raise RuntimeError('恢复时创建了替代任务或仓库')
        def execution_done():
            row = call('GET', '/git-migrations/runs/' + str(migration['id']))
            if row['status'] in ('failed', 'interrupted', 'uncertain', 'quarantined'):
                raise RuntimeError('完整执行未通过：' + row['stage'] + '；' + row['error'])
            return row if row['status'] == 'succeeded' else None
        migration = wait(execution_done, '完整迁移与交接', seconds=1800)
        duplicate = call('POST', '/git-migrations/runs', migration['plan']['request'])
        if duplicate['id'] != migration['id']:
            raise RuntimeError('同一执行请求重复创建任务')
    docker('exec', name + '-worker', 'python', '-m', 'git_migration.worker', '--check')
    marker = '/app/data/.git-maintenance'
    digest_code = ("import sqlite3,json,hashlib; c=sqlite3.connect('/app/data/gitea-manager.db'); "
                   "names=[r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'git_migration_%' ORDER BY name\")]; "
                   "rows=[(n,c.execute('SELECT * FROM '+n+' ORDER BY id').fetchall()) for n in names]; "
                   "print(hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest())")
    docker('exec', name + '-worker', 'python', '-c', "from pathlib import Path; Path('" + marker + "').touch()")
    try:
        time.sleep(2)
        before = docker('exec', name + '-worker', 'python', '-c', digest_code)
        blocked = session.get(base + '/api/git-migrations/status', timeout=10)
        if blocked.status_code != 503:
            raise RuntimeError('维护期间管理接口未阻断')
        docker('restart', name + '-worker')
        for attempt in range(30):
            try:
                docker('exec', name + '-worker', 'python', '-m', 'git_migration.worker', '--check')
                break
            except RuntimeError:
                if attempt == 29:
                    raise
                time.sleep(1)
        after = docker('exec', name + '-worker', 'python', '-c', digest_code)
        if before != after:
            raise RuntimeError('维护期间重启执行器改写了迁移数据')
    finally:
        docker('exec', name + '-worker', 'python', '-c', "from pathlib import Path; Path('" + marker + "').unlink(missing_ok=True)")
    wait(lambda: call('GET', '/git-migrations/status')['worker']['online'], '维护后恢复')
    evidence = {'说明': '固定镜像内代表性样本完整迁移验收；不代表全范围、生产或人工节省指标' if migration else '固定镜像内真实来源发现及元数据盘点，不代表完整迁移与生产验收', '结果': '通过',
                '写入中断恢复': bool(args.interrupt_once and migration), '真实凭据失效轮换':bool(args.rotate_source_credential and migration), '重复实例连接双向阻断': alias_gate, '迁移执行': migration, '项目数': len(discovery['result']['projects']), '盘点对象数': inventory['result']['projects'][0]['object_count'],
                '代码快照': inventory['result']['projects'][0].get('code_snapshot'), '维护重启数据一致': True,
                '管理端地址': base, '后端镜像': state['backend_image'], '前端镜像': state['frontend_image']}
    output = folder / ('acceptance-' + source['kind'] + '-' + str(inventory['id']) + '.json')
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    print('隔离 Manager 验收通过：' + str(output), flush=True)


if __name__ == '__main__':
    main()
