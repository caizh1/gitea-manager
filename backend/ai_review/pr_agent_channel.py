"""自动接入的控制面。原生 PR-Agent 独立接收 Gitea 事件。"""
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit

import requests

from .common import ReviewError, endpoint
from .gitea import Gitea
from .host_policy import gitea_address

EVENTS = ['pull_request', 'pull_request_sync', 'pull_request_comment']


def available():
    return bool(os.environ.get('PR_AGENT_CHANNEL_DIR'))


def callback_path(repository):
    return f'/pr-agent/{repository}/api/v1/gitea_webhooks'


def active(repo):
    return repo.get('native_pr_agent') in ('switching', 'ready')


def owns_review(repo, engine):
    return repo.get('native_pr_agent') == 'switching' or (active(repo) and engine == 'pr_agent')


def native_settings(store, repo, config):
    model = 'openai/' + config['model']
    return {
        'config.git_provider': 'gitea',
        'gitea.url': repo['gitea_url'],
        'gitea.handle_push_trigger': True,
        'gitea.pr_commands': ['/review'],
        'gitea.push_commands': ['/review'],
        'gitea.personal_access_token': store.unseal(repo['bot_token']),
        'gitea.webhook_secret': store.unseal(repo['webhook_secret']),
        'openai.api_base': config['api_base_url'],
        'openai.key': store.unseal(config['api_key']) if config['api_key'] else '',
        'config.model': model,
        'config.weak_model': model,
        'config.fallback_models': [],
        'config.custom_model_max_tokens': config['model_context_tokens'],
        'config.max_model_tokens': config['model_context_tokens'],
        'config.max_output_tokens': config.get('model_max_output_tokens', 0),
        'config.ai_timeout': config.get('model_request_timeout_seconds', 600),
        'config.response_language': 'zh-CN',
        'config.reasoning_effort': config.get('reasoning_effort') if config.get('thinking_enabled') else None,
        'config.additional_reasoning_effort_models': [model],
        'config.repo_context_files': ['.gitea/review-rules.md'],
        'config.repo_context_from_default_branch': False,
        'config.publish_output': True,
        'pr_reviewer.enable_large_pr_chunking': True,
        'pr_reviewer.inline_key_issues': True,
        'pr_reviewer.publish_output_no_suggestions': True,
        'pr_reviewer.publish_review_failure_comment': True,
        'pr_reviewer.enable_review_labels_security': False,
        'pr_reviewer.enable_review_labels_effort': False,
    }


def sync(store, repository):
    if not available():
        return
    with store.lock(f'pr-agent-channel-{repository}', blocking=True):
        repo = store.repo(repository)
        if repo.get('access_archived'):
            remove_config(repository)
            return
        if not active(repo):
            return
        config = store.config()
        enabled = repo['native_pr_agent'] == 'ready' and repo['enabled'] and config['enabled']
        legacy = config['engine'] != 'pr_agent'
        value = {'enabled': enabled, 'legacy': legacy, 'settings': native_settings(store, repo, config)}
        folder = Path(os.environ['PR_AGENT_CHANNEL_DIR'])
        folder.mkdir(parents=True, exist_ok=True)
        # 临时文件在同一卷中，只有后端和原生服务的运行用户持有读权限。
        with tempfile.NamedTemporaryFile(dir=folder, mode='w', encoding='utf-8', delete=False) as stream:
            temporary = Path(stream.name)
            try:
                if os.geteuid() == 0:
                    os.fchown(stream.fileno(), 10001, -1)
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
                temporary.replace(folder / f'{repository}.json')
            finally:
                temporary.unlink(missing_ok=True)


def _probe(client, path, repository, ref):
    started = time.time()
    client.request('POST', path + '/tests', params={'ref': 'refs/heads/' + ref})
    deadline = time.monotonic() + 35
    url = os.environ.get('PR_AGENT_CONTROL_URL', 'http://review-pr-agent:5054') + f'/channels/{repository}'
    with requests.Session() as session:
        session.trust_env = False
        while time.monotonic() < deadline:
            try:
                reply = session.get(url, timeout=2, allow_redirects=False)
                if reply.status_code == 200 and reply.json()['received'] >= started:
                    return
            except (requests.RequestException, ValueError, KeyError):
                pass
            time.sleep(1)
    raise ReviewError('Gitea 未成功投递原生 PR-Agent 的签名验证事件')


def _prepare_switch(store, repo):
    if active(repo):
        return
    with store.tx() as connection:
        busy = connection.execute("""SELECT 1 FROM jobs WHERE repository=?
            AND (status IN ('running','publishing','recovering','publish_unknown','cancelling')
                 OR (owner!='' AND lease>?)) LIMIT 1""", (repo['id'], time.time())).fetchone()
        if busy:
            raise ReviewError('该仓库还有旧审查或发布正在处理，请稍后继续检查')
        connection.execute("""UPDATE repositories SET value=json_set(value, '$.native_pr_agent', 'switching')
                              WHERE id=?""", (repo['id'],))


def install(store, repository, manager_url, target):
    from .webhook_binding import ensure_binding

    repo = store.repo(repository)
    if repo.get('access_archived') or gitea_address(target.gitea_url) != repo['gitea_url']:
        raise ReviewError('当前 Gitea 实例与仓库接入记录不一致')
    callback = endpoint(manager_url, 'AI_REVIEW_WEBHOOK_HOSTS') + callback_path(repository)
    client = Gitea(target.gitea_url, target.api_token, repo['repo_name'], repo['repo_id'])
    try:
        actual = client.repository()
        ref = actual.get('default_branch')
        if actual.get('empty') or not ref:
            raise ReviewError('空仓库尚无提交，请有首个提交后继续接入')
        _prepare_switch(store, repo)
        sync(store, repository)
        path = client.prefix + '/hooks'
        before = repo.get('native_previous_hook') if repo.get('native_pr_agent') != 'ready' else None
        if repo.get('native_pr_agent') == 'ready' and repo.get('hook_id'):
            check_path = f"{path}/{repo['hook_id']}"
            known = client.request('GET', check_path, missing=True)
            if known is not None:
                if known.get('config', {}).get('url') != repo.get('webhook_url'):
                    raise ReviewError('已绑定的原生 Webhook 被修改，请核对 Gitea 配置')
                before = {'id': known['id'], 'active': known['active'], 'events': known['events'],
                          'config': {'url': repo['webhook_url'], 'content_type': 'json'}}
        if before is None and repo.get('hook_id') and repo.get('webhook_url'):
            candidate = client.request('GET', f"{path}/{repo['hook_id']}", missing=True)
            if candidate and candidate.get('config', {}).get('url') == repo['webhook_url'] and (
                urlsplit(repo['webhook_url']).path.endswith(f'/api/ai-review/webhooks/{repository}')
            ):
                before = {'id': candidate['id'], 'active': candidate['active'], 'events': candidate['events'],
                          'config': {'url': repo['webhook_url'], 'content_type': 'json'}}
                store.repo_status(repository, native_previous_hook=before)
        binding = repo.get('webhook_binding')
        if before:
            old_path = f"{path}/{before['id']}"
            probe_events = EVENTS + ['push']
            try:
                client.request('PATCH', old_path, {'active': True, 'events': probe_events,
                                                  'config': {'url': callback, 'content_type': 'json'}})
                if client.request('GET', old_path)['config']['url'] != callback:
                    raise ReviewError('原生 Webhook 地址回读不一致')
                _probe(client, old_path, repository, ref)
                client.request('PATCH', old_path, {'events': EVENTS})
                hook = client.request('GET', old_path)
                if not set(EVENTS).issubset(hook['events']) or 'push' in hook['events']:
                    raise ReviewError('原生 Webhook 事件订阅回读不一致')
            except ReviewError:
                client.request('PATCH', old_path, {k: v for k, v in before.items() if k != 'id'})
                restored = client.request('GET', old_path)
                if (repo.get('native_pr_agent') != 'ready' and restored['config']['url'] == before['config']['url']
                        and set(restored['events']) == set(before['events']) and restored.get('active') == before['active']):
                    store.repo_status(repository, native_pr_agent=None)
                    remove_config(repository)
                raise
        else:
            # 新接入使用已有的幂等创建回执。测试 push 投递后收回临时订阅。
            def verify(hook_id):
                hook_path = f'{path}/{hook_id}'
                client.request('PATCH', hook_path, {'events': EVENTS + ['push']})
                try:
                    _probe(client, hook_path, repository, ref)
                    return True
                finally:
                    client.request('PATCH', hook_path, {'events': EVENTS})

            hook = ensure_binding(store, repo, client, callback, EVENTS, verify)
            binding = store.repo(repository).get('webhook_binding')
        store.repo_status(repository, native_pr_agent='ready', hook_id=hook['id'], webhook_binding=binding,
                          webhook_url=callback, hook_status='原生 PR-Agent 已验签')
        with store.tx() as connection:
            connection.execute("""UPDATE jobs SET status='superseded',error='已切换到 PR-Agent 原生通道',updated=?
                                  WHERE repository=? AND status IN ('queued','awaiting_cost','budget_paused','incomplete','awaiting_publication')""",
                               (time.time(), repository))
        sync(store, repository)
        return store.repo(repository, public=True)
    finally:
        client.close()


def remove_config(repository):
    if available():
        (Path(os.environ['PR_AGENT_CHANNEL_DIR']) / f'{repository}.json').unlink(missing_ok=True)
