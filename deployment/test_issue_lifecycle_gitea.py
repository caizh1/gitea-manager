"""官方 Gitea 的跨版本协议验收；模型固定替身，不代表真实模型质量。"""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from cryptography.fernet import Fernet
from ai_review import diff_review
from ai_review.gitea import Gitea
from ai_review.store import Store
from ai_review.issue_lifecycle import reconcile, listing
from ai_review.publish import Publisher
from ai_review.progress import ProgressSync, SUMMARY_MARK

config = json.load(sys.stdin)
folder = Path('/state'); key = folder / 'master.key'
if not key.exists(): key.write_bytes(Fernet.generate_key()); key.chmod(0o600)
os.environ['AI_REVIEW_MASTER_KEY_FILE'] = str(key)
store = Store(folder / 'review.db')
if not store.config()['enabled']:
    store.save_config({'revision': store.config()['revision'], 'enabled': True, 'debounce_seconds': 0, 'model': '协议替身'})
repo = next(iter(store.repos()), None) or store.save_repo(1, config['repo_id'], {'enabled': True, 'bot_token': store.seal(config['token']),
    'webhook_secret': store.seal('隔离验收'), 'repo_name': config['repo_name'], 'bot_id': config['bot_id']})
store.repo_status(repo['id'], gitea_url='http://inline-gitea:3000')
repo = store.repo(repo['id'])
class AuditedGitea(Gitea):
    def request(self, method, path, *args, **kwargs):
        assert not any(part in path for part in ('/contents/', '/git/blobs/', '/archive/', '/raw/')), '审查读取了源码接口'
        return super().request(method, path, *args, **kwargs)
def client_for(): return AuditedGitea(repo['gitea_url'], config['token'], config['repo_name'], config['repo_id'])
client = client_for()
snapshot = diff_review.capture(client, config['number'], store.config())
ident = store.enqueue(repo['id'], config['number'], {k: snapshot[k] for k in ('head','base')}, manual=True)
job = store.claim(); assert job and job['id'] == ident
store.update(ident, job['owner'], snapshot=snapshot)
anchor = next((a for a in snapshot['anchors'].values() if a['side'] == 'new' and 'return *p' in a['quote']), None)
text = '## 固定协议验收报告\n'
if config['status'] == 'present':
    text += f"### [P1] 同一空指针问题的标题 {config['round']}\n定位：{anchor['id'] if anchor else '无'}\n\n参数为空时仍解引用；这是固定验收样本。\n"
result = diff_review.adapt({'review_scope': 'diff', 'report_markdown': text, '_review_completion': {'complete': True}}, snapshot)
calls = []
def answer(store, job, issue, messages, cfg):
    payload = json.loads(messages[1]['content']); calls.append(issue)
    evidence = []
    for field in ('historical_diff', 'current_diff'):
        source = payload[field]
        key = next((k for k,v in source.items() if v.startswith('+') and 'int f(' in v), None)
        if key: evidence.append({'id': key, 'quote': source[key]})
    return dict(issue_id=issue, snapshot_id=snapshot['id'], status=config['status'], reason='固定样本对照 diff 的预设人工结论',
        evidence=evidence, relations={f['candidate_id']: 'same' for f in payload['candidates']},
        anchor_id=anchor['id'] if anchor else None, obsolete_reason='withdrawn' if config['status']=='obsolete' else None), {}
with patch('ai_review.broker.issue_completion', answer):
    result = reconcile(store, client, job, repo, snapshot, result)
if config.get('manual_edit'):
    previous = client.pages(client.prefix + f"/pulls/{config['number']}/reviews")[0]
    original = client.pages(client.prefix + f"/pulls/{config['number']}/reviews/{previous['id']}/comments")[0]
    client.request('PATCH', client.prefix + '/issues/comments/' + str(original['id']), {'body': '人工改写原评论，自动同步必须保留'})
Publisher(store, client, job, repo, snapshot, result).publish()
store.finish(ident, job['owner'], 'succeeded')
with store.tx() as c: c.execute('UPDATE pr_state SET progress_next=0 WHERE repository=?', (repo['id'],))
assert ProgressSync(store, lambda _: client_for()).sync(repo['id'], config['number']), '固定总览同步失败'
reviews = client.pages(client.prefix + f"/pulls/{config['number']}/reviews")
comments = [row for review in reviews for row in client.pages(client.prefix + f"/pulls/{config['number']}/reviews/{review['id']}/comments")]
assert len(reviews) == len(comments) == 1, '同一问题重复发布'
items = listing(store, repo['id'], config['number'], job=ident)
assert items['total'] == 1 and items['items'][0]['status'] == config['status'], items
assert not items['items'][0]['stale']
if config.get('manual_edit'):
    assert items['items'][0]['comments'][0]['error'], items
    assert comments[0]['body'] == '人工改写原评论，自动同步必须保留'
else:
    assert not items['items'][0]['comments'][0]['error'], items
    assert snapshot['head'] in comments[0]['body'], '原评论未更新版本'
summary = [r for r in client.pages(client.prefix+f"/issues/{config['number']}/comments") if r['body'].startswith(SUMMARY_MARK)]
assert len(summary) == 1
assert len(calls) == (0 if config['round'] == 1 else 1), '旧问题请求次数不符'
print(json.dumps({'轮次':config['round'], '状态':config['status'], '提交':snapshot['head'], '问题':items['items'][0]['id'],
    '正式审查数':len(reviews), '行内评论数':len(comments), '总览数':len(summary), '复核请求':len(calls),
    '人工编辑保护':bool(config.get('manual_edit')), '结论':'工程协议通过；模型质量未验证'},ensure_ascii=False))
