#!/usr/bin/env python3
"""补测截断、超时、关闭取消、漏事件补偿；只连接本机协议替身。"""
import runpy
from pathlib import Path
import time

m=runpy.run_path(str(Path(__file__).with_name('review-boundary-acceptance.py')))
s=m['Suite']('extra-acceptance');wait=m['wait_task'];local=m['local']
code=b'int probe(void) {\n    return 1; /* REVIEW_TEST_TARGET */\n}\n'
for mode,delay in [('truncated',0),('normal',180)]:
    s.control(mode,delay);pr=s.branch(mode+'-'+str(delay),{'extra.c':code})
    task,elapsed=wait(s.h,s.repo['id'],pr['number'],pr['head']['sha'])
    reviews=s.h.gitea('GET',f'/repos/{s.full}/pulls/{pr["number"]}/reviews',actor='developer')
    s.record('输出截断' if not delay else '真实模型请求超时',{'通过':task['status']=='failed' and not reviews,
             '任务':task['id'],'错误':task['error'],'耗时秒':round(elapsed,2),'PR':s.h.gitea_url+f'/{s.full}/pulls/{pr["number"]}'})
s.control('normal',8);pr=s.branch('closed',{'closed.c':code})
for _ in range(100):
    if any(t['number']==pr['number'] and t['status']=='running' for t in s.h.manager('GET',f'/ai-review/tasks?repository={s.repo["id"]}')['items']):break
    time.sleep(.25)
s.h.gitea('PATCH',f'/repos/{s.full}/pulls/{pr["number"]}',{'state':'closed'})
time.sleep(20)
tasks=[t for t in s.h.manager('GET',f'/ai-review/tasks?repository={s.repo["id"]}')['items'] if t['number']==pr['number']]
reviews=s.h.gitea('GET',f'/repos/{s.full}/pulls/{pr["number"]}/reviews',actor='bot')
s.record('真实关闭事件取消模型中的任务',{'通过':bool(tasks) and all(t['status']=='superseded' for t in tasks) and not reviews})
s.control()
repo=s.h.manager('GET','/ai-review/repositories');repo=next(r for r in repo if r['id']==s.repo['id'])
s.h.gitea('PATCH',f'/repos/{s.full}/hooks/{repo["hook_id"]}',{'active':False})
pr=s.branch('missing-event',{'missing.c':code})
# 缩短测试等待，不直接排入任务：独立 Worker 的实际定期核对必须发现 PR。
local.compose('exec','-T','review-worker','python','-c',
    'from ai_review.store import Store; s=Store(); c=s.connect(); c.execute("UPDATE repositories SET last_reconcile=0 WHERE id=?",('+str(repo['id'])+',)); c.commit()')
s.check('Webhook 缺失由实际 Worker 扫描补偿',pr,[('missing.c','new',2)])
s.h.manager('POST',f'/ai-review/repositories/{repo["id"]}/webhook',{'manager_url':'http://backend:5000'})
print('补充验收报告：'+str(s.out))
