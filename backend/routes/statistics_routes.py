import json
import threading
from flask import Blueprint, request, jsonify
from flask_login import login_required
from models import db, GiteaServer, RepoStatistics, CommitStatistics

statistics_bp = Blueprint('statistics', __name__)


@statistics_bp.route('/statistics/<int:server_id>/overview', methods=['GET'])
@login_required
def overview(server_id):
    from services.statistics_service import get_overview
    return jsonify(get_overview(server_id))


@statistics_bp.route('/statistics/<int:server_id>/commits', methods=['GET'])
@login_required
def commit_trend(server_id):
    period = request.args.get('period', 'month')
    from services.statistics_service import get_commit_trend
    return jsonify(get_commit_trend(server_id, period))


@statistics_bp.route('/statistics/<int:server_id>/repos', methods=['GET'])
@login_required
def repo_ranking(server_id):
    sort_by = request.args.get('sort_by', 'commit_count')
    limit = request.args.get('limit', 10, type=int)
    from services.statistics_service import get_repo_ranking
    return jsonify(get_repo_ranking(server_id, sort_by, limit))


@statistics_bp.route('/statistics/<int:server_id>/authors', methods=['GET'])
@login_required
def author_ranking(server_id):
    stats = CommitStatistics.query.filter_by(server_id=server_id).all()
    author_commits = {}
    for s in stats:
        if s.top_authors:
            try:
                for a in json.loads(s.top_authors):
                    name = a.get('name', '')
                    if name:
                        author_commits[name] = author_commits.get(name, 0) + s.commit_count
            except Exception:
                pass
    ranked = sorted(author_commits.items(), key=lambda x: -x[1])[:20]
    return jsonify([{'name': k, 'commits': v} for k, v in ranked])


@statistics_bp.route('/statistics/<int:server_id>/refresh', methods=['POST'])
@login_required
def refresh_statistics(server_id):
    server = GiteaServer.query.get_or_404(server_id)

    def run():
        from app import create_app
        app = create_app()
        with app.app_context():
            from services.statistics_service import collect_statistics
            collect_statistics(server_id)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return jsonify({'ok': True})
