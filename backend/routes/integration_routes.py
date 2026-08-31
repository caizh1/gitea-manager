import hmac
import json
from datetime import datetime

from flask import Blueprint, jsonify, request

from models import (Backup, IntegrationOutbox, IntegrationServiceToken, MirrorConfig,
                    RestoreTask, ScheduledTask, db)
from services.integration_outbox_service import sanitize, token_hash, write_audit


integration_bp = Blueprint('integrations', __name__)


def _authenticate():
    authorization = request.headers.get('Authorization', '')
    if not authorization.startswith('Bearer '):
        write_audit('anonymous', 'integration.auth', request.path, 'failed', '缺少 Bearer 服务令牌')
        db.session.commit()
        return None
    digest = token_hash(authorization[7:].strip())
    token = IntegrationServiceToken.query.filter_by(active=True).all()
    matched = next((item for item in token if hmac.compare_digest(item.token_hash, digest)), None)
    if not matched:
        write_audit('anonymous', 'integration.auth', request.path, 'failed', '服务令牌无效')
        db.session.commit()
        return None
    matched.last_used_at = datetime.utcnow()
    return matched


@integration_bp.before_request
def require_service_token():
    token = _authenticate()
    if not token:
        return jsonify({'error': 'Unauthorized'}), 401
    request.integration_token = token


@integration_bp.after_request
def commit_integration_audit(response):
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return response


@integration_bp.get('/health')
def health():
    latest = IntegrationOutbox.query.order_by(IntegrationOutbox.seq.desc()).first()
    write_audit(request.integration_token.name, 'integration.health.read', 'health')
    return jsonify({'status': 'ok', 'schema_version': '1.0', 'latest_seq': latest.seq if latest else 0,
                    'server_time': datetime.utcnow().isoformat() + 'Z'})


@integration_bp.get('/events')
def events():
    try:
        after_seq = max(0, int(request.args.get('after_seq', '0')))
        limit = min(max(1, int(request.args.get('limit', '200'))), 1000)
    except ValueError:
        return jsonify({'error': 'after_seq 和 limit 必须是整数'}), 400
    rows = IntegrationOutbox.query.filter(IntegrationOutbox.seq > after_seq).order_by(IntegrationOutbox.seq).limit(limit).all()
    latest = IntegrationOutbox.query.order_by(IntegrationOutbox.seq.desc()).first()
    write_audit(request.integration_token.name, 'integration.events.read', f'after_seq={after_seq}', detail=f'count={len(rows)}')
    if rows:
        write_audit(request.integration_token.name, 'integration.cursor.advance', f'{after_seq}->{rows[-1].seq}')
    return jsonify({'schema_version': '1.0', 'events': [{
        'seq': item.seq, 'event_type': item.event_type, 'task_type': item.task_type,
        'task_id': item.task_id, 'status': item.status, 'payload': json.loads(item.payload_json or '{}'),
        'created_at': item.created_at.isoformat() + 'Z',
    } for item in rows], 'latest_seq': latest.seq if latest else 0, 'has_more': bool(rows and latest and rows[-1].seq < latest.seq)})


@integration_bp.get('/tasks/<task_type>/<int:task_id>')
def task_detail(task_type, task_id):
    if task_type == 'backup':
        item = Backup.query.get(task_id)
        payload = None if not item else {'id': item.id, 'type': 'backup', 'status': item.status, 'error': (item.error_msg or '')[:2000],
                 'source_server_id': item.source_server_id, 'file_size': item.file_size, 'commit_snapshot_status': item.commit_snapshot_status,
                 'started_at': item.started_at.isoformat() if item.started_at else None, 'completed_at': item.completed_at.isoformat() if item.completed_at else None}
    elif task_type == 'restore':
        item = RestoreTask.query.get(task_id)
        payload = None if not item else {'id': item.id, 'type': 'restore', 'status': item.status, 'error': (item.error_msg or '')[:2000],
                 'backup_id': item.backup_id, 'target_server_id': item.target_server_id, 'progress_stage': item.progress_stage,
                 'progress_percent': item.progress_percent, 'started_at': item.started_at.isoformat() if item.started_at else None,
                 'completed_at': item.completed_at.isoformat() if item.completed_at else None}
    elif task_type == 'mirror':
        item = MirrorConfig.query.get(task_id)
        payload = None if not item else {'id': item.id, 'type': 'mirror', 'status': item.status, 'last_sync_status': item.last_sync_status,
                 'source_server_id': item.source_server_id, 'target_server_id': item.target_server_id, 'progress_stage': item.progress_stage,
                 'progress_percent': item.progress_percent, 'failed_repos': item.failed_repos, 'last_sync_at': item.last_sync_at.isoformat() if item.last_sync_at else None}
    elif task_type == 'schedule':
        item = ScheduledTask.query.get(task_id)
        payload = None if not item else {'id': item.id, 'type': 'schedule', 'name': item.name, 'enabled': item.enabled,
                 'last_status': item.last_status, 'progress_stage': item.progress_stage, 'progress_percent': item.progress_percent,
                 'last_run_at': item.last_run_at.isoformat() if item.last_run_at else None}
    else:
        return jsonify({'error': '不支持的任务类型'}), 404
    if not payload:
        return jsonify({'error': '任务不存在'}), 404
    write_audit(request.integration_token.name, 'integration.task.read', f'{task_type}/{task_id}')
    return jsonify(sanitize(payload))
