"""保存仓库专用签名回调提供的原生缺陷关闭凭据。"""
import hashlib
import hmac
import json
import re
from datetime import datetime

from flask import current_app, request
from sqlalchemy.dialects.sqlite import insert

from access.scopes import server_epoch
from models import GiteaServer, db
from services.policy_gitea import PolicyError
from .git_data import OID
from .models import ClosingEvidence, Collection
from .rules import utc


def signing_secret(server_id, epoch, owner_id, repo_id, birth):
    secret = current_app.secret_key
    if isinstance(secret, str):
        secret = secret.encode()
    scope = f'engineering-issues:{server_id}:{epoch}:{owner_id}:{repo_id}:{birth.isoformat()}'.encode()
    return hmac.new(secret, scope, hashlib.sha256).hexdigest()


def install_hook(client, path, repo, base, epoch):
    if not repo.get('has_issues', True):
        return
    owner = repo['owner']['id']
    url = f"{base}/api/engineering-activity/events/{client.server_id}/{owner}/{repo['id']}"
    hooks = client.pages(path + '/hooks')
    owned = [hook for hook in hooks if hook.get('type') == 'gitea' and hook.get('config', {}).get('url') == url]
    if len(owned) > 1:
        raise PolicyError('缺陷关闭通知存在重复配置，请检查 Gitea 仓库 Webhook')
    birth = utc(repo.get('created_at'))
    if birth is None:
        raise PolicyError('无法核实仓库创建身份')
    config = {'url': url, 'content_type': 'json',
              'secret': signing_secret(client.server_id, epoch, owner, repo['id'], birth)}
    body = {'type': 'gitea', 'config': config, 'events': ['issues'], 'active': True}
    if not owned:
        client.request('POST', path + '/hooks', json=body)
        return
    entry = owned[0]
    # 原生 API 会过滤密钥，地址带实例代次的收件密钥由部署会话密钥确定。
    if entry.get('events') != ['issues'] or not entry.get('active'):
        client.request('PATCH', f"{path}/hooks/{entry['id']}", json=body)


def receive(server_id, owner_id, repo_id):
    server = db.session.get(GiteaServer, server_id)
    if server is None:
        return '', 404
    epoch = server_epoch(server)
    conf = db.session.get(Collection, (server_id, epoch, 0))
    if not conf or not conf.settings.get('webhook_base'):
        return '', 404
    if request.content_length is None or request.content_length > 1024 * 1024:
        return '', 413
    body = request.get_data()
    proof = request.headers.get('X-Gitea-Signature', '')
    try:
        payload = json.loads(body)
        birth = utc(payload['repository']['created_at'])
    except (KeyError, AttributeError, ValueError, TypeError):
        return '', 400
    if birth is None:
        return '', 400
    expected = hmac.new(signing_secret(server_id, epoch, owner_id, repo_id, birth).encode(),
                        body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(proof, expected):
        return '', 403
    if request.headers.get('X-Gitea-Event') != 'issues':
        return '', 204
    try:
        if payload.get('action') != 'closed':
            return '', 204
        issue_meta = payload['issue']
        repo = payload['repository']
        number = issue_meta['number']
        actor = payload['sender']['id']
        date = utc(issue_meta['closed_at'])
        sha = payload.get('commit_id') or ''
        delivery = request.headers.get('X-Gitea-Delivery', '')
        valid = (repo['id'] == repo_id and repo['owner']['id'] == owner_id and date and
                 isinstance(number, int) and number > 0 and isinstance(actor, int) and
                 (not sha or OID.fullmatch(sha)) and re.fullmatch(r'[a-f0-9-]{36}', delivery))
    except (AttributeError, KeyError, ValueError, TypeError):
        valid = False
    if not valid:
        return '', 400
    db.session.execute(insert(ClosingEvidence).values(
        server_id=server_id, epoch=epoch, organization_id=owner_id, repo_id=repo_id, delivery=delivery,
        issue=number, actor=actor, closed=date, sha=sha, birth=birth, received=datetime.utcnow()).on_conflict_do_nothing())
    db.session.commit()
    return '', 204


def evidence(repo, number, timestamp, actor):
    if not timestamp or not actor:
        return None
    rows = ClosingEvidence.query.filter_by(server_id=repo.server_id, epoch=repo.epoch,
                                           organization_id=repo.organization_id, repo_id=repo.repo_id,
                                           issue=number, closed=timestamp, actor=actor, birth=utc(repo.birth)).all()
    if rows and len({row.sha for row in rows}) == 1:
        return rows[0].sha
    return None

def newly_received(repo, since):
    query = ClosingEvidence.query.filter_by(server_id=repo.server_id, epoch=repo.epoch,
                                            organization_id=repo.organization_id, repo_id=repo.repo_id,
                                            birth=utc(repo.birth))
    return {number for number, in query.filter(ClosingEvidence.received >= since)
            .with_entities(ClosingEvidence.issue).distinct()}
