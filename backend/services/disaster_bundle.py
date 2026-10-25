"""将数据、固定镜像和恢复入口放入同一个离线包，不依赖原 Manager 记录。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import zipfile

import docker


def export(backup, output):
    from models import db
    from services.backup_manifest import load_manifest
    from services.docker_service import managed_client
    manifest = load_manifest(backup.file_path)
    if manifest is None or backup.status != 'success' or backup.source_server is None:
        raise RuntimeError('完整离线包需要新备份及可访问的源镜像；请在灾难发生前导出并异地保存')
    from services.backup_dependencies import DEPENDENCIES
    with zipfile.ZipFile(backup.file_path) as archive:
        if DEPENDENCIES not in archive.namelist():
            raise RuntimeError('此历史备份尚未验证启动依赖，请重新备份后导出完整离线包')
    local = docker.from_env()
    source = managed_client(backup.source_server)
    try:
        manager = local.containers.get(os.environ['HOSTNAME'])
        database = source.containers.get(backup.source_server.pg_container)
        from services.backup_consistency import database_sql
        if int(database_sql(database, backup.source_server, 'SHOW server_version_num')) // 10000 != manifest['准入']['数据库主版本']:
            raise RuntimeError('源 PostgreSQL 主版本已变化，无法为该备份导出匹配的离线镜像')
        images = {'manager': (local, manager.attrs['Image']),
                  'gitea': (source, manifest['准入']['镜像']), 'postgres': (source, database.attrs['Image'])}
        with tempfile.TemporaryDirectory(prefix='disaster-export-', dir=str(Path(output).parent)) as root:
            root = Path(root)
            shutil.copyfile(backup.file_path, root / 'backup.zip')
            for name, (client, identifier) in images.items():
                with (root / (name + '.tar')).open('wb') as stream:
                    for block in client.images.get(identifier).save(named=False):
                        stream.write(block)
            # SQLite 在线备份接口包含已提交 WAL；不逐文件复制活动数据库。
            database_path = db.engine.url.database
            if db.engine.dialect.name != 'sqlite' or not database_path or database_path == ':memory:':
                raise RuntimeError('Manager 状态导出仅支持持久 SQLite 数据库')
            with sqlite3.connect(database_path) as original, sqlite3.connect(root / 'manager-state.sqlite') as destination:
                original.backup(destination)
                if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Manager 状态快照完整性检查失败')
            metadata = {'版本': 1, '镜像': {name: value[1] for name, value in images.items()},
                        '说明': 'Manager 主数据库快照用于保留历史；一键恢复使用新数据库导入当前备份，不自动启用旧调度及授权。独立 AI Review 数据库和外部 Runner 不在此包范围内。'}
            (root / 'bundle.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
            image = images['manager'][1]
            script = f'''#!/bin/sh
set -eu
cd -- "$(dirname -- "$0")"
command -v docker >/dev/null || {{ echo '请先在目标主机安装 Docker'; exit 1; }}
sha256sum -c SHA256SUMS
docker load -i manager.tar
docker run --rm --network bridge -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD:/bundle:ro" -v "${{DISASTER_STATE_VOLUME:-gitea-disaster-manager}}:/app/data" -e BACKUP_DIR=/app/data/backups {image} python -m services.disaster_bootstrap "$@"
'''
            (root / 'recover.sh').write_text(script)
            (root / 'recover.sh').chmod(0o700)
            sums = []
            for path in sorted(root.iterdir()):
                path.chmod(0o700 if path.name == 'recover.sh' else 0o600)
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(block)
                sums.append(digest.hexdigest() + '  ' + path.name)
            (root / 'SHA256SUMS').write_text('\n'.join(sums) + '\n')
            (root / 'SHA256SUMS').chmod(0o600)
            with tarfile.open(output, 'w') as archive:
                for path in sorted(root.iterdir()):
                    archive.add(path, arcname=path.name)
            os.chmod(output, 0o600)
    finally:
        source.close()
        local.close()
