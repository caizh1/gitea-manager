"""移除审查接入：保留历史，先撤销执行权限，再核对本组件的事件通知。"""
import re
import time
from urllib.parse import urlsplit

from .common import ReviewError, encode
from .gitea import Gitea
from .host_policy import gitea_address


def remove_repository(state, repository, revision, target):
    # 与安装共用跨进程锁；不能在创建请求仍在途时归档并删除通知。
    with state.lock('webhook-install-' + str(repository), blocking=True):
        return _remove_repository(state, repository, revision, target)


def _unverified_binding(binding):
    """只有本组件持久记录的创建回执可以跳过投递验证进行移除。"""
    return (binding.get('phase') in ('creating', 'verifying')
            and isinstance(binding.get('name'), str)
            and re.fullmatch(r'Gitea Manager AI Review [0-9a-f]{32}', binding['name'])
            and isinstance(binding.get('fingerprint'), str)
            and re.fullmatch(r'[0-9a-f]{64}', binding['fingerprint'])
            and (binding.get('phase') == 'creating'
                 or type(binding.get('id')) is int and binding['id'] > 0))


def _remove_repository(state, repository, revision, target):
    if type(revision) is not int:
        raise ReviewError('请刷新仓库列表后重试')
    with state.tx() as connection:
        old = state.repo(repository, connection)
        if old.get('removal_complete'):
            return state.repo(repository, connection, public=True)
        if old.get('removal_pending'):
            if revision not in (old['revision'], old.get('removal_revision')):
                raise ReviewError('仓库配置已变化，请刷新后重试')
        else:
            if old.get('access_archived'):
                raise ReviewError('原组织归属已失效，不能操作该仓库的事件通知')
            if revision != old['revision']:
                raise ReviewError('仓库配置已变化，请刷新后重试')
            binding = old.get('webhook_binding') or {}
            if binding and binding.get('phase') != 'ready' and not _unverified_binding(binding):
                raise ReviewError('事件通知创建或清理尚待核对，请先完成通知检查再移除接入')
            active = connection.execute("""SELECT 1 FROM jobs WHERE repository=? AND
                (status IN ('running','publishing','recovering','publish_unknown','cancelling')
                 OR (owner!='' AND lease>?)) LIMIT 1""", (repository, time.time())).fetchone()
            if active:
                raise ReviewError('该仓库仍有运行中、取消中或发布待核对的任务，请先在审查工作区处理后再移除')
            value = {key: value for key, value in old.items()
                     if key not in ('id', 'server_id', 'repo_id', 'revision', 'last_event', 'last_reconcile')}
            value.update(enabled=False, access_archived=True, original_repo_id=old['repo_id'],
                         removal_pending=True, removal_revision=revision, removal_error='')
            connection.execute('UPDATE repositories SET enabled=0,revision=revision+1,value=? WHERE id=?',
                               (encode(value), repository))
            connection.execute("""UPDATE jobs SET status='superseded',error='仓库接入已停止',updated=?
                WHERE repository=? AND status IN ('queued','awaiting_cost','budget_paused','incomplete')""",
                               (time.time(), repository))
            connection.execute('DELETE FROM capabilities WHERE job IN (SELECT id FROM jobs WHERE repository=?)', (repository,))
            connection.execute('UPDATE review_native_bindings SET active=0 WHERE job IN (SELECT id FROM jobs WHERE repository=?)', (repository,))
            connection.execute('UPDATE pr_state SET progress_seq=progress_seq+1 WHERE repository=?', (repository,))
    # 不持有 SQLite 事务等待远端；进程中断后保留可重试状态，旧事件不能继续入队。
    try:
        cleanup_webhook(old, target, state)
    except ReviewError as exc:
        state.repo_status(repository, removal_error=str(exc))
        raise ReviewError('已停止该仓库的新审查，但事件通知清理尚未完成：' + str(exc)) from None
    state.repo_status(repository, removal_pending=False, removal_complete=True, removal_error='',
                      removed_at=time.time(), hook_status='已移除', bot_token='', webhook_secret='')
    return state.repo(repository, public=True)


def cleanup_webhook(repo, target, state):
    binding = repo.get('webhook_binding') or {}
    unverified = _unverified_binding(binding)
    hook_id, callback = repo.get('hook_id'), repo.get('webhook_url')
    if not unverified and not hook_id and not callback and repo.get('hook_status', '未安装') == '未安装':
        return
    if not unverified:
        if type(hook_id) is not int or hook_id <= 0 or not isinstance(callback, str):
            raise ReviewError('缺少可核对的事件通知记录，请先在 Gitea 核对该接入，未删除任何通知')
        if urlsplit(callback).path.rstrip('/') != f"/api/ai-review/webhooks/{repo['id']}":
            raise ReviewError('事件通知记录不匹配，未删除任何通知')
    if gitea_address(target.gitea_url) != gitea_address(repo['gitea_url']):
        raise ReviewError('服务器连接已变化，未操作其他实例')
    client = Gitea(target.gitea_url, target.api_token, repo['repo_name'], repo['repo_id'])
    try:
        client.repository()
        if unverified:
            hook_id = binding.get('id')
            if hook_id is None:
                owned = [hook for hook in client.pages(client.prefix + '/hooks')
                         if hook.get('name') == binding['name']]
                if len(owned) != 1:
                    raise ReviewError('通知创建回执缺失或不唯一，仍待核对，未删除任何通知')
                hook_id = owned[0].get('id')
            if type(hook_id) is not int or hook_id <= 0:
                raise ReviewError('通知创建回执编号无效，未删除任何通知')
        path = client.prefix + '/hooks/' + str(hook_id)
        actual = client.request('GET', path, missing=True)
        if actual is None:
            return
        if unverified:
            from .webhook_binding import binding_fingerprint
            callback = (actual.get('config') or {}).get('url')
            if (actual.get('type') != 'gitea' or actual.get('name') != binding['name']
                    or not isinstance(callback, str)
                    or urlsplit(callback).path.rstrip('/') != f"/api/ai-review/webhooks/{repo['id']}"
                    or binding_fingerprint(repo, callback, state.unseal(repo['webhook_secret'])) != binding['fingerprint']):
                raise ReviewError('未验证通知的身份与创建记录不匹配，未删除任何通知')
        if actual.get('id') != hook_id or actual.get('config', {}).get('url') != callback:
            raise ReviewError('事件通知已被其他配置替换，未删除其他通知')
        if unverified and binding.get('id') is None:
            # 先保存找回的编号；删除回执丢失或进程中断后可按同一编号只读核对。
            binding = dict(binding, id=hook_id, phase='verifying')
            state.repo_status(repo['id'], webhook_binding=binding)
        try:
            client.request('DELETE', path)
        except ReviewError:
            # 删除回执丢失时只核对，不再次写入；不存在才是清理完成。
            if client.request('GET', path, missing=True) is None:
                return
            raise
        if client.request('GET', path, missing=True) is not None:
            raise ReviewError('事件通知仍存在，请稍后重试清理')
    finally:
        client.close()
