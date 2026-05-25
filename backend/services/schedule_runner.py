import json
import logging
import re
import threading
from datetime import datetime, timedelta

from models import db, Backup, GiteaServer, RestoreTask, ScheduleLog, ScheduledTask
from services.gitea_service import do_backup, do_restore, test_server_connection
from services.schedule_progress import update_schedule_progress

COOLDOWN_SECONDS = 300


def _safe_server_name(server):
    return re.sub(r'[^A-Za-z0-9]', '', server.name) if server else 'server'


def claim_schedule_task(task_id, now=None):
    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=COOLDOWN_SECONDS)
    result = db.session.execute(
        db.text(
            """
            UPDATE scheduled_tasks
            SET last_status = 'running',
                last_run_at = :now,
                last_log = '',
                progress_stage = 'prepare',
                progress_label = '正在准备定时任务',
                progress_percent = 3,
                progress_detail = '',
                progress_updated_at = :now,
                current_backup_id = NULL,
                current_restore_task_id = NULL,
                current_restore_index = 0,
                current_restore_total = 0
            WHERE id = :task_id
              AND (last_status IS NULL OR last_status != 'running')
              AND (last_run_at IS NULL OR last_run_at <= :cutoff)
            """
        ),
        {'task_id': task_id, 'now': now, 'cutoff': cutoff},
    )
    db.session.commit()
    task = ScheduledTask.query.get(task_id)
    if result.rowcount == 1:
        return True, task, ''

    if not task:
        return False, None, '任务不存在'
    if task.last_status == 'running':
        return False, task, '任务正在运行，请稍后再试'
    if task.last_run_at and (now - task.last_run_at).total_seconds() < COOLDOWN_SECONDS:
        remaining = int(COOLDOWN_SECONDS - (now - task.last_run_at).total_seconds())
        return False, task, f'5 分钟内已执行过，请等待 {remaining} 秒'
    return False, task, '任务暂时无法执行，请刷新后重试'


def start_schedule_task_thread(task_id, started_at=None):
    def target():
        from app import create_app
        app = create_app()
        with app.app_context():
            run_schedule_task(task_id, started_at=started_at)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def run_schedule_task(task_id, started_at=None):
    task = ScheduledTask.query.get(task_id)
    if not task:
        return

    run_started_at = started_at or task.last_run_at or datetime.utcnow()
    restore_results = []
    logs = []
    backup_status = 'failed'

    try:
        target_ids = json.loads(task.target_ids or '[]')
    except Exception:
        target_ids = []

    update_schedule_progress(
        task,
        'prepare',
        '正在准备定时任务',
        3,
        task.name,
        reset_current=True,
        current_restore_total=len(target_ids),
    )

    try:
        source = GiteaServer.query.get(task.source_server_id)
        if not source:
            raise Exception('源服务器不存在')

        filename = f'gitea-dump-sched-{_safe_server_name(source)}-{run_started_at.strftime("%Y%m%d-%H%M%S-%f")}.zip'
        backup = Backup(
            source_server_id=task.source_server_id,
            filename=filename,
            file_path='',
            status='running',
            source_api_token=source.api_token,
            source_server_name=source.name,
            started_at=run_started_at,
        )
        db.session.add(backup)
        db.session.commit()

        update_schedule_progress(
            task.id,
            'backup',
            '正在执行备份',
            10,
            source.name,
            current_backup_id=backup.id,
            current_restore_total=len(target_ids),
        )
        do_backup(backup.id)

        backup = Backup.query.get(backup.id)
        if not backup or backup.status != 'success':
            backup_err = backup.error_msg if backup else 'unknown'
            from services.alert_service import on_backup_completed
            on_backup_completed(
                task.source_server_id,
                False,
                backup_err,
                backup_id=backup.id if backup else 0,
            )
            raise Exception('备份失败: ' + backup_err)

        backup_status = 'success'
        from services.alert_service import on_backup_completed, on_restore_completed
        on_backup_completed(task.source_server_id, True, backup_id=backup.id)
        logs.append(f'备份完成: {backup.filename} ({backup.file_size} bytes)')
        update_schedule_progress(task.id, 'backup_done', '备份完成', 30, backup.filename)

        total = len(target_ids)
        for index, target_id in enumerate(target_ids, start=1):
            target = GiteaServer.query.get(target_id)
            target_name = target.name if target else str(target_id)
            restore_task = RestoreTask(
                backup_id=backup.id,
                target_server_id=target_id,
                target_server_name=target_name if target else '',
                status='running',
                started_at=datetime.utcnow(),
            )
            db.session.add(restore_task)
            db.session.commit()

            update_schedule_progress(
                task.id,
                'restore',
                f'正在恢复 {index}/{total}',
                int(30 + 65 * (index - 1) / max(total, 1)),
                target_name,
                current_restore_task_id=restore_task.id,
                current_restore_index=index,
                current_restore_total=total,
            )
            do_restore(restore_task.id)

            restore_task = RestoreTask.query.get(restore_task.id)
            target = GiteaServer.query.get(target_id)
            target_name = target.name if target else str(target_id)
            info = {
                'target': target_name,
                'status': restore_task.status if restore_task else 'unknown',
                'restore_task_id': restore_task.id if restore_task else None,
                'started_at': restore_task.started_at.isoformat() if restore_task and restore_task.started_at else None,
                'completed_at': restore_task.completed_at.isoformat() if restore_task and restore_task.completed_at else None,
            }
            if restore_task and restore_task.status == 'success':
                logs.append(f'恢复成功: {target_name}')
                on_restore_completed(target_id, True, task_id=restore_task.id)
            else:
                error = (restore_task.error_msg if restore_task else '')[:200]
                info['error'] = error
                logs.append(f'恢复失败: {target_name} - {error}')
                on_restore_completed(target_id, False, error, task_id=restore_task.id if restore_task else 0)
                if target:
                    ok, _ = test_server_connection(target)
                    target.status = 'online' if ok else 'offline'
                    db.session.commit()
            restore_results.append(info)

            update_schedule_progress(
                task.id,
                'restore_done',
                f'已完成恢复 {index}/{total}',
                int(30 + 65 * index / max(total, 1)),
                target_name,
                current_restore_task_id=restore_task.id if restore_task else None,
                current_restore_index=index,
                current_restore_total=total,
            )

        all_ok = all(r['status'] == 'success' for r in restore_results)
        task = ScheduledTask.query.get(task_id)
        task.last_status = 'success' if all_ok else 'failed'
        task.last_log = '; '.join(logs)
        db.session.add(ScheduleLog(
            schedule_task_id=task_id,
            status='success' if all_ok else 'failed',
            log=task.last_log,
            backup_status=backup_status,
            backup_id=backup.id,
            backup_filename=backup.filename,
            backup_error=backup.error_msg or '',
            restore_results=json.dumps(restore_results, ensure_ascii=False),
            started_at=run_started_at,
            completed_at=datetime.utcnow(),
        ))
        db.session.commit()
        update_schedule_progress(
            task.id,
            'completed' if all_ok else 'failed',
            '定时任务执行完成' if all_ok else '定时任务执行失败',
            100,
            task.last_log,
            current_restore_task_id=None,
        )
    except Exception as e:
        db.session.rollback()
        logging.error('[定时任务] %s 失败: %s', task.name, e)
        task = ScheduledTask.query.get(task_id)
        if not task:
            return
        task.last_status = 'failed'
        task.last_log = str(e)[:500]
        backup = Backup.query.get(task.current_backup_id) if task.current_backup_id else None
        db.session.add(ScheduleLog(
            schedule_task_id=task_id,
            status='failed',
            log=task.last_log,
            backup_status=backup_status,
            backup_id=backup.id if backup else None,
            backup_filename=backup.filename if backup else '',
            backup_error=backup.error_msg if backup else task.last_log,
            restore_results=json.dumps(restore_results, ensure_ascii=False),
            started_at=run_started_at,
            completed_at=datetime.utcnow(),
        ))
        db.session.commit()
        update_schedule_progress(
            task.id,
            'failed',
            '定时任务执行失败',
            100,
            task.last_log,
            current_restore_task_id=None,
        )
