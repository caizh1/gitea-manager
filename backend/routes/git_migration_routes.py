"""Git 来源管理与异步发现，所有接口仅向平台管理员开放。"""
from flask import Blueprint, jsonify, request, g
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


@git_migration_bp.before_app_request
def protect_connection_alias():
    if request.endpoint not in ('servers.add_server', 'servers.update_server'):
        return
    from git_migration.instance import connection_lock, validate_connection_change
    lock = connection_lock()
    try:
        lock.__enter__()
        g.git_connection_lock = lock
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            validate_connection_change(data, (request.view_args or {}).get('sid'))
    except MigrationError as error:
        return jsonify(error=str(error), code=error.code), error.status


@git_migration_bp.teardown_app_request
def release_connection_alias(_error):
    lock = g.pop('git_connection_lock', None)
    if lock:
        lock.__exit__(None, None, None)


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


@git_migration_bp.post('/sources/<int:source_id>/credentials')
def rotate_credentials(source_id):
    return jsonify(store.public_source(store.rotate_credentials(source_id, body())))


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


@git_migration_bp.post('/runs/<int:run_id>/abandon')
def abandon_run(run_id):
    from git_migration import runs as service
    data = body()
    require(set(data) == {'reason'}, '终止请求需要且只接受原因')
    return jsonify(service.public(service.abandon(run_id, data['reason'])))


@git_migration_bp.post('/runs/<int:run_id>/target-credentials')
def rotate_target_credentials(run_id):
    from git_migration import runs as service
    data = body()
    require(set(data) == {'token'}, '目标凭据更新只接受令牌')
    return jsonify(service.public(service.rotate_target_credentials(run_id, data['token'])))


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


@git_migration_bp.get('/inventories/<int:inventory_id>/principal-details')
def principal_details(inventory_id):
    import json
    from git_migration.people import details
    row = store.get(GitMigrationInventory, inventory_id)
    require(row.status == 'succeeded', '盘点尚未完成')
    return jsonify(details(json.loads(row.result_json)))


@git_migration_bp.route('/account-mappings', methods=['GET', 'POST'])
def account_mappings():
    from git_migration import account_mapping as service
    from git_migration.models import GitMigrationAccountMapping
    if request.method == 'POST':
        require(health()['online'], '人员匹配执行器未在线', 'worker', 503)
        return jsonify(service.public(service.enqueue(body(), current_user.id))), 202
    rows = GitMigrationAccountMapping.query
    for name in ('inventory_id', 'server_id', 'group_id'):
        value = request.args.get(name, type=int)
        if value is not None:
            rows = rows.filter(getattr(GitMigrationAccountMapping, name) == value)
    return jsonify([service.public(row, size=1) for row in rows.order_by(GitMigrationAccountMapping.id.desc()).limit(50)])


@git_migration_bp.route('/account-mappings/<int:mapping_id>', methods=['GET', 'PATCH'])
def account_mapping(mapping_id):
    from git_migration import account_mapping as service
    from git_migration.models import GitMigrationAccountMapping
    row = service.mutate(mapping_id, body()) if request.method == 'PATCH' else store.get(GitMigrationAccountMapping, mapping_id)
    return jsonify(service.public(row, request.args.get('q', ''), request.args.get('status', 'all'),
                                  request.args.get('page', 1, type=int), request.args.get('page_size', 50, type=int)))


@git_migration_bp.get('/account-mappings/<int:mapping_id>/accounts')
def mapping_accounts(mapping_id):
    import json
    from git_migration import account_mapping as service
    from git_migration.models import GitMigrationAccountMapping
    row = store.get(GitMigrationAccountMapping, mapping_id)
    require(not service.stale(row), '人员匹配已失效，请重新核对', 'stale', 409)
    state = json.loads(row.result_json)
    needle = service.normalized(request.args.get('q', ''))
    require(len(needle) <= 200, '账号查询过长')
    item = next((r for r in state.get('rows', []) if r['key'] == request.args.get('key')), None)
    require(item is not None, '请选择来源人员')
    rights = {int(k): v for k, v in state.get('rights', {}).items()}
    found = [dict(user, eligible=service.eligible(user, item, rights)) for user in state.get('accounts', [])
             if not needle or any(needle in service.normalized(user[k]) for k in ('login','name','email'))]
    return jsonify(accounts=found[:100], total=len(found))


@git_migration_bp.route('/preflights', methods=['GET','POST'])
def preflights():
    from git_migration import preflights as service
    from git_migration.models import GitMigrationPreflight
    if request.method == 'POST':
        require(health()['online'], '预迁移执行器未在线', 'worker', 503)
        return jsonify(service.public(service.enqueue(body(), current_user.id))), 202
    return jsonify([service.public(row, False) for row in GitMigrationPreflight.query.order_by(GitMigrationPreflight.id.desc()).limit(100)])


@git_migration_bp.get('/preflights/<int:preflight_id>')
def preflight_detail(preflight_id):
    from git_migration import preflights as service
    from git_migration.models import GitMigrationPreflight
    return jsonify(service.public(store.get(GitMigrationPreflight, preflight_id)))


@git_migration_bp.post('/preflights/<int:preflight_id>/start')
def start_preflight(preflight_id):
    from git_migration import preflights as service
    require(health()['online'], '正式启动前复检执行器未在线', 'worker', 503)
    return jsonify(service.public(service.request_start(preflight_id, body(), current_user.id))), 202


@git_migration_bp.post('/preflights/<int:preflight_id>/compatibility-acknowledgements')
def acknowledge_preflight_compatibility(preflight_id):
    from git_migration import preflights as service
    require(health()['online'], '预迁移执行器未在线', 'worker', 503)
    return jsonify(service.public(service.acknowledge_compatibility(preflight_id, body(), current_user.id))), 202


@git_migration_bp.post('/preflights/<int:preflight_id>/cancel')
def cancel_preflight(preflight_id):
    from git_migration import preflights as service
    require(not body(), '结束预迁移无需修改输入')
    require(health()['online'], '预迁移执行器未在线', 'worker', 503)
    return jsonify(service.public(service.request_cancel(preflight_id))), 202


@git_migration_bp.post('/preflights/<int:preflight_id>/target-credentials')
def preflight_target_credentials(preflight_id):
    from git_migration import preflights as service
    data=body()
    require(set(data)=={'token'}, '只允许更新目标令牌')
    return jsonify(service.public(service.rotate_target_credentials(preflight_id,data['token'])))

@git_migration_bp.post('/preflights/<int:preflight_id>/resume')
def resume_preflight(preflight_id):
    from git_migration import preflights as service
    require(not body(), '继续预迁移不能修改原计划')
    require(health()['online'], '预迁移执行器未在线', 'worker',503)
    return jsonify(service.public(service.resume(preflight_id))),202
