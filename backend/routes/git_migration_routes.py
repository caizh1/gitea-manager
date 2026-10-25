"""Git 来源管理与异步发现，所有接口仅向平台管理员开放。"""
from flask import Blueprint, jsonify, request
from flask_login import current_user
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from models import db
from services.upgrade_maintenance import active
from svn_migration.common import MigrationError, require
from git_migration.models import GitMigrationSource, GitMigrationDiscovery, GitMigrationInventory
from git_migration import store
from git_migration.worker import health


git_migration_bp = Blueprint('git_migration', __name__)


@git_migration_bp.before_request
def maintenance():
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        require(not active(), '升级维护中，暂不能修改 Git 迁移任务', 'maintenance', 503)


@git_migration_bp.errorhandler(MigrationError)
def migration_error(error):
    db.session.rollback()
    return jsonify(error=str(error), code=error.code), error.status


@git_migration_bp.errorhandler(StaleDataError)
@git_migration_bp.errorhandler(IntegrityError)
def concurrent_change(_error):
    db.session.rollback()
    return jsonify(error='来源已存在或数据已被其他操作修改，请刷新后重试', code='conflict'), 409


def body():
    data = request.get_json(silent=True)
    require(isinstance(data, dict), '请求必须是 JSON 对象')
    return data


@git_migration_bp.after_request
def audit_change(response):
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and response.status_code < 300:
        from access.authentication import audit
        audit('Git 迁移来源管理', request.path, {'操作': request.endpoint, '响应状态': response.status_code})
        db.session.commit()
    return response


@git_migration_bp.get('/status')
def status():
    return jsonify(worker=health(), migration_available=False,
                   boundary='当前仅开放来源发现；完整盘点、无损迁移与规则交接尚未通过验收。')


@git_migration_bp.route('/sources', methods=['GET', 'POST'])
def sources():
    if request.method == 'POST':
        with store.file_lock(store.root() / 'enqueue.lock', blocking=True):
            return jsonify(store.public_source(store.save_source(body()))), 201
    return jsonify([store.public_source(row) for row in GitMigrationSource.query.order_by(GitMigrationSource.id)])


@git_migration_bp.put('/sources/<int:source_id>')
def source(source_id):
    with store.file_lock(store.root() / 'enqueue.lock', blocking=True):
        db.session.expire_all()
        return jsonify(store.public_source(store.save_source(body(), source_id)))


@git_migration_bp.route('/sources/<int:source_id>/discoveries', methods=['GET', 'POST'])
def discoveries(source_id):
    store.get(GitMigrationSource, source_id)
    if request.method == 'POST':
        require(health()['online'], 'Git 来源执行器未在线，请先启动执行器', 'worker', 503)
        row = store.enqueue_discovery(source_id, body(), current_user.id)
        return jsonify(store.public_discovery(row)), 202
    return jsonify([store.public_discovery(row, detail=False) for row in
                    GitMigrationDiscovery.query.filter_by(source_id=source_id).order_by(
                        GitMigrationDiscovery.id.desc()).limit(100)])


@git_migration_bp.get('/discoveries/<int:discovery_id>')
def discovery(discovery_id):
    return jsonify(store.public_discovery(store.get(GitMigrationDiscovery, discovery_id)))


@git_migration_bp.route('/sources/<int:source_id>/inventories', methods=['GET', 'POST'])
def inventories(source_id):
    store.get(GitMigrationSource, source_id)
    if request.method == 'POST':
        require(health()['online'], 'Git 来源执行器未在线，请先启动执行器', 'worker', 503)
        row = store.enqueue_inventory(source_id, body(), current_user.id)
        return jsonify(store.public_discovery(row)), 202
    return jsonify([store.public_discovery(row, detail=False) for row in
                    GitMigrationInventory.query.filter_by(source_id=source_id).order_by(
                        GitMigrationInventory.id.desc()).limit(100)])


@git_migration_bp.get('/inventories/<int:inventory_id>')
def inventory(inventory_id):
    return jsonify(store.public_discovery(store.get(GitMigrationInventory, inventory_id)))
