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
    return jsonify(worker=health(), migration_available=True, production_verified=False,
                   boundary='支持持久化迁移执行；未完成范围会阻断交接，完整版本矩阵仍在隔离验收。')


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


@git_migration_bp.route('/runs', methods=['GET', 'POST'])
def runs():
    from git_migration import runs as service
    from git_migration.models import GitMigrationRun
    if request.method == 'POST':
        require(health()['online'], 'Git 迁移执行器未在线', 'worker', 503)
        return jsonify(service.public(service.enqueue(body(), current_user.id))), 202
    return jsonify([service.public(row, detail=False) for row in GitMigrationRun.query.order_by(GitMigrationRun.id.desc()).limit(100)])


@git_migration_bp.get('/runs/<int:run_id>')
def run_detail(run_id):
    from git_migration import runs as service
    from git_migration.models import GitMigrationRun
    return jsonify(service.public(store.get(GitMigrationRun, run_id)))


@git_migration_bp.post('/runs/<int:run_id>/resume')
def resume_run(run_id):
    from git_migration import runs as service
    require(not body(), '继续执行无需覆盖固定计划')
    require(health()['online'], 'Git 迁移执行器未在线', 'worker', 503)
    return jsonify(service.public(service.resume(run_id))), 202


@git_migration_bp.get('/targets/<int:server_id>/groups')
def target_groups(server_id):
    from models import GiteaServer
    from svn_migration.targets import groups
    return jsonify(groups(store.get(GiteaServer, server_id), {
        'parent_id': request.args.get('parent_id', 0, type=int),
        'after_id': request.args.get('after_id', 0, type=int)}))


@git_migration_bp.get('/inventories/<int:inventory_id>/principals')
def principals(inventory_id):
    import json
    from git_migration.rules import requirements
    row = store.get(GitMigrationInventory, inventory_id)
    require(row.status == 'succeeded', '盘点尚未完成')
    return jsonify(requirements(json.loads(row.result_json)))
