from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_login import login_required
from models import db, GiteaServer
from services.gitea_service import fetch_server_info, test_server_connection, get_server_detail

server_bp = Blueprint('servers', __name__)


def server_to_dict(s):
    return {
        'id': s.id,
        'name': s.name,
        'role': s.role,
        'host': s.host,
        'ssh_port': s.ssh_port,
        'ssh_user': s.ssh_user,
        'gitea_container': s.gitea_container,
        'pg_container': s.pg_container,
        'pg_dbname': s.pg_dbname,
        'pg_user': s.pg_user,
        'gitea_port': s.gitea_port,
        'gitea_url': s.gitea_url,
        'api_token': s.api_token,
        'status': s.status,
        'version': s.version,
        'repo_count': s.repo_count,
        'user_count': s.user_count,
        'is_local': s.is_local,
        'disk_usage': s.disk_usage,
        'last_check_at': s.last_check_at.isoformat() if s.last_check_at else None,
        'created_at': s.created_at.isoformat() if s.created_at else None,
    }


@server_bp.route('/servers', methods=['GET'])
@login_required
def list_servers():
    servers = GiteaServer.query.order_by(GiteaServer.created_at.desc()).all()
    return jsonify([server_to_dict(s) for s in servers])


@server_bp.route('/servers/<int:sid>', methods=['GET'])
@login_required
def get_server(sid):
    s = GiteaServer.query.get_or_404(sid)
    return jsonify(server_to_dict(s))


@server_bp.route('/servers', methods=['POST'])
@login_required
def add_server():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    server = GiteaServer(
        name=data.get('name', ''),
        role=data.get('role', 'backup'),
        host=data.get('host', ''),
        ssh_port=data.get('ssh_port', 22),
        ssh_user=data.get('ssh_user', 'root'),
        gitea_container=data.get('gitea_container', 'gitea'),
        pg_container=data.get('pg_container', 'gitea-postgres'),
        pg_dbname=data.get('pg_dbname', 'gitea'),
        pg_user=data.get('pg_user', 'gitea'),
        gitea_port=data.get('gitea_port', 3000),
        gitea_url=data.get('gitea_url', ''),
        api_token=data.get('api_token', ''),
        status='unknown',
        created_at=datetime.utcnow(),
    )
    db.session.add(server)
    db.session.commit()

    ok, msg = test_server_connection(server)
    server.status = 'online' if ok else 'offline'
    db.session.commit()

    fetch_server_info(server)
    db.session.commit()

    _repair_orphan_records(server)

    return jsonify(server_to_dict(server)), 201


def _repair_orphan_records(server):
    from models import Backup, RestoreTask
    Backup.query.filter(
        Backup.source_server_name == server.name,
        ~Backup.source_server_id.in_(db.session.query(GiteaServer.id))
    ).update({'source_server_id': server.id}, synchronize_session='fetch')

    RestoreTask.query.filter(
        RestoreTask.target_server_name == server.name,
        ~RestoreTask.target_server_id.in_(db.session.query(GiteaServer.id))
    ).update({'target_server_id': server.id}, synchronize_session='fetch')
    db.session.commit()


@server_bp.route('/servers/<int:sid>/delete-info', methods=['GET'])
@login_required
def delete_info(sid):
    from models import Backup, RestoreTask
    backup_count = Backup.query.filter_by(source_server_id=sid).count()
    restore_count = RestoreTask.query.filter_by(target_server_id=sid).count()
    return jsonify({'backup_count': backup_count, 'restore_count': restore_count})


@server_bp.route('/servers/<int:sid>', methods=['PUT'])
@login_required
def update_server(sid):
    s = GiteaServer.query.get_or_404(sid)
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    fields = ['name', 'role', 'host', 'ssh_port', 'ssh_user',
              'gitea_container', 'pg_container', 'pg_dbname', 'pg_user',
              'gitea_port', 'gitea_url', 'api_token']
    for f in fields:
        if f in data:
            setattr(s, f, data[f])
    db.session.commit()
    return jsonify(server_to_dict(s))


@server_bp.route('/servers/<int:sid>', methods=['DELETE'])
@login_required
def delete_server(sid):
    from models import Backup, RestoreTask
    s = GiteaServer.query.get_or_404(sid)
    backup_count = Backup.query.filter_by(source_server_id=sid).count()
    restore_count = RestoreTask.query.filter_by(target_server_id=sid).count()
    db.session.delete(s)
    db.session.commit()
    return jsonify({'ok': True, 'deleted_backups': backup_count, 'deleted_restores': restore_count})


@server_bp.route('/servers/<int:sid>/check', methods=['POST'])
@login_required
def check_server(sid):
    s = GiteaServer.query.get_or_404(sid)
    ok, msg = test_server_connection(s)
    s.status = 'online' if ok else 'offline'
    db.session.commit()
    return jsonify({'status': s.status, 'ok': ok, 'message': msg})


@server_bp.route('/servers/<int:sid>/refresh', methods=['POST'])
@login_required
def refresh_server(sid):
    s = GiteaServer.query.get_or_404(sid)
    ok, _ = test_server_connection(s)
    s.status = 'online' if ok else 'offline'
    db.session.commit()
    fetch_server_info(s)
    db.session.commit()
    return jsonify(server_to_dict(s))


@server_bp.route('/servers/<int:sid>/detail', methods=['GET'])
@login_required
def get_server_detail_route(sid):
    s = GiteaServer.query.get_or_404(sid)
    base = server_to_dict(s)
    base['detail'] = get_server_detail(s)
    db.session.commit()
    return jsonify(base)
