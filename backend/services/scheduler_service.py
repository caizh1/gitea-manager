import threading
import time
import logging
from datetime import datetime


_scheduler_started = False


def start_scheduler(app):
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def loop():
        from models import db, ScheduledTask, Backup, RestoreTask, GiteaServer, ScheduleLog
        from services.gitea_service import do_backup, do_restore, test_server_connection
        from services.alert_service import on_backup_completed, on_restore_completed
        import json

        while True:
            try:
                with app.app_context():
                    now = datetime.utcnow()
                    tasks = ScheduledTask.query.filter_by(enabled=True).all()
                    for task in tasks:
                        if task.schedule_hour != now.hour or task.schedule_minute != now.minute:
                            continue
                        if task.last_run_at and (now - task.last_run_at).total_seconds() < 300:
                            continue

                        logging.info('[定时任务] 触发: %s', task.name)
                        task.last_status = 'running'
                        task.last_run_at = now
                        db.session.commit()

                        try:
                            target_ids = json.loads(task.target_ids or '[]')
                            import re
                            source = GiteaServer.query.get(task.source_server_id)
                            safe_name = re.sub(r'[^A-Za-z0-9]', '', source.name) if source else 'server'
                            filename = f'gitea-dump-sched-{safe_name}-{now.strftime("%Y%m%d-%H%M%S")}.zip'
                            backup = Backup(
                                source_server_id=task.source_server_id,
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
                                on_backup_completed(task.source_server_id, False, backup_err, backup_id=backup.id if backup else 0)
                                raise Exception('备份失败: ' + backup_err)

                            on_backup_completed(task.source_server_id, True, backup_id=backup.id)

                            restore_results = []
                            logs = [f'备份完成: {backup.filename} ({backup.file_size} bytes)']

                            for tid in target_ids:
                                rt = RestoreTask(
                                    backup_id=backup.id,
                                    target_server_id=tid,
                                    status='running',
                                    started_at=datetime.utcnow(),
                                )
                                db.session.add(rt)
                                db.session.commit()
                                do_restore(rt.id)
                                rt = RestoreTask.query.get(rt.id)
                                ts = GiteaServer.query.get(tid)
                                ts_name = ts.name if ts else str(tid)
                                info = {'target': ts_name, 'status': rt.status if rt else 'unknown'}
                                if rt and rt.status == 'success':
                                    logs.append(f'恢复成功: {ts_name}')
                                    on_restore_completed(tid, True, task_id=rt.id)
                                else:
                                    err = (rt.error_msg if rt else '')[:200]
                                    info['error'] = err
                                    logs.append(f'恢复失败: {ts_name} - {err}')
                                    on_restore_completed(tid, False, err, task_id=rt.id if rt else 0)
                                    if ts:
                                        ok, _ = test_server_connection(ts)
                                        ts.status = 'online' if ok else 'offline'
                                        db.session.commit()
                                restore_results.append(info)

                            all_ok = all(r['status'] == 'success' for r in restore_results)
                            task.last_status = 'success' if all_ok else 'failed'
                            task.last_log = '; '.join(logs)
                            log_entry = ScheduleLog(
                                schedule_task_id=task.id,
                                status='success' if all_ok else 'failed',
                                log=task.last_log,
                                backup_status='success',
                                restore_results=json.dumps(restore_results, ensure_ascii=False),
                                started_at=now,
                                completed_at=datetime.utcnow(),
                            )
                            db.session.add(log_entry)

                        except Exception as e:
                            task.last_status = 'failed'
                            task.last_log = str(e)[:500]
                            log_entry = ScheduleLog(
                                schedule_task_id=task.id,
                                status='failed',
                                log=task.last_log,
                                backup_status='failed',
                                restore_results=json.dumps([]),
                                started_at=now,
                                completed_at=datetime.utcnow(),
                            )
                            db.session.add(log_entry)
                            logging.error('[定时任务] %s 失败: %s', task.name, e)

                        db.session.commit()
            except Exception as e:
                logging.error('[定时任务] 调度循环异常: %s', e)

            time.sleep(60)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
