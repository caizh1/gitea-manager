"""以可恢复的创建回执同步 Webhook 密钥；不依赖 Gitea PATCH 修改 secret。"""
import hashlib
import secrets

from .common import AccessRevoked, ReviewError, PublishUnknown, WriteRejected, digest


def _identity(hook):
    config = hook.get('config') or {}
    return {'id': hook['id'], 'type': hook.get('type'), 'name': hook.get('name', ''),
            'url': config.get('url'), 'content_type': config.get('content_type'),
            'events': sorted(hook.get('events') or []), 'active': hook.get('active'),
            'branch_filter': hook.get('branch_filter', '')}


def ensure_binding(state, repo, client, callback, events):
    """调用者持有仓库锁；先验证新勾子，再清理本次记录的旧勾子。"""
    path = client.prefix + '/hooks'
    secret = state.unseal(repo['webhook_secret'])
    fingerprint = digest({'url': callback, 'gitea_url': repo['gitea_url'], 'repo_id': repo['repo_id'],
                          'secret': hashlib.sha256(secret.encode()).hexdigest()})
    binding = repo.get('webhook_binding') or {}
    matches = [h for h in client.pages(path) if (h.get('config') or {}).get('url') == callback]
    if any(h.get('type') != 'gitea' for h in matches):
        raise ReviewError('回调地址已有其他类型的 Webhook，未自动替换')

    if binding.get('fingerprint') != fingerprint:
        if binding and binding.get('phase') != 'ready':
            raise ReviewError('上次 Webhook 创建或清理尚待核对，不能切换密钥后重复创建')
        if len(matches) > 1:
            raise ReviewError('存在重复 Webhook，请先在 Gitea 中核对')
        binding = {'fingerprint': fingerprint, 'name': 'Gitea Manager AI Review ' + secrets.token_hex(16),
                   'phase': 'planned', 'previous': [_identity(h) for h in matches]}
        state.repo_status(repo['id'], webhook_binding=binding)

    owned = [h for h in matches if h.get('name') == binding['name']]
    if len(owned) > 1:
        raise ReviewError('本次 Webhook 创建回执不唯一，未重复创建或删除')
    if not owned and binding['phase'] == 'ready':
        # 已确认完成的勾子后来被删除；可开启一次新的安装。
        if len(matches) > 1:
            raise ReviewError('存在重复 Webhook，请先在 Gitea 中核对')
        binding = {'fingerprint': fingerprint, 'name': 'Gitea Manager AI Review ' + secrets.token_hex(16),
                   'phase': 'planned', 'previous': [_identity(h) for h in matches]}
        state.repo_status(repo['id'], webhook_binding=binding)
    previous_ids = {h['id'] for h in binding['previous']}
    if any(h['id'] not in previous_ids and h not in owned for h in matches):
        raise ReviewError('回调地址出现未记录的 Webhook，未自动删除')

    value = {'type': 'gitea', 'name': binding['name'], 'active': True, 'events': events,
             'config': {'url': callback, 'content_type': 'json', 'secret': secret}}
    if owned:
        hook = owned[0]
        if binding.get('id') is not None and hook['id'] != binding['id']:
            raise ReviewError('Webhook 创建回执编号发生变化，未接管')
        client.request('PATCH', path + '/' + str(hook['id']),
                       {'active': True, 'events': events, 'config': {'url': callback, 'content_type': 'json'}})
    else:
        if binding['phase'] != 'planned':
            raise ReviewError('上次 Webhook 创建结果未知且暂未找到回执，未重复发送创建请求')
        binding['phase'] = 'creating'
        state.repo_status(repo['id'], webhook_binding=binding)
        try:
            hook = client.request('POST', path, value)
        except (WriteRejected, AccessRevoked):
            binding['phase'] = 'planned'
            state.repo_status(repo['id'], webhook_binding=binding)
            raise
        except PublishUnknown:
            raise ReviewError('Webhook 创建响应未知，旧勾子已保留；请继续检查以核对同一回执') from None
        binding.update(id=hook['id'], phase='verifying')
        state.repo_status(repo['id'], webhook_binding=binding)

    actual = client.request('GET', path + '/' + str(hook['id']))
    if (actual.get('type') != 'gitea' or actual.get('name') != binding['name'] or
            not actual.get('active') or (actual.get('config') or {}).get('url') != callback or
            (actual.get('config') or {}).get('content_type') != 'json' or
            not set(events).issubset(actual.get('events') or [])):
        raise ReviewError('新 Webhook 回读验证失败，旧勾子已保留')
    binding.update(id=hook['id'], phase='cleanup')
    state.repo_status(repo['id'], webhook_binding=binding, hook_id=hook['id'])
    for before in binding['previous']:
        if before['id'] == hook['id']:
            continue
        old_path = path + '/' + str(before['id'])
        old = client.request('GET', old_path, missing=True)
        if old is None or (old.get('config') or {}).get('url') != callback:
            continue  # 已删除或改为其他用途的勾子不再处理。
        if _identity(old) != before:
            raise ReviewError('旧 Webhook 已被其他操作修改，新勾子已保留，未自动删除旧勾子')
        client.request('DELETE', old_path, missing=True)
        if client.request('GET', old_path, missing=True) is not None:
            raise ReviewError('旧 Webhook 删除回执待核对，新勾子已保留')
    remaining = [h for h in client.pages(path) if (h.get('config') or {}).get('url') == callback]
    if len(remaining) != 1 or remaining[0]['id'] != hook['id']:
        raise ReviewError('Webhook 清理后回读不唯一，请继续检查')
    binding['phase'] = 'ready'
    state.repo_status(repo['id'], webhook_binding=binding)
    return actual
