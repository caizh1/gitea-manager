from flask import Flask, request, jsonify
from flask_cors import CORS
from config import SECRET_KEY, DATABASE_URL, INIT_PASSWORD
from models import db, Setting, User
from auth import init_auth
import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)


def create_app():
    app = Flask(__name__)
    app.secret_key = SECRET_KEY
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    CORS(app, supports_credentials=True)

    db.init_app(app)
    init_auth(app)

    with app.app_context():
        import sqlalchemy.exc
        try:
            db.create_all()
        except sqlalchemy.exc.OperationalError:
            pass

        for sql in [
            "ALTER TABLE gitea_servers ADD COLUMN is_local BOOLEAN DEFAULT 0",
            "ALTER TABLE gitea_servers ADD COLUMN disk_usage VARCHAR(100) DEFAULT ''",
            "ALTER TABLE backups ADD COLUMN source_api_token VARCHAR(200) DEFAULT ''",
            "ALTER TABLE schedule_logs ADD COLUMN backup_status VARCHAR(20) DEFAULT ''",
            "ALTER TABLE schedule_logs ADD COLUMN restore_results TEXT DEFAULT '[]'",
            "CREATE TABLE IF NOT EXISTS schedule_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_task_id INTEGER NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'running', log TEXT DEFAULT '', backup_status VARCHAR(20) DEFAULT '', restore_results TEXT DEFAULT '[]', started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at DATETIME)",
        ]:
            try:
                db.session.execute(db.text(sql))
                db.session.commit()
            except Exception:
                db.session.rollback()

        setting = Setting.query.get('admin_password')
        if not setting:
            User.set_password(INIT_PASSWORD)

    from routes.auth_routes import auth_bp
    from routes.server_routes import server_bp
    from routes.backup_routes import backup_bp
    from routes.restore_routes import restore_bp
    from routes.settings_routes import settings_bp
    from routes.schedule_routes import schedule_bp

    app.register_blueprint(auth_bp, url_prefix='/api')
    app.register_blueprint(server_bp, url_prefix='/api')
    app.register_blueprint(restore_bp, url_prefix='/api')
    app.register_blueprint(backup_bp, url_prefix='/api')
    app.register_blueprint(settings_bp, url_prefix='/api')
    app.register_blueprint(schedule_bp, url_prefix='/api')

    @app.before_request
    def check_host_ip():
        if not request.endpoint:
            return None
        read_only = {'.list_servers', '.get_server', '.list_backups', '.list_restore_tasks', '.list_schedules'}
        protected_prefixes = ('servers.', 'backups.', 'restore.', 'schedule.')
        action = request.endpoint.split('.')[-1] if '.' in request.endpoint else ''
        endpoint_val = request.endpoint

        if any(endpoint_val.startswith(p) for p in protected_prefixes) and action not in read_only:
            from models import get_setting
            host_ip = get_setting('host_ip', '')
            if not host_ip:
                return jsonify({'error': '请先在「系统设置」中配置本机IP'}), 400
        return None

    from services.scheduler_service import start_scheduler
    start_scheduler(app)

    return app
