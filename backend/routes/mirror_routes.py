import json
import threading
from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_login import login_required
from models import db, MirrorConfig

mirror_bp = Blueprint('mirror', __name__)


def config_to_dict(c):
    return {
        'id': c.id,
        'source_server_id': c.source_server_id,
        'source_server_name': c.source_server.name if c.source_server else '',
        'target_server_id': c.target_server_id,
        'target_server_name': c.target_server.name if c.target_server else '',
        'sync_mode': c.sync_mode,
        'sync_interval': c.sync_interval,
        'enabled': c.enabled,
        'status': c.status,
        'last_sync_at': c.last_sync_at.isoformat() if c.last_sync_at else None,
        'last_sync_status': c.last_sync_status,
        'last_sync_log': c.last_sync_log,
        'total_repos': c.total_repos,
        'synced_repos': c.synced_repos,
        'failed_repos': c.failed_repos,
        'created_at': c.created_at.isoformat() if c.created_at else None,
    }


@mirror_bp.route('/mirrors', methods=['GET'])
@login_required
def list_mirrors():
    configs = MirrorConfig.query.order_by(MirrorConfig.created_at.desc()).all()
    return jsonify([config_to_dict(c) for c in configs])


@mirror_bp.route('/mirrors', methods=['POST'])
@login_required
def create_mirror():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    c = MirrorConfig(
        source_server_id=data.get('source_server_id', 0),
        target_server_id=data.get('target_server_id', 0),
        sync_mode=data.get('sync_mode', 'gitea_mirror'),
        sync_interval=data.get('sync_interval', 30),
        enabled=data.get('enabled', True),
        created_at=datetime.utcnow(),
    )
    db.session.add(c)
    db.session.commit()
    return jsonify(config_to_dict(c)), 201


@mirror_bp.route('/mirrors/<int:cid>', methods=['PUT'])
@login_required
def update_mirror(cid):
    c = MirrorConfig.query.get_or_404(cid)
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    for f in ['sync_mode', 'sync_interval', 'enabled', 'source_server_id', 'target_server_id']:
        if f in data:
            setattr(c, f, data[f])
    db.session.commit()
    return jsonify(config_to_dict(c))


@mirror_bp.route('/mirrors/<int:cid>', methods=['DELETE'])
@login_required
def delete_mirror(cid):
    c = MirrorConfig.query.get_or_404(cid)
    from models import MirrorRepoStatus
    MirrorRepoStatus.query.filter_by(mirror_config_id=cid).delete()
    db.session.delete(c)
    db.session.commit()
    return jsonify({'ok': True})


@mirror_bp.route('/mirrors/<int:cid>/setup', methods=['POST'])
@login_required
def setup_mirror(cid):
    c = MirrorConfig.query.get_or_404(cid)

    def run():
        from app import create_app
        app = create_app()
        with app.app_context():
            from services.mirror_service import setup_mirror as _setup
            _setup(cid)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return jsonify({'ok': True})


@mirror_bp.route('/mirrors/<int:cid>/sync', methods=['POST'])
@login_required
def sync_mirror(cid):
    c = MirrorConfig.query.get_or_404(cid)

    def run():
        from app import create_app
        app = create_app()
        with app.app_context():
            from services.mirror_service import sync_mirror as _sync
            _sync(cid)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return jsonify({'ok': True})


@mirror_bp.route('/mirrors/<int:cid>/status', methods=['GET'])
@login_required
def mirror_status(cid):
    from services.mirror_service import get_mirror_status
    return jsonify(get_mirror_status(cid))


@mirror_bp.route('/mirrors/<int:cid>/sync-repo/<path:repo_name>', methods=['POST'])
@login_required
def sync_repo(cid, repo_name):
    from services.mirror_service import sync_single_repo
    success, error = sync_single_repo(cid, repo_name)
    if success:
        return jsonify({'ok': True})
    return jsonify({'error': error}), 400
