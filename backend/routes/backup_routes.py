from services.operation_progress import progress_for_response
import os
import time
from datetime import datetime
from flask import Blueprint, request, jsonify, send_file
from flask_login import login_required
from models import db, GiteaServer, Backup
from services.task_manager import task_manager

backup_bp = Blueprint('backups', __name__)


@backup_bp.route('/backups/import', methods=['POST'])
@login_required
def import_backup():
    import hashlib
    import json
    import tempfile
    import uuid
    from config import BACKUP_DIR
    from models import BackupRepoCommit
    from services.backup_manifest import load_manifest
    from services.backup_consistency import load_snapshot, commit_rows
    upload = request.files.get('file')
    if upload is None:
        return jsonify(error='请选择灾备 ZIP 文件'), 400
    os.makedirs(BACKUP_DIR, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='import-', suffix='.partial', dir=BACKUP_DIR)
    os.close(fd)
    destination = None
    try:
        upload.save(temporary)
        manifest = load_manifest(temporary)
        snapshot = load_snapshot(temporary)
        if manifest is None or snapshot is None:
            raise ValueError('独立导入需要含准入清单和停写快照的新备份')
        token = manifest.get('API令牌')
        if not isinstance(token, str) or not token or len(token) > 200:
            raise ValueError('备份缺少有效的源 API 令牌')
        marker = uuid.uuid4().hex
        filename = 'gitea-import-' + marker + '.zip'
        destination = os.path.join(BACKUP_DIR, filename)
        # 占位来源没有连接凭据，也不作为主服务器自动参与调度。
        source = GiteaServer(name=str(manifest.get('来源名称', '离线来源'))[:100], role='archive',
            host='127.0.0.1', ssh_user='', gitea_port=0, gitea_url='', api_token='',
            gitea_container='offline-' + marker, pg_container='offline-' + marker, status='archived')
        db.session.add(source)
        db.session.flush()
        now = datetime.utcnow()
        backup = Backup(source_server_id=source.id, source_server_name=source.name, source_api_token=token,
            filename=filename, file_path=destination, file_size=os.path.getsize(temporary), status='success',
            commit_snapshot_status='success', completed_at=now, commit_snapshot_collected_at=now)
        db.session.add(backup)
        db.session.flush()
        rows = list(commit_rows(snapshot))
        for name, commits in rows:
            db.session.add(BackupRepoCommit(backup_id=backup.id, repo_name=name, commit_count=len(commits),
                latest_commit_sha=commits[0] if commits else '', commit_ids=json.dumps(sorted(commits)),
                commit_ids_hash=hashlib.sha256(','.join(sorted(commits)).encode()).hexdigest(), collected_at=now))
        backup.commit_snapshot_repo_count = len(rows)
        os.replace(temporary, destination)
        db.session.commit()
        return jsonify(backup_to_dict(backup)), 201
    except Exception:
        db.session.rollback()
        if destination and os.path.exists(destination):
            os.remove(destination)
        return jsonify(error='导入失败：备份清单、文件或校验记录无效；未修改目标实例'), 400
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def backup_to_dict(b):
    server_exists = b.source_server is not None
    server_name = b.source_server_name or (b.source_server.name if server_exists else '未知服务器')
    return {
        'id': b.id,
        'source_server_id': b.source_server_id,
        'source_server_name': server_name,
        'source_server_deleted': not server_exists,
        'filename': b.filename,
        'file_path': b.file_path,
        'file_size': b.file_size,
        'status': b.status,
        'error_msg': b.error_msg,
        'diagnostic_id': b.diagnostic_id,
        **progress_for_response(b),
        'commit_snapshot_status': b.commit_snapshot_status or '',
        'commit_snapshot_repo_count': b.commit_snapshot_repo_count or 0,
        'commit_snapshot_error': b.commit_snapshot_error or '',
        'commit_snapshot_collected_at': b.commit_snapshot_collected_at.isoformat() if b.commit_snapshot_collected_at else None,
        'started_at': b.started_at.isoformat() if b.started_at else None,
        'completed_at': b.completed_at.isoformat() if b.completed_at else None,
    }


@backup_bp.route('/backups', methods=['GET'])
@login_required
def list_backups():
    backups = Backup.query.order_by(Backup.started_at.desc()).all()
    return jsonify([backup_to_dict(b) for b in backups])


@backup_bp.route('/backups', methods=['POST'])
@login_required
def create_backup():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    source_id = data.get('source_server_id')
    server = GiteaServer.query.get(source_id)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    now = datetime.utcnow()
    import re
    safe_name = re.sub(r'[^A-Za-z0-9]', '', server.name) or 'server'
    filename = f'gitea-dump-{safe_name}-{now.strftime("%Y%m%d-%H%M%S-%f")}.zip'
    file_path = ''  # Set after backup completes

    backup = Backup(
        source_server_id=server.id,
        source_api_token=server.api_token,
        source_server_name=server.name,
        commit_snapshot_status='pending',
        commit_snapshot_repo_count=0,
        commit_snapshot_error='',
        filename=filename,
        file_path=file_path,
        status='running',
        started_at=now,
    )
    db.session.add(backup)
    db.session.commit()

    task_manager.run_backup(backup.id)

    return jsonify(backup_to_dict(backup)), 201


@backup_bp.route('/backups/<int:bid>', methods=['DELETE'])
@login_required
def delete_backup(bid):
    b = Backup.query.get_or_404(bid)
    if b.status in ('running', 'pending') or b.recovery_pending:
        return jsonify(error='备份仍在运行或尚未完成中断收尾，不能删除恢复凭据'), 409
    if b.file_path and os.path.exists(b.file_path):
        os.remove(b.file_path)
    from pathlib import Path
    from config import BACKUP_DIR
    for bundle in Path(BACKUP_DIR).glob('disaster-' + str(bid) + '-*.tar'):
        bundle.unlink()
    db.session.delete(b)
    db.session.commit()
    return jsonify({'ok': True})


@backup_bp.route('/backups/<int:bid>/download', methods=['GET'])
@login_required
def download_backup(bid):
    b = Backup.query.get_or_404(bid)
    if not b.file_path or not os.path.exists(b.file_path):
        return jsonify({'error': 'File not found'}), 404
    return send_file(b.file_path, as_attachment=True, download_name=b.filename)


@backup_bp.route('/backups/<int:bid>/disaster-bundle', methods=['POST'])
@login_required
def export_disaster_bundle(bid):
    import tempfile
    from config import BACKUP_DIR
    from services.disaster_bundle import export
    from services.diagnostics import redact
    backup = Backup.query.get_or_404(bid)
    fd, path = tempfile.mkstemp(prefix='disaster-' + str(bid) + '-', suffix='.tar', dir=BACKUP_DIR)
    os.close(fd)
    try:
        export(backup, path)
        return jsonify(download_url='/api/backups/' + str(bid) + '/disaster-bundle/' + os.path.basename(path))
    except Exception as exc:
        os.remove(path)
        return jsonify(error=redact(str(exc))), 400


@backup_bp.route('/backups/<int:bid>/disaster-bundle/<filename>', methods=['GET'])
@login_required
def download_disaster_bundle(bid, filename):
    import re
    from config import BACKUP_DIR
    Backup.query.get_or_404(bid)
    if not re.fullmatch(r'disaster-' + str(bid) + r'-[a-zA-Z0-9_]+\.tar', filename):
        return jsonify(error='灾备包不存在'), 404
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.isfile(path):
        return jsonify(error='灾备包不存在'), 404
    return send_file(path, as_attachment=True, download_name='gitea-disaster-' + str(bid) + '.tar')


@backup_bp.route('/backups/<int:bid>/steps', methods=['GET'])
@login_required
def get_backup_steps(bid):
    from models import BackupStepLog
    from services.restore_step_service import restore_step_to_dict
    Backup.query.get_or_404(bid)
    steps = BackupStepLog.query.filter_by(backup_id=bid).order_by(BackupStepLog.id).all()
    return jsonify([restore_step_to_dict(step) for step in steps])
