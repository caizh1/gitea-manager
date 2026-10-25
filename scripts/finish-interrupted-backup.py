"""仅为已随 API 重启中断的指定备份收尾；不删除文件，不操作远端 Gitea。"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote


def master_started():
    boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
    ticks = int(Path('/proc/1/stat').read_text().rsplit(')', 1)[1].split()[19])
    return boot + ticks / os.sysconf('SC_CLK_TCK')


def finish(database, diagnostic_root, backup_id, started):
    with sqlite3.connect('file:' + quote(str(database)) + '?mode=rw', uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT status,diagnostic_id FROM backups WHERE id=?', (backup_id,)).fetchone()
        if row is None:
            raise RuntimeError('指定备份不存在，未修改记录')
        if row['status'] != 'running':
            return {'说明':'备份已经结束，未修改记录', '任务':backup_id, '状态':row['status']}
        diagnostic = row['diagnostic_id'] or ''
        if not diagnostic or Path(diagnostic).name != diagnostic:
            raise RuntimeError('备份缺少有效诊断标识，拒绝无证据收尾')
        value = json.loads((Path(diagnostic_root)/diagnostic/'status.json').read_text())
        if started <= float(value['开始时间']) + 1:
            raise RuntimeError('本次 API 进程启动早于备份，无法证明原线程已退出；未修改记录')
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(' ')
        reason = 'API 重启中断备份；维护命令已确认原进程退出，本备份未通过完整性校验，禁止用于恢复'
        connection.execute('''UPDATE backups SET status='failed',completed_at=?,error_msg=?,
            commit_snapshot_status='failed',progress_stage='failed',progress_label='备份已中断',
            progress_detail=?,progress_updated_at=? WHERE id=? AND status='running' ''',
            (now,reason,reason,now,backup_id))
        connection.execute('''UPDATE backup_step_logs SET status='failed',completed_at=?,detail=?
            WHERE backup_id=? AND status='running' ''', (now,reason,backup_id))
        return {'说明':'指定中断备份已标记失败，原有文件和诊断保留', '任务':backup_id, '状态':'failed'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup-id', type=int, required=True)
    args = parser.parse_args()
    url = os.environ.get('DATABASE_URL', 'sqlite:////app/data/gitea-manager.db')
    if not url.startswith('sqlite:////') or '?' in url:
        raise RuntimeError('仅支持当前部署的本机 SQLite 数据库')
    result = finish(url.removeprefix('sqlite:///'),
        os.environ.get('MANAGEMENT_DIAGNOSTICS_ROOT', '/app/data/diagnostics'), args.backup_id, master_started())
    print(json.dumps(result, ensure_ascii=False))
