import json
from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_login import login_required
from models import Backup, db, GiteaServer, ScheduledTask, ScheduleLog
from services.schedule_progress import schedule_progress_for_response
from services.schedule_runner import claim_schedule_task, start_schedule_task_thread

schedule_bp = Blueprint('schedule', __name__)


def validate_transfer_settings(data, task=None):
    source = data.get('source_server_id', task.source_server_id if task else None)
    targets = data.get('target_ids', json.loads(task.target_ids) if task else [])
    target = data.get('fast_restore_target_id', task.fast_restore_target_id if task else None)
    reuse = data.get('reuse_source_chunks', task.reuse_source_chunks if task else False)
    if type(reuse) is not bool:
        raise ValueError('跨夜内容复用设置无效')
    if (task and 'fast_restore_target_id' not in data and isinstance(targets, list)
            and (target not in targets or target == source)):
        target = None
    if target is not None:
        if type(target) is not int or not isinstance(targets, list) or target not in targets or target == source:
            raise ValueError('快速恢复备机必须是该任务已选且与源站不同的目标')
        server = db.session.get(GiteaServer, target)
        if server is None or server.role != 'backup':
            raise ValueError('请选择已登记的备机')
    return target, reuse


def task_to_dict(t):
    data = {
        'id': t.id,
        'name': t.name,
        'enabled': t.enabled,
        'source_server_id': t.source_server_id,
        'source_server_name': t.source_server.name if t.source_server else '',
        'target_ids': json.loads(t.target_ids or '[]'),
        'fast_restore_target_id': t.fast_restore_target_id,
        'reuse_source_chunks': t.reuse_source_chunks,
        'schedule_hour': t.schedule_hour,
        'schedule_minute': t.schedule_minute,
        'last_run_at': t.last_run_at.isoformat() if t.last_run_at else None,
        'last_status': t.last_status,
        'last_log': t.last_log,
        'created_at': t.created_at.isoformat() if t.created_at else None,
    }
    data.update(schedule_progress_for_response(t))
    return data


@schedule_bp.route('/schedules', methods=['GET'])
@login_required
def list_schedules():
    tasks = ScheduledTask.query.order_by(ScheduledTask.created_at.desc()).all()
    return jsonify([task_to_dict(t) for t in tasks])


@schedule_bp.route('/schedules', methods=['POST'])
@login_required
def create_schedule():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    try:
        fast_target, reuse = validate_transfer_settings(data)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    task = ScheduledTask(
        name=data.get('name', ''),
        enabled=data.get('enabled', True),
        source_server_id=data.get('source_server_id', 0),
        target_ids=json.dumps(data.get('target_ids', [])),
        fast_restore_target_id=fast_target,
        reuse_source_chunks=reuse,
        schedule_hour=data.get('schedule_hour', 2),
        schedule_minute=data.get('schedule_minute', 0),
        created_at=datetime.utcnow(),
    )
    db.session.add(task)
    db.session.commit()
    return jsonify(task_to_dict(task)), 201


@schedule_bp.route('/schedules/<int:tid>', methods=['PUT'])
@login_required
def update_schedule(tid):
    t = ScheduledTask.query.get_or_404(tid)
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    try:
        t.fast_restore_target_id, t.reuse_source_chunks = validate_transfer_settings(data, t)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    fields = ['name', 'enabled', 'source_server_id', 'schedule_hour', 'schedule_minute']
    for f in fields:
        if f in data:
            setattr(t, f, data[f])
    if 'target_ids' in data:
        t.target_ids = json.dumps(data['target_ids'])
    db.session.commit()
    return jsonify(task_to_dict(t))


@schedule_bp.route('/schedules/<int:tid>', methods=['DELETE'])
@login_required
def delete_schedule(tid):
    t = ScheduledTask.query.get_or_404(tid)
    db.session.delete(t)
    db.session.commit()
    return jsonify({'ok': True})


@schedule_bp.route('/schedules/<int:tid>/run', methods=['POST'])
@login_required
def run_schedule(tid):
    ScheduledTask.query.get_or_404(tid)
    ok, task, message = claim_schedule_task(tid)
    if not ok:
        return jsonify({'error': message, 'cooldown': '等待' in message}), 400
    start_schedule_task_thread(tid, started_at=task.last_run_at)
    return jsonify(task_to_dict(task))


def _schedule_log_steps(log_entry, restore_results, backup_outcome=None):
    steps = []
    first_log_part = (log_entry.log or '').split(';')[0].strip()
    backup_status = log_entry.backup_status or log_entry.status
    backup_detail = ''
    if backup_outcome and backup_outcome['error_msg']:
        backup_detail = backup_outcome['error_msg']
    elif log_entry.backup_error:
        backup_detail = '备份失败: ' + log_entry.backup_error
    elif log_entry.backup_filename:
        backup_detail = '备份完成: ' + log_entry.backup_filename
    elif first_log_part:
        backup_detail = first_log_part
    else:
        backup_detail = '-'

    steps.append({
        'stage': '备份',
        'target': '',
        'status': backup_status,
        'detail': backup_detail,
        'backup_id': log_entry.backup_id,
        'backup_outcome': backup_outcome,
        'started_at': log_entry.started_at.isoformat() if log_entry.started_at else None,
        'completed_at': log_entry.completed_at.isoformat() if log_entry.completed_at and not restore_results else None,
    })

    for result in restore_results:
        status = result.get('status') or 'unknown'
        target = result.get('target') or ''
        error = result.get('error') or ''
        detail = error if status == 'failed' else '成功'
        steps.append({
            'stage': '恢复',
            'target': target,
            'status': status,
            'detail': detail,
            'restore_task_id': result.get('restore_task_id'),
            'started_at': result.get('started_at'),
            'completed_at': result.get('completed_at'),
        })

    return steps


def _load_restore_results(raw):
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


@schedule_bp.route('/schedules/<int:tid>/logs', methods=['GET'])
@login_required
def get_schedule_logs(tid):
    logs = ScheduleLog.query.filter_by(schedule_task_id=tid)\
        .order_by(ScheduleLog.started_at.desc()).limit(20).all()
    result = []
    from services.backup_outcome import outcome_for_response
    backup_ids = {entry.backup_id for entry in logs if entry.backup_id}
    backups = {backup.id: outcome_for_response(backup)
               for backup in Backup.query.filter(Backup.id.in_(backup_ids)).all()} if backup_ids else {}
    for log_entry in logs:
        restore_results = _load_restore_results(log_entry.restore_results)
        result.append({
            'id': log_entry.id,
            'status': log_entry.status,
            'log': log_entry.log,
            'backup_status': log_entry.backup_status,
            'backup_id': log_entry.backup_id,
            'backup_filename': log_entry.backup_filename,
            'backup_error': log_entry.backup_error,
            'restore_results': restore_results,
            'steps': _schedule_log_steps(log_entry, restore_results, backups.get(log_entry.backup_id)),
            'started_at': log_entry.started_at.isoformat() if log_entry.started_at else None,
            'completed_at': log_entry.completed_at.isoformat() if log_entry.completed_at else None,
        })
    return jsonify(result)
