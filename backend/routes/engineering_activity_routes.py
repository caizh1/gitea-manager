"""研发上库的授权查询和采集设置。"""
from datetime import datetime

from flask import Blueprint, jsonify, request

from access.errors import AccessError
from access.scopes import require_server, server_epoch
from models import GiteaServer, db
from services.policy_gitea import PolicyGitea, PolicyError
from engineering_activity.closing import receive
from engineering_activity.models import Collection
from engineering_activity.report import dashboard, details, query_args, saved_repos
from engineering_activity.rules import validate_settings
from engineering_activity.worker import available_teams, catalog


engineering_bp = Blueprint('engineering_activity', __name__)


@engineering_bp.errorhandler(PolicyError)
def collection_error(exc):
    db.session.rollback()
    return jsonify(error=str(exc)), 502


def context(server_id):
    binding = require_server(server_id)
    server = db.session.get(GiteaServer, server_id)
    if server is None:
        raise AccessError('实例不存在或无权访问', 404)
    key = (server.id, server_epoch(server), binding.organization_id if binding else 0)
    return server, binding, key, PolicyGitea(server)


def collection_status(row):
    if not row:
        return {'status': 'disabled'}
    return {'status': row.status, 'progress': row.progress,
            'finished_at': row.finished.isoformat() + 'Z' if row.finished else None}


@engineering_bp.get('/<int:server_id>/options')
def options(server_id):
    _, binding, key, client = context(server_id)
    repos = catalog(client, binding)
    row = db.session.get(Collection, key)
    return jsonify(repositories=[{'id': r['id'], 'name': r['full_name']} for r in repos.values()],
                   teams=list(available_teams(client, binding).values()),
                   settings=row.settings if row else validate_settings({}), collection=collection_status(row))


def selection(server_id):
    server, binding, key, client = context(server_id)
    row = db.session.get(Collection, key)
    config = row.settings if row else validate_settings({})
    repos = catalog(client, binding)
    raw_repo = request.args.get('repository', '')
    if raw_repo:
        repo_id = request.args.get('repository', type=int)
        if repo_id not in repos:
            raise AccessError('仓库不存在或无权访问', 404)
        repos = {repo_id: repos[repo_id]}
    raw_team = request.args.get('team', '')
    team_id = None
    org_id = 0
    if raw_team:
        team_id = request.args.get('team', type=int)
        if team_id not in config['teams']:
            raise AccessError('研发团队不存在或无权访问', 404)
        # 查询时重新核实团队归属。旧快照不得继承删除或转移的团队身份。
        actual = available_teams(client, binding).get(team_id)
        if actual is None:
            raise AccessError('研发团队不存在或无权访问', 404)
        org_id = actual['organization_id']
        repos = {rid: repo for rid, repo in repos.items() if repo['owner']['id'] == org_id}
    return server, key, row, config, repos, team_id, org_id


@engineering_bp.get('/<int:server_id>/dashboard')
def overview(server_id):
    args = query_args()
    server, key, row, _, repos, team, org_id = selection(server_id)
    data = dashboard(server, key, saved_repos(server, key[2], repos), repos, team, org_id,
                     request.args.get('person'), args)
    return jsonify(**data, collection=collection_status(row))


@engineering_bp.get('/<int:server_id>/details')
def detail(server_id):
    args = query_args()
    server, key, _, _, repos, team, org_id = selection(server_id)
    return jsonify(details(server, key, saved_repos(server, key[2], repos), team, org_id,
                           request.args.get('person'), args))


@engineering_bp.post('/<int:server_id>/collect')
def collect(server_id):
    _, binding, key, client = context(server_id)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise AccessError('采集请求格式错误', 400)
    row = db.session.get(Collection, key)
    config = row.settings if row else validate_settings({})
    if 'settings' in payload:
        config = validate_settings(payload['settings'])
        if binding and config['webhook_base']:
            raise AccessError('只有平台管理员可以设置缺陷关闭回调', 403)
        repos = catalog(client, binding)
        if set(map(int, config['paths'])) - repos.keys():
            raise AccessError('目录规则包含无权访问的仓库', 404)
        if set(config['teams']) - available_teams(client, binding).keys():
            raise AccessError('配置包含无权访问的团队', 404)
    values = {'server_id': key[0], 'epoch': key[1], 'organization_id': key[2], 'settings': config,
              'due': datetime.utcnow(), 'requested': 1, 'status': 'queued', 'progress': {}}
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    command = sqlite_insert(Collection).values(**values)
    db.session.execute(command.on_conflict_do_update(
        index_elements=['server_id', 'epoch', 'organization_id'],
        set_={'settings': config if 'settings' in payload else Collection.settings,
              'due': values['due'], 'requested': Collection.requested + 1, 'status': 'queued'}))
    db.session.commit()
    return jsonify(status='queued'), 202


@engineering_bp.post('/events/<int:server_id>/<int:owner_id>/<int:repo_id>')
def event(server_id, owner_id, repo_id):
    return receive(server_id, owner_id, repo_id)
