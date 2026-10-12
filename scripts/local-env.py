#!/usr/bin/env python3
"""启动、初始化或停止独立本地环境；不读取或覆盖现有生产数据库。"""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'work' / '本地环境'
ENV = STATE / '.env'


def run(args, capture=False):
    env = os.environ.copy()
    docker_bin = Path('/Applications/Docker.app/Contents/Resources/bin')
    if docker_bin.is_dir():
        env['PATH'] = str(docker_bin) + os.pathsep + env.get('PATH', '')
    try:
        return subprocess.run(args, cwd=ROOT, env=env, check=True, text=True, capture_output=capture).stdout
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f'{args[0]} 执行失败，退出码 {exc.returncode}；为保护凭据不输出命令参数') from None


def env_values():
    STATE.mkdir(parents=True, exist_ok=True)
    if not ENV.exists():
        values = {key: secrets.token_urlsafe(32) for key in ('PG_PASSWORD', 'MANAGER_SECRET', 'MANAGER_PASSWORD', 'INTEGRATION_TOKEN', 'GITEA_PASSWORD')}
        with open(ENV, 'x', encoding='utf-8') as handle:
            os.chmod(ENV, 0o600)
            handle.write('# 本地独立凭据，禁止提交或装入部署包。\n')
            handle.write('\n'.join(k + '=' + v for k, v in values.items()) + '\n')
    return dict(line.split('=', 1) for line in ENV.read_text().splitlines() if '=' in line and not line.startswith('#'))


def compose(*args):
    run(['docker', 'compose', '--env-file', str(ENV), '-f', 'compose.local.yml', *args])


def wait_http(url):
    for _ in range(120):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(1)
    raise RuntimeError('服务启动后未能就绪：' + url)


def initialize(values):
    wait_http('http://127.0.0.1:3000/api/v1/version')
    wait_http('http://127.0.0.1:5480/api/session')
    cmd = ['docker', 'exec', '--user', 'git', 'gitea-policy-local', 'gitea', '--config', '/data/gitea/conf/app.ini', 'admin', 'user']
    users = run(cmd + ['list'], capture=True)
    if 'policy-admin' not in users:
        run(cmd + ['create', '--username', 'policy-admin', '--password', values['GITEA_PASSWORD'], '--email', 'policy-admin@example.invalid', '--admin', '--must-change-password=false'], capture=True)
    if 'GITEA_TOKEN' not in values:
        token = run(cmd + ['generate-access-token', '--username', 'policy-admin', '--token-name', 'manager-local-' + secrets.token_hex(4), '--scopes', 'all', '--raw'], capture=True).strip().splitlines()[-1]
        with ENV.open('a', encoding='utf-8') as handle:
            handle.write('GITEA_TOKEN=' + token + '\n')
        values['GITEA_TOKEN'] = token
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    access_csrf = ''
    def api(method, path, data=None):
        nonlocal access_csrf
        request = urllib.request.Request('http://127.0.0.1:5480/api' + path, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type': 'application/json', 'X-CSRF-Token': access_csrf})
        with opener.open(request, timeout=60) as response:
            value = json.load(response)
            if isinstance(value, dict) and value.get('csrf_token'):
                access_csrf = value['csrf_token']
            return value
    api('GET', '/session')
    api('POST', '/login', {'password': values['MANAGER_PASSWORD']})
    api('POST', '/settings', {'host_ip': '127.0.0.1'})
    if not any(s['name'] == '本地策略验收' for s in api('GET', '/servers')):
        api('POST', '/servers', {'name': '本地策略验收', 'role': 'primary', 'host': '127.0.0.1', 'ssh_user': 'git',
            'gitea_container': 'gitea-policy-local', 'pg_container': 'gitea-policy-postgres', 'pg_dbname': 'gitea', 'pg_user': 'gitea',
            'gitea_port': 3000, 'gitea_url': 'http://gitea:3000', 'api_token': values['GITEA_TOKEN']})
    credentials = STATE / '本地访问说明.txt'
    credentials.write_text('本地独立验证环境\n\nGitea：http://127.0.0.1:3000\n用户名：policy-admin\n密码：' + values['GITEA_PASSWORD'] + '\n\nManager：http://127.0.0.1:5480\n管理员密码：' + values['MANAGER_PASSWORD'] + '\n\n凭据仅限本地使用，请勿提交或分享。\n', encoding='utf-8')
    os.chmod(credentials, 0o600)
    print('本地环境已初始化。访问说明：' + str(credentials))


def main():
    parser = argparse.ArgumentParser(description='管理独立本地 Gitea 和 Manager 环境')
    parser.add_argument('action', choices=['start', 'init', 'stop', 'status'])
    args = parser.parse_args()
    values = env_values()
    if args.action == 'start':
        run(['npm', '--prefix', 'frontend', 'run', 'build'])
        compose('up', '-d', '--build')
        initialize(values)
    elif args.action == 'init':
        initialize(values)
    elif args.action == 'stop':
        compose('stop')
    else:
        compose('ps')


if __name__ == '__main__':
    main()
