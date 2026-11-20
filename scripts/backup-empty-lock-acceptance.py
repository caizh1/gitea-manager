"""在独立的 Gitea 与 PostgreSQL 上重放旧备份空锁及后续定时备份。"""
from contextlib import closing
from datetime import datetime
import importlib.util
import ipaddress
import json
import os
import tempfile

os.environ['ACCEPTANCE_ROOT'] = tempfile.mkdtemp(prefix='backup-empty-lock-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.environ['ACCEPTANCE_ROOT'] + '/manager.db'
os.environ.setdefault('ACCEPTANCE_RESULTS', 'backup-empty-lock.json')

spec = importlib.util.spec_from_file_location('acceptance', '/scripts/operation-progress-acceptance.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

import access.models
import access.scopes
import access.statistics
import git_migration.models
import svn_migration.models
from models import ScheduleLog, ScheduledTask
from services import backup_recovery as recovery
from services.commit_service import _require_target_health
from services.docker_service import managed_client
from services.schedule_runner import claim_schedule_task, run_schedule_task


def run(source, container, target, database):
    def execute(*args):
        result = container.exec_run(list(args), user='0')
        assert result.exit_code == 0, result.output.decode(errors='replace')
        return result.output.decode().strip()

    def started():
        container.reload()
        assert container.status == 'running'
        return container.attrs['State']['StartedAt']

    execute('mkdir', recovery.LOCK)
    execute('touch', recovery.LOCK + '/.foreign')
    before = started()
    conflict = a.Backup(source_server_id=source.id, filename='conflict.zip', file_path='', status='running')
    a.db.session.add(conflict)
    a.db.session.commit()
    a.do_backup(conflict.id)
    a.db.session.refresh(conflict)
    assert conflict.status == 'failed' and not conflict.recovery_pending, conflict.error_msg
    assert '本次备份未停源' in conflict.error_msg
    assert started() == before
    execute('test', '-f', recovery.LOCK + '/.foreign')
    a.note('未知占用在准备阶段失败，源站未停源且未知文件保留')
    execute('rm', recovery.LOCK + '/.foreign')

    first_error = '固定导出元数据超过 16 MiB'
    task = a.Backup(source_server_id=source.id, filename='legacy.zip', file_path='', status='failed',
                    error_msg=first_error, source_api_token=source.api_token, recovery_pending=True)
    a.db.session.add(task)
    a.db.session.commit()
    # 旧版误报取锁成功，先停源，失败后启动，但其解锁助手一直找不到所有者凭据。
    journal_path, _ = recovery.paths(recovery.directory(), task.id, task.recovery_token)
    journal = recovery.Journal.create(journal_path, task)
    with closing(managed_client(source)) as client:
        journal.attach(source, container, database, 'manager-backup-' + task.recovery_token)
        with journal.instance_lock:
            journal.before_lock()
            journal.value['锁已取得'] = True
            journal.before_stop(container)
            container.stop(timeout=30)
            journal.stopped(container)
            task.source_stopped_at = datetime.utcnow()
            a.db.session.commit()
            journal.resume(container)
            _require_target_health(source)
            journal.recovered(container)
            token = task.recovery_token
            name = 'manager-backup-unlock-' + token
            journal.value['解锁助手名称'] = name
            journal.save('旧版第一次解锁失败')
            command = [recovery._legacy_unlock_script(), 'sh', recovery.LOCK, journal.retired_lock(),
                       journal.value['锁所有者SHA256'], journal.staged_lock(), 'yes']
            helper = client.containers.create(container.attrs['Image'], name=name, network_mode='none',
                entrypoint=['sh', '-ec'], command=command, volumes_from=[container.id],
                labels={'gitea.manager.backup-cleanup': token})
            a.owned.append(helper)
            helper.start()
            assert helper.wait(timeout=60)['StatusCode'] == 1
            assert b'backup-owner.json' in helper.logs()
    identifier, source_id, target_id = task.id, source.id, target.id
    old_start = started()
    a.db.session.remove()
    recovery.recover_pending()
    task = a.db.session.get(a.Backup, identifier)
    assert task.status == 'failed' and task.error_msg == first_error
    assert not task.recovery_pending and task.recovery_info['state'] == 'completed', task.recovery_info
    assert task.recovery_info['note'] == recovery.EMPTY_LOCK_RELEASED
    assert started() == old_start
    execute('test', '!', '-e', recovery.LOCK)
    a.note('已有旧版失败助手和空锁由巡检自动收尾，首错保留，源额外启动与人工释放均为零')

    execute('mkdir', recovery.LOCK)
    schedule = ScheduledTask(name='隔离空锁后的定时备份', source_server_id=source_id,
                             target_ids=json.dumps([target_id]), schedule_hour=0, schedule_minute=0)
    a.db.session.add(schedule)
    a.db.session.commit()
    assert claim_schedule_task(schedule.id)[0]
    run_schedule_task(schedule.id)
    log = ScheduleLog.query.filter_by(schedule_task_id=schedule.id).one()
    assert log.status == 'success', log.log
    next_backup = a.db.session.get(a.Backup, log.backup_id)
    assert next_backup.status == 'success'
    assert next_backup.recovery_info['note'] == recovery.EMPTY_LOCK_RELEASED
    execute('test', '!', '-e', recovery.LOCK)
    a.note('同源下一次定时备份回收空锁并成功，独立目标恢复成功')


def use_ssh(servers):
    import paramiko

    gateway = a.create('gitea/gitea:1.26.4', 'ssh', entrypoint=['sleep'], command=['infinity'],
        volumes={'/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}})
    gateway.start()
    # 通过预拉取的 CLI 镜像准备容器，避免验收时依赖发行版包仓库。
    cli = a.create('docker:28-cli', 'docker-cli')
    stream, _ = cli.get_archive('/usr/local/bin/docker')
    with tempfile.TemporaryFile() as archive:
        for chunk in stream:
            archive.write(chunk)
        archive.seek(0)
        assert gateway.put_archive('/usr/local/bin', archive)
    result = gateway.exec_run(['sh', '-ec', 'docker --version; ssh-keygen -A; mkdir -m 700 -p /root/.ssh'])
    assert result.exit_code == 0, '隔离 SSH 网关初始化失败'
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(str(a.root / 'ssh-key'))
    a.upload(gateway, '/root/.ssh', 'authorized_keys', key.get_name() + ' ' + key.get_base64() + '\n')
    pub = gateway.exec_run(['cat', '/etc/ssh/ssh_host_ed25519_key.pub']).output.decode().split()
    (a.root / 'known-hosts').write_text(gateway.name + ' ' + pub[0] + ' ' + pub[1] + '\n')
    command = ['/usr/sbin/sshd', '-o', 'PermitRootLogin=prohibit-password', '-o', 'PasswordAuthentication=no']
    assert gateway.exec_run(command).exit_code == 0, '隔离 SSH 网关未启动'
    for server in servers:
        server.connection_mode, server.is_local, server.host = 'ssh', False, gateway.name



def main():
    used = [ipaddress.ip_network(item['Subnet']) for network in a.client.networks.list()
            for item in network.attrs.get('IPAM', {}).get('Config', []) or []]
    subnet = next(candidate for candidate in (ipaddress.ip_network(f'10.243.{i}.0/24') for i in range(1, 255))
                  if not any(candidate.overlaps(other) for other in used if other.version == 4))
    ipam = a.docker.types.IPAMConfig(pool_configs=[a.docker.types.IPAMPool(subnet=str(subnet))])
    network = a.client.networks.create(a.marker, labels={'manager.progress.acceptance': a.marker}, ipam=ipam)
    try:
        network.connect(os.environ['HOSTNAME'])
        os.environ['ACCEPTANCE_NETWORK'] = network.name
        with a.app.app_context():
            a.db.create_all()
            source, container, _ = a.node('source')
            database = a.client.containers.get(source.pg_container)
            target, target_container, _ = a.node('target')
            source.gitea_url = f'http://{container.name}:3000'
            target.gitea_url = f'http://{target_container.name}:3000'
            if os.environ.get('ACCEPTANCE_SSH') == '1':
                use_ssh((source, target))
            a.db.session.commit()
            response = a.requests.post(source.gitea_url + '/api/v1/user/repos',
                headers={'Authorization': 'token ' + source.api_token},
                json={'name': 'empty-lock-test', 'auto_init': True, 'private': True}, timeout=30)
            response.raise_for_status()
            run(source, container, target, database)
    finally:
        for container in reversed(a.owned):
            try:
                # 测试已结束的 SSH 客户端可能已关闭，按记录的容器 ID 通过本地引擎清理。
                a.client.api.remove_container(container.id, force=True, v=True)
            except a.docker.errors.NotFound:
                pass
        network.disconnect(os.environ['HOSTNAME'], force=True)
        network.remove()


if __name__ == '__main__':
    main()
