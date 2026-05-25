import logging
from datetime import datetime
from models import db, Alert, GiteaServer


def create_alert(alert_type, server_id, message, source_id=0):
    server = GiteaServer.query.get(server_id)
    server_name = server.name if server else str(server_id)
    alert = Alert(
        alert_type=alert_type,
        server_id=server_id,
        server_name=server_name,
        message=message[:2000],
        status='active',
        source_id=source_id,
        created_at=datetime.utcnow(),
    )
    db.session.add(alert)
    db.session.commit()
    logging.info('[告警] 创建: type=%s server=%s msg=%s', alert_type, server_name, message[:100])


def auto_resolve_alerts(alert_type, server_id):
    count = Alert.query.filter_by(
        alert_type=alert_type,
        server_id=server_id,
        status='active',
    ).update({'status': 'resolved', 'resolved_at': datetime.utcnow()})
    if count > 0:
        db.session.commit()
        logging.info('[告警] 自动解决: type=%s server_id=%s count=%d', alert_type, server_id, count)


def on_backup_completed(server_id, success, error_msg='', backup_id=0):
    if success:
        auto_resolve_alerts('backup_failed', server_id)
    else:
        create_alert('backup_failed', server_id, error_msg, source_id=backup_id)


def on_restore_completed(target_server_id, success, error_msg='', task_id=0):
    if success:
        auto_resolve_alerts('restore_failed', target_server_id)
    else:
        create_alert('restore_failed', target_server_id, error_msg, source_id=task_id)
