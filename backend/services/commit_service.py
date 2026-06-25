import hashlib
import json
import logging
import requests
import time
from datetime import datetime
from urllib.parse import quote
from models import db, BackupRepoCommit, RestoreVerification, GiteaServer, Backup, RestoreTask
from services.restore_progress import update_restore_progress


RECENT_COMMIT_LIMIT = 100
COMMIT_API_PAGE_SIZE = 50


RESTORE_HEALTH_CHECK_ATTEMPTS = 30
RESTORE_HEALTH_CHECK_INTERVAL_SECONDS = 2


class RestoreValidationError(Exception):
    def __init__(self, message, details=None, retryable=False):
        super().__init__(message)
        self.details = details or []
        self.retryable = retryable


def _ensure_url(url):
    if not url.startswith(('http://', 'https://')):
        url = 'http://' + url
    return url


def _compact_text(text, limit=500):
    text = str(text or '').strip()
    if len(text) <= limit:
        return text
    half = max(100, limit // 2)
    return text[:half] + '\n... <truncated> ...\n' + text[-half:]


def _api_detail(kind, path, message, repo='Gitea API', url=None, page=None):
    detail = {
        'type': kind,
        'repo': repo,
        'path': path,
        'message': message,
    }
    if page is not None:
        detail['page'] = page
    if url:
        detail['url'] = url
    return detail


def _api_get_json(server, path, params=None, timeout=30, repo='Gitea API', page=None):
    url = f'{_ensure_url(server.gitea_url)}{path}'
    headers = {'Authorization': f'token {server.api_token}'}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    except Exception as e:
        message = f'{url} 请求失败: {e}'
        raise RestoreValidationError(
            message,
            [_api_detail('api_unreachable', path, message, repo=repo, url=url, page=page)],
            retryable=True,
        )

    if resp.status_code != 200:
        body = _compact_text(getattr(resp, 'text', ''), 500)
        message = f'{url} 返回 HTTP {resp.status_code}'
        if body:
            message += f': {body}'
        raise RestoreValidationError(
            message,
            [_api_detail('api_status_error', path, message, repo=repo, url=url, page=page)],
            retryable=resp.status_code >= 500,
        )

    try:
        return resp.json()
    except Exception as e:
        message = f'{url} 返回内容不是有效 JSON: {e}'
        raise RestoreValidationError(
            message,
            [_api_detail('api_json_error', path, message, repo=repo, url=url, page=page)],
        )


def _require_target_health(server):
    last_error = None
    for attempt in range(1, RESTORE_HEALTH_CHECK_ATTEMPTS + 1):
        try:
            _api_get_json(server, '/api/v1/version', timeout=10)
            _api_get_json(server, '/api/v1/user', timeout=10)
            return
        except RestoreValidationError as e:
            last_error = e
            if not e.retryable or attempt >= RESTORE_HEALTH_CHECK_ATTEMPTS:
                break
            logging.info(
                '[恢复验证] Gitea API 尚未就绪，等待重试 (%d/%d): %s',
                attempt,
                RESTORE_HEALTH_CHECK_ATTEMPTS,
                e,
            )
            time.sleep(RESTORE_HEALTH_CHECK_INTERVAL_SECONDS)

    message = str(last_error) if last_error else 'Gitea API 健康检查失败'
    if last_error and last_error.retryable and RESTORE_HEALTH_CHECK_ATTEMPTS > 1:
        message = f'Gitea API 在 {RESTORE_HEALTH_CHECK_ATTEMPTS} 次重试后仍不可用: {message}'
    raise RestoreValidationError(
        message,
        last_error.details if last_error else [_api_detail('api_unreachable', '/api/v1/version', message)],
        retryable=getattr(last_error, 'retryable', False),
    )


def _get_all_repos(server):
    repos = []
    page = 1
    while True:
        data = _api_get_json(
            server,
            '/api/v1/repos/search',
            params={'page': page, 'limit': 50},
            timeout=30,
        )
        if not isinstance(data, dict):
            message = '/api/v1/repos/search 返回结构异常'
            raise RestoreValidationError(message, [_api_detail('api_schema_error', '/api/v1/repos/search', message)])
        items = data.get('data', [])
        if not isinstance(items, list):
            message = '/api/v1/repos/search 的 data 字段不是列表'
            raise RestoreValidationError(message, [_api_detail('api_schema_error', '/api/v1/repos/search', message)])
        if not items:
            break
        repos.extend(items)
        if len(items) < 50:
            break
        page += 1
    return repos


def _get_repo_commits(server, owner, repo, max_commits=RECENT_COMMIT_LIMIT):
    commits = []
    if max_commits <= 0:
        return commits

    page = 1
    owner_path = quote(owner, safe='')
    repo_path = quote(repo, safe='')
    path = f'/api/v1/repos/{owner_path}/{repo_path}/commits'
    while len(commits) < max_commits:
        remaining = max_commits - len(commits)
        page_size = min(COMMIT_API_PAGE_SIZE, remaining)
        data = _api_get_json(
            server,
            path,
            params={'page': page, 'limit': page_size},
            timeout=30,
            repo=f'{owner}/{repo}',
            page=page,
        )
        if not isinstance(data, list):
            message = f'{path} 返回结构异常'
            raise RestoreValidationError(
                message,
                [_api_detail('api_schema_error', path, message, f'{owner}/{repo}', page=page)],
            )
        if not data:
            break
        for c in data:
            sha = c.get('sha', '') if isinstance(c, dict) else ''
            if sha:
                commits.append(sha)
        if len(data) < page_size:
            break
        page += 1
    return commits


def _get_or_create_verification(task_id):
    verification = RestoreVerification.query.filter_by(restore_task_id=task_id).first()
    if not verification:
        verification = RestoreVerification(
            restore_task_id=task_id,
            status='running',
            created_at=datetime.utcnow(),
        )
        db.session.add(verification)
        db.session.commit()
    return verification


def _load_commit_ids(raw):
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    return [str(x) for x in data if x]


def _mark_restore_validation_failed(task, verification, message, details=None):
    details = details or [{
        'type': 'validation_failed',
        'repo': '恢复验证',
        'message': message,
    }]
    verification.status = 'failed'
    verification.mismatch_repos = len(details)
    verification.mismatch_details = json.dumps(details, ensure_ascii=False)
    verification.verified_at = datetime.utcnow()
    task.status = 'failed'
    task.error_msg = _compact_text(message, 2000)
    task.completed_at = datetime.utcnow()
    update_restore_progress(task, 'verify_failed', '恢复验证失败', 100, _compact_text(message, 500))
    db.session.commit()


def _mark_restore_validation_success(task, verification, matched):
    verification.status = 'success'
    verification.matched_repos = matched
    verification.mismatch_repos = 0
    verification.mismatch_details = '[]'
    verification.verified_at = datetime.utcnow()
    task.status = 'success'
    task.error_msg = ''
    task.completed_at = datetime.utcnow()
    update_restore_progress(task, 'completed', '恢复完成，健康检查和 Commit ID 验证通过', 100, '')
    db.session.commit()


def collect_backup_commits(backup_id, max_commits=RECENT_COMMIT_LIMIT):
    backup = Backup.query.get(backup_id)
    if not backup:
        raise RestoreValidationError('Backup not found', [{
            'type': 'missing_backup',
            'repo': 'Commit ID 快照',
            'message': 'Backup not found',
        }])
    server = GiteaServer.query.get(backup.source_server_id)
    if not server:
        raise RestoreValidationError('Source server not found', [{
            'type': 'missing_source',
            'repo': 'Commit ID 快照',
            'message': 'Source server not found',
        }])

    logging.info('[Commit采集] 开始 - backup_id=%d server=%s', backup_id, server.name)
    repos = _get_all_repos(server)
    logging.info('[Commit采集] 发现 %d 个仓库', len(repos))
    BackupRepoCommit.query.filter_by(backup_id=backup_id).delete()
    db.session.flush()
    saved_count = 0

    for repo_info in repos:
        full_name = repo_info.get('full_name', '')
        if not full_name:
            continue
        parts = full_name.split('/', 1)
        if len(parts) != 2:
            continue
        owner, repo_name = parts

        commits = _get_repo_commits(server, owner, repo_name, max_commits=max_commits)
        commit_count = len(commits)
        latest_sha = commits[0] if commits else ''

        sorted_commits = sorted(commits)
        hash_val = hashlib.sha256(','.join(sorted_commits).encode()).hexdigest()

        commit_ids_json = ''
        if commit_count <= 10000:
            commit_ids_json = json.dumps(sorted_commits)

        db.session.add(BackupRepoCommit(
            backup_id=backup_id,
            repo_name=full_name,
            commit_count=commit_count,
            latest_commit_sha=latest_sha,
            commit_ids_hash=hash_val,
            commit_ids=commit_ids_json,
            collected_at=datetime.utcnow(),
        ))
        saved_count += 1

    db.session.commit()
    logging.info('[Commit采集] 完成 - backup_id=%d repos=%d', backup_id, saved_count)
    return saved_count


def verify_restore(task_id):
    task = RestoreTask.query.get(task_id)
    if not task:
        return

    verification = _get_or_create_verification(task_id)
    verification.status = 'running'
    verification.verified_at = None
    verification.matched_repos = 0
    verification.mismatch_repos = 0
    verification.mismatch_details = '[]'
    db.session.commit()

    try:
        backup = Backup.query.get(task.backup_id)
        if not backup:
            raise RestoreValidationError('Backup not found', [{
                'type': 'missing_backup',
                'repo': '恢复验证',
                'message': 'Backup not found',
            }])

        snapshot_status = (backup.commit_snapshot_status or '').strip()
        expected_repo_count = backup.commit_snapshot_repo_count or 0
        if snapshot_status != 'success':
            raise RestoreValidationError('备份缺少 Commit ID 快照，无法验证恢复一致性', [{
                'type': 'missing_backup_commit_snapshot',
                'repo': '恢复验证',
                'message': '备份缺少 Commit ID 快照，无法验证恢复一致性',
                'commit_snapshot_status': snapshot_status,
                'commit_snapshot_error': backup.commit_snapshot_error or '',
            }])

        target = GiteaServer.query.get(task.target_server_id)
        if not target:
            raise RestoreValidationError('Target server not found', [{
                'type': 'missing_target',
                'repo': '恢复验证',
                'message': 'Target server not found',
            }])

        backup_commits = BackupRepoCommit.query.filter_by(backup_id=backup.id).all()
        backup_commit_map = {bc.repo_name: bc for bc in backup_commits}
        verification.total_repos = len(backup_commits)

        if expected_repo_count > 0 and len(backup_commits) != expected_repo_count:
            raise RestoreValidationError('备份 Commit ID 快照记录不完整，无法验证恢复一致性', [{
                'type': 'incomplete_backup_commit_snapshot',
                'repo': '恢复验证',
                'message': '备份 Commit ID 快照记录不完整，无法验证恢复一致性',
                'expected_repo_count': expected_repo_count,
                'actual_repo_count': len(backup_commits),
            }])

        update_restore_progress(task, 'verify_health', '正在检查 Gitea API 和 Token', 95, target.name)
        _require_target_health(target)

        update_restore_progress(task, 'verify_repos', '正在读取目标仓库列表', 96, target.name)
        target_repos = _get_all_repos(target)
        target_repo_map = {
            repo.get('full_name', ''): repo
            for repo in target_repos
            if isinstance(repo, dict) and repo.get('full_name')
        }

        mismatch_details = []
        matched = 0

        for repo_name in sorted(set(target_repo_map.keys()) - set(backup_commit_map.keys())):
            mismatch_details.append({
                'type': 'extra_repo',
                'repo': repo_name,
                'backup_commit_count': 0,
                'target_commit_count': None,
                'missing_count': 0,
                'extra_count': None,
                'missing_samples': [],
                'extra_samples': [],
                'message': '目标服务器存在备份快照中不存在的仓库',
            })

        update_restore_progress(task, 'verify_commits', '正在对比 Commit ID', 97, f'{len(backup_commits)} 个仓库')
        for bc in sorted(backup_commits, key=lambda item: item.repo_name):
            full_name = bc.repo_name
            parts = full_name.split('/', 1)
            if len(parts) != 2:
                mismatch_details.append({
                    'type': 'invalid_repo_name',
                    'repo': full_name,
                    'backup_commit_count': bc.commit_count,
                    'target_commit_count': None,
                    'missing_count': 0,
                    'extra_count': 0,
                    'missing_samples': [],
                    'extra_samples': [],
                    'message': '备份仓库名格式异常，无法验证',
                })
                continue

            if full_name not in target_repo_map:
                backup_ids = _load_commit_ids(bc.commit_ids) or []
                mismatch_details.append({
                    'type': 'missing_repo',
                    'repo': full_name,
                    'backup_commit_count': bc.commit_count,
                    'target_commit_count': 0,
                    'missing_count': bc.commit_count,
                    'extra_count': 0,
                    'missing_samples': backup_ids[:5],
                    'extra_samples': [],
                    'message': '目标服务器缺少备份中的仓库',
                })
                continue

            owner, repo_name = parts
            target_commits = _get_repo_commits(target, owner, repo_name)
            target_sorted = sorted(target_commits)
            target_hash = hashlib.sha256(','.join(target_sorted).encode()).hexdigest()

            if target_hash == bc.commit_ids_hash:
                matched += 1
                continue

            backup_ids = _load_commit_ids(bc.commit_ids)
            missing = []
            extra = []
            missing_count = 0
            extra_count = 0
            if backup_ids is not None:
                backup_set = set(backup_ids)
                target_set = set(target_sorted)
                missing = sorted(backup_set - target_set)
                extra = sorted(target_set - backup_set)
                missing_count = len(missing)
                extra_count = len(extra)

            mismatch_details.append({
                'type': 'commit_mismatch',
                'repo': full_name,
                'backup_commit_count': bc.commit_count,
                'target_commit_count': len(target_commits),
                'missing_count': missing_count,
                'extra_count': extra_count,
                'missing_samples': missing[:5],
                'extra_samples': extra[:5],
                'message': '目标仓库 Commit ID 集合与备份快照不一致',
            })

        verification.total_repos = len(backup_commits)
        verification.matched_repos = matched
        if mismatch_details:
            _mark_restore_validation_failed(
                task,
                verification,
                f'恢复验证失败：{len(mismatch_details)} 个仓库或 API 检查项不匹配',
                mismatch_details,
            )
        else:
            _mark_restore_validation_success(task, verification, matched)

        logging.info('[验证] 完成 - task_id=%d status=%s matched=%d mismatch=%d',
                     task_id, verification.status, matched, len(mismatch_details))
    except RestoreValidationError as e:
        _mark_restore_validation_failed(task, verification, str(e), e.details)
        logging.warning('[验证] 失败 - task_id=%d: %s', task_id, e)
    except Exception as e:
        _mark_restore_validation_failed(task, verification, str(e))
        logging.error('[验证] 异常 - task_id=%d: %s', task_id, e, exc_info=True)


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
