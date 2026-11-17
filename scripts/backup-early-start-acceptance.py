"""独立容器验证源端固定导出、恢复后业务写入和失败重收。"""
import importlib.util
import os
import time
import tempfile
from pathlib import Path
os.environ['ACCEPTANCE_ROOT'] = tempfile.mkdtemp(prefix='early-start-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.environ['ACCEPTANCE_ROOT'] + '/manager.db'
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('acceptance', '/scripts/operation-progress-acceptance.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
from services import backup_staging
import access.models
import access.scopes
import access.statistics
import git_migration.models
import svn_migration.models
from models import BackupExport
from services.backup_recovery import recover_pending

import ipaddress
networks = [ipaddress.ip_network(item['Subnet']) for n in a.client.networks.list() for item in (n.attrs.get('IPAM', {}).get('Config') or []) if item.get('Subnet')]
subnet = next(ipaddress.ip_network(f'10.241.{i}.0/24') for i in range(1, 255)
              if not any(ipaddress.ip_network(f'10.241.{i}.0/24').overlaps(n) for n in networks if n.version == 4))
network = a.client.networks.create(a.marker, labels={'manager.progress.acceptance': a.marker},
    ipam=a.docker.types.IPAMConfig(pool_configs=[a.docker.types.IPAMPool(subnet=str(subnet))]))
network.connect(os.environ['HOSTNAME'])
os.environ['ACCEPTANCE_NETWORK'] = network.name
try:
    with a.app.app_context():
        a.db.create_all()
        source, container, _ = a.node('source')
        target, target_container, _ = a.node('target')
        source.gitea_url = 'http://' + container.name + ':3000'
        target.gitea_url = 'http://' + target_container.name + ':3000'
        if os.environ.get('EARLY_START_SSH') == '1':
            import paramiko
            gateway = a.create('gitea/gitea:1.26.4', 'ssh', entrypoint=['sleep'], command=['infinity'],
                volumes={'/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}})
            gateway.start()
            result = gateway.exec_run(['sh', '-ec', 'apk add --no-cache docker-cli >/tmp/setup.log 2>&1; ssh-keygen -A; mkdir -p /root/.ssh; chmod 700 /root/.ssh'])
            assert result.exit_code == 0, '隔离SSH网关初始化失败'
            key = paramiko.RSAKey.generate(2048)
            key.write_private_key_file(str(a.root / 'ssh-key'))
            a.upload(gateway, '/root/.ssh', 'authorized_keys', key.get_name() + ' ' + key.get_base64() + '\n')
            host = gateway.name
            pub = gateway.exec_run(['cat', '/etc/ssh/ssh_host_ed25519_key.pub']).output.decode().split()
            (a.root / 'known-hosts').write_text(host + ' ' + pub[0] + ' ' + pub[1] + '\n')
            assert gateway.exec_run(['/usr/sbin/sshd', '-o', 'PermitRootLogin=prohibit-password', '-o', 'PasswordAuthentication=no']).exit_code == 0
            for server in (source, target):
                server.connection_mode, server.is_local, server.host = 'ssh', False, host
        a.db.session.commit()
        response = a.requests.post(source.gitea_url + '/api/v1/user/repos', headers={'Authorization': 'token ' + source.api_token},
                                   json={'name': 'before-snapshot', 'auto_init': True, 'private': True}, timeout=30)
        response.raise_for_status()
        result = container.exec_run(['sh', '-ec', 'mkdir -p /data/gitea/actions_artifacts; dd if=/dev/urandom of=/data/gitea/actions_artifacts/sample bs=1M count=16; chown -R git:git /data/gitea/actions_artifacts'])
        assert result.exit_code == 0
        original_receive = backup_staging.receive_export
        def online_receive(*args, **kwargs):
            container.reload()
            assert container.status == 'running'
            assert container.exec_run(['test', '!', '-e', '/data/.manager-restore-lock']).exit_code == 0
            response = a.requests.post(source.gitea_url + '/api/v1/user/repos', headers={'Authorization': 'token ' + source.api_token},
                json={'name': 'after-snapshot', 'auto_init': True}, timeout=30)
            response.raise_for_status()
            before = container.attrs['State']['StartedAt']
            time.sleep(3)
            result = original_receive(*args, **kwargs)
            container.reload()
            assert container.attrs['State']['StartedAt'] == before
            a.note('传输前源已恢复健康，传输期间新增仓库成功且源进程未重启')
            return result
        with patch.object(backup_staging, 'receive_export', side_effect=online_receive):
            sample = a.backup(source, 'early-start.zip')
        assert sample.source_restored_at > sample.source_stopped_at
        assert sample.source_export.state == 'cleaned'
        a.note('停机和总耗时实测', 停机秒=(sample.source_restored_at - sample.source_stopped_at).total_seconds(),
               总耗时秒=(sample.completed_at - sample.started_at).total_seconds())
        a.restore(sample, target)
        response = a.requests.get(target.gitea_url + '/api/v1/repos/progressadmin/before-snapshot', headers={'Authorization': 'token ' + source.api_token}, timeout=15)
        assert response.status_code == 200
        response = a.requests.get(target.gitea_url + '/api/v1/repos/progressadmin/after-snapshot', headers={'Authorization': 'token ' + source.api_token}, timeout=15)
        assert response.status_code == 404
        a.note('恢复到独立目标后仅包含停写时间点仓库，成功源端副本已清理')
        failed = a.Backup(source_server_id=source.id, source_server_name=source.name, source_api_token=source.api_token,
            filename='failed.zip', file_path='', status='running', commit_snapshot_status='pending')
        a.db.session.add(failed); a.db.session.commit()
        with patch.object(backup_staging, 'receive_export', side_effect=OSError('验收注入传输中断')):
            a.do_backup(failed.id)
        a.db.session.expire_all()
        assert failed.status == 'failed' and not failed.recovery_pending
        assert failed.source_export.state == 'ready'
        before = container.reload() or container.attrs['State']['StartedAt']
        a.app.before_request_funcs[None].remove(a.readonly)
        with patch('routes.backup_routes.task_manager.run_backup'):
            response = a.app.test_client().post('/api/backups/' + str(failed.id) + '/retry-transfer')
        assert response.status_code == 201, response.json
        retry = a.db.session.get(a.Backup, response.json['id'])
        a.do_backup(retry.id)
        a.db.session.expire_all()
        assert retry.status == 'success' and retry.source_export.state == 'cleaned'
        assert failed.status == 'failed'
        container.reload()
        assert container.attrs['State']['StartedAt'] == before
        a.note('传输中断保留副本，重收完成且源站未再次启停，原失败记录保留')
finally:
    # 只清理本次验收创建的资源。
    for container in reversed(a.owned):
        try:
            container.remove(force=True, v=True)
        except a.docker.errors.NotFound:
            pass
    with a.app.app_context():
        for export in BackupExport.query.all():
            try:
                a.client.volumes.get(export.volume).remove()
            except a.docker.errors.NotFound:
                pass
    network.disconnect(os.environ['HOSTNAME'])
    network.remove()
    a.client.close()
