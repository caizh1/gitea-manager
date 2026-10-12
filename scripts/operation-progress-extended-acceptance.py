#!/usr/bin/env python3
"""在本轮已隔离的数据上验证大文件、失败终态和双 Worker 可见性。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

roots = list(Path('/tmp').glob('operation-acceptance-*/manager.db'))
if len(roots) != 1: raise RuntimeError('无法唯一确定隔离数据库')
os.environ['ACCEPTANCE_ROOT'] = str(roots[0].parent)
spec = importlib.util.spec_from_file_location('acceptance', '/acceptance.py')
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
from services.operation_progress import update_backup_progress
from models import BackupRepoCommit
with a.app.app_context():
    source = a.db.session.get(a.GiteaServer, 1); target = a.db.session.get(a.GiteaServer, 2)
    for server in (source, target): server.is_local = True; server.connection_mode = 'local'; server.host = '127.0.0.1'
    a.db.session.commit()
    container = a.client.containers.get(source.gitea_container)
    if container.exec_run(['sh', '-c', 'dd if=/dev/urandom of=/data/gitea/progress-sample.bin bs=1048576 count=64']).exit_code:
        raise RuntimeError('大文件样本准备失败')
    sample = a.backup(source, 'large-sample.zip')
    assert sample.file_size > 64 * 1024 * 1024
    steps = a.BackupStepLog.query.filter_by(backup_id=sample.id, step_key='transfer').one()
    metrics = json.loads(steps.metrics_json)
    assert metrics['当前数量'] == metrics['总数量'] == sample.file_size
    a.restore(sample, target)
    original = container.exec_run(['sha256sum', '/data/gitea/progress-sample.bin']).output.split()[0]
    restored = a.client.containers.get(target.gitea_container).exec_run(['sha256sum', '/data/gitea/progress-sample.bin']).output.split()[0]
    assert original == restored
    a.note('真实 64 MiB 随机文件备份恢复及内容校验通过', 备份字节=sample.file_size)
    task = a.Backup(source_server_id=source.id, filename='transfer-failure.zip', file_path='', status='running')
    a.db.session.add(task); a.db.session.commit()
    with patch('services.gitea_service.local_cp_from', side_effect=OSError('受控注入：备份传输失败')):
        a.do_backup(task.id)
    a.db.session.expire_all()
    assert task.status == 'failed'
    assert a.BackupStepLog.query.filter_by(backup_id=task.id, status='running').count() == 0
    a.note('受控传输失败保留失败终态，未重放备份')
    # 使用完整快照并只破坏校验哈希，使真实恢复验证进入回退分支。
    bad = a.Backup(source_server_id=source.id, source_api_token=source.api_token, filename='mismatch-sample.zip', file_path=sample.file_path, status='success', commit_snapshot_status='success', commit_snapshot_repo_count=60)
    a.db.session.add(bad); a.db.session.flush()
    for index, row in enumerate(BackupRepoCommit.query.filter_by(backup_id=sample.id).all()):
        a.db.session.add(BackupRepoCommit(backup_id=bad.id, repo_name=row.repo_name, commit_count=row.commit_count,
                        latest_commit_sha=row.latest_commit_sha, commit_ids_hash='broken' if index == 0 else row.commit_ids_hash, commit_ids=row.commit_ids))
    a.db.session.commit()
    failed = a.restore(bad, target, expected='failed')
    assert a.RestoreStepLog.query.filter_by(restore_task_id=failed.id, step_key='rollback', status='success').count() == 1
    assert a.RestoreStepLog.query.filter_by(restore_task_id=failed.id, step_key='cleanup', status='success').count() == 1
    a.note('真实 Commit 校验失败触发回退，回退和清理步骤分别成功')

# 两个真实 Gunicorn Worker 读取同一磁盘数据库；测试端口只绑定容器回环。
Path('/tmp/progress_worker_app.py').write_text('from acceptance import app\nimport os\n@app.after_request\ndef worker_id(response):\n    response.headers["X-Test-Worker"] = str(os.getpid())\n    return response\n')
worker = subprocess.Popen(['gunicorn', '--workers', '2', '--bind', '127.0.0.1:5088', '--pythonpath', '/,/tmp', 'progress_worker_app:app'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    deadline = time.monotonic() + 20
    workers = set()
    while len(workers) < 2 and time.monotonic() < deadline:
        try:
            response = a.requests.get('http://127.0.0.1:5088/api/backups', timeout=2)
            response.raise_for_status(); workers.add(response.headers['X-Test-Worker'])
            assert any(row['id'] == sample.id and row['progress_stage'] == 'completed' for row in response.json())
        except a.requests.ConnectionError: time.sleep(.1)
    assert len(workers) == 2
    a.note('两个真实 Worker 均读取到持久化进度', Worker数量=len(workers))
finally:
    worker.terminate(); worker.wait(timeout=10)
Path('/evidence/operation-progress-extended.json').write_text(json.dumps({'说明': '本轮隔离实例扩展验收', '检查': a.records}, ensure_ascii=False, indent=2)+'\n')
