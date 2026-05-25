from flask import Blueprint, request, jsonify
from flask_login import login_required
from models import get_setting, set_setting

settings_bp = Blueprint('settings', __name__)


@settings_bp.route('/settings', methods=['GET'])
@login_required
def get_settings():
    return jsonify({
        'host_ip': get_setting('host_ip', ''),
    })


@settings_bp.route('/settings', methods=['POST'])
@login_required
def update_settings():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400
    if 'host_ip' in data:
        set_setting('host_ip', data['host_ip'].strip())
    return jsonify({'ok': True, 'host_ip': get_setting('host_ip', '')})
