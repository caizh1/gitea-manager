#!/usr/bin/env python3
"""以官方 Gitea 1.26.4 固定样本验收每日研发上库统计。"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def http_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def invoke(args, **kwargs):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs).stdout


class Sample:
    def __init__(self, binary, directory):
        self.binary = binary
        self.directory = directory
        self.password = secrets.token_urlsafe(24)
        self.port = http_port()
        self.origin = f'http://127.0.0.1:{self.port}'
        self.config = directory / 'app.ini'
        self.config.write_text(f"""APP_NAME = 研发上库隔离验收
[server]
HTTP_ADDR = 127.0.0.1
HTTP_PORT = {self.port}
ROOT_URL = {self.origin}/
DISABLE_SSH = true
APP_DATA_PATH = {directory}/gitea-data
[database]
DB_TYPE = sqlite3
PATH = {directory}/gitea.db
[repository]
ROOT = {directory}/repositories
DEFAULT_BRANCH = main
[security]
INSTALL_LOCK = true
[service]
DISABLE_REGISTRATION = true
[webhook]
ALLOWED_HOST_LIST = loopback
[log]
LEVEL = Warn
""")
        self.env = {**os.environ, 'GITEA_WORK_DIR': str(directory)}

    def command(self, *args):
        return [str(self.binary), '--config', str(self.config), *args]

    def start(self):
        with (self.directory / 'gitea.log').open('w') as log:
            self.process = subprocess.Popen(self.command('web'), env=self.env, stdout=log, stderr=log)
        wait_for(lambda: requests.get(self.origin + '/api/v1/version', timeout=2).ok)
        invoke(self.command('admin', 'user', 'create', '--username', 'fixture-admin', '--password', self.password,
                            '--email', 'admin@example.test', '--admin', '--must-change-password=false'), env=self.env)
        output = invoke(self.command('admin', 'user', 'generate-access-token', '--username', 'fixture-admin',
                                     '--token-name', 'engineering-fixture', '--scopes', 'all'), env=self.env).decode()
        self.token = output.strip().split()[-1]

    def api(self, method, path, payload=None, username=None):
        kwargs = {'auth': (username, self.password)} if username else {'headers': {'Authorization': 'token ' + self.token}}
        response = requests.request(method, self.origin + '/api/v1' + path, json=payload, timeout=5, **kwargs)
        if not response.ok:
            raise AssertionError((method, path, response.status_code, response.text))
        return response.json() if response.content else None

    def create_repo(self, org, name):
        return self.api('POST', f'/orgs/{org}/repos', {'name': name, 'private': True, 'default_branch': 'main'})

    def push(self, repo, *args):
        url = urlsplit(self.origin)
        authority = f'fixture-admin:{self.password}@{url.netloc}'
        target = urlunsplit((url.scheme, authority, f'/{repo["full_name"]}.git', '', ''))
        invoke(['git', '-C', str(self.directory / 'code'), 'push', target, *args])

    def commit(self, user, sequence, label=None):
        code = self.directory / 'code'
        filename = f'file{sequence}.c'
        (code / filename).write_text(f'int value{sequence};\n' * sequence)
        invoke(['git', '-C', str(code), 'add', filename])
        invoke(['git', '-C', str(code), '-c', 'user.name=同名研发', '-c', f'user.email={user}@example.test',
                'commit', '-m', label or f'样本提交 {sequence}'],
               env={**os.environ, 'GIT_AUTHOR_DATE': '2025-01-01T22:00:00+08:00',
                    'GIT_COMMITTER_DATE': '2025-01-01T22:00:00+08:00'})
        return invoke(['git', '-C', str(code), 'rev-parse', 'HEAD']).decode().strip()


def wait_for(predicate):
    for _ in range(120):
        try:
            if predicate():
                return
        except (requests.ConnectionError, AssertionError):
            pass
        time.sleep(.25)
    raise AssertionError('等待隔离服务或事件超时')


def exercise(sample, verify_browser=False):
    for org in ('engineering-a', 'engineering-b'):
        sample.api('POST', '/orgs', {'username': org, 'visibility': 'private'})
    target = sample.create_repo('engineering-a', 'firmware')
    empty = sample.create_repo('engineering-a', 'tools')
    other = sample.create_repo('engineering-b', 'restricted')
    developers = []
    for idx in range(1, 13):
        login = f'developer-{idx:02}'
        developers.append(sample.api('POST', '/admin/users', {'username': login, 'email': f'{login}@example.test',
                              'full_name': '同名研发', 'password': sample.password, 'must_change_password': False}))
    team = sample.api('POST', '/orgs/engineering-a/teams', {
        'name': 'firmware', 'permission': 'admin', 'includes_all_repositories': True,
    })
    for user in developers:
        sample.api('PUT', f'/teams/{team["id"]}/members/{user["login"]}')
    prefix = '/repos/' + target['full_name']
    label = sample.api('POST', prefix + '/labels', {'name': 'bug', 'color': 'ff0000'})
    code = sample.directory / 'code'
    invoke(['git', 'init', '-b', 'main', str(code)])
    initial = sample.commit(developers[0]['login'], 1)
    sample.push(target, 'HEAD:refs/heads/main')
    wait_for(lambda: any(row['content'] for row in sample.api('GET', prefix + '/activities/feeds')
                         if row['op_type'] == 'commit_repo'))
    bug = sample.api('POST', prefix + '/issues', {'title': '由 PR 关闭', 'labels': [label['id']]})
    manual = sample.api('POST', prefix + '/issues', {'title': '重开后换人', 'labels': [label['id']],
                          'assignees': [developers[1]['login']]})
    manual_path = f'{prefix}/issues/{manual["number"]}'
    sample.api('PATCH', manual_path, {'state': 'closed'})

    os.environ.update(DATABASE_URL=f'sqlite:///{sample.directory}/manager.db', DISABLE_BACKGROUND_TASKS='1',
                      BACKUP_DIR=str(sample.directory / 'backups'), AI_REVIEW_DB=str(sample.directory / 'review.db'),
                      SECRET_KEY=secrets.token_urlsafe(48), INIT_PASSWORD=sample.password)
    from app import create_app
    from access.models import ManagerAccount, OrganizationBinding, OrganizationGrant
    from access.scopes import server_epoch
    from engineering_activity.models import Activity, Collection, Repository
    from engineering_activity.worker import tick
    from engineering_activity.rules import day, validate_settings
    from models import GiteaServer, db, set_setting
    from werkzeug.security import generate_password_hash
    app = create_app()
    from werkzeug.serving import make_server
    def serve(environ, start_response):
        path = environ['PATH_INFO']
        if path.startswith('/api/'):
            return app(environ, start_response)
        from werkzeug.wrappers import Response
        type_name = 'text/css' if path.endswith('.css') else 'application/javascript'
        dist = ROOT / 'frontend' / 'dist'
        if path.startswith('/assets/') and Path(path).name == path[len('/assets/'):]:
            asset = dist / 'assets' / Path(path).name
            if asset.suffix == '.png':
                type_name = 'image/png'
            return Response(asset.read_bytes(), content_type=type_name)(environ, start_response)
        return Response((dist / 'index.html').read_bytes(), content_type='text/html')(environ, start_response)

    manager = make_server('127.0.0.1', 0, serve)
    sample.manager = manager
    threading.Thread(target=manager.serve_forever, daemon=True).start()
    callback = f'http://127.0.0.1:{manager.server_port}'
    with app.app_context():
        server = GiteaServer(name='隔离 Gitea', host='127.0.0.1', ssh_user='fixture', gitea_url=sample.origin,
                            gitea_port=sample.port, api_token=sample.token, version='1.26.4')
        db.session.add(server)
        set_setting('host_ip', '127.0.0.1')
        db.session.commit()
        key = (server.id, server_epoch(server), 0)
        db.session.add(Collection(server_id=key[0], epoch=key[1], organization_id=0,
                                  settings=validate_settings({'teams': [team['id']], 'webhook_base': callback}), due=datetime.utcnow()))
        db.session.commit()
        tick()
        assert db.session.get(Collection, key).status == 'ready', db.session.get(Collection, key).progress
        assert Repository.query.count() == 3

        manual_started = time.monotonic()
        # 工作树保留一份基线，12 个提交随新分支一次推送。
        for idx, user in enumerate(developers, start=2):
            sample.commit(user['login'], idx)
        head = invoke(['git', '-C', str(code), 'rev-parse', 'HEAD']).decode().strip()
        sample.push(target, 'HEAD:refs/heads/feature', 'HEAD:refs/heads/copy')
        wait_for(lambda: sum(head in row['content'] for row in sample.api('GET', prefix + '/activities/feeds')
                             if row['op_type'] == 'commit_repo') >= 2)
        pull = sample.api('POST', prefix + '/pulls', {'head': 'feature', 'base': 'main',
                          'title': '整组提交合入主线', 'body': f'Closes #{bug["number"]}'},
                          username=developers[0]['login'])
        pull_path = f'{prefix}/pulls/{pull["number"]}'
        wait_for(lambda: sample.api('GET', pull_path).get('mergeable'))
        sample.api('POST', pull_path + '/merge', {'Do': 'squash'})
        wait_for(lambda: sample.api('GET', f'{prefix}/issues/{bug["number"]}')['state'] == 'closed')
        sample.api('PATCH', manual_path, {'state': 'open'})
        sample.api('PATCH', manual_path, {'assignees': [developers[2]['login']]})
        sample.api('PATCH', manual_path, {'state': 'closed'})
        sample.api('PATCH', manual_path, {'assignees': [developers[3]['login']]})
        expected_lines = sum(range(1, 14))
        manual_setup = time.monotonic() - manual_started

        from engineering_activity.models import ClosingEvidence
        def captured():
            db.session.expire_all()
            return ClosingEvidence.query.filter(ClosingEvidence.sha != '').count() == 1
        wait_for(captured)
        shell = app.test_client()
        initial_session = shell.get('/api/session').get_json()
        login = shell.post('/api/login', json={'password': sample.password},
                           headers={'X-CSRF-Token': initial_session['csrf_token']})
        assert login.status_code == 200, login.get_json()
        path = f'/api/engineering-activity/{server.id}'
        token = shell.get('/api/session').get_json()['csrf_token']
        accepted = shell.post(path + '/collect', json={}, headers={'X-CSRF-Token': token})
        assert accepted.status_code == 202, accepted.get_json()
        tick()
        summary = db.session.get(Collection, key)
        assert summary.status == 'ready', summary.progress

        now = day(datetime.utcnow())
        query = {'start': now, 'end': now}
        report = shell.get(path + '/dashboard', query_string=query)
        assert report.status_code == 200, report.get_json()
        data = report.get_json()
        # 验收结果不带私有凭据，逐条查阅仍能指向隔离原始事件。
        activity_path = sample.directory / 'report.json'
        activity_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        dump = [{'日期': row.day, '类别': row.kind, '证据': row.key, '身份': row.person,
                 '明细': row.values} for row in Activity.query.all()]
        (sample.directory / 'events.json').write_text(json.dumps(dump, ensure_ascii=False, indent=2))
        (sample.directory / 'bug-timeline.json').write_text(json.dumps(
            sample.api('GET', f'{prefix}/issues/{bug["number"]}/timeline'), ensure_ascii=False, indent=2))
        assert data['summary']['commits'] == 14
        assert data['summary']['additions'] == 181
        assert data['summary']['pr_open'] == data['summary']['pr_merge'] == 1, data['summary']
        assert data['summary']['bug'] == 2 and data['summary']['bug_reopen'] == 1, data['summary']
        fixed = shell.get(path + '/details', query_string={**query, 'kind': 'bug'}).get_json()
        assert any(row.get('pull') == pull['number'] and row['reason'] == '修复 PR' for row in fixed['items']), fixed
        assert len(data['ranking']) == 10, data['ranking']
        expected = {f'user:{user["id"]}': {
            'commits': 3 if index == 2 else 1,
            'additions': index + 91 if index == 2 else index,
            'bug': int(index in (2, 4)),
        } for index, user in enumerate(developers, start=2)}
        assert {row['person']: {metric: row[metric] for metric in ('commits', 'additions', 'bug')}
                for row in data['daily'] if row['commits']} == expected
        individual = shell.get(path + '/details', query_string={**query, 'person': f'user:{developers[2]["id"]}'}).get_json()
        assert any(row['kind'] == 'bug' and row['reason'] == '关闭时唯一指派人' for row in individual['items']), individual
        main = shell.get(path + '/dashboard', query_string={**query, 'view': 'main'}).get_json()
        assert main['summary']['commits'] == 1, main['summary']
        assert main['summary']['additions'] == expected_lines - 1, main['summary']
        roster = shell.get(path + '/dashboard', query_string={**query, 'team': team['id']}).get_json()
        assert len(roster['daily']) == 12, roster
        no_events = shell.get(path + '/dashboard', query_string={
            **query, 'team': team['id'], 'repository': empty['id'],
        }).get_json()
        assert len(no_events['daily']) == 12 and not any(row['commits'] for row in no_events['daily'])
        if verify_browser:
            subprocess.run(['node', str(ROOT / 'scripts/test_engineering_activity_ui.cjs'), callback, str(sample.directory)],
                           check=True, timeout=120, env={**os.environ, 'GM_ENGINEERING_FIXTURE_PASSWORD': sample.password})
        seen = shell.get(path + '/details', query_string={**query, 'kind': 'commit'}).get_json()
        branch_rows = [row for row in seen['items'] if row['branch'] in ('copy', 'feature')]
        assert len(branch_rows) == 12, seen
        assert all(row['pusher'] == 'fixture-admin' for row in branch_rows), branch_rows
        assert len({row['url'] for row in branch_rows}) == 12, branch_rows

        # 组织账号走正常登录和权限入口，拒绝指定另一个组织的仓库。
        organization = sample.api('GET', '/orgs/engineering-a')
        binding = OrganizationBinding(server_id=server.id, organization_id=organization['id'],
                                      organization_name='engineering-a', instance_epoch=key[1], enabled=True)
        account = ManagerAccount(username='org-fixture', display_name='组织隔离账号', subject='fixture:org',
                                  password_hash=generate_password_hash(sample.password))
        db.session.add_all([binding, account])
        db.session.flush()
        from access.identity import organization_birth, register_organization
        from services.policy_gitea import PolicyGitea
        register_organization(binding, organization_birth(PolicyGitea(server), binding.organization_name, binding.organization_id))
        db.session.add(OrganizationGrant(account_id=account.id, binding_id=binding.id, granted_by=1))
        db.session.commit()
        restricted = app.test_client()
        from flask import session
        from access.authentication import issue_session
        with app.test_request_context('/api/login'):
            issue_session(account)
            signed = dict(session)
        with restricted.session_transaction() as browser_session:
            browser_session.update(signed)
        for endpoint in ('dashboard', 'details'):
            with app.app_context():
                denied = restricted.get(path + '/' + endpoint, query_string={**query, 'repository': other['id']},
                                        headers={'X-Organization-ID': str(binding.id)})
            assert denied.status_code == 404, denied.get_json()

        # 重复触发不改变已落库的回放结果。
        prior = db.session.query(Activity).count()
        with app.app_context():
            accepted = shell.post(path + '/collect', json={}, headers={'X-CSRF-Token': token})
            assert accepted.status_code == 202
        tick()
        assert db.session.query(Activity).count() == prior

        invoke(['git', '-C', str(code), 'checkout', '--detach', initial])
        replacement = sample.commit(developers[-1]['login'], 14, '强推替换旧分支')
        sample.push(target, '--force', 'HEAD:refs/heads/copy', ':refs/heads/feature')
        wait_for(lambda: any(replacement in row['content'] for row in sample.api('GET', prefix + '/activities/feeds')
                             if row['op_type'] == 'commit_repo'))
        with app.app_context():
            assert shell.post(path + '/collect', json={}, headers={'X-CSRF-Token': token}).status_code == 202
        tick()
        replacement_row = Activity.query.filter_by(kind='commit', key=replacement).one()
        assert replacement_row.person == f'user:{developers[-1]["id"]}'
        assert Activity.query.filter_by(kind='commit').count() == 15

        flattened = [(row['person'], row['day'], row['commits']) for row in data['daily']]
        assert len({row[0] for row in flattened if row[0] != 'unknown'}) >= 12
        print(json.dumps({'隔离组织': 2, '隔离仓库': 3, '研发人数': 12, '初始提交': initial,
                          '样本准备秒数': round(manual_setup, 2), '当日指标': data['summary']},
                         ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gitea', type=Path, required=True, help='官方 Gitea 1.26.4 本机可执行文件')
    parser.add_argument('--output', type=Path, help='保留隔离验收输出的父目录')
    parser.add_argument('--browser', action='store_true', help='用已构建前端执行后台浏览器验收')
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix='engineering-acceptance-', dir=args.output)).resolve()
    print(f'隔离验收目录 {directory}', flush=True)
    sample = Sample(args.gitea.resolve(), directory)
    try:
        sample.start()
        exercise(sample, args.browser)
    finally:
        if hasattr(sample, 'manager'):
            sample.manager.shutdown()
        sample.process.terminate()
        sample.process.wait(timeout=20)


if __name__ == '__main__':
    main()
