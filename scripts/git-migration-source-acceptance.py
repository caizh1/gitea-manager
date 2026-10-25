#!/usr/bin/env python3
"""创建独立来源容器，验证真实平台认证与项目发现；保留现场，不操作现有实例。"""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from git_migration.providers import Provider, version_tuple
from git_migration.transport import Transport
from git_migration.inventory import Inventory


def docker(*args, data=None, timeout=120):
    result = subprocess.run(['docker', *args], input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        # 容器或认证命令的 stderr 可能含配置，不把全文写入证据。
        raise RuntimeError('隔离 Docker 操作失败：' + args[0])
    return result.stdout.decode().strip()


def api(base, path, token=None, body=None, auth=None):
    response = requests.request('POST' if body is not None else 'GET', base + path,
                                headers={'Authorization': 'Bearer ' + token} if token else {},
                                auth=auth, json=body, timeout=30, allow_redirects=False)
    if not 200 <= response.status_code < 300:
        raise RuntimeError('隔离来源 API 返回 HTTP ' + str(response.status_code))
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', required=True, choices=['gitea', 'gitlab'])
    parser.add_argument('--image', required=True)
    parser.add_argument('--resume-state', type=Path, help='恢复已启动但尚未初始化样本的独立来源')
    parser.add_argument('--verify-existing', action='store_true', help='已有固定样本时仅复验，避免重复创建')
    args = parser.parse_args()
    if args.resume_state:
        state = json.loads(args.resume_state.read_text())
        folder = args.resume_state.parent
        prefix, password, base = state['name'], state['password'], state['base']
        details = json.loads(docker('inspect', prefix))[0]
        if details['Config']['Labels'].get('io.gitea-manager.test') != 'git-migration-source' or state['kind'] != args.kind or state['image'] != args.image:
            raise RuntimeError('恢复对象与请求的独立来源不一致')
    else:
        prefix = 'gm-git-source-' + args.kind + '-' + time.strftime('%Y%m%d%H%M%S') + '-' + secrets.token_hex(2)
        folder = ROOT / 'work' / 'git-migration' / prefix
        folder.mkdir(mode=0o700, parents=True)
        password = secrets.token_urlsafe(28)
        state = {'说明': '仅本次隔离来源验收资源，含随机凭据，不得发布', 'name': prefix,
                 'kind': args.kind, 'image': args.image, 'password': password}
        state_path = folder / 'resources.json'

        def save():
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
            state_path.chmod(0o600)

        save()
        image = json.loads(docker('image', 'inspect', args.image))[0]
        state['image_id'] = image['Id']
        save()
        label = 'io.gitea-manager.test=git-migration-source'
        docker('volume', 'create', '--label', label, prefix)
        common = ['--name', prefix, '--platform', 'linux/amd64', '--label', label,
                  '--cpus', '2', '--pids-limit', '1024']
        if args.kind == 'gitea':
            docker('run', '-d', *common, '--memory', '512m', '--memory-swap', '512m',
                   '-p', '127.0.0.1::3000', '-v', prefix + ':/data',
                   '-e', 'GITEA__database__DB_TYPE=sqlite3', '-e', 'GITEA__security__INSTALL_LOCK=true',
                   '-e', 'GITEA__service__DISABLE_REGISTRATION=true',
                   '-e', 'GITEA__server__LFS_START_SERVER=true',
                   '-e', 'GITEA__server__ROOT_URL=http://127.0.0.1:3000/',
                   '-e', 'INSTALL_LOCK=true', '-e', 'DISABLE_REGISTRATION=true',
                   '-e', 'LFS_START_SERVER=true', '-e', 'RUN_MODE=prod', args.image)
            port = docker('port', prefix, '3000/tcp').rsplit(':', 1)[1]
        else:
            config = ("external_url 'http://127.0.0.1'; "
                      "gitlab_rails['initial_root_password'] = '" + password + "'; "
                      "puma['worker_processes'] = 0; puma['min_threads'] = 1; puma['max_threads'] = 2; "
                      "sidekiq['concurrency'] = 2; prometheus_monitoring['enable'] = false; "
                      "gitlab_kas['enable'] = false; registry['enable'] = false; "
                      "gitlab_rails['gitlab_shell_ssh_port'] = 22")
            env = folder / 'container.env'
            env.write_text('GITLAB_OMNIBUS_CONFIG=' + config + '\n')
            env.chmod(0o600)
            docker('run', '-d', *common, '--memory', '3g', '--memory-swap', '3g', '--shm-size', '256m',
                   '-p', '127.0.0.1::80', '-v', prefix + ':/var/opt/gitlab', '--env-file', str(env), args.image)
            port = docker('port', prefix, '80/tcp').rsplit(':', 1)[1]
        base = 'http://127.0.0.1:' + port
        state['base'] = base
        save()
    deadline = time.monotonic() + 1200
    ready_path = '/api/v1/version' if args.kind == 'gitea' else '/users/sign_in'
    while time.monotonic() < deadline:
        try:
            response = requests.get(base + ready_path, timeout=5)
            if response.status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(3)
    else:
        raise RuntimeError('隔离来源在时限内未启动，已保留现场：' + prefix)
    if args.verify_existing:
        if not args.resume_state:
            raise RuntimeError('复验必须指定原隔离资源')
        username = 'source-admin' if args.kind == 'gitea' else 'root'
    else:
        if args.kind == 'gitea':
            legacy = version_tuple(api(base, '/api/v1/version')['version']) < (1, 12, 0)
            action = 'admin create-user' if legacy else 'admin user create'
            command = ('gitea --config /data/gitea/conf/app.ini ' + action + ' --username source-admin '
                       '--password ' + password + ' --email source-admin@example.test --admin --must-change-password=false\n')
            docker('exec', '-i', '-u', 'git', prefix, 'sh', data=command.encode())
            token = api(base, '/api/v1/users/source-admin/tokens', auth=('source-admin', password),
                        body={'name': '隔离夹具准备', 'scopes': ['all']})['sha1']
            headers = {'Authorization': 'token ' + token}
            for name in ['source-project', 'private-project']:
                response = requests.post(base + '/api/v1/user/repos', headers=headers,
                    json={'name': name, 'private': name.startswith('private'), 'auto_init': True}, timeout=30)
                if response.status_code != 201:
                    raise RuntimeError('创建隔离 Gitea 项目失败')
            username = 'source-admin'
            path = '/api/v1/repos/source-admin/source-project'
            issue = api(base, path + '/issues', token, {'title': '迁移验收议题', 'body': '保留中文与原始上下文'})
            api(base, path + '/issues/' + str(issue['number']) + '/comments', token, {'body': '迁移验收评论'})
            api(base, path + '/labels', token, {'name': '迁移验收', 'color': '#336699' if legacy else '336699'})
        else:
            response = requests.post(base + '/oauth/token', data={'grant_type': 'password', 'username': 'root',
                                      'password': password, 'scope': 'api'}, timeout=30)
            if response.status_code != 200:
                raise RuntimeError('隔离 GitLab 密码认证未成功')
            token = response.json()['access_token']
            group = api(base, '/api/v4/groups', token, {'name': '来源群组', 'path': 'source-group', 'visibility': 'private'})
            child = api(base, '/api/v4/groups', token, {'name': '来源子群组', 'path': 'sub-group', 'parent_id': group['id']})
            for name, namespace in [('source-project', group['id']), ('private-project', child['id'])]:
                api(base, '/api/v4/projects', token, {'name': name, 'namespace_id': namespace, 'visibility': 'private', 'initialize_with_readme': True})
            username = 'root'
    http = Transport(base)
    try:
        provider = Provider(http, args.kind, username, password)
        result = provider.discover()
        selected = next(p for p in result['projects'] if p['path'].endswith('/source-project'))
        inventory = Inventory(provider).inspect(selected)
    finally:
        http.close()
    names = {row['path'].rsplit('/', 1)[-1] for row in result['projects']}
    if names != {'source-project', 'private-project'}:
        raise RuntimeError('真实来源项目发现结果与固定样本不一致')
    if args.kind == 'gitea':
        issues = next(d['rows'] for d in inventory['datasets'] if d['category'] == '议题与合并请求')
        comments = next(d['rows'] for d in inventory['datasets'] if d['category'] == '议题评论')
        if not any(i['title'] == '迁移验收议题' for i in issues) or not any(c['body'] == '迁移验收评论' for c in comments):
            raise RuntimeError('真实 Gitea 议题或评论盘点遗漏')
    evidence = {'说明': '真实隔离平台的来源发现验收，不代表迁移、完整资料、企业许可或生产验收',
                '平台': args.kind, '版本': result['version'], '镜像': args.image,
                '镜像摘要': state['image_id'], '项目数': len(result['projects']),
                '项目路径': [row['path'] for row in result['projects']], '结果': '通过',
                '盘点对象数': inventory['object_count'], '盘点缺口': inventory['gaps'],
                '平台限制': 'macOS ARM 上运行 Linux amd64 容器'}
    (folder / 'acceptance.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    print('来源发现验收通过，证据：' + str(folder / 'acceptance.json'), flush=True)


if __name__ == '__main__':
    main()
