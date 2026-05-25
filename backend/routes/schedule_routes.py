import json
from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_login import login_required
from models import db, ScheduledTask, GiteaServer, Backup, RestoreTask, ScheduleLog

schedule_bp = Blueprint('schedule', __name__)


def task_to_dict(t):
    return {
        'id': t.id,
        'name': t.name,
        'enabled': t.enabled,
        'source_server_id': t.source_server_id,
        'source_server_name': t.source_server.name if t.source_server else '',
        'target_ids': json.loads(t.target_ids or '[]'),
        'schedule_hour': t.schedule_hour,
        'schedule_minute': t.schedule_minute,
        'last_run_at': t.last_run_at.isoformat() if t.last_run_at else None,
        'last_status': t.last_status,
        'last_log': t.last_log,
        'created_at': t.created_at.isoformat() if t.created_at else None,
    }


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

    task = ScheduledTask(
        name=data.get('name', ''),
        enabled=data.get('enabled', True),
        source_server_id=data.get('source_server_id', 0),
        target_ids=json.dumps(data.get('target_ids', [])),
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
    t = ScheduledTask.query.get_or_404(tid)
    if t.last_run_at:
        seconds = (datetime.utcnow() - t.last_run_at).total_seconds()
        if seconds < 300:
            remaining = int(300 - seconds)
            return jsonify({'error': f'5 分钟内已执行过，请等待 {remaining} 秒', 'cooldown': True}), 400
    t.last_status = 'running'
    t.last_run_at = datetime.utcnow()
    db.session.commit()
    import threading
    from services.gitea_service import do_backup, do_restore, test_server_connection

    def run():
        from app import create_app
        from services.alert_service import on_backup_completed, on_restore_completed
        app = create_app()
        with app.app_context():
            t2 = ScheduledTask.query.get(tid)
            t2.last_status = 'running'
            t2.last_run_at = datetime.utcnow()
            db.session.commit()

            try:
                target_ids = json.loads(t2.target_ids or '[]')
                now = datetime.utcnow()
                import re
                source = GiteaServer.query.get(t2.source_server_id)
                safe_name = re.sub(r'[^A-Za-z0-9]', '', source.name) if source else 'server'
                filename = f'gitea-dump-sched-{safe_name}-{now.strftime("%Y%m%d-%H%M%S")}.zip'
                backup = Backup(
                    source_server_id=t2.source_server_id,
                    filename=filename,
                    file_path='',
                    status='running',
                    source_api_token=source.api_token if source else '',
                    started_at=now,
                )
                db.session.add(backup)
                db.session.commit()
                do_backup(backup.id)

                backup = Backup.query.get(backup.id)
                if not backup or backup.status != 'success':
                    backup_err = backup.error_msg if backup else 'unknown'
                    on_backup_completed(t2.source_server_id, False, backup_err, backup_id=backup.id if backup else 0)
                    raise Exception('备份失败: ' + backup_err)

                on_backup_completed(t2.source_server_id, True, backup_id=backup.id)

                logs = [f'备份完成: {backup.filename}']
                restore_results = []

                for tid2 in target_ids:
                    rt = RestoreTask(
                        backup_id=backup.id,
                        target_server_id=tid2,
                        status='running',
                        started_at=datetime.utcnow(),
                    )
                    db.session.add(rt)
                    db.session.commit()
                    do_restore(rt.id)
                    rt = RestoreTask.query.get(rt.id)
                    ts = GiteaServer.query.get(tid2)
                    ts_name = ts.name if ts else str(tid2)
                    info = {'target': ts_name, 'status': rt.status if rt else 'unknown'}
                    if rt and rt.status == 'success':
                        logs.append(f'恢复成功: {ts_name}')
                        on_restore_completed(tid2, True, task_id=rt.id)
                    else:
                        info['error'] = (rt.error_msg if rt else '')[:200]
                        logs.append(f'恢复失败: {ts_name} - {info["error"]}')
                        on_restore_completed(tid2, False, info['error'], task_id=rt.id if rt else 0)
                        if ts:
                            ok, _ = test_server_connection(ts)
                            ts.status = 'online' if ok else 'offline'
                            db.session.commit()
                    restore_results.append(info)

                all_ok = all(r['status'] == 'success' for r in restore_results)
                t2.last_status = 'success' if all_ok else 'failed'
                t2.last_log = '; '.join(logs)
                db.session.add(ScheduleLog(
                    schedule_task_id=tid,
                    status='success' if all_ok else 'failed',
                    log=t2.last_log,
                    backup_status='success',
                    restore_results=json.dumps(restore_results, ensure_ascii=False),
                    started_at=now,
                    completed_at=datetime.utcnow(),
                ))
            except Exception as e:
                t2.last_status = 'failed'
                t2.last_log = str(e)[:500]
                db.session.add(ScheduleLog(
                    schedule_task_id=tid,
                    status='failed',
                    log=t2.last_log,
                    backup_status='failed',
                    restore_results='[]',
                    started_at=now,
                    completed_at=datetime.utcnow(),
                ))
            db.session.commit()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return jsonify({'ok': True})


@schedule_bp.route('/schedules/<int:tid>/logs', methods=['GET'])
@login_required
def get_schedule_logs(tid):
    logs = ScheduleLog.query.filter_by(schedule_task_id=tid)\
        .order_by(ScheduleLog.started_at.desc()).limit(20).all()
    return jsonify([{
        'id': l.id,
        'status': l.status,
        'log': l.log,
        'backup_status': l.backup_status,
        'restore_results': json.loads(l.restore_results) if l.restore_results else [],
        'started_at': l.started_at.isoformat() if l.started_at else None,
        'completed_at': l.completed_at.isoformat() if l.completed_at else None,
    } for l in logs])
