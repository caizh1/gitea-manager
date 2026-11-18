"""用独立 Gitea 验收旧上限以上的快照、定时备份、导入和恢复。"""
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import tarfile
import tempfile
import zipfile

with tempfile.TemporaryDirectory(prefix='large-backup-') as work:
    os.environ['ACCEPTANCE_ROOT'] = work
    os.environ['DATABASE_URL'] = 'sqlite:///' + work + '/manager.db'
    spec = importlib.util.spec_from_file_location('acceptance', '/scripts/operation-progress-acceptance.py')
    a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a)

    import access.models
    import access.scopes
    import access.statistics
    import git_migration.models
    import svn_migration.models
    from models import BackupExport, ScheduleLog, ScheduledTask
    from services.backup_consistency import SNAPSHOT_FILE
    from services.schedule_runner import claim_schedule_task, run_schedule_task

    used = [ipaddress.ip_network(entry['Subnet']) for network in a.client.networks.list()
            for entry in network.attrs.get('IPAM', {}).get('Config', []) or []]
    candidates = (ipaddress.ip_network(f'10.242.{i}.0/24') for i in range(1, 255))
    subnet = next(candidate for candidate in candidates if not any(candidate.overlaps(net) for net in used))
    ipam = a.docker.types.IPAMConfig(pool_configs=[a.docker.types.IPAMPool(subnet=str(subnet))])
    network = a.client.networks.create(a.marker, labels={'manager.progress.acceptance': a.marker}, ipam=ipam)
    network.connect(os.environ['HOSTNAME'])
    os.environ['ACCEPTANCE_NETWORK'] = network.name
    try:
        with a.app.app_context():
            a.db.create_all()
            source, source_container, _ = a.node('source')
            target, target_container, _ = a.node('target')
            source.gitea_url = f'http://{source_container.name}:3000'
            target.gitea_url = f'http://{target_container.name}:3000'
            response = a.requests.post(source.gitea_url + '/api/v1/user/repos',
                headers={'Authorization': 'token ' + source.api_token},
                json={'name': 'large-snapshot', 'auto_init': True, 'private': True}, timeout=30)
            response.raise_for_status()
            repository = '/data/git/repositories/progressadmin/large-snapshot.git'
            head = source_container.exec_run(['git', '--git-dir', repository, 'rev-parse', 'HEAD'], user='git')
            assert head.exit_code == 0
            prefix = 'refs/tags/' + '/'.join(letter * 200 for letter in 'abcd')
            count = 85000
            with tempfile.TemporaryFile() as packed, tempfile.TemporaryFile() as wire:
                packed.write(b'# pack-refs with: peeled fully-peeled sorted\n')
                for index in range(count):
                    packed.write(head.output.strip() + f' {prefix}/fixture-{index:06d}\n'.encode())
                info = tarfile.TarInfo('packed-refs')
                info.size, info.uid, info.gid, info.mode = packed.tell(), 1000, 1000, 0o644
                packed.seek(0)
                with tarfile.open(fileobj=wire, mode='w') as archive:
                    archive.addfile(info, packed)
                wire.seek(0)
                assert source_container.put_archive(repository, wire)
            a.note('隔离仓库已写入真实 Git 引用', 引用数=count)

            schedule = ScheduledTask(name='隔离大元数据备份', source_server_id=source.id,
                                     target_ids=json.dumps([target.id]), schedule_hour=0, schedule_minute=0)
            a.db.session.add(schedule)
            a.db.session.commit()
            claimed, _, _ = claim_schedule_task(schedule.id)
            assert claimed
            run_schedule_task(schedule.id)
            log = ScheduleLog.query.filter_by(schedule_task_id=schedule.id).one()
            assert log.status == 'success', log.log
            backup = a.db.session.get(a.Backup, log.backup_id)
            with zipfile.ZipFile(backup.file_path) as archive:
                size = archive.getinfo(SNAPSHOT_FILE).file_size
                assert size > 64 * 1024 * 1024, size
            a.note('真实调度完成备份及目标恢复', 仓库快照字节数=size, 备份编号=backup.id)

            a.app.before_request_funcs[None].remove(a.readonly)
            with open(backup.file_path, 'rb') as stream:
                response = a.app.test_client().post('/api/backups/import', data={'file': (stream, 'large-backup.zip')})
            assert response.status_code == 201, response.json
            imported = a.db.session.get(a.Backup, response.json['id'])
            a.restore(imported, target)
            a.note('独立导入和恢复通过，目标引用及提交已由恢复流程核对', 备份编号=imported.id)
    finally:
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
