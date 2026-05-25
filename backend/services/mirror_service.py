import json
import logging
import requests
from datetime import datetime
from models import db, GiteaServer, MirrorConfig, MirrorRepoStatus


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


def _get_target_orgs(server):
    headers = {'Authorization': f'token {server.api_token}'}
    orgs = []
    page = 1
    while True:
        try:
            resp = requests.get(
                f'{_ensure_url(server.gitea_url)}/api/v1/admin/orgs',
                headers=headers, params={'page': page, 'limit': 50}, timeout=30
            )
            if resp.status_code != 200:
                break
            data = resp.json()
            if not data:
                break
            orgs.extend(data)
            if len(data) < 50:
                break
            page += 1
        except Exception:
            break
    return orgs


def _ensure_org_exists(server, org_name):
    headers = {'Authorization': f'token {server.api_token}'}
    try:
        resp = requests.get(
            f'{_ensure_url(server.gitea_url)}/api/v1/orgs/{org_name}',
            headers=headers, timeout=10
        )
        if resp.status_code == 200:
            return True
        resp = requests.post(
            f'{_ensure_url(server.gitea_url)}/api/v1/orgs',
            headers=headers,
            json={'username': org_name, 'full_name': org_name},
            timeout=10
        )
        return resp.status_code in (201, 200)
    except Exception:
        return False


def _migrate_repo(source, target, repo_info):
    headers = {'Authorization': f'token {target.api_token}'}
    full_name = repo_info.get('full_name', '')
    parts = full_name.split('/', 1)
    if len(parts) != 2:
        return False, 'Invalid repo name'
    owner, repo_name = parts

    clone_url = repo_info.get('clone_url', '') or repo_info.get('html_url', '')
    if not clone_url:
        clone_url = f'{_ensure_url(source.gitea_url)}/{full_name}.git'

    _ensure_org_exists(target, owner)

    payload = {
        'auth_token': source.api_token,
        'clone_addr': clone_url,
        'mirror': True,
        'mirror_interval': '30m',
        'repo_name': repo_name,
        'repo_owner': owner,
        'service': 'gitea',
        'private': repo_info.get('private', False),
    }

    try:
        resp = requests.post(
            f'{_ensure_url(target.gitea_url)}/api/v1/repos/migrate',
            headers=headers, json=payload, timeout=60
        )
        if resp.status_code in (201, 200):
            return True, ''
        return False, f'Gitea API returned {resp.status_code}: {resp.text[:200]}'
    except Exception as e:
        return False, str(e)[:200]


def _git_clone_mirror(source, target, repo_info):
    from services.ssh_service import SSHService
    from services.docker_service import local_exec

    full_name = repo_info.get('full_name', '')
    parts = full_name.split('/', 1)
    if len(parts) != 2:
        return False, 'Invalid repo name'
    owner, repo_name = parts

    source_url = f'{_ensure_url(source.gitea_url)}/{full_name}.git'
    target_url = f'{_ensure_url(target.gitea_url)}/{full_name}.git'

    if target.is_local:
        try:
            cmd = f'git clone --mirror {source_url} /tmp/{repo_name}.git 2>&1'
            ec, out = local_exec('gitea', cmd)
            if ec != 0:
                return False, f'git clone failed: {out[:200]}'

            push_url = target_url.replace('http://', f'http://{target.api_token}@').replace('https://', f'https://{target.api_token}@')
            cmd2 = f'cd /tmp/{repo_name}.git && git remote set-url --push origin {push_url} && git push --mirror 2>&1'
            ec2, out2 = local_exec('gitea', cmd2)
            local_exec('gitea', f'rm -rf /tmp/{repo_name}.git')
            if ec2 != 0:
                return False, f'git push failed: {out2[:200]}'
            return True, ''
        except Exception as e:
            return False, str(e)[:200]
    else:
        try:
            ssh = SSHService(target.host, target.ssh_port, target.ssh_user)
            ec, out, err = ssh.exec(f'git clone --mirror {source_url} /tmp/{repo_name}.git')
            if ec != 0:
                return False, f'git clone failed: {out[:200]}'

            push_url = target_url.replace('http://', f'http://{target.api_token}@').replace('https://', f'https://{target.api_token}@')
            ec2, out2, err2 = ssh.exec(f'cd /tmp/{repo_name}.git && git remote set-url --push origin {push_url} && git push --mirror')
            ssh.exec(f'rm -rf /tmp/{repo_name}.git')
            if ec2 != 0:
                return False, f'git push failed: {out2[:200]}'
            return True, ''
        except Exception as e:
            return False, str(e)[:200]


def setup_mirror(config_id):
    config = MirrorConfig.query.get(config_id)
    if not config:
        return

    source = GiteaServer.query.get(config.source_server_id)
    target = GiteaServer.query.get(config.target_server_id)
    if not source or not target:
        config.status = 'failed'
        config.last_sync_log = 'Source or target server not found'
        db.session.commit()
        return

    config.status = 'syncing'
    db.session.commit()

    repos = _get_all_repos(source)
    config.total_repos = len(repos)
    synced = 0
    failed = 0
    logs = []

    for repo_info in repos:
        full_name = repo_info.get('full_name', '')
        if not full_name:
            continue

        existing = MirrorRepoStatus.query.filter_by(
            mirror_config_id=config_id, repo_name=full_name
        ).first()
        if not existing:
            existing = MirrorRepoStatus(
                mirror_config_id=config_id,
                repo_name=full_name,
                source_repo_id=repo_info.get('id', 0),
                status='pending',
                sync_mode=config.sync_mode,
                created_at=datetime.utcnow(),
            )
            db.session.add(existing)
            db.session.commit()

        existing.status = 'syncing'
        db.session.commit()

        success = False
        error_msg = ''

        if config.sync_mode == 'gitea_mirror':
            success, error_msg = _migrate_repo(source, target, repo_info)
            if not success:
                logging.warning('[Mirror] Gitea mirror failed for %s, trying git clone: %s', full_name, error_msg)
                success, error_msg = _git_clone_mirror(source, target, repo_info)
                if success:
                    existing.sync_mode = 'git_clone'
        else:
            success, error_msg = _git_clone_mirror(source, target, repo_info)

        if success:
            synced += 1
            existing.status = 'success'
            existing.last_sync_at = datetime.utcnow()
            logs.append(f'✅ {full_name}')
        else:
            failed += 1
            existing.status = 'failed'
            existing.error_msg = error_msg
            logs.append(f'❌ {full_name}: {error_msg[:80]}')

        db.session.commit()

    config.synced_repos = synced
    config.failed_repos = failed
    config.status = 'success' if failed == 0 else 'partial'
    config.last_sync_at = datetime.utcnow()
    config.last_sync_status = 'success' if failed == 0 else 'failed'
    config.last_sync_log = '\n'.join(logs[-50:])
    db.session.commit()
    logging.info('[Mirror] Setup complete - config_id=%d synced=%d failed=%d', config_id, synced, failed)


def sync_mirror(config_id):
    config = MirrorConfig.query.get(config_id)
    if not config:
        return

    source = GiteaServer.query.get(config.source_server_id)
    target = GiteaServer.query.get(config.target_server_id)
    if not source or not target:
        return

    config.status = 'syncing'
    db.session.commit()

    headers = {'Authorization': f'token {target.api_token}'}
    repos = MirrorRepoStatus.query.filter_by(mirror_config_id=config_id).all()
    synced = 0
    failed = 0

    for repo in repos:
        parts = repo.repo_name.split('/', 1)
        if len(parts) != 2:
            continue
        owner, repo_name = parts

        if repo.sync_mode == 'gitea_mirror' or not repo.sync_mode:
            try:
                resp = requests.post(
                    f'{_ensure_url(target.gitea_url)}/api/v1/repos/{owner}/{repo_name}/mirror-sync',
                    headers=headers, timeout=30
                )
                if resp.status_code in (200, 201):
                    synced += 1
                    repo.status = 'success'
                    repo.last_sync_at = datetime.utcnow()
                else:
                    failed += 1
                    repo.status = 'failed'
                    repo.error_msg = f'mirror-sync returned {resp.status_code}'
            except Exception as e:
                failed += 1
                repo.status = 'failed'
                repo.error_msg = str(e)[:200]
        else:
            repo_info = {'full_name': repo.repo_name, 'id': repo.source_repo_id}
            success, error_msg = _git_clone_mirror(source, target, repo_info)
            if success:
                synced += 1
                repo.status = 'success'
                repo.last_sync_at = datetime.utcnow()
            else:
                failed += 1
                repo.status = 'failed'
                repo.error_msg = error_msg

        db.session.commit()

    config.synced_repos = synced
    config.failed_repos = failed
    config.last_sync_at = datetime.utcnow()
    config.last_sync_status = 'success' if failed == 0 else 'failed'
    config.status = 'success' if failed == 0 else 'partial'
    db.session.commit()


def sync_single_repo(config_id, repo_name):
    config = MirrorConfig.query.get(config_id)
    if not config:
        return False, 'Config not found'

    source = GiteaServer.query.get(config.source_server_id)
    target = GiteaServer.query.get(config.target_server_id)
    repo = MirrorRepoStatus.query.filter_by(mirror_config_id=config_id, repo_name=repo_name).first()
    if not repo:
        return False, 'Repo not found'

    parts = repo_name.split('/', 1)
    if len(parts) != 2:
        return False, 'Invalid repo name'
    owner, repo_name_part = parts

    if repo.sync_mode == 'gitea_mirror' or not repo.sync_mode:
        headers = {'Authorization': f'token {target.api_token}'}
        try:
            resp = requests.post(
                f'{_ensure_url(target.gitea_url)}/api/v1/repos/{owner}/{repo_name_part}/mirror-sync',
                headers=headers, timeout=30
            )
            if resp.status_code in (200, 201):
                repo.status = 'success'
                repo.last_sync_at = datetime.utcnow()
                db.session.commit()
                return True, ''
            return False, f'mirror-sync returned {resp.status_code}'
        except Exception as e:
            return False, str(e)[:200]
    else:
        repo_info = {'full_name': repo.repo_name, 'id': repo.source_repo_id}
        success, error_msg = _git_clone_mirror(source, target, repo_info)
        if success:
            repo.status = 'success'
            repo.last_sync_at = datetime.utcnow()
            db.session.commit()
        return success, error_msg


def get_mirror_status(config_id):
    repos = MirrorRepoStatus.query.filter_by(mirror_config_id=config_id).all()
    return [{
        'id': r.id,
        'repo_name': r.repo_name,
        'source_repo_id': r.source_repo_id,
        'target_repo_id': r.target_repo_id,
        'status': r.status,
        'sync_mode': r.sync_mode,
        'last_sync_at': r.last_sync_at.isoformat() if r.last_sync_at else None,
        'error_msg': r.error_msg,
    } for r in repos]
