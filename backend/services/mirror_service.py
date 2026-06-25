import logging
from datetime import datetime
from urllib.parse import quote

import requests

from models import db, GiteaServer, MirrorConfig, MirrorRepoStatus


PUSH_MIRROR_MODE = 'push_mirror'
DEPRECATED_MIRROR_MODES = {'gitea_mirror', 'pull_mirror', 'git_clone'}


class MirrorServiceError(Exception):
    pass


def _ensure_url(url):
    if not url.startswith(('http://', 'https://')):
        url = 'http://' + url
    return url.rstrip('/')


def _is_deprecated_mode(mode):
    return mode in DEPRECATED_MIRROR_MODES or mode != PUSH_MIRROR_MODE


def is_deprecated_config(config):
    return bool(config and _is_deprecated_mode(config.sync_mode))


def _api_headers(server):
    return {'Authorization': f'token {server.api_token}'}


def _compact(text, limit=500):
    text = str(text or '').strip()
    if len(text) <= limit:
        return text
    half = max(100, limit // 2)
    return text[:half] + '\n... <truncated> ...\n' + text[-half:]


def _api_request(server, method, path, expected=(200,), timeout=30, **kwargs):
    url = f'{_ensure_url(server.gitea_url)}{path}'
    try:
        resp = requests.request(
            method,
            url,
            headers=_api_headers(server),
            timeout=timeout,
            **kwargs,
        )
    except Exception as e:
        raise MirrorServiceError(f'{method} {url} 请求失败: {e}')

    if resp.status_code not in expected:
        body = _compact(getattr(resp, 'text', ''), 1000)
        message = f'{method} {url} 返回 HTTP {resp.status_code}'
        if body:
            message += f': {body}'
        raise MirrorServiceError(message)

    if resp.status_code == 204:
        return {}
    try:
        return resp.json()
    except Exception:
        return {}


def _get_all_repos(server):
    repos = []
    page = 1
    while True:
        data = _api_request(
            server,
            'GET',
            '/api/v1/repos/search',
            params={'page': page, 'limit': 50},
            expected=(200,),
            timeout=30,
        )
        items = data.get('data', [])
        if not isinstance(items, list):
            raise MirrorServiceError('/api/v1/repos/search 返回结构异常')
        if not items:
            break
        repos.extend(items)
        if len(items) < 50:
            break
        page += 1
    return repos


def _get_token_login(server):
    data = _api_request(server, 'GET', '/api/v1/user', expected=(200,), timeout=10)
    login = data.get('login') or data.get('username') or data.get('name')
    if not login:
        raise MirrorServiceError(f'{server.name} API token 无法读取当前用户')
    return login


def _repo_path(owner, repo):
    return f'/api/v1/repos/{quote(owner, safe="")}/{quote(repo, safe="")}'


def _target_repo_url(target, full_name):
    return f'{_ensure_url(target.gitea_url)}/{full_name}.git'


def _ensure_org_exists(server, org_name):
    org_path = quote(org_name, safe='')
    try:
        _api_request(server, 'GET', f'/api/v1/orgs/{org_path}', expected=(200,), timeout=10)
        return
    except MirrorServiceError:
        pass

    _api_request(
        server,
        'POST',
        '/api/v1/orgs',
        json={'username': org_name, 'full_name': org_name},
        expected=(200, 201),
        timeout=10,
    )


def _ensure_target_repo(target, repo_info):
    full_name = repo_info.get('full_name', '')
    parts = full_name.split('/', 1)
    if len(parts) != 2:
        raise MirrorServiceError('Invalid repo name')
    owner, repo_name = parts

    try:
        repo = _api_request(target, 'GET', _repo_path(owner, repo_name), expected=(200,), timeout=15)
        return repo.get('id', 0)
    except MirrorServiceError as e:
        if 'HTTP 404' not in str(e):
            raise

    target_login = _get_token_login(target)
    payload = {
        'name': repo_name,
        'private': repo_info.get('private', False),
        'description': repo_info.get('description', '') or '',
        'auto_init': False,
    }
    if owner == target_login:
        repo = _api_request(target, 'POST', '/api/v1/user/repos', json=payload, expected=(200, 201), timeout=30)
    else:
        _ensure_org_exists(target, owner)
        repo = _api_request(
            target,
            'POST',
            f'/api/v1/org/{quote(owner, safe="")}/repos',
            json=payload,
            expected=(200, 201),
            timeout=30,
        )
    return repo.get('id', 0)


def _list_push_mirrors(source, owner, repo_name):
    data = _api_request(
        source,
        'GET',
        f'{_repo_path(owner, repo_name)}/push_mirrors',
        expected=(200,),
        timeout=30,
    )
    return data if isinstance(data, list) else []


def _find_existing_push_mirror(source, owner, repo_name, remote_address):
    try:
        for mirror in _list_push_mirrors(source, owner, repo_name):
            if (mirror.get('remote_address') or '').rstrip('/') == remote_address.rstrip('/'):
                return mirror
    except MirrorServiceError:
        return None
    return None


def _sync_push_mirror_repo(source, owner, repo_name):
    _api_request(
        source,
        'POST',
        f'{_repo_path(owner, repo_name)}/push_mirrors-sync',
        expected=(200, 201, 204),
        timeout=30,
    )


def _ensure_push_mirror(source, target, repo_info, config):
    full_name = repo_info.get('full_name', '')
    parts = full_name.split('/', 1)
    if len(parts) != 2:
        raise MirrorServiceError('Invalid repo name')
    owner, repo_name = parts

    target_repo_id = _ensure_target_repo(target, repo_info)
    remote_address = _target_repo_url(target, full_name)
    existing = _find_existing_push_mirror(source, owner, repo_name, remote_address)
    if existing:
        _sync_push_mirror_repo(source, owner, repo_name)
        return target_repo_id, existing.get('remote_name', '')

    payload = {
        'remote_address': remote_address,
        'remote_username': _get_token_login(target),
        'remote_password': target.api_token,
        'interval': f'{int(config.sync_interval or 30)}m',
        'sync_on_commit': bool(config.sync_on_commit),
    }
    mirror = _api_request(
        source,
        'POST',
        f'{_repo_path(owner, repo_name)}/push_mirrors',
        json=payload,
        expected=(200, 201),
        timeout=30,
    )
    _sync_push_mirror_repo(source, owner, repo_name)
    return target_repo_id, mirror.get('remote_name', '')


def setup_mirror(config_id):
    config = MirrorConfig.query.get(config_id)
    if not config:
        return

    if is_deprecated_config(config):
        config.status = 'failed'
        config.last_sync_status = 'failed'
        config.last_sync_log = '旧 pull mirror/git clone 镜像配置已弃用，请删除后重新创建 Push Mirror'
        db.session.commit()
        return

    source = GiteaServer.query.get(config.source_server_id)
    target = GiteaServer.query.get(config.target_server_id)
    if not source or not target:
        config.status = 'failed'
        config.last_sync_status = 'failed'
        config.last_sync_log = 'Source or target server not found'
        db.session.commit()
        return

    config.status = 'syncing'
    config.sync_mode = PUSH_MIRROR_MODE
    db.session.commit()

    synced = 0
    failed = 0
    logs = []

    try:
        repos = _get_all_repos(source)
    except Exception as e:
        config.status = 'failed'
        config.last_sync_status = 'failed'
        config.last_sync_log = str(e)[:1000]
        db.session.commit()
        return

    config.total_repos = len(repos)
    db.session.commit()

    for repo_info in repos:
        full_name = repo_info.get('full_name', '')
        if not full_name:
            continue

        status = MirrorRepoStatus.query.filter_by(
            mirror_config_id=config_id, repo_name=full_name
        ).first()
        if not status:
            status = MirrorRepoStatus(
                mirror_config_id=config_id,
                repo_name=full_name,
                source_repo_id=repo_info.get('id', 0),
                status='pending',
                sync_mode=PUSH_MIRROR_MODE,
                created_at=datetime.utcnow(),
            )
            db.session.add(status)
            db.session.commit()

        status.status = 'syncing'
        status.sync_mode = PUSH_MIRROR_MODE
        status.error_msg = ''
        db.session.commit()

        try:
            target_repo_id, remote_name = _ensure_push_mirror(source, target, repo_info, config)
            synced += 1
            status.status = 'success'
            status.target_repo_id = target_repo_id or status.target_repo_id
            status.remote_name = remote_name or status.remote_name
            status.last_sync_at = datetime.utcnow()
            logs.append(f'OK {full_name}')
        except Exception as e:
            failed += 1
            status.status = 'failed'
            status.error_msg = str(e)[:1000]
            error = str(e)
            logs.append(f'FAIL {full_name}: {_compact(error, 500)}')
            logging.warning(
                '[PushMirror] setup repo failed - config_id=%d repo=%s error=%s',
                config_id,
                full_name,
                error,
            )

        db.session.commit()

    config.synced_repos = synced
    config.failed_repos = failed
    config.status = 'success' if failed == 0 else 'partial'
    config.last_sync_at = datetime.utcnow()
    config.last_sync_status = 'success' if failed == 0 else 'failed'
    config.last_sync_log = '\n'.join(logs[-50:])
    db.session.commit()
    logging.info(
        '[PushMirror] setup complete - config_id=%d synced=%d failed=%d log=%s',
        config_id,
        synced,
        failed,
        _compact(config.last_sync_log, 2000),
    )


def sync_mirror(config_id):
    config = MirrorConfig.query.get(config_id)
    if not config:
        return

    if is_deprecated_config(config):
        config.status = 'failed'
        config.last_sync_status = 'failed'
        config.last_sync_log = '旧 pull mirror/git clone 镜像配置已弃用，请删除后重新创建 Push Mirror'
        db.session.commit()
        return

    source = GiteaServer.query.get(config.source_server_id)
    if not source:
        return

    config.status = 'syncing'
    db.session.commit()

    repos = MirrorRepoStatus.query.filter_by(mirror_config_id=config_id).all()
    synced = 0
    failed = 0
    logs = []

    for repo in repos:
        parts = repo.repo_name.split('/', 1)
        if len(parts) != 2:
            continue
        owner, repo_name = parts
        try:
            _sync_push_mirror_repo(source, owner, repo_name)
            synced += 1
            repo.status = 'success'
            repo.error_msg = ''
            repo.last_sync_at = datetime.utcnow()
            logs.append(f'OK {repo.repo_name}')
        except Exception as e:
            failed += 1
            repo.status = 'failed'
            repo.error_msg = str(e)[:1000]
            error = str(e)
            logs.append(f'FAIL {repo.repo_name}: {_compact(error, 500)}')
            logging.warning(
                '[PushMirror] sync repo failed - config_id=%d repo=%s error=%s',
                config_id,
                repo.repo_name,
                error,
            )
        db.session.commit()

    config.synced_repos = synced
    config.failed_repos = failed
    config.last_sync_at = datetime.utcnow()
    config.last_sync_status = 'success' if failed == 0 else 'failed'
    config.last_sync_log = '\n'.join(logs[-50:])
    config.status = 'success' if failed == 0 else 'partial'
    db.session.commit()


def sync_single_repo(config_id, repo_name):
    config = MirrorConfig.query.get(config_id)
    if not config:
        return False, 'Config not found'
    if is_deprecated_config(config):
        return False, '旧 pull mirror/git clone 镜像配置已弃用，请删除后重新创建 Push Mirror'

    source = GiteaServer.query.get(config.source_server_id)
    repo = MirrorRepoStatus.query.filter_by(mirror_config_id=config_id, repo_name=repo_name).first()
    if not source or not repo:
        return False, 'Repo not found'

    parts = repo_name.split('/', 1)
    if len(parts) != 2:
        return False, 'Invalid repo name'
    owner, repo_name_part = parts
    try:
        _sync_push_mirror_repo(source, owner, repo_name_part)
        repo.status = 'success'
        repo.error_msg = ''
        repo.last_sync_at = datetime.utcnow()
        db.session.commit()
        return True, ''
    except Exception as e:
        repo.status = 'failed'
        repo.error_msg = str(e)[:1000]
        db.session.commit()
        return False, str(e)[:500]


def _delete_push_mirror(source, repo, target):
    parts = repo.repo_name.split('/', 1)
    if len(parts) != 2:
        return ''
    owner, repo_name = parts
    remote_name = repo.remote_name
    if not remote_name:
        remote_address = _target_repo_url(target, repo.repo_name) if target else ''
        existing = _find_existing_push_mirror(source, owner, repo_name, remote_address) if remote_address else None
        remote_name = existing.get('remote_name', '') if existing else ''
    if not remote_name:
        return ''

    try:
        _api_request(
            source,
            'DELETE',
            f'{_repo_path(owner, repo_name)}/push_mirrors/{quote(remote_name, safe="")}',
            expected=(204,),
            timeout=30,
        )
        return ''
    except Exception as e:
        return f'{repo.repo_name}: {e}'


def delete_mirror_config(config_id):
    config = MirrorConfig.query.get(config_id)
    if not config:
        return {'ok': True, 'cleanup_errors': []}

    cleanup_errors = []
    source = GiteaServer.query.get(config.source_server_id)
    target = GiteaServer.query.get(config.target_server_id)
    repos = MirrorRepoStatus.query.filter_by(mirror_config_id=config_id).all()
    if source and config.sync_mode == PUSH_MIRROR_MODE:
        for repo in repos:
            err = _delete_push_mirror(source, repo, target)
            if err:
                cleanup_errors.append(err)
                logging.warning('[PushMirror] cleanup failed: %s', err)

    MirrorRepoStatus.query.filter_by(mirror_config_id=config_id).delete()
    db.session.delete(config)
    db.session.commit()
    return {'ok': True, 'cleanup_errors': cleanup_errors}


def get_mirror_status(config_id):
    repos = MirrorRepoStatus.query.filter_by(mirror_config_id=config_id).all()
    return [{
        'id': r.id,
        'repo_name': r.repo_name,
        'source_repo_id': r.source_repo_id,
        'target_repo_id': r.target_repo_id,
        'remote_name': r.remote_name,
        'status': r.status,
        'sync_mode': r.sync_mode,
        'last_sync_at': r.last_sync_at.isoformat() if r.last_sync_at else None,
        'error_msg': r.error_msg,
    } for r in repos]
