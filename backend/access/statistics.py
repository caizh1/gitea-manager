"""仓库粒度统计快照；组织查询不使用实例汇总推算。"""
import json
import threading
from collections import defaultdict
from datetime import datetime

from flask import current_app, jsonify, request
from models import db, GiteaServer
from access.errors import AccessError
from access.scopes import repositories_for, server_epoch, actor_snapshot, check_task_actor


class RepositoryStatisticsSnapshot(db.Model):
    __tablename__ = 'access_repository_statistics'
    server_id = db.Column(db.Integer, primary_key=True)
    repo_id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, nullable=False)
    instance_epoch = db.Column(db.String(64), nullable=False)
    value_json = db.Column(db.Text, nullable=False)
    snapshot_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


def save_snapshot(server, repo, commits, values, complete=True):
    from services.statistics_service import _parse_commit_date, _get_period_key
    authors = {}
    periods = defaultdict(lambda: defaultdict(int))
    for commit in commits:
        identity = commit.get('commit', {}).get('author', {})
        name = identity.get('name', 'unknown')
        author = authors.setdefault(name, {'name': name, 'email': identity.get('email', ''), 'commits': 0,
                                           'additions': 0, 'deletions': 0, 'first_date': None, 'last_date': None})
        author['commits'] += 1
        for key in ('additions', 'deletions'):
            author[key] += commit.get('stats', {}).get(key, 0) or 0
        date = _parse_commit_date(identity.get('date', ''))
        if date:
            value = date.isoformat()
            author['first_date'] = min(author['first_date'] or value, value)
            author['last_date'] = max(author['last_date'] or value, value)
            for period in ('month', 'quarter', 'half_year', 'year'):
                periods[period + ':' + _get_period_key(date, period)][name] += 1
    row = db.session.get(RepositoryStatisticsSnapshot, (server.id, repo['id']))
    if row is None:
        row = RepositoryStatisticsSnapshot(server_id=server.id, repo_id=repo['id'])
        db.session.add(row)
    row.organization_id = repo['owner']['id']
    row.instance_epoch = server_epoch(server)
    row.snapshot_at = datetime.utcnow()
    row.value_json = json.dumps({**values, 'repo_id': repo['id'], 'repo_name': repo['full_name'],
                                'authors': authors, 'periods': dict(periods), 'complete': complete}, ensure_ascii=False)
    db.session.commit()


def response(endpoint, binding):
    if endpoint == 'statistics.refresh_statistics':
        app = current_app._get_current_object()
        actor = actor_snapshot(binding)
        def collect():
            with app.app_context():
                from services.statistics_service import collect_statistics
                try:
                    check_task_actor(actor)
                    collect_statistics(binding.server_id, binding_id=binding.id, access_actor=actor)
                except Exception:
                    app.logger.exception('组织统计采集未完成，组织绑定 %s', binding.id)
                finally:
                    db.session.remove()
        threading.Thread(target=collect, daemon=True, name='organization-statistics').start()
        return jsonify(ok=True), 202
    repositories = repositories_for(binding)
    rows = RepositoryStatisticsSnapshot.query.filter_by(server_id=binding.server_id,
        organization_id=binding.organization_id, instance_epoch=binding.instance_epoch).filter(
        RepositoryStatisticsSnapshot.repo_id.in_(repositories)).all()
    values = [json.loads(row.value_json) for row in rows]
    ready = len(rows) == len(repositories) and all(v['complete'] for v in values)
    # 状态用响应头传递，保持既有数组接口兼容；概览包含显式状态。
    def reply(value):
        result = jsonify(value)
        result.headers['X-Statistics-Ready'] = 'true' if ready else 'false'
        return result
    if endpoint == 'statistics.overview':
        languages = defaultdict(int)
        for value in values:
            for name, count in value.get('language_breakdown', {}).items():
                languages[name] += count
        return reply({'ready': ready, 'pending_collection': not ready, 'message': '' if ready else '组织统计待生成或采集未完成',
                      'total_repos': len(repositories), 'collected_repos': len(rows),
                      **{'total_' + key: sum(v.get(key, 0) for v in values) if ready else None
                         for key in ('commits', 'code_lines', 'doc_lines', 'other_lines')},
                      'top_languages': [{'name': k, 'lines': n} for k, n in sorted(languages.items(), key=lambda p: -p[1])[:10]]})
    period = request.args.get('period', 'month')
    if period not in ('month', 'quarter', 'half_year', 'year'):
        raise AccessError('不支持的统计周期')
    name = (request.view_args or {}).get('author_name')
    if endpoint in ('statistics.commit_trend', 'statistics.author_trend'):
        aggregate = defaultdict(lambda: {'authors': defaultdict(int), 'repos': set()})
        for value in values:
            for key, authors in value['periods'].items():
                kind, date = key.split(':', 1)
                if kind != period:
                    continue
                for author, count in authors.items():
                    if name is None or name == author:
                        aggregate[date]['authors'][author] += count
                        aggregate[date]['repos'].add(value['repo_id'])
        return reply([{'period_key': date, 'commit_count': sum(v['authors'].values()),
                       'commits': sum(v['authors'].values()), 'repo_count': len(v['repos']),
                       'author_count': len(v['authors']), 'top_authors': [{'name': a, 'count': n}
                            for a, n in sorted(v['authors'].items(), key=lambda p: -p[1])[:10]]}
                      for date, v in sorted(aggregate.items())])
    if endpoint == 'statistics.repo_ranking':
        key = request.args.get('sort_by', 'commit_count')
        if key not in ('commit_count', 'code_lines', 'doc_lines', 'doc_ratio'):
            key = 'commit_count'
        for row in values:
            row['doc_ratio'] = round(row.get('doc_lines', 0) / max(sum(row.get(k, 0) for k in ('code_lines', 'doc_lines', 'other_lines')), 1) * 100, 1)
        limit = min(5000, max(1, request.args.get('limit', 10, type=int)))
        return reply([{k: v for k, v in row.items() if k not in ('authors', 'periods')}
                      for row in sorted(values, key=lambda v: -v.get(key, 0))[:limit]])
    if endpoint == 'statistics.author_repos':
        result = [dict(v['authors'][name], repo_name=v['repo_name'], commit_count=v['authors'][name]['commits'],
                       repo_total_commits=v['commits'], contribution_pct=round(v['authors'][name]['commits'] / max(v['commits'], 1) * 100, 1))
                  for v in values if name in v['authors']]
        key = request.args.get('sort_by', 'commits')
        key = 'last_date' if key == 'recent' else 'additions' if key == 'additions' else 'commits'
        return reply(sorted(result, key=lambda v: v.get(key) or ('' if key == 'last_date' else 0), reverse=True))
    authors = {}
    for value in values:
        for author, source in value['authors'].items():
            target = authors.setdefault(author, {**source, 'commits': 0, 'additions': 0, 'deletions': 0, 'repo_count': 0})
            for key in ('commits', 'additions', 'deletions'):
                target[key] += source[key]
            target['repo_count'] += 1
            for key, operator in (('first_date', min), ('last_date', max)):
                dates = [d for d in (target[key], source[key]) if d]
                target[key] = operator(dates) if dates else None
    if name is not None:
        if name not in authors:
            raise AccessError('作者不存在或无权访问', 404)
        author = authors[name]
        return reply({**author, **{'total_' + k: author[k] for k in ('commits', 'additions', 'deletions')}})
    key = request.args.get('sort_by', 'commits')
    key = {'repos': 'repo_count'}.get(key, key)
    if key not in ('commits', 'additions', 'repo_count'):
        key = 'commits'
    limit = min(5000, max(1, request.args.get('limit', 20, type=int)))
    return reply(sorted(authors.values(), key=lambda v: -v[key])[:limit])
