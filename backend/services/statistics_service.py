import json
import logging
import requests
from datetime import datetime
from collections import defaultdict
from models import db, GiteaServer, RepoStatistics, CommitStatistics, AuthorStatistics


CODE_EXTENSIONS = {
    '.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.java', '.c', '.cpp', '.h', '.hpp',
    '.rs', '.rb', '.php', '.cs', '.swift', '.kt', '.scala', '.sh', '.bash', '.sql',
    '.vue', '.svelte', '.dart', '.lua', '.r', '.m', '.mm', '.pl', '.ex', '.exs',
    '.erl', '.clj', '.hs', '.ml', '.fs', '.v', '.sv', '.tcl', '.zig', '.nim',
    '.css', '.scss', '.less', '.sass', '.html', '.xml', '.yaml', '.yml', '.json',
    '.toml', '.ini', '.cfg', '.conf', '.cmake', '.makefile', '.dockerfile',
}

DOC_EXTENSIONS = {
    '.md', '.txt', '.rst', '.adoc', '.doc', '.docx', '.pdf', '.odt',
    '.tex', '.org', '.epub', '.htm',
}


def _ensure_url(url):
    if not url.startswith(('http://', 'https://')):
        url = 'http://' + url
    return url


def _get_all_repos(server):
    headers = {'Authorization': f'token {server.api_token}'}
    repos = []
    page = 1
    while True:
        try:
            resp = requests.get(
                f'{_ensure_url(server.gitea_url)}/api/v1/repos/search',
                headers=headers, params={'page': page, 'limit': 50}, timeout=30
            )
            if resp.status_code != 200:
                break
            data = resp.json()
            items = data.get('data', [])
            if not items:
                break
            repos.extend(items)
            if len(items) < 50:
                break
            page += 1
        except Exception:
            break
    return repos


def _get_repo_commits(server, owner, repo):
    headers = {'Authorization': f'token {server.api_token}'}
    commits = []
    page = 1
    while True:
        try:
            resp = requests.get(
                f'{_ensure_url(server.gitea_url)}/api/v1/repos/{owner}/{repo}/commits',
                headers=headers, params={'page': page, 'limit': 50, 'stat': 'true'}, timeout=30
            )
            if resp.status_code != 200:
                break
            data = resp.json()
            if not data:
                break
            commits.extend(data)
            if len(data) < 50:
                break
            page += 1
        except Exception:
            break
    return commits


def _get_repo_languages(server, owner, repo):
    headers = {'Authorization': f'token {server.api_token}'}
    try:
        resp = requests.get(
            f'{_ensure_url(server.gitea_url)}/api/v1/repos/{owner}/{repo}/languages',
            headers=headers, timeout=30
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {}


def _get_repo_tree(server, owner, repo, branch='master'):
    headers = {'Authorization': f'token {server.api_token}'}
    try:
        resp = requests.get(
            f'{_ensure_url(server.gitea_url)}/api/v1/repos/{owner}/{repo}/git/trees/{branch}?recursive=true',
            headers=headers, timeout=30
        )
        if resp.status_code == 200:
            return resp.json().get('tree', [])
    except Exception:
        pass
    return []


def _classify_file(filename):
    import os
    _, ext = os.path.splitext(filename.lower())
    if ext in CODE_EXTENSIONS:
        return 'code'
    if ext in DOC_EXTENSIONS:
        return 'doc'
    return 'other'


def _get_period_key(dt, period_type):
    if period_type == 'month':
        return dt.strftime('%Y-%m')
    elif period_type == 'quarter':
        q = (dt.month - 1) // 3 + 1
        return f'{dt.year}-Q{q}'
    elif period_type == 'half_year':
        h = 1 if dt.month <= 6 else 2
        return f'{dt.year}-H{h}'
    elif period_type == 'year':
        return dt.strftime('%Y')
    return dt.strftime('%Y-%m')


def _parse_commit_date(cdate_str):
    try:
        from datetime import datetime as _dt
        return _dt.fromisoformat(cdate_str.replace('Z', '+00:00')).replace(tzinfo=None)
    except Exception:
        return None


def collect_statistics(server_id):
    server = GiteaServer.query.get(server_id)
    if not server:
        return

    logging.info('[统计] 开始采集 - server=%s', server.name)
    repos = _get_all_repos(server)
    logging.info('[统计] 发现 %d 个仓库', len(repos))

    period_commits = defaultdict(lambda: {
        'commits': 0,
        'authors': set(),
        'repos': set(),
        'author_commits': defaultdict(int),
    })

    for repo_info in repos:
        full_name = repo_info.get('full_name', '')
        if not full_name:
            continue
        parts = full_name.split('/', 1)
        if len(parts) != 2:
            continue
        owner, repo_name = parts

        commits = _get_repo_commits(server, owner, repo_name)
        commit_count = len(commits)
        all_repo_commits[full_name] = commits

        last_sha = commits[0].get('sha', '') if commits else ''
        last_date = None
        if commits and commits[0].get('commit', {}).get('committer', {}).get('date'):
            last_date = _parse_commit_date(commits[0]['commit']['committer']['date'])

        author_data = defaultdict(lambda: {
            'email': '', 'additions': 0, 'deletions': 0, 'commit_count': 0,
            'first_date': None, 'last_date': None,
        })

        for c in commits:
            cdate_str = c.get('commit', {}).get('author', {}).get('date', '')
            author = c.get('commit', {}).get('author', {}).get('name', 'unknown')
            author_email = c.get('commit', {}).get('author', {}).get('email', '')
            stats = c.get('stats', {})
            additions = stats.get('additions', 0) or 0
            deletions = stats.get('deletions', 0) or 0

            if not author_data[author]['email'] and author_email:
                author_data[author]['email'] = author_email
            author_data[author]['additions'] += additions
            author_data[author]['deletions'] += deletions
            author_data[author]['commit_count'] += 1

            if cdate_str:
                cdate = _parse_commit_date(cdate_str)
                if cdate:
                    ad = author_data[author]
                    if ad['first_date'] is None or cdate < ad['first_date']:
                        ad['first_date'] = cdate
                    if ad['last_date'] is None or cdate > ad['last_date']:
                        ad['last_date'] = cdate

                    for pt in ('month', 'quarter', 'half_year', 'year'):
                        pk = _get_period_key(cdate, pt)
                        period_commits[(pt, pk)]['commits'] += 1
                        period_commits[(pt, pk)]['authors'].add(author)
                        period_commits[(pt, pk)]['repos'].add(full_name)
                        period_commits[(pt, pk)]['author_commits'][author] += 1

        for author_name, ad in author_data.items():
            existing_author = AuthorStatistics.query.filter_by(
                server_id=server_id, author_name=author_name, repo_name=full_name
            ).first()
            if existing_author:
                existing_author.author_email = ad['email']
                existing_author.commit_count = ad['commit_count']
                existing_author.additions = ad['additions']
                existing_author.deletions = ad['deletions']
                existing_author.first_commit_date = ad['first_date']
                existing_author.last_commit_date = ad['last_date']
                existing_author.snapshot_at = datetime.utcnow()
            else:
                db.session.add(AuthorStatistics(
                    server_id=server_id,
                    author_name=author_name,
                    author_email=ad['email'],
                    repo_name=full_name,
                    commit_count=ad['commit_count'],
                    additions=ad['additions'],
                    deletions=ad['deletions'],
                    first_commit_date=ad['first_date'],
                    last_commit_date=ad['last_date'],
                    snapshot_at=datetime.utcnow(),
                ))
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

        languages = _get_repo_languages(server, owner, repo_name)
        code_lines = 0
        doc_lines = 0
        other_lines = 0
        lang_breakdown = {}
        for lang, bytes_count in languages.items():
            estimated_lines = max(1, bytes_count // 50)
            lang_breakdown[lang] = estimated_lines
            lang_lower = lang.lower()
            if any(kw in lang_lower for kw in ['markdown', 'text', 'restructuredtext', 'html', 'css']):
                doc_lines += estimated_lines
            else:
                code_lines += estimated_lines

        default_branch = repo_info.get('default_branch', 'master') or 'master'
        tree = _get_repo_tree(server, owner, repo_name, default_branch)
        code_files = 0
        doc_files = 0
        other_files = 0
        for item in tree:
            if item.get('type') != 'blob':
                continue
            ftype = _classify_file(item.get('path', ''))
            if ftype == 'code':
                code_files += 1
            elif ftype == 'doc':
                doc_files += 1
            else:
                other_files += 1

        if not languages and tree:
            code_lines = code_files * 80
            doc_lines = doc_files * 30
            other_lines = other_files * 20

        existing = RepoStatistics.query.filter_by(server_id=server_id, repo_name=full_name).first()
        if existing:
            existing.commit_count = commit_count
            existing.code_lines = code_lines
            existing.doc_lines = doc_lines
            existing.other_lines = other_lines
            existing.code_files = code_files
            existing.doc_files = doc_files
            existing.other_files = other_files
            existing.language_breakdown = json.dumps(lang_breakdown)
            existing.last_commit_sha = last_sha
            existing.last_commit_date = last_date
            existing.snapshot_at = datetime.utcnow()
        else:
            db.session.add(RepoStatistics(
                server_id=server_id,
                repo_name=full_name,
                commit_count=commit_count,
                code_lines=code_lines,
                doc_lines=doc_lines,
                other_lines=other_lines,
                code_files=code_files,
                doc_files=doc_files,
                other_files=other_files,
                language_breakdown=json.dumps(lang_breakdown),
                last_commit_sha=last_sha,
                last_commit_date=last_date,
                snapshot_at=datetime.utcnow(),
            ))
        db.session.commit()

    for (pt, pk), data in period_commits.items():
        author_commits = data['author_commits']
        authors_sorted = sorted(author_commits.keys(), key=lambda a: -author_commits[a])
        top_authors = [{'name': a, 'count': author_commits[a]} for a in authors_sorted[:10]]

        existing = CommitStatistics.query.filter_by(
            server_id=server_id, period_type=pt, period_key=pk
        ).first()
        if existing:
            existing.commit_count = data['commits']
            existing.repo_count = len(data['repos'])
            existing.author_count = len(data['authors'])
            existing.top_authors = json.dumps(top_authors)
            existing.snapshot_at = datetime.utcnow()
        else:
            db.session.add(CommitStatistics(
                server_id=server_id,
                period_type=pt,
                period_key=pk,
                commit_count=data['commits'],
                repo_count=len(data['repos']),
                author_count=len(data['authors']),
                top_authors=json.dumps(top_authors),
                snapshot_at=datetime.utcnow(),
            ))
        db.session.commit()

    logging.info('[统计] 采集完成 - server=%s repos=%d', server.name, len(repos))


def get_overview(server_id):
    repo_stats = RepoStatistics.query.filter_by(server_id=server_id).all()
    total_commits = sum(r.commit_count for r in repo_stats)
    total_code_lines = sum(r.code_lines for r in repo_stats)
    total_doc_lines = sum(r.doc_lines for r in repo_stats)
    total_other_lines = sum(r.other_lines for r in repo_stats)
    total_repos = len(repo_stats)

    lang_agg = defaultdict(int)
    for r in repo_stats:
        try:
            lb = json.loads(r.language_breakdown)
            for lang, lines in lb.items():
                lang_agg[lang] += lines
        except Exception:
            pass

    top_langs = sorted(lang_agg.items(), key=lambda x: -x[1])[:10]
    return {
        'total_commits': total_commits,
        'total_code_lines': total_code_lines,
        'total_doc_lines': total_doc_lines,
        'total_other_lines': total_other_lines,
        'total_repos': total_repos,
        'top_languages': [{'name': k, 'lines': v} for k, v in top_langs],
    }


def get_commit_trend(server_id, period='month'):
    stats = CommitStatistics.query.filter_by(
        server_id=server_id, period_type=period
    ).order_by(CommitStatistics.period_key).all()
    return [{
        'period_key': s.period_key,
        'commit_count': s.commit_count,
        'repo_count': s.repo_count,
        'author_count': s.author_count,
        'top_authors': json.loads(s.top_authors) if s.top_authors else [],
    } for s in stats]


def get_repo_ranking(server_id, sort_by='commit_count', limit=10):
    repos = RepoStatistics.query.filter_by(server_id=server_id).all()

    if sort_by == 'code_lines':
        repos.sort(key=lambda r: r.code_lines, reverse=True)
    elif sort_by == 'doc_ratio':
        repos.sort(key=lambda r: (r.doc_lines / max(r.code_lines + r.doc_lines + r.other_lines, 1)), reverse=True)
    else:
        repos.sort(key=lambda r: r.commit_count, reverse=True)

    return [{
        'repo_name': r.repo_name,
        'commit_count': r.commit_count,
        'code_lines': r.code_lines,
        'doc_lines': r.doc_lines,
        'other_lines': r.other_lines,
        'code_files': r.code_files,
        'doc_files': r.doc_files,
        'doc_ratio': round(r.doc_lines / max(r.code_lines + r.doc_lines + r.other_lines, 1) * 100, 1),
    } for r in repos[:limit]]


def get_author_list(server_id, sort_by='commits', limit=50):
    authors = AuthorStatistics.query.filter_by(server_id=server_id).all()
    agg = defaultdict(lambda: {'commits': 0, 'additions': 0, 'deletions': 0, 'repos': set(), 'email': '', 'first_date': None, 'last_date': None})
    for a in authors:
        d = agg[a.author_name]
        d['commits'] += a.commit_count
        d['additions'] += a.additions
        d['deletions'] += a.deletions
        d['repos'].add(a.repo_name)
        if a.author_email and not d['email']:
            d['email'] = a.author_email
        if a.first_commit_date:
            if d['first_date'] is None or a.first_commit_date < d['first_date']:
                d['first_date'] = a.first_commit_date
        if a.last_commit_date:
            if d['last_date'] is None or a.last_commit_date > d['last_date']:
                d['last_date'] = a.last_commit_date

    result = []
    for name, d in agg.items():
        result.append({
            'name': name,
            'email': d['email'],
            'commits': d['commits'],
            'additions': d['additions'],
            'deletions': d['deletions'],
            'repo_count': len(d['repos']),
            'first_date': d['first_date'].isoformat() if d['first_date'] else None,
            'last_date': d['last_date'].isoformat() if d['last_date'] else None,
        })

    if sort_by == 'additions':
        result.sort(key=lambda x: -x['additions'])
    elif sort_by == 'repos':
        result.sort(key=lambda x: -x['repo_count'])
    else:
        result.sort(key=lambda x: -x['commits'])

    return result[:limit]


def get_author_detail(server_id, author_name):
    records = AuthorStatistics.query.filter_by(server_id=server_id, author_name=author_name).all()
    if not records:
        return None

    total_commits = sum(r.commit_count for r in records)
    total_additions = sum(r.additions for r in records)
    total_deletions = sum(r.deletions for r in records)
    repo_count = len(records)
    email = next((r.author_email for r in records if r.author_email), '')
    first_date = None
    last_date = None
    for r in records:
        if r.first_commit_date:
            if first_date is None or r.first_commit_date < first_date:
                first_date = r.first_commit_date
        if r.last_commit_date:
            if last_date is None or r.last_commit_date > last_date:
                last_date = r.last_commit_date

    return {
        'name': author_name,
        'email': email,
        'total_commits': total_commits,
        'total_additions': total_additions,
        'total_deletions': total_deletions,
        'repo_count': repo_count,
        'first_date': first_date.isoformat() if first_date else None,
        'last_date': last_date.isoformat() if last_date else None,
    }


def get_author_repos(server_id, author_name, sort_by='commits'):
    records = AuthorStatistics.query.filter_by(server_id=server_id, author_name=author_name).all()
    result = []
    for r in records:
        repo_stat = RepoStatistics.query.filter_by(server_id=server_id, repo_name=r.repo_name).first()
        repo_commits = repo_stat.commit_count if repo_stat else 0
        pct = round(r.commit_count / max(repo_commits, 1) * 100, 1)
        result.append({
            'repo_name': r.repo_name,
            'commits': r.commit_count,
            'additions': r.additions,
            'deletions': r.deletions,
            'repo_total_commits': repo_commits,
            'contribution_pct': pct,
            'last_date': r.last_commit_date.isoformat() if r.last_commit_date else None,
        })

    if sort_by == 'additions':
        result.sort(key=lambda x: -x['additions'])
    elif sort_by == 'recent':
        result.sort(key=lambda x: x['last_date'] or '', reverse=True)
    else:
        result.sort(key=lambda x: -x['commits'])
    return result


def get_author_trend(server_id, author_name, period='month'):
    records = AuthorStatistics.query.filter_by(server_id=server_id, author_name=author_name).all()
    if not records:
        return []

    repo_names = [r.repo_name for r in records]
    period_stats = CommitStatistics.query.filter_by(
        server_id=server_id, period_type=period
    ).order_by(CommitStatistics.period_key).all()

    result = []
    for ps in period_stats:
        author_commits_in_period = 0
        top_authors = json.loads(ps.top_authors) if ps.top_authors else []
        for a in top_authors:
            if a.get('name') == author_name:
                author_commits_in_period = a.get('count', 0)
                break
        if author_commits_in_period > 0:
            result.append({
                'period_key': ps.period_key,
                'commits': author_commits_in_period,
            })

    return result
