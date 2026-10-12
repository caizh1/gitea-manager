"""独立审查服务调用 Manager 当前权限判定，不挂载管理员数据库或凭据。"""
import os
from pathlib import Path
import requests
from .common import ReviewError


def authorize(repo, job=None, action='read'):
    url = os.environ.get('AI_REVIEW_AUTHORIZATION_URL', '')
    if not url:
        if repo.get('access_scope') or job and job.get('access_actor') not in (None, '', '{}'):
            raise ReviewError('未配置组织授权服务，已停止任务')
        return  # 尚未纳入组织授权的旧平台服务保留原身份边界。
    try:
        token = Path(os.environ['AI_REVIEW_AUTHORIZATION_TOKEN_FILE']).read_text().strip()
        if len(token) < 32:
            raise ValueError()
        payload = {'repository_id': repo['id'], 'action': action}
        if job:
            payload['job_id'] = job['id']
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(url, json=payload, headers={'Authorization': 'Bearer ' + token},
                                    timeout=(3, 10), allow_redirects=False)
        if response.status_code != 200 or response.json().get('allowed') is not True:
            raise ReviewError('组织权限或仓库归属已失效，已停止后续操作')
    except (OSError, KeyError, ValueError, requests.RequestException):
        raise ReviewError('组织授权服务不可用，已停止后续操作') from None


def guarded_client(store, repo, job=None):
    from .gitea import Gitea
    checked = False
    def guard():
        nonlocal checked
        if not checked:
            authorize(repo, job)
            checked = True
        if job:
            store.active(job['id'], job['owner'])
    client = Gitea(repo['gitea_url'], store.unseal(repo['bot_token']), repo['repo_name'], repo['repo_id'], guard)
    client.access_authorize = lambda action: authorize(repo, job, action)
    return client
