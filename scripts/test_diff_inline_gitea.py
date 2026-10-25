#!/usr/bin/env python3
"""新建独立标记 Gitea，真实 Git 提交及 PR，验证候选交付镜像中的发布器。"""
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
    parser.add_argument('--test-script', default='deployment/test_diff_inline_gitea.py')
    args = parser.parse_args()
    ident = 'gm-diff-inline-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
    folder = ROOT / 'work' / ident; folder.mkdir(mode=0o700)
    token = password = ''
    def run(argv, **kwargs):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120, **kwargs)
        if result.returncode:
            detail = result.stderr
            for secret in (token, password):
                if secret: detail = detail.replace(secret, '[已隐藏]')
            failure = folder / 'failure.json'
            if not failure.exists():
                failure.write_text(json.dumps({'结果': '失败', '错误': detail}, ensure_ascii=False, indent=2) + '\n')
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
        (source / 'main.c').write_text('int value = 1;\n' + '// 上下文\n' * 20 + 'int tail = 1;\n')
        (source / 'deleted.c').write_text(''.join(f'int deleted_{i} = 1;\n' for i in range(20)))
        (source / 'original.c').write_text('int before = 1;\n' + '// 保留上下文\n' * 12)
        (source / 'space name.c').write_text('int before = 1;\n')
        (source / 'unicode.c').write_bytes('/* 原始注释 */\r\nint before = 1;\r\n'.encode())
        (source / 'no_newline.c').write_text('/* 原始注释 */\nint before = 1;')
        (source / 'replacement.c').write_text(''.join(f'int before_{i} = 1;\n' for i in range(12)))
        git('add', '.'); git('commit', '-m', '建立定位验收基线'); git('push', 'origin', 'main')
        git('switch', '-c', 'change')
        (source / 'main.c').write_text('int value = 0;\n' + '// 上下文\n' * 20 + 'int tail = 0;\n')
        (source / 'deleted.c').unlink()
        (source / 'added.c').write_text(''.join(f'int added_{i} = 0;\n' for i in range(20)))
        (source / 'original.c').rename(source / 'renamed.c')
        (source / 'renamed.c').write_text('int after = 0;\n' + '// 保留上下文\n' * 12)
        (source / 'space name.c').write_text('int after = 0;\n')
        (source / 'unicode.c').write_bytes('/* 新注释 */\r\nint after = 0;\r\n'.encode())
        (source / 'no_newline.c').write_text('/* 新注释 */\nint after = 0;')
        (source / 'replacement.c').write_text(''.join(f'int after_{i} = 0;\n' for i in range(12)))
        git('add', '.'); git('commit', '-m', '覆盖新旧侧及重命名'); git('push', 'origin', 'change')
        pr = api('POST', '/repos/reviewer/inline-fixture/pulls', {'head': 'change', 'base': 'main', 'title': '行内定位隔离验收'})
        print('独立 Gitea 与真实 Git PR 已就绪，正在验证候选镜像发布器', flush=True)
        output = run(['docker', 'run', '--rm', '-i', '--platform', 'linux/amd64', '--network', ident,
            '-e', 'AI_REVIEW_GITEA_HOSTS=inline-gitea', '-e', 'AI_REVIEW_HOST_POLICY_FILE=',
            '-e', 'PYTHONPATH=/app', '-v', str(ROOT/args.test_script)+':/test.py:ro',
            '--entrypoint', 'python', args.image, '/test.py'], input=json.dumps({
                'token': token, 'repo_name': repo['full_name'], 'repo_id': repo['id'],
                'bot_id': user['id'], 'number': pr['number']}))
        (folder / 'acceptance.json').write_text(output)
        print(output, end='')
    finally:
        run(['docker', 'rm', '-f', ident])
        run(['docker', 'volume', 'rm', ident])
        run(['docker', 'network', 'rm', ident])


if __name__ == '__main__': main()
