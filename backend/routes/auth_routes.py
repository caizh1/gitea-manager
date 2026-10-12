"""个人登录、会话与退出接口。"""
from flask import Blueprint, request, jsonify
from flask_login import login_required
from access.errors import AccessError
from access.authentication import authenticate_login, logout_session, session_payload

auth_bp = Blueprint('auth', __name__)


@auth_bp.errorhandler(AccessError)
def handle_access_error(error):
    return jsonify(error=str(error)), error.status


@auth_bp.get('/session')
def check_session():
    return jsonify(session_payload())


@auth_bp.post('/login')
def login():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise AccessError('登录参数必须为对象')
    authenticate_login(data)
    return jsonify(session_payload() | {'ok': True})


@auth_bp.post('/logout')
@login_required
def logout():
    logout_session()
    return jsonify(session_payload() | {'ok': True})
