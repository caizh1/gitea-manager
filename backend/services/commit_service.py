import hashlib
import json
import logging
import requests
from datetime import datetime
from models import db, BackupRepoCommit, RestoreVerification, GiteaServer, Backup, RestoreTask


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
                headers=headers, params={'page': page, 'limit': 50}, timeout=30
            )
            if resp.status_code != 200:
                break
            data = resp.json()
            if not data:
                break
            for c in data:
                sha = c.get('sha', '')
                if sha:
                    commits.append(sha)
            if len(data) < 50:
                break
            page += 1
        except Exception:
            break
    return commits


def collect_backup_commits(backup_id):
    backup = Backup.query.get(backup_id)
    if not backup:
        return
    server = GiteaServer.query.get(backup.source_server_id)
    if not server:
        return

    logging.info('[Commit采集] 开始 - backup_id=%d server=%s', backup_id, server.name)
    repos = _get_all_repos(server)
    logging.info('[Commit采集] 发现 %d 个仓库', len(repos))

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
        latest_sha = commits[0] if commits else ''

        sorted_commits = sorted(commits)
        hash_val = hashlib.sha256(','.join(sorted_commits).encode()).hexdigest()

        commit_ids_json = ''
        if commit_count <= 10000:
            commit_ids_json = json.dumps(sorted_commits)

        existing = BackupRepoCommit.query.filter_by(
            backup_id=backup_id, repo_name=full_name
        ).first()
        if existing:
            existing.commit_count = commit_count
            existing.latest_commit_sha = latest_sha
            existing.commit_ids_hash = hash_val
            existing.commit_ids = commit_ids_json
            existing.collected_at = datetime.utcnow()
        else:
            db.session.add(BackupRepoCommit(
                backup_id=backup_id,
                repo_name=full_name,
                commit_count=commit_count,
                latest_commit_sha=latest_sha,
                commit_ids_hash=hash_val,
                commit_ids=commit_ids_json,
                collected_at=datetime.utcnow(),
            ))
        db.session.commit()

    logging.info('[Commit采集] 完成 - backup_id=%d', backup_id)


def verify_restore(task_id):
    task = RestoreTask.query.get(task_id)
    if not task:
        return

    verification = RestoreVerification.query.filter_by(restore_task_id=task_id).first()
    if not verification:
        verification = RestoreVerification(
            restore_task_id=task_id,
            status='running',
            created_at=datetime.utcnow(),
        )
        db.session.add(verification)
        db.session.commit()

    backup = Backup.query.get(task.backup_id)
    if not backup:
        verification.status = 'failed'
        verification.verified_at = datetime.utcnow()
        db.session.commit()
        return

    backup_commits = BackupRepoCommit.query.filter_by(backup_id=backup.id).all()
    backup_commit_map = {bc.repo_name: bc for bc in backup_commits}

    target = GiteaServer.query.get(task.target_server_id)
    if not target:
        verification.status = 'failed'
        verification.verified_at = datetime.utcnow()
        db.session.commit()
        return

    target_repos = _get_all_repos(target)
    verification.total_repos = len(backup_commits)
    matched = 0
    mismatch_details = []

    for target_repo in target_repos:
        full_name = target_repo.get('full_name', '')
        if not full_name:
            continue

        bc = backup_commit_map.get(full_name)
        if not bc:
            continue

        parts = full_name.split('/', 1)
        if len(parts) != 2:
            continue
        owner, repo_name = parts

        target_commits = _get_repo_commits(target, owner, repo_name)
        target_sorted = sorted(target_commits)
        target_hash = hashlib.sha256(','.join(target_sorted).encode()).hexdigest()

        if target_hash == bc.commit_ids_hash:
            matched += 1
        else:
            backup_count = bc.commit_count
            target_count = len(target_commits)
            missing = []
            extra = []
            if bc.commit_ids:
                backup_set = set(json.loads(bc.commit_ids))
                target_set = set(target_sorted)
                missing = list(backup_set - target_set)[:10]
                extra = list(target_set - backup_set)[:10]
            mismatch_details.append({
                'repo': full_name,
                'backup_commit_count': backup_count,
                'target_commit_count': target_count,
                'missing_count': len(missing),
                'extra_count': len(extra),
                'missing_samples': missing[:5],
                'extra_samples': extra[:5],
            })

    verification.matched_repos = matched
    verification.mismatch_repos = len(mismatch_details)
    verification.mismatch_details = json.dumps(mismatch_details, ensure_ascii=False)
    verification.status = 'success' if len(mismatch_details) == 0 else 'failed'
    verification.verified_at = datetime.utcnow()
    db.session.commit()
    logging.info('[验证] 完成 - task_id=%d status=%s matched=%d mismatch=%d',
                 task_id, verification.status, matched, len(mismatch_details))


def get_verification(task_id):
    v = RestoreVerification.query.filter_by(restore_task_id=task_id).first()
    if not v:
        return None
    return {
        'id': v.id,
        'restore_task_id': v.restore_task_id,
        'status': v.status,
        'total_repos': v.total_repos,
        'matched_repos': v.matched_repos,
        'mismatch_repos': v.mismatch_repos,
        'mismatch_details': json.loads(v.mismatch_details) if v.mismatch_details else [],
        'verified_at': v.verified_at.isoformat() if v.verified_at else None,
        'created_at': v.created_at.isoformat() if v.created_at else None,
    }


def get_backup_commits(backup_id):
    commits = BackupRepoCommit.query.filter_by(backup_id=backup_id).all()
    return [{
        'id': c.id,
        'repo_name': c.repo_name,
        'commit_count': c.commit_count,
        'latest_commit_sha': c.latest_commit_sha,
        'commit_ids_hash': c.commit_ids_hash,
        'collected_at': c.collected_at.isoformat() if c.collected_at else None,
    } for c in commits]
