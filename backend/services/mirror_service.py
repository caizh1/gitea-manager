import logging
from datetime import datetime
from urllib.parse import quote

import requests

from models import db, GiteaServer, MirrorAuditLog, MirrorConfig, MirrorRepoStatus
from services.mirror_progress import update_mirror_progress


PUSH_MIRROR_MODE = 'push_mirror'
DEPRECATED_MIRROR_MODES = {'gitea_mirror', 'pull_mirror', 'git_clone'}
MISSING_SOURCE_STATUS = 'missing_source'


class MirrorServiceError(Exception):
    pass


def _stage_percent(start, end, index, total):
    total = max(int(total or 0), 1)
    index = max(0, min(int(index or 0), total))
    return int(round(start + (end - start) * index / total))


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


def _audit(config_id, repo_name, action, reason, status='success', detail=''):
    db.session.add(MirrorAuditLog(
        mirror_config_id=config_id,
        repo_name=(repo_name or '')[:300],
        action=action,
        reason=_compact(reason, 1000),
        status=status,
        detail=_compact(detail, 2000),
        created_at=datetime.utcnow(),
    ))


def _split_repo_name(full_name):
    parts = (full_name or '').split('/', 1)
    if len(parts) != 2:
        raise MirrorServiceError('Invalid repo name')
    return parts[0], parts[1]


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


def _get_repo_or_none(server, owner, repo_name):
    try:
        return _api_request(server, 'GET', _repo_path(owner, repo_name), expected=(200,), timeout=15)
    except MirrorServiceError as e:
        if 'HTTP 404' in str(e):
            return None
        raise


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


def _rename_target_repo_if_needed(target, old_full_name, new_full_name, config_id):
    if not old_full_name or old_full_name == new_full_name:
        return None

    old_owner, old_repo = _split_repo_name(old_full_name)
    new_owner, new_repo = _split_repo_name(new_full_name)
    if old_owner != new_owner:
        _audit(
            config_id,
            new_full_name,
            'rename_target_repo',
            '源仓库 owner 发生变化，gitea-manager 不自动迁移目标仓库 owner',
            'skipped',
            f'old={old_full_name}; new={new_full_name}',
        )
        return None

    existing_new = _get_repo_or_none(target, new_owner, new_repo)
    if existing_new:
        _audit(
            config_id,
            new_full_name,
            'rename_target_repo',
            '源仓库已改名，目标新仓库已存在，直接复用',
            'skipped',
            f'old={old_full_name}; new={new_full_name}; target_repo_id={existing_new.get("id", 0)}',
        )
        return existing_new.get('id', 0)

    try:
        repo = _api_request(
            target,
            'PATCH',
            _repo_path(old_owner, old_repo),
            json={'name': new_repo},
            expected=(200,),
            timeout=30,
        )
        _audit(
            config_id,
            new_full_name,
            'rename_target_repo',
            '源仓库 source_repo_id 未变但 full_name 已变化，目标仓库跟随改名',
            'success',
            f'old={old_full_name}; new={new_full_name}; target_repo_id={repo.get("id", 0)}',
        )
        return repo.get('id', 0)
    except MirrorServiceError as e:
        if 'HTTP 404' in str(e):
            _audit(
                config_id,
                new_full_name,
                'rename_target_repo',
                '源仓库已改名，但目标旧仓库不存在，改为确保新目标仓库存在',
                'skipped',
                f'old={old_full_name}; new={new_full_name}',
            )
            return None
        _audit(
            config_id,
            new_full_name,
            'rename_target_repo',
            '源仓库已改名，尝试目标仓库跟随改名失败',
            'failed',
            str(e),
        )
        raise


def _ensure_target_repo(target, repo_info, config_id=0, old_full_name=None):
    full_name = repo_info.get('full_name', '')
    owner, repo_name = _split_repo_name(full_name)

    renamed_repo_id = _rename_target_repo_if_needed(target, old_full_name, full_name, config_id)
    if renamed_repo_id:
        return renamed_repo_id

    repo = _get_repo_or_none(target, owner, repo_name)
    if repo:
        return repo.get('id', 0)

    target_login = _get_token_login(target)
    payload = {
        'name': repo_name,
        'private': repo_info.get('private', False),
        'description': repo_info.get('description', '') or '',
        'auto_init': False,
    }
    try:
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
        _audit(
            config_id,
            full_name,
            'create_target_repo',
            '源仓库存在但目标仓库不存在，刷新修复需要创建目标仓库',
            'success',
            f'target_repo_id={repo.get("id", 0)}; repo={full_name}',
        )
    except Exception as e:
        _audit(
            config_id,
            full_name,
            'create_target_repo',
            '源仓库存在但目标仓库不存在，创建目标仓库失败',
            'failed',
            str(e),
        )
        raise
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
        expected=(200, 201, 202, 204),
        timeout=30,
    )


def _delete_push_mirror_remote(source, owner, repo_name, remote_name):
    _api_request(
        source,
        'DELETE',
        f'{_repo_path(owner, repo_name)}/push_mirrors/{quote(remote_name, safe="")}',
        expected=(204,),
        timeout=30,
    )


def _cleanup_stale_push_mirrors(source, target, owner, repo_name, old_full_name, remote_name, config_id, full_name):
    if not old_full_name and not remote_name:
        return

    old_remote_address = _target_repo_url(target, old_full_name) if old_full_name else ''
    try:
        mirrors = _list_push_mirrors(source, owner, repo_name)
    except MirrorServiceError as e:
        _audit(
            config_id,
            full_name,
            'update_push_mirror',
            '刷新修复需要检查旧 push mirror remote，但读取源仓库 mirror 列表失败',
            'failed',
            str(e),
        )
        raise

    for mirror in mirrors:
        rn = mirror.get('remote_name') or ''
        address = (mirror.get('remote_address') or '').rstrip('/')
        should_delete = (remote_name and rn == remote_name) or (
            old_remote_address and address == old_remote_address.rstrip('/')
        )
        if not should_delete:
            continue
        try:
            _delete_push_mirror_remote(source, owner, repo_name, rn)
            _audit(
                config_id,
                full_name,
                'update_push_mirror',
                '源仓库改名后旧 push mirror remote 指向旧目标仓库，先删除再重建',
                'success',
                f'remote_name={rn}; old_remote_address={old_remote_address}; repo={full_name}',
            )
        except Exception as e:
            _audit(
                config_id,
                full_name,
                'update_push_mirror',
                '源仓库改名后清理旧 push mirror remote 失败',
                'failed',
                str(e),
            )
            raise


def _ensure_push_mirror(source, target, repo_info, config, old_full_name=None, known_remote_name=''):
    full_name = repo_info.get('full_name', '')
    owner, repo_name = _split_repo_name(full_name)

    target_repo_id = _ensure_target_repo(target, repo_info, config.id, old_full_name)
    remote_address = _target_repo_url(target, full_name)
    existing = _find_existing_push_mirror(source, owner, repo_name, remote_address)
    if existing:
        try:
            _sync_push_mirror_repo(source, owner, repo_name)
            _audit(
                config.id,
                full_name,
                'sync_push_mirror',
                'push mirror 已存在，刷新修复后触发同步',
                'success',
                f'remote_name={existing.get("remote_name", "")}; repo={full_name}',
            )
        except Exception as e:
            _audit(
                config.id,
                full_name,
                'sync_push_mirror',
                'push mirror 已存在，但触发同步失败',
                'failed',
                str(e),
            )
            raise
        return target_repo_id, existing.get('remote_name', '')

    _cleanup_stale_push_mirrors(
        source,
        target,
        owner,
        repo_name,
        old_full_name,
        known_remote_name,
        config.id,
        full_name,
    )

    payload = {
        'remote_address': remote_address,
        'remote_username': _get_token_login(target),
        'remote_password': target.api_token,
        'interval': f'{int(config.sync_interval or 30)}m',
        'sync_on_commit': bool(config.sync_on_commit),
    }
    try:
        mirror = _api_request(
            source,
            'POST',
            f'{_repo_path(owner, repo_name)}/push_mirrors',
            json=payload,
            expected=(200, 201),
            timeout=30,
        )
        _audit(
            config.id,
            full_name,
            'create_push_mirror',
            '源仓库存在但没有指向当前目标仓库的 push mirror，需要创建',
            'success',
            f'remote_name={mirror.get("remote_name", "")}; remote_address={remote_address}',
        )
    except Exception as e:
        _audit(
            config.id,
            full_name,
            'create_push_mirror',
            '源仓库存在但创建 push mirror 失败',
            'failed',
            str(e),
        )
        raise

    try:
        _sync_push_mirror_repo(source, owner, repo_name)
        _audit(
            config.id,
            full_name,
            'sync_push_mirror',
            'push mirror 创建或修复完成后触发同步',
            'success',
            f'remote_name={mirror.get("remote_name", "")}; repo={full_name}',
        )
    except Exception as e:
        _audit(
            config.id,
            full_name,
            'sync_push_mirror',
            'push mirror 创建或修复完成后触发同步失败',
            'failed',
            str(e),
        )
        raise
    return target_repo_id, mirror.get('remote_name', '')


def _mirror_counts(config_id):
    repos = MirrorRepoStatus.query.filter_by(mirror_config_id=config_id).all()
    total = len(repos)
    synced = sum(1 for r in repos if r.status == 'success')
    failed = sum(1 for r in repos if r.status in {'failed', MISSING_SOURCE_STATUS})
    return total, synced, failed


def setup_mirror(config_id):
    config = MirrorConfig.query.get(config_id)
    if not config:
        return

    if is_deprecated_config(config):
        config.status = 'failed'
        config.last_sync_status = 'failed'
        config.last_sync_log = '旧 pull mirror/git clone 镜像配置已弃用，请删除后重新创建 Push Mirror'
        update_mirror_progress(config, 'failed', '刷新/修复失败', 100, config.last_sync_log, reset_current=True)
        return

    source = GiteaServer.query.get(config.source_server_id)
    target = GiteaServer.query.get(config.target_server_id)
    if not source or not target:
        config.status = 'failed'
        config.last_sync_status = 'failed'
        config.last_sync_log = 'Source or target server not found'
        update_mirror_progress(config, 'failed', '刷新/修复失败', 100, config.last_sync_log, reset_current=True)
        return

    config.status = 'syncing'
    config.sync_mode = PUSH_MIRROR_MODE
    update_mirror_progress(config, 'prepare', '正在准备刷新/修复镜像配置', 3, '', reset_current=True)

    logs = []

    try:
        update_mirror_progress(config, 'scan_source', '正在读取源 Gitea 仓库列表', 8, source.name)
        repos = _get_all_repos(source)
    except Exception as e:
        config.status = 'failed'
        config.last_sync_status = 'failed'
        config.last_sync_log = str(e)[:1000]
        update_mirror_progress(config, 'failed', '刷新/修复失败', 100, config.last_sync_log, reset_current=True)
        return

    config.total_repos = len(repos)
    update_mirror_progress(config, 'scan_source', '源仓库列表读取完成', 10, f'共发现 {len(repos)} 个仓库')

    existing_statuses = MirrorRepoStatus.query.filter_by(mirror_config_id=config_id).all()
    by_source_id = {
        s.source_repo_id: s
        for s in existing_statuses
        if s.source_repo_id
    }
    by_name = {s.repo_name: s for s in existing_statuses}
    processed_ids = set()
    repo_total = len(repos)

    for index, repo_info in enumerate(repos, start=1):
        full_name = repo_info.get('full_name', '')
        if not full_name:
            continue
        update_mirror_progress(
            config,
            'reconcile_repo',
            f'正在修复镜像 {index}/{repo_total}',
            _stage_percent(10, 90, index - 1, repo_total),
            f'{full_name} - 准备处理',
            current_repo_name=full_name,
            current_repo_index=index,
            current_repo_total=repo_total,
        )
        source_repo_id = repo_info.get('id', 0) or 0
        old_full_name = ''

        status = by_source_id.get(source_repo_id) if source_repo_id else None
        if status:
            old_full_name = status.repo_name
            if status.repo_name != full_name:
                _audit(
                    config_id,
                    full_name,
                    'discover_repo',
                    '源仓库 source_repo_id 未变但 full_name 已变化，判定为仓库改名',
                    'success',
                    f'source_repo_id={source_repo_id}; old={status.repo_name}; new={full_name}',
                )
                status.repo_name = full_name
        else:
            status = by_name.get(full_name)
            if status and source_repo_id and not status.source_repo_id:
                status.source_repo_id = source_repo_id
                _audit(
                    config_id,
                    full_name,
                    'discover_repo',
                    '旧镜像记录缺少 source_repo_id，按相同仓库路径回填源仓库 ID',
                    'success',
                    f'source_repo_id={source_repo_id}; repo={full_name}',
                )

        if not status:
            status = MirrorRepoStatus(
                mirror_config_id=config_id,
                repo_name=full_name,
                source_repo_id=source_repo_id,
                status='pending',
                sync_mode=PUSH_MIRROR_MODE,
                created_at=datetime.utcnow(),
            )
            db.session.add(status)
            db.session.commit()
            _audit(
                config_id,
                full_name,
                'discover_repo',
                '刷新源 Gitea 仓库列表时发现新增仓库',
                'success',
                f'source_repo_id={source_repo_id}; repo={full_name}',
            )
        elif source_repo_id:
            status.source_repo_id = source_repo_id

        status.status = 'syncing'
        status.sync_mode = PUSH_MIRROR_MODE
        status.error_msg = ''
        db.session.commit()
        processed_ids.add(status.id)

        try:
            update_mirror_progress(
                config,
                'reconcile_repo',
                f'正在修复镜像 {index}/{repo_total}',
                _stage_percent(10, 90, index - 1, repo_total),
                f'{full_name} - 确保目标仓库和 Push Mirror',
                current_repo_name=full_name,
                current_repo_index=index,
                current_repo_total=repo_total,
            )
            target_repo_id, remote_name = _ensure_push_mirror(
                source,
                target,
                repo_info,
                config,
                old_full_name=old_full_name if old_full_name != full_name else None,
                known_remote_name=status.remote_name,
            )
            status.status = 'success'
            status.target_repo_id = target_repo_id or status.target_repo_id
            status.remote_name = remote_name or status.remote_name
            status.last_sync_at = datetime.utcnow()
            logs.append(f'OK {full_name}')
            update_mirror_progress(
                config,
                'reconcile_repo',
                f'正在修复镜像 {index}/{repo_total}',
                _stage_percent(10, 90, index, repo_total),
                f'{full_name} - 修复/同步完成',
                current_repo_name=full_name,
                current_repo_index=index,
                current_repo_total=repo_total,
            )
        except Exception as e:
            status.status = 'failed'
            status.error_msg = str(e)[:1000]
            error = str(e)
            logs.append(f'FAIL {full_name}: {_compact(error, 500)}')
            update_mirror_progress(
                config,
                'reconcile_repo',
                f'正在修复镜像 {index}/{repo_total}',
                _stage_percent(10, 90, index, repo_total),
                f'{full_name} - 失败: {_compact(error, 180)}',
                current_repo_name=full_name,
                current_repo_index=index,
                current_repo_total=repo_total,
            )
            logging.warning(
                '[PushMirror] setup repo failed - config_id=%d repo=%s error=%s',
                config_id,
                full_name,
                error,
            )

        db.session.commit()

    stale_statuses = MirrorRepoStatus.query.filter_by(mirror_config_id=config_id).all()
    stale_to_mark = [s for s in stale_statuses if s.id not in processed_ids]
    stale_total = len(stale_to_mark)
    for stale_index, status in enumerate(stale_to_mark, start=1):
        update_mirror_progress(
            config,
            'mark_missing_source',
            f'正在标记源缺失 {stale_index}/{stale_total}',
            _stage_percent(90, 96, stale_index - 1, stale_total),
            f'{status.repo_name} - 源仓库不存在或不可见',
            current_repo_name=status.repo_name,
            current_repo_index=stale_index,
            current_repo_total=stale_total,
        )
        status.status = MISSING_SOURCE_STATUS
        status.error_msg = '源仓库不存在或当前 token 不可见，gitea-manager 保留记录和目标仓库以便审计'
        _audit(
            config_id,
            status.repo_name,
            'mark_missing_source',
            '刷新源 Gitea 仓库列表后未发现该仓库，按策略标记失效并保留记录',
            'success',
            f'source_repo_id={status.source_repo_id}; repo={status.repo_name}',
        )
        logs.append(f'MISSING {status.repo_name}: 源仓库不存在或不可见')
        db.session.commit()
        update_mirror_progress(
            config,
            'mark_missing_source',
            f'正在标记源缺失 {stale_index}/{stale_total}',
            _stage_percent(90, 96, stale_index, stale_total),
            f'{status.repo_name} - 已标记为源缺失',
            current_repo_name=status.repo_name,
            current_repo_index=stale_index,
            current_repo_total=stale_total,
        )

    total, synced, failed = _mirror_counts(config_id)
    config.total_repos = total
    config.synced_repos = synced
    config.failed_repos = failed
    config.status = 'success' if failed == 0 else 'partial'
    config.last_sync_at = datetime.utcnow()
    config.last_sync_status = 'success' if failed == 0 else 'failed'
    config.last_sync_log = '\n'.join(logs[-50:])
    update_mirror_progress(
        config,
        'completed' if failed == 0 else 'completed_partial',
        '刷新/修复完成' if failed == 0 else '刷新/修复完成，部分仓库需要处理',
        100,
        f'成功 {synced}/{total}，失败或缺失 {failed}',
        reset_current=True,
    )
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
        update_mirror_progress(config, 'failed', '同步失败', 100, config.last_sync_log, reset_current=True)
        return

    source = GiteaServer.query.get(config.source_server_id)
    if not source:
        config.status = 'failed'
        config.last_sync_status = 'failed'
        config.last_sync_log = 'Source server not found'
        update_mirror_progress(config, 'failed', '同步失败', 100, config.last_sync_log, reset_current=True)
        return

    config.status = 'syncing'
    update_mirror_progress(config, 'prepare', '正在准备同步镜像', 3, '', reset_current=True)

    repos = MirrorRepoStatus.query.filter_by(mirror_config_id=config_id).all()
    repo_total = len(repos)
    synced = 0
    failed = 0
    logs = []

    if repo_total == 0:
        config.synced_repos = 0
        config.failed_repos = 0
        config.last_sync_at = datetime.utcnow()
        config.last_sync_status = 'success'
        config.last_sync_log = '没有可同步的仓库'
        config.status = 'success'
        update_mirror_progress(config, 'completed', '同步完成', 100, '没有可同步的仓库', reset_current=True)
        return

    for index, repo in enumerate(repos, start=1):
        update_mirror_progress(
            config,
            'sync_repo',
            f'正在同步 {index}/{repo_total}',
            _stage_percent(5, 95, index - 1, repo_total),
            f'{repo.repo_name} - 准备同步',
            current_repo_name=repo.repo_name,
            current_repo_index=index,
            current_repo_total=repo_total,
        )
        if repo.status == MISSING_SOURCE_STATUS:
            failed += 1
            message = '源仓库不存在或当前 token 不可见，普通同步跳过该仓库'
            repo.error_msg = message
            logs.append(f'SKIP {repo.repo_name}: {message}')
            update_mirror_progress(
                config,
                'skip_missing_source',
                f'正在同步 {index}/{repo_total}',
                _stage_percent(5, 95, index, repo_total),
                f'{repo.repo_name} - 跳过：源仓库不存在或不可见',
                current_repo_name=repo.repo_name,
                current_repo_index=index,
                current_repo_total=repo_total,
            )
            _audit(
                config_id,
                repo.repo_name,
                'skip_missing_source',
                '普通同步只处理有效镜像记录，missing_source 需要先刷新修复或人工确认',
                'skipped',
                f'source_repo_id={repo.source_repo_id}; repo={repo.repo_name}',
            )
            db.session.commit()
            continue

        try:
            owner, repo_name = _split_repo_name(repo.repo_name)
        except MirrorServiceError:
            continue
        try:
            _sync_push_mirror_repo(source, owner, repo_name)
            synced += 1
            repo.status = 'success'
            repo.error_msg = ''
            repo.last_sync_at = datetime.utcnow()
            logs.append(f'OK {repo.repo_name}')
            update_mirror_progress(
                config,
                'sync_repo',
                f'正在同步 {index}/{repo_total}',
                _stage_percent(5, 95, index, repo_total),
                f'{repo.repo_name} - 同步完成',
                current_repo_name=repo.repo_name,
                current_repo_index=index,
                current_repo_total=repo_total,
            )
            _audit(
                config_id,
                repo.repo_name,
                'sync_push_mirror',
                '用户触发普通同步，镜像记录有效，执行 push mirror 同步',
                'success',
                f'remote_name={repo.remote_name}; repo={repo.repo_name}',
            )
        except Exception as e:
            failed += 1
            repo.status = 'failed'
            repo.error_msg = str(e)[:1000]
            error = str(e)
            logs.append(f'FAIL {repo.repo_name}: {_compact(error, 500)}')
            update_mirror_progress(
                config,
                'sync_repo',
                f'正在同步 {index}/{repo_total}',
                _stage_percent(5, 95, index, repo_total),
                f'{repo.repo_name} - 失败: {_compact(error, 180)}',
                current_repo_name=repo.repo_name,
                current_repo_index=index,
                current_repo_total=repo_total,
            )
            _audit(
                config_id,
                repo.repo_name,
                'sync_push_mirror',
                '用户触发普通同步，但 push mirror 同步失败',
                'failed',
                error,
            )
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
    update_mirror_progress(
        config,
        'completed' if failed == 0 else 'completed_partial',
        '同步完成' if failed == 0 else '同步完成，部分仓库失败或跳过',
        100,
        f'成功 {synced}/{repo_total}，失败或跳过 {failed}',
        reset_current=True,
    )


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
    if repo.status == MISSING_SOURCE_STATUS:
        message = '源仓库不存在或当前 token 不可见，普通同步跳过该仓库'
        repo.error_msg = message
        config.status = 'partial'
        config.last_sync_at = datetime.utcnow()
        config.last_sync_status = 'failed'
        config.last_sync_log = f'SKIP {repo.repo_name}: {message}'
        update_mirror_progress(
            config,
            'skip_missing_source',
            '正在同步 1/1',
            100,
            f'{repo.repo_name} - 跳过：源仓库不存在或不可见',
            current_repo_name=repo.repo_name,
            current_repo_index=1,
            current_repo_total=1,
        )
        _audit(
            config_id,
            repo.repo_name,
            'skip_missing_source',
            '用户触发单仓库同步，但记录为 missing_source，需要先刷新修复或人工确认',
            'skipped',
            f'source_repo_id={repo.source_repo_id}; repo={repo.repo_name}',
        )
        db.session.commit()
        return False, message

    try:
        owner, repo_name_part = _split_repo_name(repo_name)
    except MirrorServiceError:
        return False, 'Invalid repo name'
    try:
        config.status = 'syncing'
        update_mirror_progress(
            config,
            'sync_repo',
            '正在同步 1/1',
            5,
            f'{repo.repo_name} - 准备同步',
            current_repo_name=repo.repo_name,
            current_repo_index=1,
            current_repo_total=1,
        )
        _sync_push_mirror_repo(source, owner, repo_name_part)
        repo.status = 'success'
        repo.error_msg = ''
        repo.last_sync_at = datetime.utcnow()
        _audit(
            config_id,
            repo.repo_name,
            'sync_push_mirror',
            '用户触发单仓库同步，镜像记录有效，执行 push mirror 同步',
            'success',
            f'remote_name={repo.remote_name}; repo={repo.repo_name}',
        )
        config.status = 'success'
        config.last_sync_at = datetime.utcnow()
        config.last_sync_status = 'success'
        config.last_sync_log = f'OK {repo.repo_name}'
        update_mirror_progress(config, 'completed', '同步完成', 100, f'{repo.repo_name} - 同步完成', reset_current=True)
        return True, ''
    except Exception as e:
        repo.status = 'failed'
        repo.error_msg = str(e)[:1000]
        _audit(
            config_id,
            repo.repo_name,
            'sync_push_mirror',
            '用户触发单仓库同步，但 push mirror 同步失败',
            'failed',
            str(e),
        )
        config.status = 'partial'
        config.last_sync_at = datetime.utcnow()
        config.last_sync_status = 'failed'
        config.last_sync_log = f'FAIL {repo.repo_name}: {_compact(str(e), 500)}'
        update_mirror_progress(config, 'failed', '同步失败', 100, f'{repo.repo_name} - 失败: {_compact(str(e), 180)}', reset_current=True)
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
