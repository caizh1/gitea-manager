from flask import Blueprint, jsonify, request
from flask_login import login_required
from models import db, Alert

alert_bp = Blueprint('alert', __name__)


def alert_to_dict(a):
    return {
        'id': a.id,
        'alert_type': a.alert_type,
        'server_id': a.server_id,
        'server_name': a.server_name,
        'message': a.message,
        'status': a.status,
        'source_id': a.source_id,
        'resolved_at': a.resolved_at.isoformat() if a.resolved_at else None,
        'created_at': a.created_at.isoformat() if a.created_at else None,
    }


@alert_bp.route('/alerts', methods=['GET'])
@login_required
def list_alerts():
    alerts = Alert.query.filter(Alert.status != 'cleared')\
        .order_by(Alert.created_at.desc()).limit(200).all()
    return jsonify([alert_to_dict(a) for a in alerts])


@alert_bp.route('/alerts/summary', methods=['GET'])
@login_required
def alert_summary():
    active_count = Alert.query.filter_by(status='active').count()
    latest = Alert.query.filter(Alert.status != 'cleared')\
        .order_by(Alert.created_at.desc()).first()
    return jsonify({
        'active_count': active_count,
        'latest_alert': alert_to_dict(latest) if latest else None,
    })


@alert_bp.route('/alerts/<int:aid>/clear', methods=['POST'])
@login_required
def clear_alert(aid):
    alert = Alert.query.get_or_404(aid)
    alert.status = 'cleared'
    db.session.commit()
    return jsonify({'ok': True})


@alert_bp.route('/alerts/clear-resolved', methods=['POST'])
@login_required
def clear_resolved():
    Alert.query.filter_by(status='resolved').update({'status': 'cleared'})
    db.session.commit()
    return jsonify({'ok': True})
