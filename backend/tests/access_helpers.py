"""业务测试使用真实持久随机会话，禁止伪造固定管理员 ID。"""
import secrets
from flask import g, session, has_app_context
from auth import init_auth
from models import db, Setting, User
from access.authentication import issue_session, initialize_access
from access.models import ManagerAccount
from services.database_startup import database_startup_lock


def init_test_auth(app):
    if 'sqlalchemy' not in app.extensions:
        app.config.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
        db.init_app(app)
    init_auth(app)
    with app.app_context(), database_startup_lock(app):
        db.create_all()
        if db.session.get(Setting, 'admin_password') is None:
            User.set_password(secrets.token_urlsafe(32))
        initialize_access()


def login_platform(client):
    app = client.application
    with app.test_request_context():
        account = ManagerAccount.query.filter_by(source_id=None, subject='local:fixture').one_or_none()
        if account is None:
            account = ManagerAccount(subject='local:fixture', username='fixture-admin', display_name='隔离验收管理员',
                is_platform_admin=True, enabled=True, password_hash='不使用密码跳过验证，仅由夹具建立测试会话')
            db.session.add(account)
            db.session.commit()
        issue_session(account)
        values = dict(session)
    with client.session_transaction() as target:
        target.clear()
        target.update(values)
    if has_app_context():
        g.pop('_login_user', None)
    return values['access_csrf']
