from flask import Blueprint, jsonify
from flask_login import login_required
from models import Backup, RestoreTask

dashboard_bp = Blueprint('dashboard', __name__)


def _fmt_size(size):
    if not size:
        return '0B'
    if size < 1048576:
        return f'{size/1024:.0f}K'
    if size < 1073741824:
        return f'{size/1048576:.1f}M'
    return f'{size/1073741824:.1f}G'


@dashboard_bp.route('/dashboard/recent', methods=['GET'])
@login_required
def recent_activity():
    recent_backups = Backup.query.order_by(Backup.started_at.desc()).limit(3).all()
    recent_restores = RestoreTask.query.order_by(RestoreTask.started_at.desc()).limit(3).all()

    backup_list = []
    for b in recent_backups:
        backup_list.append({
            'id': b.id,
            'filename': b.filename,
            'source_server_name': b.source_server.name if b.source_server else '',
            'status': b.status,
            'file_size': b.file_size,
            'file_size_display': _fmt_size(b.file_size),
            'error_msg': (b.error_msg or '')[:200],
            'started_at': b.started_at.isoformat() if b.started_at else None,
            'completed_at': b.completed_at.isoformat() if b.completed_at else None,
        })

    restore_list = []
    for r in recent_restores:
        restore_list.append({
            'id': r.id,
            'backup_filename': r.backup.filename if r.backup else '',
            'target_server_name': r.target_server.name if r.target_server else '',
            'status': r.status,
            'error_msg': (r.error_msg or '')[:200],
            'started_at': r.started_at.isoformat() if r.started_at else None,
            'completed_at': r.completed_at.isoformat() if r.completed_at else None,
        })

    return jsonify({
        'recent_backups': backup_list,
        'recent_restores': restore_list,
    })
