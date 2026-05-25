import os
import time
from datetime import datetime
from flask import Blueprint, request, jsonify, send_file
from flask_login import login_required
from models import db, GiteaServer, Backup
from services.task_manager import task_manager

backup_bp = Blueprint('backups', __name__)


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
    filename = f'gitea-dump-{safe_name}-{now.strftime("%Y%m%d-%H%M%S")}.zip'
    file_path = ''  # Set after backup completes

    backup = Backup(
        source_server_id=server.id,
        source_api_token=server.api_token,
        source_server_name=server.name,
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
    if b.file_path and os.path.exists(b.file_path):
        os.remove(b.file_path)
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
