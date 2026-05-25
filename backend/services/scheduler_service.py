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
        from models import ScheduledTask
        from services.schedule_runner import claim_schedule_task, run_schedule_task

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
                        claimed, claimed_task, message = claim_schedule_task(task.id, now=now)
                        if not claimed:
                            logging.info('[定时任务] 跳过: %s - %s', task.name, message)
                            continue
                        run_schedule_task(claimed_task.id, started_at=claimed_task.last_run_at)
            except Exception as e:
                logging.error('[定时任务] 调度循环异常: %s', e)

            time.sleep(60)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
