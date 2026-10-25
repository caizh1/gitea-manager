#!/usr/bin/env python3
"""通过真实 HTTP、Git 和 LFS 核验独立离线实例，不输出令牌。"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument('--bundle', required=True)
parser.add_argument('--expected', required=True)
parser.add_argument('--url', default='http://127.0.0.1:35875')
parser.add_argument('--output', required=True)
parser.add_argument('--ssh-key')
parser.add_argument('--ssh-port', type=int, default=35878)
parser.add_argument('--container')
args = parser.parse_args()
root = Path(args.bundle)
expected = json.loads(Path(args.expected).read_text())
with zipfile.ZipFile(root / 'backup.zip') as archive:
    manifest = json.loads(archive.read('manager-manifest.json'))
    snapshot = json.loads(archive.read('manager-snapshot.json'))
token = manifest['API令牌']
records = []


def request(path, data=None, headers=None, anonymous=False):
    headers = dict(headers or {})
    if not anonymous and 'Authorization' not in headers:
        headers['Authorization'] = 'token ' + token
    url = path if path.startswith('http') else args.url + path
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read()


def api(path):
    return json.loads(request('/api/v1' + path))


def contains_rule(value):
    if isinstance(value, dict):
        return value.get('name') == '灾备审批规则' or any(contains_rule(v) for v in value.values())
    return isinstance(value, list) and any(contains_rule(v) for v in value)


parent = api('/governance/groups/' + str(expected['父组']))
child = api('/governance/groups/' + str(expected['子组']))
assert parent['id'] == expected['父组'] and child['parent_id'] == parent['id']
records.append('父子组层级与稳定 ID 一致')
try:
    request('/api/v1/governance/groups/' + str(parent['id']), anonymous=True)
except urllib.error.HTTPError as exc:
    assert exc.code in (401, 403, 404)
else:
    raise AssertionError('匿名访问私有组未被阻止')
records.append('原管理员可访问私有组，匿名访问被拒绝')
assert contains_rule(api('/governance/repositories/' + str(expected['仓库']) + '/approval-rules'))
records.append('审批规则可通过增强版业务接口读取')
assert api('/repos/progressadmin/after-rename')['id'] == expected['仓库']
with tempfile.TemporaryDirectory(prefix='disaster-git-') as work:
    config = Path(work) / 'git-config'
    auth = base64.b64encode(('progressadmin:' + token).encode()).decode()
    config.write_text('[http]\nextraHeader = Authorization: Basic ' + auth + '\n')
    config.chmod(0o600)
    env = dict(os.environ, GIT_CONFIG_GLOBAL=str(config), GIT_TERMINAL_PROMPT='0')
    repo = Path(work) / 'repo'
    subprocess.run(['git', 'clone', '--quiet', args.url + '/progressadmin/after-rename.git', str(repo)], env=env, check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'fsck', '--full'], env=env, check=True, capture_output=True)
    head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], env=env).decode().strip()
    assert head in next(iter(snapshot['Git仓库'].values()))['提交']
records.append('改名仓库经真实 Git clone 与 fsck 校验，提交保持一致')
if args.ssh_key:
    import shlex
    with tempfile.TemporaryDirectory(prefix='disaster-ssh-') as work:
        known = Path(work) / 'known-hosts'
        known.write_text('[127.0.0.1]:' + str(args.ssh_port) + ' ' + expected['SSH主机公钥'] + '\n')
        command = shlex.join(['ssh', '-F', '/dev/null', '-i', str(Path(args.ssh_key).resolve()), '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(known)])
        env = dict(os.environ, GIT_SSH_COMMAND=command, GIT_TERMINAL_PROMPT='0')
        subprocess.run(['git', 'clone', '--quiet', 'ssh://git@127.0.0.1:' + str(args.ssh_port) + '/progressadmin/after-rename.git', str(Path(work) / 'repo')], env=env, check=True, capture_output=True)
    records.append('Git SSH 使用恢复前的主机公钥及用户密钥完成 clone，未跳过主机身份检查')
basic = base64.b64encode(('progressadmin:' + token).encode()).decode()
result = json.loads(request('/progressadmin/after-rename.git/info/lfs/objects/batch',
    {'operation': 'download', 'transfers': ['basic'], 'objects': [{'oid': expected['LFS摘要'], 'size': expected['LFS大小']}]},
    {'Authorization': 'Basic ' + basic, 'Accept': 'application/vnd.git-lfs+json', 'Content-Type': 'application/vnd.git-lfs+json'}))
action = result['objects'][0]['actions']['download']
body = request(action['href'], headers=action.get('header'))
assert len(body) == expected['LFS大小'] and hashlib.sha256(body).hexdigest() == expected['LFS摘要']
records.append('真实 LFS 下载成功，正文大小与 SHA-256 一致')
if 'Actions摘要' in expected:
    assert args.container, '制品与启动依赖验收需要指定本次隔离恢复容器'
    for filename, key in (('/data/actions-external/review-proof.bin', 'Actions摘要'),
                          ('/data/gitea/conf/manager-startup-oauth2-jwt_secret', 'OAuth密钥摘要')):
        digest = subprocess.check_output(['docker', 'exec', args.container, 'sha256sum', filename]).decode().split()[0]
        assert digest == expected[key]
    records.append('Actions 正文与外置 OAuth 密钥的 SHA-256 均与源一致')
    status = subprocess.run(['docker', 'exec', args.container, 'test', '-e', '/external-config/oauth-jwt'])
    assert status.returncode == 1
    records.append('未依赖原外置密钥挂载，实际离线实例已正常启动')
with sqlite3.connect('file:' + str(root / 'manager-state.sqlite') + '?mode=ro', uri=True) as connection:
    assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
records.append('离线包内 Manager 主数据库快照完整性检查通过')
Path(args.output).write_text(json.dumps({'说明': '源容器已销毁；新恢复实例的真实业务检查', '检查': records}, ensure_ascii=False, indent=2))
print(json.dumps({'结果': '通过', '检查': records}, ensure_ascii=False))
