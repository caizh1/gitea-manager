"""在本任务的独立安装实例上验证原升级入口与 SVN 凭据、计划保留。"""
import argparse
import ipaddress
import json
from pathlib import Path
import secrets
import subprocess
import time
from urllib.request import build_opener, ProxyHandler, HTTPCookieProcessor, Request
import http.cookiejar

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--installation', type=Path, required=True)
parser.add_argument('--package-dir', type=Path, required=True)
args = parser.parse_args()
directory = args.installation.resolve()
state_path = directory / 'state/installation.json'
state = json.loads(state_path.read_text())
assert directory.name.startswith('svn-installation-') and state['directory'] == str(directory) and state['complete']
compose_path = directory / 'compose.install.json'
config = json.loads(compose_path.read_text())
command = ['docker', 'compose', '-p', state['project'], '-f', str(compose_path)]


def run(argv, **kwargs):
    result = subprocess.run(argv, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError('隔离命令失败：' + result.stderr[-1500:])
    return result.stdout


backend = run(command + ['ps', '-q', 'backend']).strip()
metadata = json.loads(run(['docker', 'inspect', backend]))[0]
assert metadata['Config']['Labels']['com.docker.compose.project'] == state['project']
browser = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))
csrf = ''


def api(method, path, value=None):
    data = json.dumps(value).encode() if value is not None else None
    req = Request(state['url'] + '/api' + path, data=data, method=method,
                  headers={'Content-Type': 'application/json', 'X-CSRF-Token': csrf})
    with browser.open(req, timeout=30) as response:
        return json.load(response)


csrf = api('GET', '/session')['csrf_token']
login = api('POST', '/login', {'username': 'admin', 'password': state['admin_password']})
csrf = login.get('csrf_token') or api('GET', '/session')['csrf_token']
if login.get('user', {}).get('must_change_password'):
    changed = secrets.token_urlsafe(32)
    result = api('POST', '/access/password', {'current_password': state['admin_password'], 'new_password': changed})
    state['admin_password'] = changed
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2)); state_path.chmod(0o600)
    csrf = result.get('csrf_token') or api('GET', '/session')['csrf_token']
api('POST', '/settings', {'host_ip': '127.0.0.1'})
if not api('GET', '/svn-migrations/sources'):
    source = api('POST', '/svn-migrations/sources', {'name': '升级保留验收来源', 'slug': 'upgrade-fixture',
        'url': 'svn://127.0.0.1/fixture', 'username': 'migration', 'password': secrets.token_urlsafe(32)})
    repo = api('POST', f'/svn-migrations/sources/{source["id"]}/repositories',
        {'name': 'fixture', 'url': 'svn://127.0.0.1/fixture/project'})[0]
    target = api('POST', '/servers', {'name': '升级保留测试目标', 'host': '127.0.0.1', 'ssh_user': 'test',
        'gitea_url': 'http://127.0.0.1:1', 'api_token': secrets.token_hex(20), 'is_local': False})
    api('POST', '/svn-migrations/plans', {'name': '升级前草稿', 'server_id': target['id'], 'repositories': [{'id': repo['id']}]})

probe = """
import hashlib,json,sqlite3
from pathlib import Path
from cryptography.fernet import Fernet
p=Path('/app/data'); c=sqlite3.connect('file:/app/data/gitea-manager.db?mode=ro',uri=True)
result={}
for table in ('svn_sources','svn_repositories','svn_migration_plans','svn_migration_runs','svn_migration_items','svn_migration_events','svn_target_bindings','svn_reservations'):
 rows=c.execute('select * from '+table+' order by rowid').fetchall()
 result[table]={'行数':len(rows),'摘要':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()}
key=(p/'svn-migration.key').read_bytes();result['密钥摘要']=hashlib.sha256(key).hexdigest()
result['全部密码可解密']=all(bool(Fernet(key).decrypt(row[0].encode())) for row in c.execute("select password_ciphertext from svn_sources where password_ciphertext!=''"))
print(json.dumps(result,ensure_ascii=False))
"""
before = json.loads(run(['docker', 'exec', backend, 'python', '-c', probe]))
# 桌面升级只接受显式测试标签；仅重建本脚本核对过的新安装后端。
if metadata['Config']['Labels'].get('io.gitea-manager.upgrade-test') != 'true':
    config['services']['backend'].setdefault('labels', {})['io.gitea-manager.upgrade-test'] = 'true'
    compose_path.write_text(json.dumps(config, ensure_ascii=False, indent=2)); compose_path.chmod(0o600)
    run(command + ['up', '-d', '--no-build', '--pull', 'never', '--no-deps', 'backend'])
    backend = run(command + ['ps', '-q', 'backend']).strip()

name = state['project'] + '_gm-review-execution'
if subprocess.run(['docker', 'network', 'inspect', name], capture_output=True).returncode:
    ids = run(['docker', 'network', 'ls', '-q']).split()
    networks = json.loads(run(['docker', 'network', 'inspect', *ids]))
    used = [ipaddress.ip_network(c['Subnet']) for n in networks for c in (n.get('IPAM', {}).get('Config') or []) if c.get('Subnet')]
    subnet = next(n for n in ipaddress.ip_network('10.249.0.0/16').subnets(new_prefix=24)
                  if not any(n.version == u.version and n.overlaps(u) for u in used))
    run(['docker', 'network', 'create', '--internal', '--subnet', str(subnet),
         '--label', 'io.gitea-manager.test=svn-upgrade', name])

started = time.monotonic()
result = subprocess.run(['bash', str(args.package_dir.resolve() / 'upgrade.sh'), '--container', backend, '--desktop-test'], capture_output=True, text=True)
output = result.stdout + '\n' + result.stderr
for key in ('admin_password', 'secret_key', 'master_key', 'executor_token', 'access_authorization_token'):
    if state.get(key): output = output.replace(state[key], '[隔离秘密已隐藏]')
(directory / 'upgrade-output.txt').write_text(output)
(directory / ('upgrade-output-' + str(time.time_ns()) + '.txt')).write_text(output)
assert result.returncode == 0, '升级未通过，保留现场：' + str(directory / 'upgrade-output.txt')
backend = run(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=' + state['project'], '--filter', 'label=com.docker.compose.service=backend']).strip()
after = json.loads(run(['docker', 'exec', backend, 'python', '-c', probe]))
assert before == after, '升级前后的 SVN 记录、凭据或密钥不一致'
worker = run(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=' + state['project'], '--filter', 'label=com.docker.compose.service=svn-migration-worker']).strip()
run(['docker', 'exec', worker, 'python', '-m', 'svn_migration.worker', '--check'])
assert api('GET', '/session')['authenticated'], '原登录会话未保留'
report = {'状态':'通过', '升级秒数':round(time.monotonic()-started, 2), '项目':state['project'],
          '包摘要':(args.package_dir / 'gitea-manager.tar.gz.sha256').read_text().split()[0],
          'SVN记录与密钥保持一致':True, '原会话保留':True, '执行器检查':'通过', '数据摘要':after,
          '边界':'独立 Docker Desktop Linux amd64 原安装与原升级入口；不代表 Ubuntu systemd 真机或生产 SVN 验收。'}
report_path = directory / 'upgrade-acceptance.json'
if report_path.exists():
    (directory / ('upgrade-acceptance-' + str(time.time_ns()) + '.json')).write_bytes(report_path.read_bytes())
report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
print('原升级入口通过，SVN 来源、计划、加密凭据、密钥和登录会话保持一致')
