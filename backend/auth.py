"""Flask-Login 适配；会话身份必须由持久随机令牌验证。"""
from flask_login import LoginManager
from access.authentication import load_session_user

login_manager = LoginManager()


def init_auth(app):
    login_manager.init_app(app)
    login_manager.login_view = None

    @login_manager.unauthorized_handler
    def unauthorized():
        return {'error': '请先登录'}, 401


@login_manager.user_loader
def load_user(user_id):
    return load_session_user(user_id)
