from flask_login import LoginManager
from models import User

login_manager = LoginManager()


def init_auth(app):
    login_manager.init_app(app)
    login_manager.login_view = None

    @login_manager.unauthorized_handler
    def unauthorized():
        return {'error': 'Unauthorized'}, 401


@login_manager.user_loader
def load_user(user_id):
    return User()


class Auth:
    @staticmethod
    def login(password):
        user = User()
        if user.check_password(password):
            return user
        return None
