"""移除审查接入：保留历史，先撤销执行权限，再核对本组件的事件通知。"""
import time
from urllib.parse import urlsplit

from .common import ReviewError, encode
from .gitea import Gitea
from .host_policy import gitea_address


def remove_repository(state, repository, revision, target):
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
        cleanup_webhook(old, target)
    except ReviewError as exc:
        state.repo_status(repository, removal_error=str(exc))
        raise ReviewError('已停止该仓库的新审查，但事件通知清理尚未完成：' + str(exc)) from None
    state.repo_status(repository, removal_pending=False, removal_complete=True, removal_error='',
                      removed_at=time.time(), hook_status='已移除', bot_token='', webhook_secret='')
    return state.repo(repository, public=True)


def cleanup_webhook(repo, target):
    hook_id, callback = repo.get('hook_id'), repo.get('webhook_url')
    if not hook_id and not callback and repo.get('hook_status', '未安装') == '未安装':
        return
    if type(hook_id) is not int or hook_id <= 0 or not isinstance(callback, str):
        raise ReviewError('缺少可核对的事件通知记录，请先在 Gitea 核对该接入，未删除任何通知')
    if urlsplit(callback).path.rstrip('/') != f"/api/ai-review/webhooks/{repo['id']}":
        raise ReviewError('事件通知记录不匹配，未删除任何通知')
    if gitea_address(target.gitea_url) != gitea_address(repo['gitea_url']):
        raise ReviewError('服务器连接已变化，未操作其他实例')
    client = Gitea(target.gitea_url, target.api_token, repo['repo_name'], repo['repo_id'])
    try:
        client.repository()
        path = client.prefix + '/hooks/' + str(hook_id)
        actual = client.request('GET', path, missing=True)
        if actual is None:
            return
        if actual.get('id') != hook_id or actual.get('config', {}).get('url') != callback:
            raise ReviewError('事件通知已被其他配置替换，未删除其他通知')
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
