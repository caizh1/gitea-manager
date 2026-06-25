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
            "ALTER TABLE backups ADD COLUMN commit_snapshot_status VARCHAR(20) DEFAULT ''",
            "ALTER TABLE backups ADD COLUMN commit_snapshot_repo_count INTEGER DEFAULT 0",
            "ALTER TABLE backups ADD COLUMN commit_snapshot_error TEXT DEFAULT ''",
            "ALTER TABLE backups ADD COLUMN commit_snapshot_collected_at DATETIME",
            "ALTER TABLE schedule_logs ADD COLUMN backup_status VARCHAR(20) DEFAULT ''",
            "ALTER TABLE schedule_logs ADD COLUMN backup_id INTEGER",
            "ALTER TABLE schedule_logs ADD COLUMN backup_filename VARCHAR(500) DEFAULT ''",
            "ALTER TABLE schedule_logs ADD COLUMN backup_error TEXT DEFAULT ''",
            "ALTER TABLE schedule_logs ADD COLUMN restore_results TEXT DEFAULT '[]'",
            "CREATE TABLE IF NOT EXISTS schedule_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_task_id INTEGER NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'running', log TEXT DEFAULT '', backup_status VARCHAR(20) DEFAULT '', backup_id INTEGER, backup_filename VARCHAR(500) DEFAULT '', backup_error TEXT DEFAULT '', restore_results TEXT DEFAULT '[]', started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at DATETIME)",
            "CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, alert_type VARCHAR(30) NOT NULL, server_id INTEGER NOT NULL REFERENCES gitea_servers(id), server_name VARCHAR(100) NOT NULL, message TEXT DEFAULT '', status VARCHAR(20) NOT NULL DEFAULT 'active', source_id INTEGER DEFAULT 0, resolved_at DATETIME, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS restore_verifications (id INTEGER PRIMARY KEY AUTOINCREMENT, restore_task_id INTEGER NOT NULL REFERENCES restore_tasks(id), status VARCHAR(20) NOT NULL DEFAULT 'pending', total_repos INTEGER DEFAULT 0, matched_repos INTEGER DEFAULT 0, mismatch_repos INTEGER DEFAULT 0, mismatch_details TEXT DEFAULT '[]', verified_at DATETIME, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS backup_repo_commits (id INTEGER PRIMARY KEY AUTOINCREMENT, backup_id INTEGER NOT NULL REFERENCES backups(id), repo_name VARCHAR(300) NOT NULL, commit_count INTEGER DEFAULT 0, latest_commit_sha VARCHAR(40) DEFAULT '', commit_ids_hash VARCHAR(64) DEFAULT '', commit_ids TEXT DEFAULT '', collected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS mirror_configs (id INTEGER PRIMARY KEY AUTOINCREMENT, source_server_id INTEGER NOT NULL REFERENCES gitea_servers(id), target_server_id INTEGER NOT NULL REFERENCES gitea_servers(id), sync_mode VARCHAR(20) NOT NULL DEFAULT 'push_mirror', sync_interval INTEGER DEFAULT 30, sync_on_commit BOOLEAN DEFAULT 1, enabled BOOLEAN DEFAULT 1, status VARCHAR(20) DEFAULT 'pending', last_sync_at DATETIME, last_sync_status VARCHAR(20) DEFAULT '', last_sync_log TEXT DEFAULT '', total_repos INTEGER DEFAULT 0, synced_repos INTEGER DEFAULT 0, failed_repos INTEGER DEFAULT 0, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS mirror_repo_status (id INTEGER PRIMARY KEY AUTOINCREMENT, mirror_config_id INTEGER NOT NULL REFERENCES mirror_configs(id), repo_name VARCHAR(300) NOT NULL, source_repo_id INTEGER DEFAULT 0, target_repo_id INTEGER DEFAULT 0, remote_name VARCHAR(100) DEFAULT '', status VARCHAR(20) DEFAULT 'pending', sync_mode VARCHAR(20) DEFAULT '', last_sync_at DATETIME, error_msg TEXT DEFAULT '', created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
            "ALTER TABLE mirror_configs ADD COLUMN sync_on_commit BOOLEAN DEFAULT 1",
            "ALTER TABLE mirror_repo_status ADD COLUMN remote_name VARCHAR(100) DEFAULT ''",
            "CREATE TABLE IF NOT EXISTS repo_statistics (id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER NOT NULL REFERENCES gitea_servers(id), repo_name VARCHAR(300) NOT NULL, commit_count INTEGER DEFAULT 0, code_lines INTEGER DEFAULT 0, doc_lines INTEGER DEFAULT 0, other_lines INTEGER DEFAULT 0, code_files INTEGER DEFAULT 0, doc_files INTEGER DEFAULT 0, other_files INTEGER DEFAULT 0, language_breakdown TEXT DEFAULT '{}', last_commit_sha VARCHAR(40) DEFAULT '', last_commit_date DATETIME, snapshot_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS commit_statistics (id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER NOT NULL REFERENCES gitea_servers(id), period_type VARCHAR(20) NOT NULL, period_key VARCHAR(20) NOT NULL, commit_count INTEGER DEFAULT 0, repo_count INTEGER DEFAULT 0, author_count INTEGER DEFAULT 0, top_authors TEXT DEFAULT '[]', code_lines_added INTEGER DEFAULT 0, code_lines_deleted INTEGER DEFAULT 0, doc_lines_added INTEGER DEFAULT 0, doc_lines_deleted INTEGER DEFAULT 0, snapshot_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
            "ALTER TABLE backups ADD COLUMN source_server_name VARCHAR(100) DEFAULT ''",
            "ALTER TABLE restore_tasks ADD COLUMN target_server_name VARCHAR(100) DEFAULT ''",
            "ALTER TABLE restore_tasks ADD COLUMN progress_stage VARCHAR(50) DEFAULT ''",
            "ALTER TABLE restore_tasks ADD COLUMN progress_label VARCHAR(100) DEFAULT ''",
            "ALTER TABLE restore_tasks ADD COLUMN progress_percent INTEGER DEFAULT 0",
            "ALTER TABLE restore_tasks ADD COLUMN progress_detail TEXT DEFAULT ''",
            "ALTER TABLE restore_tasks ADD COLUMN progress_updated_at DATETIME",
            "ALTER TABLE scheduled_tasks ADD COLUMN progress_stage VARCHAR(50) DEFAULT ''",
            "ALTER TABLE scheduled_tasks ADD COLUMN progress_label VARCHAR(100) DEFAULT ''",
            "ALTER TABLE scheduled_tasks ADD COLUMN progress_percent INTEGER DEFAULT 0",
            "ALTER TABLE scheduled_tasks ADD COLUMN progress_detail TEXT DEFAULT ''",
            "ALTER TABLE scheduled_tasks ADD COLUMN progress_updated_at DATETIME",
            "ALTER TABLE scheduled_tasks ADD COLUMN current_backup_id INTEGER",
            "ALTER TABLE scheduled_tasks ADD COLUMN current_restore_task_id INTEGER",
            "ALTER TABLE scheduled_tasks ADD COLUMN current_restore_index INTEGER DEFAULT 0",
            "ALTER TABLE scheduled_tasks ADD COLUMN current_restore_total INTEGER DEFAULT 0",
            "CREATE TABLE IF NOT EXISTS author_statistics (id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER NOT NULL REFERENCES gitea_servers(id), author_name VARCHAR(200) NOT NULL, author_email VARCHAR(300) DEFAULT '', repo_name VARCHAR(300) NOT NULL, commit_count INTEGER DEFAULT 0, additions INTEGER DEFAULT 0, deletions INTEGER DEFAULT 0, first_commit_date DATETIME, last_commit_date DATETIME, snapshot_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, CONSTRAINT uq_author_repo UNIQUE (server_id, author_name, repo_name))",
            "CREATE TABLE IF NOT EXISTS commit_message_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER NOT NULL REFERENCES gitea_servers(id), name VARCHAR(100) NOT NULL, pattern TEXT NOT NULL, reject_message TEXT DEFAULT '', enabled BOOLEAN DEFAULT 1, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME)",
            "CREATE TABLE IF NOT EXISTS commit_gate_assignments (id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER NOT NULL REFERENCES gitea_servers(id), repo_name VARCHAR(300) NOT NULL, rule_id INTEGER NOT NULL REFERENCES commit_message_rules(id), install_status VARCHAR(20) DEFAULT 'pending', install_log TEXT DEFAULT '', applied_at DATETIME, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, CONSTRAINT uq_commit_gate_repo UNIQUE (server_id, repo_name))",
        ]:
            try:
                db.session.execute(db.text(sql))
                db.session.commit()
            except Exception:
                db.session.rollback()

        try:
            from services.commit_gate_service import (
                DEFAULT_PATTERN,
                DEFAULT_REJECT_MESSAGE,
                OLD_DEFAULT_PATTERN,
                OLD_DEFAULT_REJECT_MESSAGE,
            )
            db.session.execute(
                db.text(
                    """
                    UPDATE commit_message_rules
                    SET pattern = :new_pattern,
                        reject_message = CASE
                            WHEN reject_message = :old_reject OR reject_message = ''
                            THEN :new_reject
                            ELSE reject_message
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE pattern = :old_pattern
                    """
                ),
                {
                    'new_pattern': DEFAULT_PATTERN,
                    'new_reject': DEFAULT_REJECT_MESSAGE,
                    'old_pattern': OLD_DEFAULT_PATTERN,
                    'old_reject': OLD_DEFAULT_REJECT_MESSAGE,
                },
            )
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
    from routes.alert_routes import alert_bp
    from routes.dashboard_routes import dashboard_bp
    from routes.mirror_routes import mirror_bp
    from routes.statistics_routes import statistics_bp
    from routes.commit_gate_routes import commit_gate_bp

    app.register_blueprint(auth_bp, url_prefix='/api')
    app.register_blueprint(server_bp, url_prefix='/api')
    app.register_blueprint(restore_bp, url_prefix='/api')
    app.register_blueprint(backup_bp, url_prefix='/api')
    app.register_blueprint(settings_bp, url_prefix='/api')
    app.register_blueprint(schedule_bp, url_prefix='/api')
    app.register_blueprint(alert_bp, url_prefix='/api')
    app.register_blueprint(dashboard_bp, url_prefix='/api')
    app.register_blueprint(mirror_bp, url_prefix='/api')
    app.register_blueprint(statistics_bp, url_prefix='/api')
    app.register_blueprint(commit_gate_bp, url_prefix='/api')

    @app.before_request
    def check_host_ip():
        if not request.endpoint:
            return None
        read_only = {'list_servers', 'get_server', 'list_backups', 'list_restore_tasks', 'list_schedules', 'get_schedule_logs', 'list_alerts', 'alert_summary', 'recent_activity', 'list_mirrors', 'mirror_status', 'overview', 'commit_trend', 'repo_ranking', 'author_ranking', 'author_detail', 'author_repos', 'author_trend', 'delete_info', 'list_rules', 'list_repos', 'test_rule'}
        protected_prefixes = ('servers.', 'backups.', 'restore.', 'schedule.', 'commit_gate.')
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
