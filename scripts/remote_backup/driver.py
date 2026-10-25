"""隔离网络内的真实 API、SSH 和 Git 验收驱动。"""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import threading
import traceback

import paramiko
import requests
from .errors import CheckFailure, EnvironmentBlocked

EVIDENCE = Path(os.environ.get('REMOTE_BACKUP_EVIDENCE', '/evidence'))


class Lab:
    def __init__(self):
        self.root = EVIDENCE
        self.path = self.root / 'private.json'
        self.state = json.loads(self.path.read_text())
        self.manager = requests.Session()
        self.manager.trust_env = False
        self.manager_url = 'http://backend:5000/api'
        self.started = time.monotonic()
        self.active_case = ''

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + '\n')
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def scrub(self, value):
        text = str(value)
        secrets = [self.state['manager_password'], self.state['manager_secret']]
        for node in self.state['nodes'].values():
            secrets += [node.get(key, '') for key in ('password', 'db_password', 'secret', 'token', 'original_token', 'scoped_token', 'totp', 'action_secret')]
            secrets += list(node.get('passwords', {}).values())
            secrets += list(node.get('tokens', {}).values())
        secrets += [value for value in self.state.get('integrations', {}).values() if isinstance(value, str)]
        for secret in secrets:
            if secret:
                text = text.replace(secret, '[已脱敏]')
        return text

    def note(self, category, case, status, description='', **values):
        row = {'类别': category, '用例': case, '状态': status, '说明': self.scrub(description),
               '驱动版本': os.environ.get('REMOTE_DRIVER_VERSION', '初始版本见镜像清单'),
               '时间': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
               '本阶段耗时秒': round(time.monotonic() - self.started, 3), **values}
        raw = json.dumps(row, ensure_ascii=False)
        with (self.root / 'results.jsonl').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(raw + '\n')
        print(raw, flush=True)

    @contextlib.contextmanager
    def case(self, category, label):
        begin = time.monotonic()
        self.active_case = label
        self.progress('执行中', 节点='', 操作=label)
        try:
            yield
        except EnvironmentBlocked as exc:
            self.progress('环境阻塞', 说明=self.scrub(str(exc)))
            self.note(category, label, '环境阻塞', str(exc), 耗时秒=round(time.monotonic() - begin, 3))
            raise
        except Exception as exc:
            self.progress('失败', 说明=self.scrub(str(exc)))
            self.note(category, label, '失败', '%s: %s' % (type(exc).__name__, exc), 耗时秒=round(time.monotonic() - begin, 3))
            raise
        else:
            self.progress('通过')
            self.note(category, label, '通过', 耗时秒=round(time.monotonic() - begin, 3))

    def progress(self, status, **details):
        value = {'说明': '实时执行位置，不能代替最终验收结果', '用例': self.active_case, '状态': status,
                 '时间': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), **details}
        temporary = self.root / 'progress.tmp'
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(self.root / 'progress.json')

    def ssh(self, node, username='root'):
        client = paramiko.SSHClient()
        client.load_host_keys(str(self.root / 'known_hosts'))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(self.state['addresses'][node], username=username,
                       key_filename=str(self.root / 'id_rsa'), allow_agent=False, look_for_keys=False,
                       timeout=10, banner_timeout=10, auth_timeout=10)
        return client

    def command(self, node, args, timeout=600, input_data=None, check=True):
        self.progress('执行中', 节点=node, 操作=' '.join(args[:2]), 超时秒=timeout)
        with self.ssh(node) as client:
            stdin, stdout, stderr = client.exec_command(shlex.join(args), timeout=timeout)
            if input_data is not None:
                stdin.write(input_data)
                stdin.flush()
            stdin.channel.shutdown_write()
            channel = stdout.channel
            deadline = time.monotonic() + timeout
            out, err = bytearray(), bytearray()
            while True:
                if channel.recv_ready():
                    out.extend(channel.recv(65536))
                if channel.recv_stderr_ready():
                    err.extend(channel.recv_stderr(65536))
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() > deadline:
                    channel.close()
                    raise TimeoutError('隔离主机命令超时')
                time.sleep(0.02)
            code = channel.recv_exit_status()
            if check and code:
                raise CheckFailure('远端命令退出码 %d：%s' % (code, self.scrub(err.decode(errors='replace')[-1500:] + out.decode(errors='replace')[-1500:])))
            return code, bytes(out), bytes(err)

    def put(self, node, path, data, mode=0o600):
        with self.ssh(node) as client:
            with client.open_sftp() as sftp:
                with sftp.file(path, 'wb') as stream:
                    stream.write(data.encode() if isinstance(data, str) else data)
                sftp.chmod(path, mode)

    def compose_config(self, node, raw=None):
        # 产品会把原 JSON Compose 保存成 YAML，文件后缀不决定实际格式。
        import yaml
        raw = raw if raw is not None else self.command(node, ['cat', '/lab/compose.json'])[1]
        value = yaml.safe_load(raw)
        if not isinstance(value, dict) or not isinstance(value.get('services'), dict):
            raise CheckFailure('测试部署 Compose 结构无效')
        return value

    def gitea_url(self, node):
        return 'http://' + self.state['addresses'][node] + ':3000'

    def request(self, node, method, path, *, expected=(200, 201, 204), user=None, **kwargs):
        session = requests.Session()
        session.trust_env = False
        token = self.state['nodes'][node].get('token')
        if user:
            token = self.state['nodes'][node].get('tokens', {}).get(user)
        headers = kwargs.pop('headers', {})
        if token and 'Authorization' not in headers:
            headers['Authorization'] = 'token ' + token
        response = session.request(method, self.gitea_url(node) + path, headers=headers, timeout=kwargs.pop('timeout', 120), **kwargs)
        if expected is not None and response.status_code not in expected:
            raise CheckFailure('Gitea %s %s 返回 %s：%s' % (method, path, response.status_code, self.scrub(response.text[:500])))
        return response

    def api(self, node, method, path, **kwargs):
        return self.request(node, method, '/api/v1' + path, **kwargs)

    def login(self):
        deadline = time.monotonic() + 120
        while True:
            try:
                response = self.manager.get(self.manager_url + '/session', timeout=5)
                response.raise_for_status()
                data = response.json()
                break
            except (requests.RequestException, ValueError):
                if time.monotonic() > deadline:
                    raise CheckFailure('Manager 会话接口未就绪')
                time.sleep(1)
        token = data.get('csrf_token') or data.get('csrf')
        if not token:
            raise CheckFailure('Manager 会话未提供 CSRF')
        self.manager.headers['X-CSRF-Token'] = token
        response = self.manager.post(self.manager_url + '/login', json={
            'source': 'local', 'username': 'admin', 'password': self.state['manager_password']}, timeout=15)
        if response.status_code != 200:
            raise CheckFailure('Manager 登录失败：' + self.scrub(response.text[:500]))
        token = response.json().get('csrf_token') or response.json().get('csrf')
        if token:
            self.manager.headers['X-CSRF-Token'] = token

    def manager_api(self, method, path, expected=(200, 201, 204), **kwargs):
        response = self.manager.request(method, self.manager_url + path, timeout=kwargs.pop('timeout', 120), **kwargs)
        if expected is not None and response.status_code not in expected:
            raise CheckFailure('Manager %s %s 返回 %s：%s' % (method, path, response.status_code, self.scrub(response.text[:800])))
        return response

    def wait_gitea(self, node, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.request(node, 'GET', '/api/v1/version', expected=None, timeout=2).status_code == 200:
                    if not self.state['nodes'][node].get('token') or self.api(node, 'GET', '/user', expected=None, timeout=2).status_code == 200:
                        return
            except requests.RequestException:
                pass
            if time.monotonic() > deadline:
                raise CheckFailure('Gitea %s 健康检查超时' % node)
            time.sleep(1)

    def poll_task(self, kind, identifier, timeout=14400):
        from .polling import task_rows
        deadline = time.monotonic() + timeout
        last = None
        start = time.monotonic()
        stages = []
        refresh_used = False
        while True:
            rows, refresh_used = task_rows(self, kind, identifier, refresh_used)
            task = next((row for row in rows if row['id'] == identifier), None)
            if task is None:
                raise CheckFailure('已创建任务在列表中消失')
            stage = task.get('progress_stage')
            self.progress('执行中', 任务类型=kind, 任务ID=identifier, 任务状态=task['status'],
                          任务阶段=stage, 后台进度更新时间=task.get('progress_updated_at'))
            if stage != last:
                event = {'阶段': stage, '已用秒': round(time.monotonic() - start, 3)}
                stages.append(event)
                print(json.dumps({'任务': identifier, **event}, ensure_ascii=False), flush=True)
                last = stage
            timed_out = time.monotonic() > deadline
            if task['status'] not in ('running', 'pending') or timed_out:
                from .metrics import elapsed_seconds
                task['阶段记录'] = stages
                task['阶段时间基准'] = '本次轮询开始；故障注入前的阶段时间需看服务端记录'
                task['观察耗时秒'] = round(time.monotonic() - start, 3)
                task['总耗时秒'] = elapsed_seconds(task.get('started_at'), task.get('completed_at'))
                target = self.root / 'tasks'
                target.mkdir(exist_ok=True)
                manager_name = self.manager_url.split('//', 1)[1].split(':', 1)[0]
                (target / ('%s-%s-%s-%s.json' % (kind, identifier, manager_name, time.time_ns()))).write_text(json.dumps(task, ensure_ascii=False, indent=2) + '\n')
                if timed_out and task['status'] in ('running', 'pending'):
                    raise CheckFailure('任务超过验收观察上限，实际状态和阶段证据已保存，不能推断为完成')
                return task
            time.sleep(2)

    def ensure_space(self, future_backups=0):
        from .payload import archive_budget
        sizes = [item.get('file_size', 0) or 0 for item in self.manager_api('GET', '/backups').json()]
        required = archive_budget(sizes, future_backups)
        available = shutil.disk_usage(self.root).free
        # 已拒绝的上传和刚删除的变异副本可能还在释放物理块；短暂等待，不降低空间预算。
        deadline = time.monotonic() + 5
        while available < required and time.monotonic() < deadline:
            time.sleep(1)
            available = shutil.disk_usage(self.root).free
        if available < required:
            raise EnvironmentBlocked('宿主剩余空间低于多份副本预算：可用 %.2f GiB，需要 %.2f GiB，后续保留 %d 份归档；保留现场，未删除其他任务或缩小真实样本' %
                                     (available / 1024**3, required / 1024**3, future_backups))

    def backup(self, node):
        self.ensure_space()
        source = self.state['nodes'][node]
        with self.observe('backup-' + node):
            task = self.manager_api('POST', '/backups', json={'source_server_id': source['server_id']}).json()
            result = self.poll_task('backup', task['id'])
            if result['status'] == 'success':
                self.wait_gitea(node)
        if result['status'] != 'success':
            raise CheckFailure('备份失败：' + result.get('error_msg', ''))
        self.state['backup_id'] = task['id']
        self.state['backup_node'] = node
        self.save()
        return result

    def restore(self, node, backup_id=None):
        backup_id = backup_id or self.state['backup_id']
        with self.observe('restore-' + node):
            task = self.manager_api('POST', '/restore', json={'backup_id': backup_id,
                      'target_server_id': self.state['nodes'][node]['server_id']}).json()
            result = self.poll_task('restore', task['id'])
            # 恢复后的业务身份来自备份，健康采样必须持续到真实认证可用。
            if result['status'] == 'success':
                self.adopt_identity(node, self.state['backup_node'])
                self.wait_gitea(node)
        return result

    def adopt_identity(self, node, source_node):
        source = self.state['nodes'][source_node]
        for key in ('token', 'password', 'tokens', 'passwords', 'seed', 'totp', 'scoped_token', 'governance',
                    'action_secret', 'action_runs', 'actions_seeded', 'integration_hook', 'integrations_seeded'):
            if key in source:
                self.state['nodes'][node][key] = json.loads(json.dumps(source[key]))
        # 保留首次部署固定的目标 SSH 主机公钥，后续真实 Git 访问会拒绝意外身份变化。
        self.save()

    @contextlib.contextmanager
    def observe(self, label):
        """采样真实服务可达性和 Manager 会话；阶段进度不能代替停机测量。"""
        stop = threading.Event()
        rows = []
        begin = time.monotonic()
        def sample():
            from .metrics import probe_service
            session = requests.Session(); session.trust_env = False
            services = requests.Session(); services.trust_env = False
            session.cookies.update(self.manager.cookies)
            session.headers.update(self.manager.headers)
            count = 0
            while not stop.is_set():
                row = {'秒': round(time.monotonic() - begin, 3), '墙钟秒': time.time()}
                for node in ('a', 'b'):
                    proof = probe_service(services, self.gitea_url(node), self.state['nodes'][node].get('token'))
                    row[node], row[node + '探针'] = proof['可用'], proof
                started = time.monotonic()
                try:
                    response = session.get(self.manager_url + '/session', timeout=2)
                    row['Manager会话可用'] = response.status_code == 200 and response.json().get('authenticated', False)
                except (requests.RequestException, ValueError):
                    row['Manager会话可用'] = False
                row['Manager响应秒'] = round(time.monotonic() - started, 3)
                if count % 10 == 0:
                    started = time.monotonic()
                    try:
                        response = session.post(self.manager_url + '/settings', timeout=3,
                                                json={'host_ip': self.state['addresses']['manager']})
                        row['Manager写入可用'] = response.status_code == 200
                    except requests.RequestException:
                        row['Manager写入可用'] = False
                    row['Manager写入响应秒'] = round(time.monotonic() - started, 3)
                rows.append(row)
                count += 1
                stop.wait(1)
        worker = threading.Thread(target=sample, daemon=True)
        worker.start()
        try:
            yield rows
        finally:
            stop.set(); worker.join(timeout=5)
            directory = self.root / 'availability'; directory.mkdir(exist_ok=True)
            value = {'说明': '每秒认证版本接口采样；未知响应不计作停机，间隔内变化不作精确停机承诺',
                     '探针版本': 2, '操作': label, '样本': rows}
            for node in ('a', 'b'):
                value[node + '不可达样本数'] = sum(row[node] is False for row in rows)
                value[node + '未确认样本数'] = sum(row[node] is None for row in rows)
            value['Manager不可用样本数'] = sum(not row['Manager会话可用'] for row in rows)
            (directory / (label + '-' + str(time.time_ns()) + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def deploy(lab):
    for node in ('a', 'b'):
        state = lab.state['nodes'][node]
        url = lab.gitea_url(node)
        env = {'GITEA__database__DB_TYPE': 'postgres', 'GITEA__database__HOST': 'postgres:5432',
               'GITEA__database__NAME': 'gitea', 'GITEA__database__USER': 'gitea',
               'GITEA__database__PASSWD': state['db_password'], 'GITEA__security__INSTALL_LOCK': 'true',
               'GITEA__security__SECRET_KEY': state['secret'], 'GITEA__server__ROOT_URL': url + '/',
               'GITEA__server__DOMAIN': lab.state['addresses'][node], 'GITEA__server__SSH_DOMAIN': lab.state['addresses'][node],
               'GITEA__server__SSH_PORT': '2222', 'GITEA__server__APP_DATA_PATH': '/data/gitea',
               'GITEA__repository__ROOT': '/data/git/repositories', 'GITEA__repository__DEFAULT_BRANCH': 'main',
               'GITEA__server__LFS_START_SERVER': 'true', 'GITEA__lfs__PATH': '/data/git/lfs',
               'GITEA__actions__ENABLED': 'true', 'GITEA__service__DISABLE_REGISTRATION': 'true',
               'GITEA__packages__ENABLED': 'true'}
        config = {'name': 'remote-node-' + node, 'services': {
            'postgres': {'image': lab.state['images']['postgres'], 'pull_policy': 'never', 'platform': 'linux/amd64',
                         'container_name': 'postgres', 'environment': {'POSTGRES_USER': 'gitea', 'POSTGRES_DB': 'gitea', 'POSTGRES_PASSWORD': state['db_password']},
                         'volumes': ['/lab/persist/postgres:/var/lib/postgresql/data'], 'restart': 'unless-stopped', 'mem_limit': '768m'},
            'gitea': {'image': lab.state['images']['gitea'], 'pull_policy': 'never', 'platform': 'linux/amd64',
                      'container_name': 'gitea', 'environment': env, 'depends_on': ['postgres'],
                      'volumes': ['/lab/persist/gitea:/data'], 'ports': ['3000:3000', '2222:22'],
                      'restart': 'unless-stopped', 'mem_limit': '1536m'}}}
        lab.put(node, '/lab/compose.json', json.dumps(config))
        lab.command(node, ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d'])
        lab.wait_gitea(node)
        existing = lab.command(node, ['docker', 'exec', '-u', 'git', 'gitea', 'gitea', 'admin', 'user', 'list'], check=False)[1].decode()
        if 'labadmin' not in existing:
            lab.command(node, ['docker', 'exec', '-u', 'git', 'gitea', 'gitea', 'admin', 'user', 'create', '--username', 'labadmin',
                             '--password', state['password'], '--email', 'admin@lab.invalid', '--admin', '--must-change-password=false'])
        if not state['token']:
            result = lab.command(node, ['docker', 'exec', '-u', 'git', 'gitea', 'gitea', 'admin', 'user', 'generate-access-token',
                             '--username', 'labadmin', '--token-name', 'remote-lab-' + node, '--scopes', 'all', '--raw'])[1].decode().strip()
            state['token'] = result.splitlines()[-1]
            state['original_token'] = state['token']
            lab.save()
        lab.api(node, 'GET', '/user')
    lab.login()
    lab.manager_api('POST', '/settings', json={'host_ip': lab.state['addresses']['manager']})
    for node in ('a', 'b'):
        state = lab.state['nodes'][node]
        if 'server_id' not in state:
            response = lab.manager_api('POST', '/servers', json={
                'name': '跨网段测试-' + node.upper(), 'role': 'primary' if node == 'a' else 'backup',
                'host': lab.state['addresses'][node], 'ssh_port': 22, 'ssh_user': 'root',
                'gitea_container': 'gitea', 'pg_container': 'postgres', 'pg_dbname': 'gitea', 'pg_user': 'gitea',
                'gitea_port': 3000, 'gitea_url': lab.gitea_url(node), 'api_token': state['token'], 'connection_mode': 'ssh'})
            state['server_id'] = response.json()['id']
            lab.save()
    print(json.dumps({'部署': '完成', '说明': 'Manager 登录、两个远端 API 和 SSH Docker 均可访问'}, ensure_ascii=False))


def probe(lab, unreachable=False):
    success = []
    for node in ('a', 'b'):
        try:
            with lab.ssh(node):
                pass
            success.append(True)
        except Exception:
            success.append(False)
    if success != ([False, False] if unreachable else [True, True]):
        raise CheckFailure('路由可达性不符合预期：' + str(success))
    print(json.dumps({'网络探测': success, '预期断开': unreachable}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase')
    parser.add_argument('--hours', type=float, default=24)
    parser.add_argument('--unreachable', action='store_true')
    args = parser.parse_args()
    lab = Lab()
    lock = (lab.root / 'test-run.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if args.phase == 'deploy':
        deploy(lab)
    elif args.phase == 'probe':
        probe(lab, args.unreachable)
    else:
        from .scenarios import execute
        execute(lab, args.phase, args.hours)


if __name__ == '__main__':
    main()
