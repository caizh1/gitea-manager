from flask import Blueprint, request, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from auth import Auth

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/session', methods=['GET'])
def check_session():
    if current_user.is_authenticated:
        return jsonify({'authenticated': True})
    return jsonify({'authenticated': False})


@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    password = data.get('password', '') if data else ''
    user = Auth.login(password)
    if user:
        login_user(user)
        return jsonify({'ok': True})
    return jsonify({'error': 'Wrong password'}), 401


@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({'ok': True})
