#!/usr/bin/env python3
"""以独立增强版实例验证停写快照、Hook 执行与发布前隔离；不访问现有实例。"""
import importlib.util
import os
from pathlib import Path
import time
import tempfile
from unittest.mock import patch

import requests

os.environ['ACCEPTANCE_ROOT'] = tempfile.mkdtemp(prefix='state-', dir='/evidence')
spec = importlib.util.spec_from_file_location('acceptance', '/scripts/operation-progress-acceptance.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
from models import BackupRepoCommit
import access.models  # 在创建隔离表之前注册权限模型。
import access.scopes
from services import backup_consistency, commit_service
from services.restore_secrets import read_file, put_file

image = os.environ.get('GITEA_TEST_IMAGE', 'gitea-enhanced:approval-identity-20261015-r1')
image = m.client.images.get(image).id
original_create = m.create


def create(image_name, suffix, **kwargs):
    if image_name.startswith('gitea/gitea:'):
        image_name = image
        if suffix == 'target':
            kwargs['ports'] = {'3000/tcp': ('127.0.0.1', int(os.environ.get('ACCEPTANCE_HTTP_PORT', '35873'))), '22/tcp': ('127.0.0.1', int(os.environ.get('ACCEPTANCE_SSH_PORT', '35874')))}
    return original_create(image_name, suffix, **kwargs)


def api(server, method, path, **kwargs):
    response = requests.request(method, server.gitea_url + '/api/v1' + path,
        headers={'Authorization': 'token ' + server.api_token}, timeout=30, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError('验收接口失败：' + path + '，状态 ' + str(response.status_code))
    return response


def wait_ready(server):
    # 验收使用 Docker 临时 IP；停启后重新读取。生产入口应使用固定域名或映射端口。
    container = m.client.containers.get(server.gitea_container)
    server.gitea_url = 'http://' + container.attrs['NetworkSettings']['Networks'][os.environ.get('ACCEPTANCE_NETWORK', 'bridge')]['IPAddress'] + ':3000'
    deadline = time.monotonic() + 60
    while True:
        try:
            if api(server, 'GET', '/version').status_code == 200: return
        except (requests.RequestException, RuntimeError):
            if time.monotonic() > deadline: raise
        time.sleep(0.5)


def command(container, args):
    code, raw = container.exec_run(args, user='git')
    if code: raise RuntimeError('隔离 Git 命令失败，退出码 ' + str(code))
    return raw.decode().strip()


def main():
    m.create = create
    try:
        with m.app.app_context():
            m.db.create_all()
            source, src, source_password = m.node('source')
            target, dst, _ = m.node('target')
            m.note('隔离增强版实例就绪', 源镜像=src.attrs['Image'], 目标镜像=dst.attrs['Image'],
                版本=api(source, 'GET', '/version').json())
            if os.environ.get('TEST_TRANSPORT') == 'ssh':
                import paramiko
                gateway = m.create('gitea-manager-restore-ssh:20261013', 'ssh', entrypoint=['sleep'], command=['infinity'],
                    volumes={'/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}})
                gateway.start(); gateway.reload()
                assert gateway.exec_run(['sh', '-ec', 'ssh-keygen -A; mkdir -p /root/.ssh; chmod 700 /root/.ssh']).exit_code == 0
                key = paramiko.RSAKey.generate(2048)
                key.write_private_key_file(str(m.root/'ssh-key'))
                m.upload(gateway, '/root/.ssh', 'authorized_keys', key.get_name() + ' ' + key.get_base64() + '\n')
                assert gateway.exec_run(['/usr/sbin/sshd', '-o', 'PermitRootLogin=prohibit-password', '-o', 'PasswordAuthentication=no']).exit_code == 0
                host = gateway.attrs['NetworkSettings']['Networks'][os.environ.get('ACCEPTANCE_NETWORK', 'bridge')]['IPAddress']
                public = gateway.exec_run(['cat', '/etc/ssh/ssh_host_ed25519_key.pub']).output.decode().split()
                (m.root/'known-hosts').write_text(host + ' ' + public[0] + ' ' + public[1] + '\n')
                for server in (source, target):
                    server.is_local = False; server.connection_mode = 'ssh'; server.host = host
                m.db.session.commit()
            api(source, 'POST', '/user/repos', json={'name': 'protected', 'auto_init': True, 'private': True})
            api(source, 'POST', '/user/repos', json={'name': 'empty', 'private': True})
            api(target, 'POST', '/user/repos', json={'name': 'target-only', 'auto_init': True, 'private': True})
            repo = '/data/git/repositories/progressadmin/protected.git'
            hook = repo + '/hooks/reference-transaction'
            before, mode = read_file(src, hook)
            assert mode.mode & 0o111, '源治理入口必须可执行'
            custom = repo + '/hooks/pre-receive.d/phase1-deny'
            put_file(src, custom, '#!/bin/sh\necho 阶段一自定义门禁 >&2\nexit 42\n'.encode(), 0o751, 1000, 1000)
            snapshot_impl = backup_consistency.repository_snapshot
            def offline_snapshot(*args):
                src.reload()
                assert src.status == 'exited', '采集快照时源 Gitea 仍在运行'
                try:
                    requests.post(source.gitea_url + '/api/v1/user/repos', timeout=1)
                except requests.RequestException:
                    pass
                else:
                    raise AssertionError('停写期间源业务端口仍然开放')
                return snapshot_impl(*args)
            collect_impl = commit_service.collect_backup_commits
            def collect_after_new_write(identifier, *args, **kwargs):
                wait_ready(source)
                api(source, 'POST', '/user/repos', json={'name': 'after-snapshot', 'auto_init': True, 'private': True})
                return collect_impl(identifier, *args, **kwargs)
            with patch.object(backup_consistency, 'repository_snapshot', offline_snapshot), patch.object(commit_service, 'collect_backup_commits', collect_after_new_write):
                backup = m.backup(source, 'phase1.zip')
            assert backup.commit_snapshot_repo_count == 2
            assert BackupRepoCommit.query.filter_by(backup_id=backup.id, repo_name='progressadmin/after-snapshot').count() == 0
            m.note('停写期间导出及源恢复服务后的并发新增隔离通过', 归档仓库数=2, 在线源仓库数=3)
            health_impl = commit_service._require_target_health
            validation_visits = []
            def isolated_health(server):
                if getattr(server, '_restore_api_reader', None) is None:
                    wait_ready(server)
                    return health_impl(server)
                original = m.client.containers.get(target.gitea_container)
                assert original.status == 'exited', '验证完成前正式目标被启动'
                reader = server._restore_api_reader
                verifier = reader.__self__
                verifier.container.reload()
                assert not verifier.container.attrs['HostConfig'].get('PortBindings')
                assert verifier.network.attrs.get('Internal'), '验证网络不是内部网络'
                import configparser
                from services.restore_isolation import CRON_TASKS
                configuration = configparser.ConfigParser(interpolation=None)
                configuration.read_string('[DEFAULT]\n' + read_file(verifier.container, '/tmp/manager-verify.ini')[0].decode())
                assert configuration.get('server', 'HTTP_ADDR') == '127.0.0.1'
                assert all(not configuration.getboolean('cron.' + name, 'ENABLED') for name in CRON_TASKS)
                for _ in range(5):
                    try:
                        requests.post(target.gitea_url + '/api/v1/user/repos',
                            headers={'Authorization': 'token ' + target.api_token},
                            json={'name': 'must-not-be-accepted'}, timeout=0.5)
                    except requests.RequestException:
                        pass
                    else:
                        raise AssertionError('验证期间目标仍可接收业务请求')
                health_impl(server)
                validation_visits.append(True)
            # 故意破坏校验基准，要求旧目标的数据、凭据和入口全部恢复。
            record = BackupRepoCommit.query.filter_by(backup_id=backup.id, repo_name='progressadmin/protected').one()
            digest = record.commit_ids_hash
            record.commit_ids_hash = '0' * 64; m.db.session.commit()
            with patch.object(commit_service, '_require_target_health', isolated_health):
                from models import RestoreTask
                from services.gitea_service import do_restore
                task = RestoreTask(backup_id=backup.id, target_server_id=target.id, target_server_name=target.name, status='running')
                m.db.session.add(task); m.db.session.commit()
                do_restore(task.id)
                assert task.status == 'failed', task.error_msg
                assert '快照提交不一致' in task.error_msg, task.error_msg
            wait_ready(target)
            assert api(target, 'GET', '/repos/progressadmin/target-only').status_code == 200
            m.note('验证失败回退通过，验证期外部写请求全部拒绝', 验证期间请求数=5)
            record.commit_ids_hash = digest; m.db.session.commit()
            with patch.object(commit_service, '_require_target_health', isolated_health):
                task = RestoreTask(backup_id=backup.id, target_server_id=target.id, target_server_name=target.name, status='running')
                m.db.session.add(task); m.db.session.commit()
                do_restore(task.id)
                assert task.status == 'success', task.error_msg
            wait_ready(target)
            dst = m.client.containers.get(target.gitea_container)
            after, restored_mode = read_file(dst, hook)
            assert before == after and restored_mode.mode & 0o777 == mode.mode & 0o777
            _, restored_custom = read_file(dst, custom)
            assert restored_custom.mode & 0o777 == 0o751
            head = command(dst, ['git', '--git-dir', repo, 'rev-parse', 'HEAD'])
            code, output = dst.exec_run(['git', '--git-dir', repo, 'update-ref', 'refs/heads/unauthorized', head], user='git')
            assert code != 0, '治理入口未阻止缺少身份的直接引用更新'
            assert b'ignored' not in output, 'Git 跳过了不可执行 Hook'
            code, _ = dst.exec_run(['git', '--git-dir', repo, 'rev-parse', '--verify', 'refs/heads/unauthorized'], user='git')
            assert code != 0
            code, output = dst.exec_run([custom], user='git')
            assert code == 42 and '阶段一自定义门禁' in output.decode()
            push_url = target.gitea_url.replace('http://', 'http://progressadmin:' + source_password + '@') + '/progressadmin/protected.git'
            code, output = src.exec_run(['git', '--git-dir', repo, 'push', push_url, 'HEAD:refs/heads/blocked-by-custom'], user='git')
            assert code != 0 and '阶段一自定义门禁' in output.decode(), '真实 Git push 未执行恢复后的自定义门禁'
            assert requests.get(target.gitea_url + '/api/v1/repos/progressadmin/after-snapshot',
                headers={'Authorization': 'token ' + target.api_token}, timeout=10).status_code == 404
            assert len(validation_visits) == 2
            m.note('增强版恢复与真实 Git 治理入口阻断通过', 治理权限=oct(restored_mode.mode), 自定义权限=oct(restored_custom.mode), 发布前验证次数=2)
            api(target, 'POST', '/user/repos', json={'name': 'before-publish', 'auto_init': True, 'private': True})
            def fail_after_public_write(server):
                if getattr(server, '_restore_api_reader', None) is not None:
                    return isolated_health(server)
                wait_ready(server)
                api(server, 'POST', '/user/repos', json={'name': 'after-publish', 'auto_init': True, 'private': True})
                raise RuntimeError('验收注入：开放后健康确认失败')
            with patch.object(commit_service, '_require_target_health', fail_after_public_write):
                task = RestoreTask(backup_id=backup.id, target_server_id=target.id, target_server_name=target.name, status='running')
                m.db.session.add(task); m.db.session.commit()
                do_restore(task.id)
                assert task.status == 'failed' and '保留当前数据' in task.error_msg, task.error_msg
            assert api(target, 'GET', '/repos/progressadmin/after-publish').status_code == 200
            assert requests.get(target.gitea_url + '/api/v1/repos/progressadmin/before-publish',
                headers={'Authorization': 'token ' + target.api_token}, timeout=10).status_code == 404
            m.note('开放访问后故障不会回退并吞掉新写入', 模式=target.connection_mode, 新仓库='after-publish')
    finally:
        for container in reversed(m.owned):
            try: container.remove(force=True, v=True)
            except Exception: pass


if __name__ == '__main__':
    main()
