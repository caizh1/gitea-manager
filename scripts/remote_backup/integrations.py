"""通过隔离接收器验证认证、邮件、Webhook 和镜像的实际调用。"""
import base64
import json
import re
import time

import requests

from .actions import web_session
from .driver import CheckFailure


def events(lab):
    path = lab.root / 'integrations' / 'integration-events.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def await_event(lab, since, predicate, timeout=45):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        for item in events(lab)[since:]:
            if predicate(item):
                return item
        time.sleep(0.5)
    raise CheckFailure('隔离接收器没有收到符合断言的真实请求')


def seed(lab):
    node = 'a'
    state = lab.state['nodes'][node]
    if state.get('integrations_seeded'):
        return
    if 'integrations' not in lab.state:
        raise CheckFailure('先构建并部署隔离集成服务')
    credentials = lab.state['integrations']
    host = lab.state['addresses']['integration']
    compose = lab.compose_config(node)
    compose['services']['gitea']['environment'].update({
        'GITEA__mailer__ENABLED': 'true', 'GITEA__mailer__PROTOCOL': 'smtp',
        'GITEA__mailer__SMTP_ADDR': host, 'GITEA__mailer__SMTP_PORT': '1025',
        'GITEA__mailer__FROM': 'remote-lab@lab.invalid',
        'GITEA__webhook__ALLOWED_HOST_LIST': host,
        'GITEA__migrations__ALLOWED_DOMAINS': '127.0.0.1',
        'GITEA__migrations__ALLOW_LOCALNETWORKS': 'true',
        'GITEA__service__ENABLE_NOTIFY_MAIL': 'true',
        'GITEA__oauth2_client__ENABLE_AUTO_REGISTRATION': 'true'})
    lab.put(node, '/lab/compose.json', json.dumps(compose))
    lab.command(node, ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d', 'gitea'])
    lab.wait_gitea(node)
    current = lab.command(node, ['docker', 'exec', '-u', 'git', 'gitea', 'gitea', 'admin', 'auth', 'list'])[1].decode()
    if 'lab-ldap' not in current:
        lab.command(node, ['docker', 'exec', '-u', 'git', 'gitea', 'gitea', 'admin', 'auth', 'add-ldap',
            '--name', 'lab-ldap', '--host', host, '--port', '1389', '--security-protocol', 'unencrypted',
            '--bind-dn', 'cn=admin,dc=lab,dc=invalid', '--bind-password', credentials['ldap_password'],
            '--user-search-base', 'dc=lab,dc=invalid', '--user-filter', '(uid=%s)',
            '--username-attribute', 'uid', '--email-attribute', 'mail', '--firstname-attribute', 'cn'])
    if 'lab-oauth' not in current:
        url = 'http://' + host + ':8081'
        lab.command(node, ['docker', 'exec', '-u', 'git', 'gitea', 'gitea', 'admin', 'auth', 'add-oauth',
            '--name', 'lab-oauth', '--provider', 'github', '--key', 'remote-lab', '--secret', credentials['oauth_secret'],
            '--use-custom-urls=true', '--custom-auth-url', url + '/authorize', '--custom-token-url', url + '/token',
            '--custom-profile-url', url + '/user', '--custom-email-url', url + '/emails'])
    lab.command(node, ['docker', 'restart', 'gitea'])
    lab.wait_gitea(node)
    hooks = lab.api(node, 'GET', '/repos/labadmin/proof/hooks').json()
    url = 'http://' + host + ':8081/webhook/proof'
    if not any(item['config'].get('url') == url for item in hooks):
        hook = lab.api(node, 'POST', '/repos/labadmin/proof/hooks', json={'type': 'gitea',
            'config': {'url': url, 'content_type': 'json', 'secret': credentials['webhook_secret']},
            'events': ['push'], 'active': True}).json()
    else:
        hook = next(item for item in hooks if item['config'].get('url') == url)
    state['integration_hook'] = hook['id']
    mirror = lab.api(node, 'GET', '/repos/labadmin/proof-mirror', expected=None)
    if mirror.status_code == 404:
        lab.api(node, 'POST', '/repos/migrate', json={'clone_addr': 'http://127.0.0.1:3000/labadmin/proof.git',
            'auth_username': 'labadmin', 'auth_password': state['password'], 'repo_name': 'proof-mirror',
            'repo_owner': 'labadmin', 'mirror': True, 'private': True, 'service': 'git'})
    with lab.case('integrations-seed', '源实例隔离集成实际调用'):
        verify(lab, node, preparation=True)
    state['integrations_seeded'] = True
    lab.save()


def verify_mail(lab, node):
    start = len(events(lab))
    base = lab.gitea_url(node)
    session, _ = web_session(lab, node)
    page = session.get(base + '/-/admin/config', timeout=30)
    match = re.search(r'name="_csrf"\s+value="([^"]+)"', page.text)
    recipient = 'probe-' + str(time.time_ns()) + '@lab.invalid'
    fields = {'email': recipient}
    if match:
        fields['_csrf'] = match.group(1)
    response = session.post(base + '/-/admin/config/test_mail', data=fields, timeout=30)
    if response.status_code != 200:
        raise CheckFailure('实际发信操作失败')
    await_event(lab, start, lambda item: item['类型'] == '邮件' and recipient in item['收件人'])


def verify_webhook(lab, node):
    start = len(events(lab))
    identifier = lab.state['nodes'][node]['integration_hook']
    lab.api(node, 'POST', '/repos/labadmin/proof/hooks/%s/tests' % identifier, expected=(200, 201, 204))
    await_event(lab, start, lambda item: item['类型'] == 'Webhook' and item['签名有效'])


def verify_ldap(lab, node):
    credentials = lab.state['integrations']
    base = lab.gitea_url(node)
    ldap = requests.Session(); ldap.trust_env = False
    ldap.headers.update({'Origin': base, 'Referer': base + '/user/login'})
    ldap.get(base + '/user/login', timeout=30)
    response = ldap.post(base + '/user/login', data={'user_name': 'labldap', 'password': credentials['ldap_password']}, timeout=30)
    if '/user/login' in response.url or 'labldap' not in response.text:
        raise CheckFailure('恢复后的 LDAP 真实登录失败')
    require_account(ldap, base, 'labldap')


def verify_oauth(lab, node):
    start = len(events(lab))
    base = lab.gitea_url(node)
    oauth = requests.Session(); oauth.trust_env = False
    oauth.headers.update({'Origin': base, 'Referer': base + '/user/login'})
    response = oauth.get(base + '/user/oauth2/lab-oauth', timeout=30)
    # 首次自动注册可能需要确认表单；有界处理原生确认页面。
    if '/user/sign_up' in response.url:
        raise CheckFailure('OAuth 回调停留在注册页，需要显式完成原生账号关联后再验收')
    if response.status_code != 200 or 'laboauth' not in response.text:
        raise CheckFailure('恢复后的 OAuth 真实回调未登录')
    require_account(oauth, base, 'laboauth')
    await_event(lab, start, lambda item: item['类型'] == 'OAuth换令牌')
    await_event(lab, start, lambda item: item['类型'] == 'OAuth身份')


def require_account(session, base, username):
    response = session.get(base + '/user/settings', timeout=30)
    if response.status_code != 200 or '/user/settings' not in response.url or username not in response.text:
        raise CheckFailure('回调后未建立指定账号的真实登录会话')


def verify_mirror(lab, node):
    before = lab.api(node, 'GET', '/repos/labadmin/proof/branches/main').json()['commit']['id']
    filename = 'mirror-probe-' + str(time.time_ns()) + '.txt'
    update = lab.api(node, 'POST', '/repos/labadmin/proof/contents/' + filename,
        json={'content': base64.b64encode('恢复后镜像同步'.encode()).decode(), 'message': '验证镜像同步', 'branch': 'main'}).json()
    expected = update['commit']['sha']
    if before == expected:
        raise CheckFailure('镜像测试没有新增实际提交')
    lab.api(node, 'POST', '/repos/labadmin/proof-mirror/mirror-sync', expected=(200, 201, 202, 204))
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        actual = lab.api(node, 'GET', '/repos/labadmin/proof-mirror/branches/main').json()['commit']['id']
        if actual == expected:
            break
        time.sleep(1)
    else:
        raise CheckFailure('恢复后的镜像未同步到新增提交')


def verify(lab, node, preparation=False):
    failed = []
    for category, label, function in [('mail', '真实 SMTP 发信', verify_mail),
                                      ('webhook', '真实签名 Webhook', verify_webhook),
                                      ('ldap', '真实 LDAP 登录', verify_ldap),
                                      ('oauth', '真实 OAuth 登录', verify_oauth),
                                      ('mirror', '真实镜像同步', verify_mirror)]:
        try:
            with lab.case('integrations-seed' if preparation else 'integrations-' + category, node + '：' + label):
                function(lab, node)
        except Exception:
            failed.append(label)
    if failed:
        raise CheckFailure('各集成已分别检查，失败：' + '、'.join(failed))
    lab.note('integrations-partial', node + '：集成验证', '通过', 'LDAP、OAuth、签名 Webhook、隔离 SMTP 和镜像均有实际调用证据；源准备不能代替恢复后验收')


def migration(lab):
    from .scenarios import compare_target
    from .oracle import snapshot
    lab.login()
    if not lab.state['nodes']['a'].get('integrations_seeded'):
        raise CheckFailure('外部集成源样本尚未实际通过')
    with lab.case('integrations', '跨网段恢复后的五类外部集成'):
        before = snapshot(lab, 'a')
        lab.backup('a')
        result = lab.restore('b')
        if result['status'] != 'success':
            raise CheckFailure('集成恢复未成功：' + result.get('error_msg', ''))
        changes = compare_target(lab, before, 'b', 'integrations')
        verify(lab, 'b')
        if changes:
            raise CheckFailure('恢复后集成数据或业务配置存在独立差异')
