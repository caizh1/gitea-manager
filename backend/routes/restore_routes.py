from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_login import login_required
from models import db, GiteaServer, Backup, RestoreTask
from services.task_manager import task_manager

restore_bp = Blueprint('restore', __name__)


def restore_to_dict(r):
    return {
        'id': r.id,
        'backup_id': r.backup_id,
        'backup_filename': r.backup.filename if r.backup else '',
        'target_server_id': r.target_server_id,
        'target_server_name': r.target_server.name if r.target_server else '',
        'status': r.status,
        'error_msg': r.error_msg,
        'started_at': r.started_at.isoformat() if r.started_at else None,
        'completed_at': r.completed_at.isoformat() if r.completed_at else None,
    }


@restore_bp.route('/restore-tasks', methods=['GET'])
@login_required
def list_restore_tasks():
    tasks = RestoreTask.query.order_by(RestoreTask.started_at.desc()).all()
    return jsonify([restore_to_dict(t) for t in tasks])


@restore_bp.route('/restore', methods=['POST'])
@login_required
def execute_restore():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    backup_id = data.get('backup_id')
    target_id = data.get('target_server_id')

    backup = Backup.query.get(backup_id)
    target = GiteaServer.query.get(target_id)

    if not backup:
        return jsonify({'error': 'Backup not found'}), 404
    if not target:
        return jsonify({'error': 'Target server not found'}), 404
    if backup.status != 'success':
        return jsonify({'error': 'Backup is not in success state'}), 400

    task = RestoreTask(
        backup_id=backup.id,
        target_server_id=target.id,
        status='running',
        started_at=datetime.utcnow(),
    )
    db.session.add(task)
    db.session.commit()

    task_manager.run_restore(task.id)

    return jsonify(restore_to_dict(task)), 201
