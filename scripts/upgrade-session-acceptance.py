#!/usr/bin/env python3
"""使用真实登录 Cookie 持续轮询会话，验证独立实例升级和历史数据核对。"""
import argparse
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import threading
import time
from urllib.request import build_opener, ProxyHandler, HTTPCookieProcessor, Request

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-dir', type=Path, required=True)
    faults = parser.add_mutually_exclusive_group()
    faults.add_argument('--expect-session-mismatch', action='store_true')
    faults.add_argument('--fail-before-backup', action='store_true')
    args = parser.parse_args()
    package = args.package_dir.resolve()
    ident = time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
    project = 'gm-session-upgrade-' + ident
    folder = ROOT / 'work' / ('session-upgrade-check-' + ident)
    folder.mkdir(mode=0o700)
    password = secrets.token_urlsafe(32)
    report = {'说明': '仅新建标记实例；真实个人登录与 HTTP 轮询，不操作用户实例。',
              '项目': project, '预期': ('停机后备份前失败并恢复原服务' if args.fail_before_backup else
                                      '复现会话表核对失败并回退' if args.expect_session_mismatch else '升级通过且原会话可恢复')}
    stop, counts = threading.Event(), {'认证成功响应': 0, '维护期间匿名响应': 0, '不可用响应': 0}
    poller = None

    def save():
        (folder / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')

    def run(argv, timeout=120):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError('隔离命令未完成：' + result.stderr.replace(password, '[隔离口令已隐藏]')[-900:])
        return result.stdout

    ids = run(['docker', 'network', 'ls', '-q']).split()
    networks = json.loads(run(['docker', 'network', 'inspect', *ids]))
    used = [ipaddress.ip_network(c['Subnet']) for n in networks
            for c in (n.get('IPAM', {}).get('Config') or []) if c.get('Subnet')]
    free = [str(n) for n in ipaddress.ip_network('10.253.208.0/20').subnets(new_prefix=28)
            if not any(other.version == 4 and n.overlaps(other) for other in used)][:2]
    assert len(free) == 2, '没有独立升级验收网段'
    def available_port():
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            return listener.getsockname()[1]

    backend_port, frontend_port = available_port(), available_port()
    compose = {'services': {
        'backend': {'image': 'gitea-review-backend:review-parameters-20261014-r1', 'platform': 'linux/amd64',
            'labels': {'io.gitea-manager.upgrade-test': 'true'}, 'ports': [f'127.0.0.1:{backend_port}:5000'],
            'environment': {'DATABASE_URL': 'sqlite:////custom/state/gitea-manager.db',
                'BACKUP_DIR': '/custom/state/backups', 'INIT_PASSWORD': password,
                'SECRET_KEY': secrets.token_urlsafe(40), 'DISABLE_BACKGROUND_TASKS': '1'},
            'volumes': ['manager-data:/custom/state', '/var/run/docker.sock:/var/run/docker.sock']},
        'frontend': {'image': 'gitea-review-frontend:review-parameters-20261014-r1',
            'platform': 'linux/amd64', 'ports': [f'127.0.0.1:{frontend_port}:80']}},
        'volumes': {'manager-data': {}}, 'networks': {
            'default': {'ipam': {'config': [{'subnet': free[0]}]}},
            'gm-review-execution': {'internal': True, 'ipam': {'config': [{'subnet': free[1]}]}}}}
    conf = folder / 'compose.json'; conf.write_text(json.dumps(compose)); conf.chmod(0o600)
    command = ['docker', 'compose', '-p', project, '-f', str(conf)]
    run(['docker', 'network', 'create', '--internal', '--subnet', free[1],
         '--label', 'com.docker.compose.project='+project, project+'_gm-review-execution'])
    save()
    try:
        run(command + ['up', '-d', '--no-build', '--pull', 'never'])
        backend = run(command + ['ps', '-q', 'backend']).strip()
        info = json.loads(run(['docker', 'inspect', backend]))[0]
        port = info['NetworkSettings']['Ports']['5000/tcp'][0]['HostPort']
        base = 'http://127.0.0.1:' + port
        cookies = http.cookiejar.CookieJar()
        browser = build_opener(ProxyHandler({}), HTTPCookieProcessor(cookies))
        deadline = time.monotonic()+60
        while True:
            try:
                with browser.open(base+'/api/session', timeout=2) as response:
                    csrf = json.load(response)['csrf_token']
                break
            except Exception:
                if time.monotonic() >= deadline: raise RuntimeError('隔离实例未就绪')
                time.sleep(.2)
        with browser.open(Request(base+'/api/login', data=json.dumps({'password': password}).encode(),
                headers={'Content-Type': 'application/json', 'X-CSRF-Token': csrf}), timeout=10) as response:
            assert json.load(response)['authenticated'], '未取得真实个人登录会话'
        cookie = '; '.join(c.name+'='+c.value for c in cookies)
        count = run(['docker', 'exec', backend, 'python', '-c',
            "import sqlite3; c=sqlite3.connect('/custom/state/gitea-manager.db'); print(c.execute('SELECT count(*) FROM access_sessions').fetchone()[0])"]).strip()
        assert count == '1', '未建立原有会话数据'
        report['升级前已建立个人会话'] = True

        def polling():
            client = build_opener(ProxyHandler({}))
            while not stop.is_set():
                try:
                    with client.open(Request(base+'/api/session', headers={'Cookie': cookie}), timeout=.5) as response:
                        value = json.load(response)
                    counts['认证成功响应' if value['authenticated'] else '维护期间匿名响应'] += 1
                except Exception:
                    counts['不可用响应'] += 1
                stop.wait(.08)

        poller = threading.Thread(target=polling, daemon=True); poller.start()
        began = time.monotonic()
        environment = os.environ.copy()
        if args.fail_before_backup:
            shim = folder/'fault-injection'; shim.mkdir(mode=0o700)
            docker = shutil.which('docker')
            wrapper = shim/'docker'
            wrapper.write_text('#!/usr/bin/env python3\n# 停机后、数据快照生成前注入故障，核对恢复原库而非半份备份。\n'
                'import os,sys\nargs=sys.argv[1:]\n'
                'if args[-1:]==["backup"] and any(a.endswith("upgrade_runtime.py") for a in args): sys.exit(42)\n'
                'os.execv('+repr(docker)+',["docker",*args])\n')
            wrapper.chmod(0o700); environment['PATH'] = str(shim)+os.pathsep+environment.get('PATH', '')
        result = subprocess.run(['bash', str(package/'upgrade.sh'), '--container', info['Name'].lstrip('/'),
            '--desktop-test'], env=environment, capture_output=True, text=True, timeout=900)
        stop.set(); poller.join(timeout=2)
        output = (result.stdout+'\n'+result.stderr).replace(password, '[隔离口令已隐藏]')
        report.update(升级退出码=result.returncode, 耗时秒=round(time.monotonic()-began, 3),
                      轮询=counts, 升级输出=output)
        save()
        if args.fail_before_backup:
            assert result.returncode != 0 and '原服务恢复并通过健康检查' in output, '停机后备份前故障没有恢复原服务'
        elif args.expect_session_mismatch:
            assert result.returncode != 0 and '历史数据核对不一致：access_sessions' in output, '未复现预期的会话表变化'
            assert '原服务恢复并通过健康检查' in output, '失败后未成功回退'
        else:
            assert result.returncode == 0, '新版升级未通过'
            assert counts['维护期间匿名响应'] > 0, '未覆盖新服务维护期间的浏览器轮询'
        with browser.open(Request(base+'/api/session', headers={'Cookie': cookie}), timeout=10) as response:
            assert json.load(response)['authenticated'], '原有个人会话未恢复'
        report.update(结果='通过', 原Cookie可恢复=True)
        save(); print(json.dumps({k:v for k,v in report.items() if k!='升级输出'}, ensure_ascii=False), flush=True)
    except Exception as exc:
        report.update(结果='失败，保留现场', 首次失败=str(exc)); save(); raise
    finally:
        stop.set()
        if poller: poller.join(timeout=2)
        for cid in run(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project='+project]).split():
            run(['docker', 'stop', cid])
        print('隔离验收证据：'+str(folder/'acceptance.json'), flush=True)


if __name__ == '__main__':
    main()
