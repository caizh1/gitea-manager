"""升级器的离线卷操作助手；仅访问显式挂载的数据与本次备份目录。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import tempfile


def quoted(value): return '"' + value.replace('"', '""') + '"'


def database(path, reference=None):
    result = {'exists': path.is_file(), 'tables': {}, 'busy': []}
    if not path.is_file(): return result
    # 历史审查库可能较大，卷助手使用磁盘暂存，避免数据库拷贝挤满容器内存。
    with tempfile.TemporaryDirectory(dir=os.environ.get('UPGRADE_TEMP_DIR')) as folder:
        copy = Path(folder) / path.name
        for suffix in ('', '-wal', '-shm'):
            origin = Path(str(path) + suffix)
            if origin.is_file(): shutil.copyfile(origin, str(copy) + suffix)
        with sqlite3.connect(copy) as c:
            if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise RuntimeError('数据库完整性检查失败')
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            statuses = {'backups': ('running',), 'restore_tasks': ('running',), 'scheduled_tasks': ('running',),
                        'mirror_configs': ('running', 'syncing', 'initializing'), 'policy_batches': ('previewing', 'applying'),
                        'jobs': ('running', 'publishing', 'recovering'), 'setup_jobs': ('running',)}
            for table, states in statuses.items():
                if table in names:
                    field = 'last_status' if table == 'scheduled_tasks' else 'status'
                    count = c.execute('SELECT count(*) FROM ' + quoted(table) + ' WHERE ' + quoted(field) + ' IN (' + ','.join('?' for _ in states) + ')', states).fetchone()[0]
                    if count: result['busy'].append({'table': table, 'count': count})
            # 比较旧列与旧记录，新增表及新增列不被当作数据丢失。
            selected = names - {'sqlite_sequence', 'health', 'capabilities', 'integration_service_tokens', 'integration_audit_logs', 'policy_request_slots', 'setup_probes'}
            if reference: selected = set(reference['tables'])
            for table in sorted(selected):
                if table not in names: raise RuntimeError('升级后缺少原有数据表：' + table)
                columns = reference['tables'][table]['columns'] if reference else [r[1] for r in c.execute('PRAGMA table_info(' + quoted(table) + ')')]
                selected_columns = ','.join(map(quoted, columns))
                rows = c.execute('SELECT ' + selected_columns + ' FROM ' + quoted(table) + ' ORDER BY ' + selected_columns)
                sha = hashlib.sha256(); count = 0
                for row in rows:
                    raw = json.dumps(row, ensure_ascii=False, default=lambda b: {'二进制': bytes(b).hex()}).encode()
                    sha.update(str(len(raw)).encode() + b':' + raw); count += 1
                result['tables'][table] = {'columns': columns, 'rows': count, 'sha256': sha.hexdigest()}
    return result


def inventory(root, excluded):
    total = 0; preserved = []
    for p in root.rglob('*'):
        relative = str(p.relative_to(root))
        if p.is_symlink(): raise RuntimeError('数据卷含有符号链接，请先确认其持久化边界：' + relative)
        if p.is_file():
            if excluded and (relative == excluded or relative.startswith(excluded + '/')):
                st = p.stat(); preserved.append([relative, st.st_size, st.st_mtime_ns])
            else: total += p.stat().st_size
    return {'backup_bytes': total, 'preserved': sorted(preserved)}


def main():
    action, name = sys.argv[1:3]
    root = Path('/volume'); state = Path('/state')
    parameters = json.loads(os.environ.get('VOLUME_PARAMETERS', '{}'))
    file = state / (name + '.json')
    excluded = parameters.get('excluded', '')
    db_path = root / parameters.get('database', 'review.db' if name == 'review' else 'gitea-manager.db')
    if action == 'inspect':
        value = {'database': database(db_path), 'files': inventory(root, excluded)}
        print(json.dumps(value, ensure_ascii=False)); return
    if action == 'backup':
        value = {'database': database(db_path), 'files': inventory(root, excluded)}
        with tarfile.open(state / (name + '.tar'), 'w') as archive:
            for item in sorted(root.rglob('*')):
                relative = str(item.relative_to(root))
                if excluded and (relative == excluded or relative.startswith(excluded + '/')): continue
                archive.add(item, arcname=relative, recursive=False)
        file.write_text(json.dumps(value, ensure_ascii=False)); file.chmod(0o600)
        return
    if action == 'verify':
        before = json.loads(file.read_text())
        after = database(db_path, before['database']) if before['database']['exists'] else database(db_path)
        if before['database']['exists']:
            for table, value in before['database']['tables'].items():
                if after['tables'].get(table) != value: raise RuntimeError('历史数据核对不一致：' + table)
        if inventory(root, excluded)['preserved'] != before['files']['preserved']:
            raise RuntimeError('原有备份文件清单发生变化')
        return
    if action == 'restore':
        archive_path = state / (name + '.tar')
        with tarfile.open(archive_path) as archive:
            for member in archive.getmembers():
                if not (member.isfile() or member.isdir()) or not (root / member.name).resolve().is_relative_to(root):
                    raise RuntimeError('备份包含不安全的恢复路径')
            for item in root.iterdir():
                if excluded and item.name == excluded.split('/')[0]: continue
                if item.is_symlink() or item.is_file(): item.unlink()
                else: shutil.rmtree(item)
            archive.extractall(root)
        return
    if action == 'maintenance':
        marker = root / '.upgrade-maintenance'
        if parameters.get('enabled'):
            marker.write_text('正在升级，暂停领取任务与处理业务写入。\n'); marker.chmod(0o600)
        else: marker.unlink(missing_ok=True)
        return
    raise RuntimeError('未知的卷操作')


if __name__ == '__main__':
    main()
