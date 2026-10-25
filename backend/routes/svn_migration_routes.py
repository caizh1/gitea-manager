"""SVN 迁移管理接口；所有入口由统一权限清单限制为平台管理员。"""
import json
import shutil
import time
from flask import Blueprint, jsonify, request, Response
from flask_login import current_user
from sqlalchemy.orm.exc import StaleDataError

from models import db, GiteaServer
from svn_migration.common import MigrationError, encode, require, work_root, file_lock
from svn_migration.models import (SvnSource, SvnRepository, SvnMigrationPlan, SvnMigrationRun,
                                  SvnMigrationItem, SvnMigrationEvent)
from svn_migration import service

svn_migration_bp = Blueprint('svn_migration', __name__)


@svn_migration_bp.errorhandler(MigrationError)
def migration_error(error):
    db.session.rollback()
    return jsonify(error=str(error), code=error.code), error.status


@svn_migration_bp.errorhandler(StaleDataError)
def stale_write(_error):
    db.session.rollback()
    return jsonify(error='数据已被另一操作更新，请刷新后重试', code='stale'), 409


def body():
    data = request.get_json(silent=True)
    require(isinstance(data, dict), '请求必须是 JSON 对象')
    return data


@svn_migration_bp.after_request
def audit_change(response):
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and response.status_code < 300:
        from access.authentication import audit
        audit('SVN 迁移管理操作', request.path, {'操作': request.endpoint, '响应状态': response.status_code})
        db.session.commit()
    return response


@svn_migration_bp.get('/status')
def status():
    usage = shutil.disk_usage(work_root())
    from flask import current_app
    cached = current_app.extensions.get('svn_workspace_size', (0, 0))
    if time.monotonic() - cached[0] > 10:
        size = 0
        for path in work_root().rglob('*'):
            try:
                if path.is_file() and not path.is_symlink():
                    size += path.stat().st_size
            except FileNotFoundError:
                pass
        cached = (time.monotonic(), size)
        current_app.extensions['svn_workspace_size'] = cached
    return jsonify(worker=service.health(), disk_free=usage.free,
                   workspace_bytes=cached[1],
                   boundary='只迁移内容和历史，不创建业务账号、不迁移权限；目标初始保持私有。')


@svn_migration_bp.get('/targets')
def targets():
    return jsonify([{'id': row.id, 'name': row.name, 'url': row.gitea_url} for row in
                    GiteaServer.query.filter(GiteaServer.role != 'archive').order_by(GiteaServer.id)])


@svn_migration_bp.post('/targets/<int:server_id>/groups/query')
def target_groups(server_id):
    from svn_migration import targets as target_service
    return jsonify(target_service.groups(service.get(GiteaServer, server_id), body()))


@svn_migration_bp.post('/targets/<int:server_id>/paths')
def target_paths(server_id):
    from svn_migration import targets as target_service
    return jsonify(target_service.paths(service.get(GiteaServer, server_id), body()))


@svn_migration_bp.post('/targets/<int:server_id>/groups')
def create_target_group(server_id):
    from svn_migration import targets as target_service
    return jsonify(target_service.create_group(service.get(GiteaServer, server_id), body())), 201


@svn_migration_bp.route('/sources', methods=['GET', 'POST'])
def sources():
    if request.method == 'POST':
        return jsonify(service.public_source(service.source_edit(body()))), 201
    return jsonify([service.public_source(row) for row in SvnSource.query.order_by(SvnSource.id)])


@svn_migration_bp.route('/sources/<int:source_id>', methods=['PUT', 'DELETE'])
def source(source_id):
    if request.method == 'DELETE':
        row = service.get(SvnSource, source_id)
        data = service.public_source(row) | body() | {'archived': True}
    else:
        data = body()
    return jsonify(service.public_source(service.source_edit(data, source_id)))


@svn_migration_bp.post('/sources/<int:source_id>/test')
def test_source(source_id):
    row = service.get(SvnSource, source_id)
    require(not row.archived, '来源已停用')
    from svn_migration.discovery import queue, public
    return jsonify(public(queue(row))), 202


@svn_migration_bp.post('/sources/<int:source_id>/discoveries')
def discover_projects(source_id):
    from svn_migration.discovery import queue, public
    return jsonify(public(queue(service.get(SvnSource, source_id), body().get('url')))), 202


@svn_migration_bp.get('/discoveries/<int:discovery_id>')
def discovery_result(discovery_id):
    from svn_migration.discovery import public
    from svn_migration.models import SvnDiscovery
    return jsonify(public(service.get(SvnDiscovery, discovery_id)))


@svn_migration_bp.post('/sources/<int:source_id>/enroll')
def enroll_projects(source_id):
    from svn_migration.discovery import enroll
    return jsonify([service.public_repo(row) for row in enroll(service.get(SvnSource, source_id), body())]), 201


@svn_migration_bp.post('/sources/<int:source_id>/layout-detections')
def detect_layouts(source_id):
    from svn_migration.layout_detection import queue, public
    return jsonify([public(row) for row in queue(service.get(SvnSource, source_id), body().get('urls'))]), 202


@svn_migration_bp.get('/layout-detections/<int:detection_id>')
def layout_detection(detection_id):
    from svn_migration.models import SvnLayoutDetection
    from svn_migration.layout_detection import public
    return jsonify(public(service.get(SvnLayoutDetection, detection_id)))


@svn_migration_bp.route('/sources/<int:source_id>/repositories', methods=['GET', 'POST'])
def repositories(source_id):
    service.get(SvnSource, source_id)
    if request.method == 'GET':
        from svn_migration.preflights import repository_results
        rows = SvnRepository.query.filter_by(source_id=source_id).order_by(SvnRepository.id).all()
        results = repository_results(rows)
        return jsonify([dict(service.public_repo(row), **results[row.id]) for row in rows])
    data = body()
    # 批量入口接受结构化行，不从未确认的服务器目录猜测仓库清单。
    rows = data.get('repositories', [data])
    require(isinstance(rows, list) and 0 < len(rows) <= 1000, '请提供 1 至 1000 个仓库')
    result = []
    for row in rows:
        require(isinstance(row, dict), '仓库行格式无效')
        result.append(service.public_repo(service.edit_repository(source_id, row, commit=False)))
    db.session.commit()
    return jsonify(result), 201


@svn_migration_bp.put('/sources/<int:source_id>/repositories/<int:repository_id>')
def repository(source_id, repository_id):
    return jsonify(service.public_repo(service.edit_repository(source_id, body(), repository_id)))


@svn_migration_bp.route('/plans', methods=['GET', 'POST'])
def plans():
    if request.method == 'POST':
        return jsonify(service.public_plan(service.plan_edit(body()))), 201
    return jsonify([service.public_plan(row) for row in SvnMigrationPlan.query.order_by(SvnMigrationPlan.id.desc()).limit(100)])


@svn_migration_bp.route('/plans/<int:plan_id>', methods=['GET', 'PUT'])
def plan(plan_id):
    row = service.plan_edit(body(), plan_id) if request.method == 'PUT' else service.get(SvnMigrationPlan, plan_id)
    return jsonify(service.public_plan(row))


@svn_migration_bp.post('/plans/<int:plan_id>/preflight')
def preflight(plan_id):
    row = service.get(SvnMigrationPlan, plan_id)
    service.queue_preflight(row)
    return jsonify(service.public_plan(row)), 202


@svn_migration_bp.route('/runs', methods=['GET', 'POST'])
def runs():
    if request.method == 'POST':
        return jsonify(service.public_run(service.create_run(body(), current_user), True)), 202
    return jsonify([service.public_run(row) for row in SvnMigrationRun.query.order_by(SvnMigrationRun.id.desc()).limit(100)])


@svn_migration_bp.get('/runs/<int:run_id>')
def run(run_id):
    return jsonify(service.public_run(service.get(SvnMigrationRun, run_id), True))


@svn_migration_bp.post('/runs/<int:run_id>/<action>')
def control(run_id, action):
    require(action in ('pause', 'resume', 'cancel', 'reconcile'), '未知操作')
    return jsonify(service.public_run(service.control_run(service.get(SvnMigrationRun, run_id), action), True)), 202


@svn_migration_bp.get('/runs/<int:run_id>/events')
def events(run_id):
    service.get(SvnMigrationRun, run_id)
    after = request.args.get('after', 0, type=int)
    rows = SvnMigrationEvent.query.filter(SvnMigrationEvent.run_id == run_id, SvnMigrationEvent.id > after).order_by(SvnMigrationEvent.id).limit(200).all()
    return jsonify([{'id': row.id, 'item_id': row.item_id, 'stage': row.stage, 'message': row.message,
                     'created_at': row.created_at.isoformat()} for row in rows])


@svn_migration_bp.get('/runs/<int:run_id>/report')
def report(run_id):
    row = service.get(SvnMigrationRun, run_id)
    return Response(encode({'说明': '当前迁移结果；失败、部分完成和待确认均不代表迁移成功',
                            '结果': service.public_run(row, True, True)}), content_type='application/json; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="svn-migration-{run_id}.json"'})


@svn_migration_bp.post('/runs/<int:run_id>/cleanup')
def cleanup(run_id):
    row = service.get(SvnMigrationRun, run_id)
    require(row.status == 'success', '仅支持清理成功任务，失败或待确认现场必须保留', 'state', 409)
    with file_lock(work_root() / 'enqueue.lock', blocking=True):
        db.session.refresh(row)
        require(row.status == 'success', '任务状态已变化，未清理工作区', 'state', 409)
        folder = work_root() / ('run-' + str(row.id))
        if folder.exists():
            shutil.rmtree(folder)
    return jsonify(message='本地转换工作区已清理，远端仓库和数据库审计记录已保留')


@svn_migration_bp.get('/preflights/<int:preflight_id>')
def preflight_detail(preflight_id):
    from svn_migration.models import SvnPreflight
    from svn_migration.preflights import public
    return jsonify(public(service.get(SvnPreflight, preflight_id), detail=True))
