"""真实 Runner、制品与日志的端到端样本；使用预置动作避免运行期外网。"""
import base64
import hashlib
import io
import json
import re
import secrets
import time
from urllib.parse import urlparse
import zipfile

import requests

from .driver import CheckFailure

REPO = '/repos/labadmin/proof'


def runner(lab, node, recreate=False):
    if 'runner_image' not in lab.state:
        raise CheckFailure('固定 Runner 镜像未加载，流水线不可标记通过')
    if recreate:
        lab.command(node, ['docker', 'rm', '-f', 'lab-runner'], check=False)
        lab.command(node, ['rm', '-f', '/lab/runner/.runner'])
    lab.command(node, ['mkdir', '-p', '/lab/runner'])
    token = lab.api(node, 'POST', REPO + '/actions/runners/registration-token').json()['token']
    network = 'remote-node-' + node + '_default'
    lab.command(node, ['docker', 'run', '--rm', '--network', network, '-v', '/lab/runner:/runner', lab.state['runner_image'],
                      'register', '--no-interactive', '--instance', lab.gitea_url(node), '--token', token,
                      '--name', 'lab-runner-' + node, '--labels', 'remote-lab:host'])
    lab.command(node, ['docker', 'run', '-d', '--name', 'lab-runner', '--network', network, '--memory', '700m',
                      '-v', '/lab/runner:/runner', lab.state['runner_image'], 'daemon'])


def runs(lab, node):
    return lab.api(node, 'GET', REPO + '/actions/workflows/disaster.yml/runs').json()['workflow_runs']


def dispatch(lab, node, outcome):
    before = {item['id'] for item in runs(lab, node)}
    lab.api(node, 'POST', REPO + '/actions/workflows/disaster.yml/dispatches',
            json={'ref': 'main', 'inputs': {'outcome': outcome}}, expected=(200, 201, 204))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        created = [item for item in runs(lab, node) if item['id'] not in before]
        if created:
            return created[0]
        time.sleep(1)
    raise CheckFailure('流水线派发后没有生成真实运行记录')


def wait(lab, node, identifier, states=('completed',), timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = lab.api(node, 'GET', REPO + '/actions/runs/' + str(identifier)).json()
        if result['status'] in states:
            return result
        time.sleep(2)
    raise CheckFailure('真实流水线未进入预期状态')


def web_session(lab, node):
    session = requests.Session(); session.trust_env = False
    base = lab.gitea_url(node)
    session.headers.update({'Origin': base, 'Referer': base + '/user/login'})
    page = session.get(base + '/user/login', timeout=30)
    match = re.search(r'name="_csrf"\s+value="([^"]+)"', page.text)
    version = lab.api(node, 'GET', '/version').json()['version']
    if not match and (not version.startswith('1.27.') or 'action="/user/login"' not in page.text):
        raise CheckFailure('未识别登录页的同源保护机制，未执行取消')
    fields = {'user_name': 'labadmin', 'password': lab.state['nodes'][node]['password']}
    if match:
        fields['_csrf'] = match.group(1)
    response = session.post(base + '/user/login', data=fields, timeout=30)
    if '/user/login' in response.url:
        raise CheckFailure('流水线取消所需的真实网页登录失败')
    return session, version


def cancel(lab, node, run):
    session, version = web_session(lab, node)
    base = lab.gitea_url(node)
    url = base + urlparse(run['html_url']).path
    page = session.get(url, timeout=30)
    match = re.search(r'<meta name="_csrf" content="([^"]+)"', page.text) or re.search(r'name="_csrf"\s+value="([^"]+)"', page.text)
    if not match and not version.startswith('1.27.'):
        raise CheckFailure('流水线页面缺少 CSRF，未执行取消')
    response = session.post(url + '/cancel', data={'_csrf': match.group(1)} if match else {}, timeout=30)
    if response.status_code not in (200, 302, 303):
        raise CheckFailure('网页取消接口未成功')


def wait_cancel_step(lab, node, identifier):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        jobs = lab.api(node, 'GET', REPO + '/actions/runs/%s/jobs' % identifier).json()['jobs']
        if any(step.get('name') == '等待取消' and step.get('status') == 'in_progress'
               for job in jobs for step in job.get('steps', [])):
            return
        time.sleep(1)
    raise CheckFailure('流水线没有进入实际等待步骤，不能把提前取消当成运行中取消')


def evidence(lab, node, identifier):
    jobs = lab.api(node, 'GET', REPO + '/actions/runs/%s/jobs' % identifier).json()['jobs']
    result = {'jobs': {}, 'artifacts': {}}
    for job in jobs:
        deadline = time.monotonic() + 30
        while True:
            response = lab.api(node, 'GET', REPO + '/actions/jobs/%s/logs' % job['id'], expected=None)
            if response.status_code == 200:
                break
            if response.status_code != 404 or time.monotonic() >= deadline:
                raise CheckFailure('已执行的流水线日志无法下载')
            time.sleep(1)
        raw = response.content
        secret = lab.state['nodes'][node].get('action_secret', '')
        if secret and secret.encode() in raw:
            raise CheckFailure('流水线日志泄露测试 Secret')
        result['jobs'][str(job['id'])] = {'sha256': hashlib.sha256(raw).hexdigest(), 'conclusion': job['conclusion']}
    data = lab.api(node, 'GET', REPO + '/actions/runs/%s/artifacts' % identifier).json()
    for artifact in data.get('artifacts', []):
        body = lab.api(node, 'GET', REPO + '/actions/artifacts/%s/zip' % artifact['id']).content
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            result['artifacts'][str(artifact['id'])] = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()}
    # r9 的 REST 制品列表明确只返回 v4；v3 用原生网页下载 ZIP 验证。
    if not result['artifacts']:
        run = lab.api(node, 'GET', REPO + '/actions/runs/%s' % identifier).json()
        session, _ = web_session(lab, node)
        url = lab.gitea_url(node) + urlparse(run['html_url']).path + '/artifacts/lab-proof'
        response = session.get(url, timeout=60)
        if response.status_code == 200 and zipfile.is_zipfile(io.BytesIO(response.content)):
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                result['artifacts']['v3:lab-proof'] = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()}
    return result


def seed(lab, node):
    state = lab.state['nodes'][node]
    if state.get('actions_seeded'):
        return
    lab.api(node, 'PATCH', REPO, json={'has_actions': True})
    state.setdefault('action_secret', secrets.token_urlsafe(24))
    lab.save()
    lab.api(node, 'PUT', REPO + '/actions/secrets/LAB_SECRET', json={'data': state['action_secret']})
    previous_variable = lab.api(node, 'GET', REPO + '/actions/variables/LAB_VARIABLE', expected=None)
    lab.api(node, 'PUT' if previous_variable.status_code == 200 else 'POST',
            REPO + '/actions/variables/LAB_VARIABLE', json={'value': '恢复后变量有效'})
    expected = hashlib.sha256(state['action_secret'].encode()).hexdigest()
    workflow = """name: 灾备真实流水线
on:
  workflow_dispatch:
    inputs:
      outcome:
        required: true
        default: success
jobs:
  verify:
    runs-on: remote-lab
    steps:
      - name: 准备离线动作
        run: mkdir -p .gitea/actions && cp -a /opt/lab-actions/upload-artifact .gitea/actions/
      - name: 验证加密数据和变量
        env:
          CHECK_SECRET: ${{ secrets.LAB_SECRET }}
          CHECK_VARIABLE: ${{ vars.LAB_VARIABLE }}
        run: |
          node -e 'const c=require("crypto");if(c.createHash("sha256").update(process.env.CHECK_SECRET).digest("hex")!=="EXPECTED")process.exit(9);if(process.env.CHECK_VARIABLE!=="恢复后变量有效")process.exit(10);require("fs").writeFileSync("artifact.txt","流水线真实制品\\n")'
          echo '流水线日志核验标记'
      - name: 上传真实制品
        uses: ./.gitea/actions/upload-artifact
        with:
          name: lab-proof
          path: artifact.txt
      - name: 等待取消
        if: ${{ inputs.outcome == 'cancel' }}
        run: sleep 300
      - name: 预期失败
        if: ${{ inputs.outcome == 'failure' }}
        run: exit 17
""".replace('EXPECTED', expected)
    path = REPO + '/contents/.gitea/workflows/disaster.yml'
    previous = lab.api(node, 'GET', path, expected=None)
    payload = {'content': base64.b64encode(workflow.encode()).decode(), 'message': '加入真实灾备流水线', 'branch': 'main'}
    if previous.status_code == 200:
        payload['sha'] = previous.json()['sha']
    lab.api(node, 'PUT' if previous.status_code == 200 else 'POST', path, json=payload)
    for old in runs(lab, node):
        if old['status'] != 'completed':
            cancel(lab, node, old)
            wait(lab, node, old['id'])
    runner(lab, node, recreate=True)
    state.setdefault('action_runs', {})
    for outcome in ('success', 'failure', 'cancel'):
        if outcome in state['action_runs']:
            state['action_runs'][outcome]['evidence'] = evidence(lab, node, state['action_runs'][outcome]['id'])
            lab.save()
            continue
        run = dispatch(lab, node, outcome)
        if outcome == 'cancel':
            wait_cancel_step(lab, node, run['id'])
            cancel(lab, node, run)
        result = wait(lab, node, run['id'])
        wanted = 'cancelled' if outcome == 'cancel' else outcome
        if result.get('conclusion') != wanted:
            raise CheckFailure('流水线预期 %s，实际 %s' % (wanted, result.get('conclusion')))
        state['action_runs'][outcome] = {'id': run['id'], 'evidence': evidence(lab, node, run['id'])}
        lab.save()
    if not state['action_runs']['success']['evidence']['artifacts']:
        raise CheckFailure('成功流水线未生成可下载制品')
    state['actions_seeded'] = True
    lab.save()


def verify(lab, node):
    state = lab.state['nodes'][node]
    for outcome, saved in state['action_runs'].items():
        result = wait(lab, node, saved['id'])
        if result.get('conclusion') != ('cancelled' if outcome == 'cancel' else outcome):
            raise CheckFailure('历史流水线结果发生变化')
        if evidence(lab, node, saved['id']) != saved['evidence']:
            raise CheckFailure('历史日志或制品正文发生变化')
    other = 'a' if node == 'b' else 'b'
    lab.command(other, ['docker', 'stop', 'lab-runner'], check=False)
    runner(lab, node, recreate=True)
    run = dispatch(lab, node, 'success')
    result = wait(lab, node, run['id'])
    if result.get('conclusion') != 'success' or not evidence(lab, node, run['id'])['artifacts']:
        raise CheckFailure('恢复后流水线未成功产出真实制品')
    lab.api(node, 'POST', REPO + '/actions/runs/%s/rerun' % run['id'])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = lab.api(node, 'GET', REPO + '/actions/runs/%s' % run['id']).json()
        if result.get('run_attempt', 0) > 1:
            break
        time.sleep(1)
    else:
        raise CheckFailure('流水线重跑未生成新尝试')
    if wait(lab, node, run['id']).get('conclusion') != 'success':
        raise CheckFailure('流水线重跑失败')
