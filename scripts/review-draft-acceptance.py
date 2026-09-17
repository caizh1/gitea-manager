#!/usr/bin/env python3
"""真实 Gitea 私有草稿与正式发布隔离验收；仅操作新建合成测试仓库。"""
import json
from pathlib import Path
import runpy
import time

real = runpy.run_path(str(Path(__file__).with_name('review-real-acceptance.py')))
local = real['local']
h = local.Harness()
h.login()
assert h.gitea('GET', '/version')['version'] == '1.26.4+review.1'
name = 'draft-isolation-' + str(int(time.time()))
repo = h.create_repo(name)
h.manager('PUT', '/ai-review/repositories/' + str(repo['id']), dict(repo, enabled=False))
folder = real['clone'](h, name)
git = real['git']
git(h, folder, 'switch', '-c', 'draft-check')
(folder / 'probe.c').write_text('int probe(void) {\n    return 1;\n}\n')
git(h, folder, 'add', 'probe.c')
git(h, folder, 'commit', '-m', '验证草稿隔离')
git(h, folder, 'push', 'origin', 'draft-check')
full = repo['repo_name']
pr = h.gitea('POST', f'/repos/{full}/pulls', {'head': 'draft-check', 'base': 'main', 'title': '私有草稿发布验收'})
n = pr['number']
base = f'/repos/{full}/pulls/{n}/reviews'
mark = '<!-- ai-review:private-draft-test -->'
token = local.compose('exec', '-T', '--user', 'git', 'gitea', 'gitea', '--config', '/data/gitea/conf/app.ini',
                      'admin', 'user', 'generate-access-token', '--username', 'review-developer',
                      '--token-name', name, '--scopes', 'read:notification', '--raw').strip().splitlines()[-1]


def notifications():
    rows = h.request('GET', h.gitea_url + '/api/v1/notifications?all=true', headers={'Authorization': 'token ' + token})
    return [r for r in rows if r.get('repository', {}).get('full_name') == full]


h.gitea('PUT', f'/repos/{full}/subscription', {}, actor='developer')
before = notifications()
r = h.gitea('POST', base, {'event': 'PENDING', 'commit_id': pr['head']['sha'],
            'body': mark + '\n私有草稿总结 @review-developer',
            'comments': [{'path': 'probe.c', 'new_position': 2, 'body': mark + '\n私有草稿行内意见'}]}, actor='bot')
assert r['state'] == 'PENDING' and not r['html_url']
# 通知队列异步处理；等待其有机会执行，避免把尚未处理当作隔离成功。
time.sleep(5)
assert notifications() == before
assert not any(x['id'] == r['id'] for x in h.gitea('GET', base, actor='developer'))
assert not any(x.get('review_id') == r['id'] for x in h.gitea_pages(f'/repos/{full}/issues/{n}/timeline', actor='developer'))
final = h.gitea('POST', base, {'event': 'COMMENT', 'commit_id': pr['head']['sha'],
                'body': mark + '\n正式审查完成 @review-developer', 'comments': []}, actor='bot')
assert final['id'] == r['id'] and final['commit_id'] == pr['head']['sha'] and final['state'] == 'COMMENT'
comments = h.gitea('GET', base + '/' + str(r['id']) + '/comments', actor='developer')
assert len(comments) == 1 and comments[0]['path'] == 'probe.c' and comments[0]['position'] == 2
assert sum(x.get('type') == 'review' and x.get('review_id') == r['id'] for x in h.gitea_pages(f'/repos/{full}/issues/{n}/timeline', actor='developer')) == 1
for _ in range(30):
    if notifications() != before:
        break
    time.sleep(1)
else:
    raise RuntimeError('正式通知未在限定时间内到达；保留 Review，不重发')
report = {'说明': '真实 Gitea API 与异步通知测试；页面核对另存截图。', 'PR': h.gitea_url + f'/{full}/pulls/{n}',
          'Head': pr['head']['sha'], 'Review': r['id'], '草稿API与时间线隐藏': True, '草稿没有通知': True,
          '正式通知到达': True, '正式动态唯一': True, '正式行内新侧': 2, '正式与草稿Review相同': True}
out = local.STATE / ('草稿隔离验收-' + str(int(time.time())) + '.json')
out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print('真实草稿隔离及正式发布通过：' + str(out))
