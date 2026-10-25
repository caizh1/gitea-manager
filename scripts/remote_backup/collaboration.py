"""用真实 API、正文下载和 Git 补齐协作样本，源准备不等于恢复通过。"""
import base64
import hashlib
import shutil
import time

from .errors import CheckFailure
from .fixtures import ensure_repo, git


REPO = '/repos/labadmin/proof'


def body(label):
    data = ('协作附件正文-' + label + '\n').encode()
    repeat, padding = divmod(1024, len(data))
    return data * repeat + b' ' * padding


def seed(lab, node='a'):
    original = lab.state['nodes'][node]['seed']
    sample = original.setdefault('collaboration_extended', {})
    if 'comment_id' not in sample:
        sample['comment_id'] = lab.api(node, 'POST', REPO + '/issues/%s/comments' % original['issue'],
            json={'body': '带附件的既有评论'}).json()['id']
        lab.save()
    sample.setdefault('assets', {})
    endpoints = {'issue': REPO + '/issues/%s/assets' % original['issue'],
                 'pr': REPO + '/issues/%s/assets' % original['pr'],
                 'comment': REPO + '/issues/comments/%s/assets' % sample['comment_id']}
    for kind, endpoint in endpoints.items():
        name = kind + '-proof.txt'
        if kind not in sample['assets']:
            existing = lab.api(node, 'GET', endpoint).json()
            item = next((row for row in existing if row['name'] == name), None)
            if item is None:
                item = lab.api(node, 'POST', endpoint, params={'name': name},
                    files={'attachment': (name, body(kind), 'text/plain')}).json()
            sample['assets'][kind] = {key: item[key] for key in ('id', 'uuid', 'name', 'size')}
            sample['assets'][kind].update(列表路径=endpoint, SHA256=hashlib.sha256(body(kind)).hexdigest())
            lab.save()
    if 'wiki_commits' not in sample:
        page = REPO + '/wiki/page/BackupHistory'
        response = lab.api(node, 'GET', page, expected=None)
        if response.status_code == 404:
            lab.api(node, 'POST', REPO + '/wiki/new', json={'title': 'BackupHistory',
                'content_base64': base64.b64encode('灾备 Wiki 第一版'.encode()).decode(), 'message': '建立历史样本'})
        elif response.status_code != 200:
            raise CheckFailure('无法读取专用 Wiki 历史样本')
        sample['wiki_body'] = '灾备 Wiki 第二版，保留第一版历史'
        lab.api(node, 'PATCH', page, json={'title': 'BackupHistory',
            'content_base64': base64.b64encode(sample['wiki_body'].encode()).decode(), 'message': '更新历史样本'})
        commits = lab.api(node, 'GET', REPO + '/wiki/revisions/BackupHistory').json()['commits']
        if len(commits) < 2:
            raise CheckFailure('真实 Wiki 没有形成多次提交历史')
        sample['wiki_commits'] = [row['sha'] for row in commits]
        lab.save()
    lab.api(node, 'PUT', '/user/starred/labadmin/proof', user='labreader')
    lab.api(node, 'PUT', REPO + '/subscription', user='labreader')
    lab.api(node, 'PUT', REPO + '/issues/%s/subscriptions/labreader' % original['issue'], user='labreader')
    if 'issue_links' not in sample:
        sample['issue_links'] = {}
        for kind in ('issue', 'pr'):
            item = lab.api(node, 'GET', REPO + '/issues/%s' % original[kind]).json()
            sample['issue_links'][kind] = {'id': item['id'], 'body': item['body'], 'title': item['title'],
                'labels': sorted(row['id'] for row in item['labels']),
                'milestone': item['milestone']['id'] if item.get('milestone') else None}
        lab.save()
    if 'renamed_repository' not in sample:
        response = lab.api(node, 'GET', '/repos/labadmin/renamed-proof', expected=None)
        if response.status_code == 404:
            ensure_repo(lab, node, 'rename-origin', auto_init=True)
            response = lab.api(node, 'PATCH', '/repos/labadmin/rename-origin', json={'name': 'renamed-proof'})
        elif response.status_code != 200:
            raise CheckFailure('无法读取专用改名仓库')
        repo = response.json()
        head = lab.api(node, 'GET', '/repos/labadmin/renamed-proof/git/refs/heads/' + repo['default_branch']).json()
        sample['renamed_repository'] = {'id': repo['id'], 'branch': repo['default_branch'], 'sha': head[0]['object']['sha']}
        lab.save()


def verify(lab, node):
    from .scenarios import write_json
    original = lab.state['nodes'][node]['seed']
    sample = original.get('collaboration_extended')
    if not sample or set(sample.get('assets', {})) != {'issue', 'pr', 'comment'}:
        raise CheckFailure('扩展协作样本尚未完成，不能宣称附件及历史迁移通过')
    checks = []
    for kind, asset in sample['assets'].items():
        rows = lab.api(node, 'GET', asset['列表路径']).json()
        item = next((row for row in rows if row['id'] == asset['id']), None)
        if not item or any(item[key] != asset[key] for key in ('uuid', 'name', 'size')):
            raise CheckFailure('附件与原 Issue、PR 或评论关联不一致：' + kind)
        response = lab.request(node, 'GET', '/attachments/' + asset['uuid'])
        if len(response.content) != asset['size'] or hashlib.sha256(response.content).hexdigest() != asset['SHA256']:
            raise CheckFailure('真实附件下载正文不一致：' + kind)
        denied = lab.request(node, 'GET', '/attachments/' + asset['uuid'], user='labdenied',
            expected=None, allow_redirects=False)
        if denied.status_code not in (403, 404) or denied.content == response.content:
            raise CheckFailure('无权用户未明确拒绝读取私有附件：' + kind)
        checks.append({'说明': kind + ' 附件关联、正文摘要和无权读取拒绝', '通过': True})
    page = lab.api(node, 'GET', REPO + '/wiki/page/BackupHistory').json()
    commits = lab.api(node, 'GET', REPO + '/wiki/revisions/BackupHistory').json()['commits']
    if (base64.b64decode(page['content_base64']).decode() != sample['wiki_body']
            or [row['sha'] for row in commits] != sample['wiki_commits']):
        raise CheckFailure('Wiki 正文或全部专用页面历史提交不一致')
    checks.append({'说明': 'Wiki 正文和多次历史提交完全一致', '通过': True})
    lab.api(node, 'GET', '/user/starred/labadmin/proof', user='labreader', expected=(204,))
    watching = lab.api(node, 'GET', REPO + '/subscription', user='labreader').json()
    issue_watch = lab.api(node, 'GET', REPO + '/issues/%s/subscriptions/check' % original['issue'], user='labreader').json()
    if watching.get('subscribed') is not True or issue_watch.get('subscribed') is not True:
        raise CheckFailure('既有用户的收藏、仓库关注或 Issue 订阅未保留')
    checks.append({'说明': '普通用户收藏、仓库关注和 Issue 订阅保持', '通过': True})
    renamed = lab.api(node, 'GET', '/repos/labadmin/renamed-proof').json()
    if renamed['id'] != sample['renamed_repository']['id'] or renamed['private'] is not True:
        raise CheckFailure('改名仓库的身份或私有属性不一致')
    directory = lab.root / ('collaboration-git-' + str(time.time_ns()))
    try:
        git(lab, node, ['clone', lab.gitea_url(node) + '/labadmin/renamed-proof.git', str(directory)])
        git(lab, node, ['fsck', '--full'], cwd=directory)
        if git(lab, node, ['rev-parse', 'HEAD'], cwd=directory).decode().strip() != sample['renamed_repository']['sha']:
            raise CheckFailure('改名后实际克隆提交与源不一致')
    finally:
        if directory.exists():
            shutil.rmtree(directory)
    checks.append({'说明': '改名私有仓库真实 HTTP 克隆、对象检查及提交读回', '通过': True})
    for kind in ('issue', 'pr'):
        item = lab.api(node, 'GET', REPO + '/issues/%s' % original[kind]).json()
        links = {'id': item['id'], 'body': item['body'], 'title': item['title'],
                 'labels': sorted(row['id'] for row in item['labels']),
                 'milestone': item['milestone']['id'] if item.get('milestone') else None}
        if links != sample['issue_links'][kind]:
            raise CheckFailure('既有 Issue 或 PR 的正文、标签及里程碑关联不一致：' + kind)
    comment = lab.api(node, 'POST', REPO + '/issues/%s/comments' % original['issue'],
        user='labwriter', json={'body': '恢复验证新增评论'}).json()
    try:
        endpoint = REPO + '/issues/comments/%s' % comment['id']
        lab.api(node, 'PATCH', endpoint, user='labwriter', json={'body': '恢复验证编辑后的评论'})
        if lab.api(node, 'GET', endpoint, user='labreader').json()['body'] != '恢复验证编辑后的评论':
            raise CheckFailure('普通用户编辑评论后其他成员不能读回')
    finally:
        lab.api(node, 'DELETE', REPO + '/issues/comments/%s' % comment['id'], user='labwriter')
    checks.append({'说明': '既有 Issue 关联保持，普通用户实际新增、编辑、读取及删除评论', '通过': True})
    write_json(lab.root / ('collaboration-checks-' + node + '-' + str(time.time_ns()) + '.json'),
        {'说明': '真实协作业务检查；源端执行只证明夹具，恢复验证需新备份与目标实跑', '节点': node, '断言': checks})
