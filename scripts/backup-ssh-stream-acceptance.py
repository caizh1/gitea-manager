"""真实 SSH 归档流阻塞复现；仅操作独立标记容器。"""
import importlib.util
import os
import time
import hashlib
import json
import zipfile
from unittest.mock import patch
from types import SimpleNamespace
import paramiko
from requests.exceptions import ReadTimeout

spec = importlib.util.spec_from_file_location('acceptance', '/scripts/operation-progress-acceptance.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
from services.docker_service import managed_client

def main():
    m.root.mkdir(parents=True, exist_ok=True)
    previous_operations = {container.id for container in m.client.containers.list(all=True, filters={'label': 'gitea.manager.operation'})}
    remote = None
    try:
        gateway = m.create('gitea-manager-restore-ssh:20261013', 'ssh', entrypoint=['sleep'], command=['infinity'],
            volumes={'/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}})
        gateway.start(); gateway.reload()
        assert gateway.exec_run(['sh', '-ec', 'ssh-keygen -A; mkdir -p /root/.ssh; chmod 700 /root/.ssh']).exit_code == 0
        key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(str(m.root/'ssh-key'))
        m.upload(gateway, '/root/.ssh', 'authorized_keys', key.get_name()+' '+key.get_base64()+'\n')
        assert gateway.exec_run(['/usr/sbin/sshd', '-o', 'PermitRootLogin=prohibit-password', '-o', 'PasswordAuthentication=no']).exit_code == 0
        host = gateway.attrs['NetworkSettings']['Networks']['bridge']['IPAddress']
        pub = gateway.exec_run(['cat', '/etc/ssh/ssh_host_ed25519_key.pub']).output.decode().split()
        (m.root/'known-hosts').write_text(host+' '+pub[0]+' '+pub[1]+'\n')
        server = SimpleNamespace(connection_mode='ssh', host=host, ssh_port=22, ssh_user='root')
        assert gateway.exec_run(['sh', '-ec', 'mkdir /tmp/artifacts; dd if=/dev/zero of=/tmp/artifacts/sample bs=1M count=32']).exit_code == 0
        for consume in (False, True):
            remote = managed_client(server)
            # 缩短故障注入等待，不修改生产超时；两组使用相同阈值。
            remote.api.timeout = 5
            target = remote.containers.get(gateway.id)
            blocks, _ = target.get_archive('/tmp/artifacts')
            started = time.monotonic()
            if consume:
                total = sum(len(block) for block in blocks)
            try:
                result = target.exec_run(['readlink', '-f', '/tmp/artifacts'])
                m.note('先消费归档' if consume else '未消费归档', 成功=result.exit_code == 0, 耗时=round(time.monotonic()-started, 2))
                assert result.exit_code == 0
                assert consume, '旧顺序未复现，不能宣称根因确认'
            except ReadTimeout as exc:
                m.note('先消费归档' if consume else '未消费归档', 异常类型=type(exc).__name__, 耗时=round(time.monotonic()-started, 2))
                if consume: raise
            finally:
                blocks.close()
                remote.close(); remote = None
        if os.environ.get('FULL_BACKUP_CHECK') == '1':
            with m.app.app_context():
                m.db.create_all()
                create = m.create
                def enhanced(image, suffix, **kwargs):
                    if image == 'gitea/gitea:1.26.4':
                        image = 'gitea-enhanced:native-upgrade-20261015-r4-final'
                    return create(image, suffix, **kwargs)
                with patch.object(m, 'create', enhanced):
                    source, original, _ = m.node('source')
                response = m.requests.post(source.gitea_url + '/api/v1/user/repos',
                    headers={'Authorization': 'token ' + source.api_token},
                    json={'name': 'backup-sample', 'auto_init': True, 'private': True}, timeout=30)
                response.raise_for_status()
                assert original.exec_run(['sh', '-ec', 'mkdir -p /data/gitea/actions_artifacts; dd if=/dev/urandom of=/data/gitea/actions_artifacts/sample bs=1M count=32; chown -R git:git /data/gitea/actions_artifacts']).exit_code == 0
                digest = original.exec_run(['sha256sum', '/data/gitea/actions_artifacts/sample']).output.decode().split()[0]
                for transport in ('local', 'ssh', 'ssh'):
                    source.connection_mode = transport
                    source.is_local = transport == 'local'
                    source.host = host
                    m.db.session.commit()
                    sample = m.backup(source, transport + '-' + str(time.time_ns()) + '.zip')
                    with zipfile.ZipFile(sample.file_path) as archive:
                        inventory = json.loads(archive.read('manager-actions.json'))
                        assert inventory['文件']['sample'] == digest
                        name = next(name for name in archive.namelist() if name.endswith('/actions_artifacts/sample'))
                        with archive.open(name) as body:
                            assert hashlib.file_digest(body, 'sha256').hexdigest() == digest
                    original.reload()
                    assert original.status == 'running'
                    assert original.exec_run(['test', '!', '-e', '/data/.manager-restore-lock']).exit_code == 0
                    deadline = time.monotonic() + 90
                    while True:
                        try:
                            if m.requests.get(source.gitea_url + '/api/v1/version', timeout=2).status_code == 200:
                                break
                        except m.requests.RequestException:
                            pass
                        if time.monotonic() >= deadline:
                            raise AssertionError('备份后源服务未恢复健康')
                        time.sleep(0.5)
                    m.note('大制品备份正文与摘要一致，源服务运行且无残留锁', 模式=transport, 制品字节数=32*1024*1024)
                from docker.models.containers import Container
                get_archive = Container.get_archive
                def broken_archive(container, path, *args, **kwargs):
                    blocks, details = get_archive(container, path, *args, **kwargs)
                    if path != '/data/gitea/actions_artifacts':
                        return blocks, details
                    def interrupted():
                        try:
                            yield next(blocks)
                            raise OSError('验收注入的制品传输中断')
                        finally:
                            blocks.close()
                    return interrupted(), details
                with patch.object(Container, 'get_archive', broken_archive):
                    try:
                        m.backup(source, 'interrupted.zip')
                    except RuntimeError as exc:
                        assert '验收注入的制品传输中断' in str(exc), str(exc)
                    else:
                        raise AssertionError('传输中断未被判定失败')
                original.reload()
                assert original.status == 'running'
                assert original.exec_run(['test', '!', '-e', '/data/.manager-restore-lock']).exit_code == 0
                remaining = {container.id for container in m.client.containers.list(all=True, filters={'label': 'gitea.manager.operation'})}
                assert remaining == previous_operations, '本次辅助容器未清理或既有现场被改动'
                m.note('真实传输中断保留首次错误，源服务运行、锁与辅助容器均已清理')
    finally:
        if remote: remote.close()
        for container in reversed(m.owned):
            container.remove(force=True, v=True)

if __name__ == '__main__':
    main()
