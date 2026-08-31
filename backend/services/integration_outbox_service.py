import hashlib
import json
import re
import secrets
from datetime import datetime

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from models import (Backup, RestoreTask, db, IntegrationAuditLog, IntegrationOutbox,
                    IntegrationServiceToken)


_SECRET_KEY = re.compile(r'(token|password|passwd|secret|api[_-]?key|private[_-]?key|credential)', re.I)
_BEARER = re.compile(r'(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{8,}')
_PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S)
_HIGH_ENTROPY = re.compile(r'(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{40,}(?![A-Za-z0-9+/=_-])')


def token_hash(token):
    return hashlib.sha256(str(token).encode('utf-8')).hexdigest()


def sanitize(value, depth=0):
    if depth > 8:
        return '[内容层级过深，已截断]'
    if isinstance(value, dict):
        return {str(key): '[已脱敏]' if _SECRET_KEY.search(str(key)) else sanitize(item, depth + 1)
                for key, item in list(value.items())[:100]}
    if isinstance(value, list):
        return [sanitize(item, depth + 1) for item in value[:200]]
    if isinstance(value, str):
        text = value.replace('\x1b', '').replace('<', '‹').replace('>', '›')
        text = _PRIVATE_KEY.sub('[已脱敏私钥]', text)
        text = _BEARER.sub(lambda item: item.group(1) + ' [已脱敏]', text)
        return _HIGH_ENTROPY.sub('[疑似凭据，已脱敏]', text)[:2000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:2000]


def write_audit(actor, action, target='', status='success', detail=''):
    db.session.add(IntegrationAuditLog(
        actor=str(actor)[:100], action=str(action)[:100], target=str(target)[:200],
        status=str(status)[:20], detail=str(sanitize(detail))[:2000], created_at=datetime.utcnow(),
    ))


def emit_event(event_type, task_type, task_id, status, payload=None):
    event = IntegrationOutbox(
        event_type=str(event_type)[:100], task_type=str(task_type)[:30], task_id=int(task_id or 0),
        status=str(status)[:30], payload_json=json.dumps(sanitize(payload or {}), ensure_ascii=False),
        created_at=datetime.utcnow(),
    )
    db.session.add(event)
    return event


@event.listens_for(Session, 'before_flush')
def _capture_terminal_task_state(session, flush_context, instances):
    """将备份/恢复状态变化与业务状态放进同一个数据库事务。"""
    emitted = session.info.setdefault('integration_outbox_emitted', set())
    for item in list(session.new) + list(session.dirty):
        if not isinstance(item, (Backup, RestoreTask)):
            continue
        state = inspect(item)
        if item not in session.new and not state.attrs.status.history.has_changes():
            continue
        task_type = 'backup' if isinstance(item, Backup) else 'restore'
        status = item.status or 'running'
        if status not in {'running', 'success', 'failed'} or not item.id:
            continue
        event_name = f'{task_type}.started' if status == 'running' else f'{task_type}.completed' if status == 'success' else f'{task_type}.failed'
        key = (event_name, item.id, status)
        if key in emitted:
            continue
        emitted.add(key)
        payload = {'error': item.error_msg if status == 'failed' else ''}
        if isinstance(item, Backup):
            payload.update({'server_id': item.source_server_id, 'file_size': item.file_size,
                            'commit_snapshot_status': item.commit_snapshot_status})
        else:
            payload.update({'target_server_id': item.target_server_id, 'backup_id': item.backup_id})
        session.add(IntegrationOutbox(
            event_type=event_name, task_type=task_type, task_id=item.id, status=status,
            payload_json=json.dumps(sanitize(payload), ensure_ascii=False), created_at=datetime.utcnow(),
        ))


@event.listens_for(Session, 'after_transaction_end')
def _clear_transaction_dedup(session, transaction):
    if not transaction.nested:
        session.info.pop('integration_outbox_emitted', None)


def synchronize_bootstrap_token(plain_token):
    if not plain_token:
        return False
    digest = token_hash(plain_token)
    current = IntegrationServiceToken.query.filter_by(token_hash=digest, active=True).first()
    if current:
        return False
    for item in IntegrationServiceToken.query.filter_by(active=True).all():
        item.active = False
        item.rotated_at = datetime.utcnow()
        write_audit('system', 'integration.token.rotate', 'sentinel')
    db.session.add(IntegrationServiceToken(name=f'sentinel-{secrets.token_hex(4)}', token_hash=digest, active=True))
    write_audit('system', 'integration.token.create', 'sentinel')
    return True
