#!/usr/bin/env python3
"""新建独立标记 Gitea，真实 Git 十五轮更新，验证台账及原评论状态。"""
import argparse
import base64
import json
import ipaddress
import os
from pathlib import Path
import secrets
import subprocess
import time
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--keep', action='store_true', help='保留独立实例供浏览器核验')
    args = parser.parse_args()
    ident = 'gm-issue-lifecycle-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
    folder = ROOT / 'work' / ident; folder.mkdir(mode=0o700)
    token = password = ''
    def run(argv, **kwargs):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120, **kwargs)
        if result.returncode:
            detail = result.stderr
            for secret in (token, password):
                if secret: detail = detail.replace(secret, '[已隐藏]')
            raise RuntimeError('隔离验收命令失败：' + detail[-6000:])
        return result.stdout
    ids = run(['docker', 'network', 'ls', '-q']).split()
    networks = json.loads(run(['docker', 'network', 'inspect', *ids]))
    used = [ipaddress.ip_network(c['Subnet']) for n in networks
            for c in (n.get('IPAM', {}).get('Config') or []) if c.get('Subnet')]
    subnet = next(str(n) for n in ipaddress.ip_network('10.253.192.0/20').subnets(new_prefix=28)
                  if not any(other.version == 4 and n.overlaps(other) for other in used))
    run(['docker', 'network', 'create', '--subnet', subnet,
         '--label', 'io.gitea-manager.inline-test=true', ident])
    run(['docker', 'volume', 'create', '--label', 'io.gitea-manager.inline-test=true', ident])
    try:
        run(['docker', 'run', '-d', '--name', ident, '--platform', 'linux/amd64',
            '--label', 'io.gitea-manager.inline-test=true', '--network', ident, '--network-alias', 'inline-gitea',
            '-p', '127.0.0.1::3000', '-v', ident + ':/data',
            '-e', 'GITEA__database__DB_TYPE=sqlite3', '-e', 'GITEA__security__INSTALL_LOCK=true',
            '-e', 'GITEA__service__DISABLE_REGISTRATION=true', '-e', 'GITEA__repository__DEFAULT_BRANCH=main',
            'gitea/gitea:1.26.4'])
        info = json.loads(run(['docker', 'inspect', ident]))[0]
        base = 'http://127.0.0.1:' + info['NetworkSettings']['Ports']['3000/tcp'][0]['HostPort']
        http = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 60
        while True:
            try:
                with http.open(base + '/api/v1/version', timeout=2) as response:
                    assert json.load(response)['version'] == '1.26.4'
                break
            except Exception:
                if time.monotonic() >= deadline: raise RuntimeError('隔离 Gitea 未就绪')
                time.sleep(.5)
        cli = ['docker', 'exec', '--user', 'git', ident, 'gitea', '--config', '/data/gitea/conf/app.ini', 'admin', 'user']
        password = secrets.token_urlsafe(32)
        run(cli + ['create', '--username', 'reviewer', '--password', password,
                   '--email', 'reviewer@example.invalid', '--must-change-password=false', '--admin'])
        token = run(cli + ['generate-access-token', '--username', 'reviewer', '--token-name', ident,
                           '--scopes', 'all', '--raw']).strip().splitlines()[-1]
        def api(method, path, body=None):
            with http.open(Request(base + '/api/v1' + path, method=method,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={'Authorization': 'token ' + token, 'Content-Type': 'application/json'}), timeout=15) as response:
                return json.load(response)
        repo = api('POST', '/user/repos', {'name': 'inline-fixture', 'auto_init': True, 'default_branch': 'main'})
        user = api('GET', '/user')
        source = folder / 'fixture'
        env = dict(os.environ, GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.extraHeader',
            GIT_CONFIG_VALUE_0='Authorization: Basic ' + base64.b64encode(('reviewer:'+token).encode()).decode())
        def git(*argv): return run(['git', *argv], cwd=source, env=env)
        run(['git', 'clone', base + '/reviewer/inline-fixture.git', str(source)], env=env)
        git('config', 'user.name', '行内验收'); git('config', 'user.email', 'reviewer@example.invalid')
        (source / 'main.c').write_text('int f(int *p) { return p ? *p : 0; }\n')
        git('add', '.'); git('commit', '-m', '建立安全基线'); git('push', 'origin', 'main')
        git('switch', '-c', 'change')
        state = folder / 'state'; state.mkdir(mode=0o700)
        results = []
        for iteration in range(1, 16):
            status = 'present' if iteration <= 12 or iteration == 15 else 'fixed' if iteration == 13 else 'obsolete'
            body = 'int f(int *p) { return *p; }\n' if status == 'present' else 'int f(int *p) { if (!p) return 0; return *p; }\n' if status == 'fixed' else 'int f(int *p) { return p ? *p : 0; }\n'
            (source / 'main.c').write_text(('// 行号移动\n' * iteration if status != 'obsolete' else '') + body)
            git('add', '.'); git('commit', '-m', '真实版本变化 ' + str(iteration)); git('push', '-u', 'origin', 'change')
            if iteration == 1:
                pr = api('POST', '/repos/reviewer/inline-fixture/pulls', {'head':'change', 'base':'main', 'title':'问题台账十五轮隔离验收'})
            output = run(['docker', 'run', '--rm', '-i', '--platform', 'linux/amd64', '--network', ident,
                '-e', 'AI_REVIEW_GITEA_HOSTS=inline-gitea', '-e', 'AI_REVIEW_HOST_POLICY_FILE=', '-e', 'PYTHONPATH=/app',
                '-v', str(state)+':/state', '-v', str(ROOT/'deployment/test_issue_lifecycle_gitea.py')+':/test.py:ro',
                '--entrypoint', 'python', args.image, '/test.py'], input=json.dumps({'token':token, 'repo_name':repo['full_name'],
                    'repo_id':repo['id'], 'bot_id':user['id'], 'number':pr['number'], 'status':status, 'round':iteration}))
            results.append(json.loads(output)); print(output, end='', flush=True)
        (folder/'acceptance.json').write_text(json.dumps({'说明':'真实 Git 与官方 Gitea 1.26.4；模型为固定协议替身', '结果':results},ensure_ascii=False,indent=2))
        (folder/'browser.json').write_text(json.dumps({'地址':base, '用户名':'reviewer', '密码':password, '容器':ident},ensure_ascii=False)); (folder/'browser.json').chmod(0o600)
        print('浏览器地址：' + base + '/reviewer/inline-fixture/pulls/1')
        print('验收目录：' + str(folder))
    finally:
        if args.keep:
            print('保留独立标记实例：' + ident)
        else:
            run(['docker', 'rm', '-f', ident])
            run(['docker', 'volume', 'rm', ident])
            run(['docker', 'network', 'rm', ident])


if __name__ == '__main__': main()
