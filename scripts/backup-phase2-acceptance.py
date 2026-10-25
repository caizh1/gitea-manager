#!/usr/bin/env python3
"""第二批隔离验收：独立导入、准入拒绝、进程中断及发布后写入保护。"""
import importlib.util
import io
import hashlib
import json
import os
from pathlib import Path
import signal
import time
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('phase1', '/scripts/backup-phase1-acceptance.py')
p1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p1)
m = p1.m
from models import Backup, BackupRepoCommit, RestoreTask
from services import backup_manifest, restore_recovery
from services.gitea_service import do_restore


def new_task(backup, target):
    task = RestoreTask(backup_id=backup.id, target_server_id=target.id, target_server_name=target.name, status='running')
    m.db.session.add(task)
    m.db.session.commit()
    return task


def main():
    m.create = p1.create
    try:
        with m.app.app_context():
            m.db.create_all()
            source, src, _ = m.node('source')
            target, dst, _ = m.node('target')
            p1.api(source, 'POST', '/user/repos', json={'name': 'source-data', 'auto_init': True})
            p1.api(target, 'POST', '/user/repos', json={'name': 'old-target', 'auto_init': True})
            backup = m.backup(source, 'phase2.zip')
            manifest = backup_manifest.load_manifest(backup.file_path)
            assert len(manifest['数据表']) > 20
            assert any(name.startswith('governance_') for name in manifest['数据表'])
            m.note('完整数据库停写基准已生成', 表数量=len(manifest['数据表']))
            actual = backup_manifest.identity
            with patch.object(backup_manifest, 'identity', lambda *args: dict(actual(*args), 镜像='sha256:不匹配')):
                task = new_task(backup, target)
                do_restore(task.id)
                assert task.status == 'failed' and '准入不匹配' in task.error_msg
            dst.reload()
            assert dst.status == 'running'
            assert p1.api(target, 'GET', '/repos/progressadmin/old-target').status_code == 200
            m.note('构建不匹配在停服务和改数据之前被拒绝')
            path = backup.file_path
            raw = Path(path).read_bytes()
            corrupted = m.root / 'changed-business.zip'
            with zipfile.ZipFile(path) as archive:
                entries = {name: archive.read(name) for name in archive.namelist()}
                infos = {entry.filename: entry for entry in archive.infolist()}
            sql_name = next(name for name in entries if name.endswith('gitea-db.sql'))
            entries[sql_name] += "\nUPDATE repository SET description = '验收注入的业务内容变化';\n".encode()
            changed = json.loads(entries['manager-manifest.json'])
            changed['文件'][sql_name]['摘要'] = hashlib.sha256(entries[sql_name]).hexdigest()
            entries['manager-manifest.json'] = json.dumps(changed, ensure_ascii=False).encode()
            with zipfile.ZipFile(corrupted, 'w') as archive:
                for name, content in entries.items():
                    archive.writestr(infos[name], content)
            corrupted.chmod(0o600)
            backup.file_path = str(corrupted)
            m.db.session.commit()
            task = new_task(backup, target)
            do_restore(task.id)
            assert task.status == 'failed' and '业务数据表' in task.error_msg, task.error_msg
            p1.wait_ready(target)
            assert p1.api(target, 'GET', '/repos/progressadmin/old-target').status_code == 200
            backup.file_path = path
            m.db.session.commit()
            m.note('非 Git 业务内容变更被逐表门禁发现，目标原数据回退通过')
            BackupRepoCommit.query.filter_by(backup_id=backup.id).delete()
            m.db.session.delete(backup)
            m.db.session.commit()
            src.stop(timeout=30)
            from routes.backup_routes import import_backup
            with m.app.test_request_context('/api/backups/import', method='POST',
                    data={'file': (io.BytesIO(raw), 'offline.zip')}):
                response, status = import_backup()
                assert status == 201, response.json
                backup = m.db.session.get(Backup, response.json['id'])
            assert backup.source_server_id != source.id
            assert BackupRepoCommit.query.filter_by(backup_id=backup.id).count() == 1
            m.note('源服务离线且原 Manager 校验记录删除后，独立导入重建记录通过')
            for phase in ('数据库修改', '文件切换', '启动配置', '发布'):
                task = new_task(backup, target)
                task_id = task.id
                target_id = target.id
                backup_id = backup.id
                m.db.session.remove()
                child = os.fork()
                if child == 0:
                    save = restore_recovery.Journal.save
                    def interrupted(self, current):
                        save(self, current)
                        if current == phase:
                            os.kill(os.getpid(), signal.SIGKILL)
                    with patch.object(restore_recovery.Journal, 'save', interrupted):
                        do_restore(task_id)
                    os._exit(3)
                _, result = os.waitpid(child, 0)
                assert os.WIFSIGNALED(result) and os.WTERMSIG(result) == signal.SIGKILL, (phase, result)
                m.db.session.remove()
                target = m.db.session.get(m.GiteaServer, target_id)
                backup = m.db.session.get(Backup, backup_id)
                if phase == '发布':
                    m.client.containers.get(target.gitea_container).start()
                    p1.wait_ready(target)
                    p1.api(target, 'POST', '/user/repos', json={'name': 'accepted-after-crash', 'auto_init': True})
                # 正式服务尚未开放，临时桥接地址重新读取由健康检查处理。
                from services import commit_service
                original_health = commit_service._require_target_health
                def fresh_health(server):
                    p1.wait_ready(server)
                    return original_health(server)
                with patch.object(commit_service, '_require_target_health', fresh_health):
                    restore_recovery.recover(target)
                if phase == '发布':
                    assert p1.api(target, 'GET', '/repos/progressadmin/source-data').status_code == 200
                    assert p1.api(target, 'GET', '/repos/progressadmin/accepted-after-crash').status_code == 200
                else:
                    assert p1.api(target, 'GET', '/repos/progressadmin/old-target').status_code == 200
                m.note('强制终止工作进程后接管通过', 中断点=phase)
            m.note('第二批隔离故障注入验收完成')
    finally:
        sources = {mount.get('Source') for container in m.owned for mount in container.attrs.get('Mounts', [])}
        for info in m.client.api.containers(all=True):
            if (info.get('Labels') or {}).get('gitea.manager.operation', '').startswith('manager-restore-'):
                if any(mount.get('Source') in sources for mount in info.get('Mounts', [])):
                    m.client.api.remove_container(info['Id'], force=True)
        for container in reversed(m.owned):
            try: container.remove(force=True, v=True)
            except Exception: pass


if __name__ == '__main__':
    main()
