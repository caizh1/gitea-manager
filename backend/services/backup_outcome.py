"""备份首错和源站收尾各有独立的持久化记录。"""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import logging

from models import BackupStepLog, ScheduleLog, db
from services.diagnostics import CURRENT, redact, register_secret
from sqlalchemy.exc import SQLAlchemyError


class RecoveryBlocked(RuntimeError):
    """核实结果不足以授权自动修改源端。"""


LEGACY_RECOVERY_ERRORS = (
    '备份执行进程已中断，正在核实原源并恢复运行状态',
    '中断备份已完成受控收尾',
)


def health_error(error):
    from services.commit_service import RestoreValidationError
    if isinstance(error, RestoreValidationError) and error.details:
        detail = error.details[0]
        if detail.get('path') in ('/api/v1/version', '/api/v1/user'):
            return detail
    return {}


def failure_detail(error):
    detail = health_error(error)
    if detail:
        path = detail['path']
        status = detail.get('status_code')
        label = '认证检查' if path.endswith('/user') else '版本检查'
        if status:
            return f'{label} {path} 返回 HTTP {status}'
        if detail.get('type') == 'api_json_error':
            return f'{label} {path} 返回内容不是有效 JSON'
    return redact(str(error) or type(error).__name__)[:2000]


def _is_failure_detail(detail):
    return bool(detail and not detail.startswith(LEGACY_RECOVERY_ERRORS))


def record_failure(task, error, *, stage=None, at=None):
    if task.failure_at is None:
        task.error_msg = failure_detail(error)
        task.failure_stage = stage or task.progress_stage or ''
        task.failure_at = at or datetime.utcnow()
        if task.failure_stage:
            step = BackupStepLog.query.filter_by(backup_id=task.id, step_key=task.failure_stage,
                                                 status='running').order_by(BackupStepLog.id.desc()).first()
            if step:
                step.status, step.detail = 'failed', task.error_msg
                step.completed_at = task.failure_at
                step.elapsed_ms = max(0, int((task.failure_at - step.started_at).total_seconds() * 1000))
        db.session.commit()
        report = CURRENT.get()
        if report and isinstance(error, Exception) and report.error is None:
            from services.backup_activity import STAGES
            report.step = STAGES.get(task.failure_stage, ('备份', 0))[0]
            report.fail(error)


def recover_first_failure(task):
    if task.failure_at or task.status == 'success':
        return
    from services.backup_activity import STAGES
    detail = task.error_msg if _is_failure_detail(task.error_msg) else ''
    stage = task.progress_stage or ''
    when = task.completed_at
    if re.fullmatch(r'[A-Za-z0-9_-]{1,100}', task.diagnostic_id or ''):
        root = Path(os.environ.get('MANAGEMENT_DIAGNOSTICS_ROOT', '/app/data/diagnostics'))
        path = root / task.diagnostic_id / 'first-error.json'
        try:
            diagnostic = json.loads(path.read_bytes())
            if _is_failure_detail(diagnostic.get('说明')):
                detail = diagnostic['说明']
                stage = next((key for key, (label, _) in STAGES.items() if label == diagnostic.get('步骤')), stage)
                when = datetime.utcfromtimestamp(path.stat().st_mtime)
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            pass
    if not detail:
        log = ScheduleLog.query.filter(ScheduleLog.backup_id == task.id, ScheduleLog.backup_error != '').order_by(
            ScheduleLog.id.desc()).first()
        if log and _is_failure_detail(log.backup_error):
            detail, when = log.backup_error, log.completed_at
    if detail == '固定导出元数据超过 16 MiB' or detail.startswith('固定导出元数据超过 16 MiB；'):
        stage = 'freeze_export'
    if stage in ('failed', 'recovery_source', ''):
        first_step = BackupStepLog.query.filter_by(backup_id=task.id, status='failed').order_by(BackupStepLog.id).first()
        stage = first_step.step_key if first_step else ''
    if not detail:
        detail = '旧版本未保留首次失败原因' if task.status == 'failed' else '备份执行进程已中断'
    record_failure(task, detail, stage=stage or 'backup', at=when)
    # 旧版本可能已将首错写入源站恢复步骤，只有同任务、同首错且与实际故障阶段不同才纠正。
    if stage:
        for step in BackupStepLog.query.filter_by(backup_id=task.id, status='failed'):
            if step.step_key == stage:
                step.detail = task.error_msg
            elif step.step_key == 'resume_source' and step.detail == task.error_msg:
                step.detail = '旧版本未保留本次收尾失败原因'
        db.session.commit()


class RecoveryProgress:
    def __init__(self, task):
        self.task = task
        self.info = dict(task.recovery_info or {})

    def update(self, **fields):
        self.info.update(fields, updated_at=datetime.utcnow().isoformat() + 'Z')
        self.task.recovery_info = dict(self.info)
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logging.warning('源站收尾进度暂时无法写入数据库')

    def action(self, action):
        self.update(action=action, state='running')

    def source(self, state, checked_at=None, restored_at=None):
        checked_at = checked_at or datetime.utcnow()
        if state == 'available' and self.task.source_stopped_at and not self.task.source_restored_at:
            self.task.source_restored_at = restored_at or checked_at
        self.update(source_state=state, source_checked_at=checked_at.isoformat() + 'Z')

    @contextmanager
    def run(self, action):
        register_secret(self.task.source_api_token)
        self.update(state='running', action=action, error='', next_retry_at=None)
        try:
            yield self
        except Exception as error:
            db.session.rollback()
            self.failed(error)
            raise
        else:
            self.update(state='completed', error='', health_status=None, next_retry_at=None, attempts=0)

    def failed(self, error):
        attempts = self.info.get('attempts', 0) + 1
        blocked = isinstance(error, RecoveryBlocked)
        delay = 300 if blocked else min(300, 10 * 2 ** min(attempts - 1, 5))
        now = datetime.utcnow()
        retry_at = now + timedelta(seconds=delay)
        action = self.info['action']
        detail = failure_detail(error)
        last = BackupStepLog.query.filter_by(backup_id=self.task.id, step_key='backup_recovery').order_by(
            BackupStepLog.id.desc()).first()
        if not last or (last.label, last.detail) != (action, detail):
            db.session.add(BackupStepLog(backup_id=self.task.id, step_key='backup_recovery', label=action,
                                         status='failed', detail=detail, started_at=now, completed_at=now))
        self.update(state='blocked' if blocked else 'waiting', attempts=attempts,
                    next_retry_at=retry_at.isoformat() + 'Z', error=detail,
                    health_status=health_error(error).get('status_code'))


def outcome_for_response(task):
    from services.backup_activity import STAGES
    recovery = dict(task.recovery_info or {})
    if task.recovery_pending and not recovery and task.status == 'failed':
        recovery = {'state': 'waiting', 'action': '等待核实持久化备份事务'}
    if task.recovery_pending and recovery.get('state') == 'running':
        from services.backup_recovery import lease_active
        if not lease_active(task.id, task.recovery_token):
            recovery['state'] = 'waiting'
    stage = task.failure_stage
    return {
        'error_msg': redact(task.error_msg or ''),
        'actions_warning': getattr(task, 'actions_warning', None),
        'failure_stage': stage,
        'failure_label': STAGES.get(stage, ('备份' if stage else '', 0))[0],
        'failure_at': task.failure_at.isoformat() + 'Z' if task.failure_at else None,
        'recovery_pending': task.recovery_pending,
        'recovery': recovery,
    }
