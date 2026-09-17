#!/usr/bin/env python3
"""仅在独立标记实例上验证紧急待办；真实 Gitea，模型使用确定性协议替身。"""
import argparse
import base64
import json
import runpy
import time
from pathlib import Path

real = runpy.run_path(str(Path(__file__).with_name('review-real-acceptance.py')))
local = real['local']


def main():
    parser = argparse.ArgumentParser(description='隔离 Review 紧急待办验收')
    parser.add_argument('--seed-ui', action='store_true', help='流程通过后保留大表格与两组待办，供页面验收')
    args = parser.parse_args()
    h = local.Harness()
    if not h.values.get('REVIEW_PROJECT', '').startswith(('gitea-review-attention-', 'gitea-review-direct-attention-')):
        raise RuntimeError('仅允许明确标记的紧急待办隔离实例')
    h.login()
    if h.manager('GET', '/ai-review/config')['config']['model'] != 'review-fixture':
        raise RuntimeError('必须使用隔离模型协议替身')
    results = []; out = local.STATE / ('真实待办验收-'+str(int(time.time()))+'.json')

    def record(name, condition, **evidence):
        results.append(dict(场景=name, 通过=bool(condition), **evidence))
        out.write_text(json.dumps({'边界':'独立 Linux amd64、官方 Gitea 1.26.4、模型协议替身；非内网现场或模型质量验收', '结果':results}, ensure_ascii=False, indent=2)+'\n')
        print(name+'：'+('通过' if condition else '失败'), flush=True)
        if not condition: raise RuntimeError('首次失败已保存：'+str(out))

    def inside(source):
        return json.loads(local.run(['docker','exec','-i',h.values['REVIEW_PROJECT']+'-backend-1','python','-'],
            input='import json\nfrom ai_review.store import Store\ns=Store()\n'+source))

    def mark_unknown(ident):
        return inside(f"with s.tx() as c:\n c.execute(\"UPDATE jobs SET status='publish_unknown',available=9999999999,stage='发布结果待核对',error='隔离故障注入：发布回执丢失',lease=0 WHERE id=?\",({ident!r},))\nprint(json.dumps(s.job({ident!r},public=True)))")

    def queue(repo, pr):
        return inside(f"print(json.dumps(s.enqueue({repo['id']},{pr['number']},{{'head':{pr['head']['sha']!r},'base':{pr['base']['sha']!r}}},manual=True)))")

    def task(ident): return h.manager('GET','/ai-review/tasks/'+ident)
    def reviews(repo, pr): return h.gitea_pages(f"/repos/{repo['repo_name']}/pulls/{pr['number']}/reviews")
    def attention(): return h.manager('GET','/ai-review/attention')
    def request(ident): return h.manager('POST',f'/ai-review/tasks/{ident}/reconcile',{})
    def check(): return inside('from ai_review.worker import reconcile_unknown\nreconcile_unknown(s)\nprint(json.dumps(True))')
    def stop(): local.compose('stop','review-worker')
    def start(): local.compose('start','review-worker')

    def new_pr(name):
        repo=h.create_repo(name); folder=real['clone'](h,name)
        branch='attention-'+str(time.time_ns())
        real['git'](h,folder,'switch','-c',branch)
        (folder/'attention.c').write_text('int probe(void) {\n    return 1; /* REVIEW_TEST_TARGET */\n}\n')
        real['git'](h,folder,'add','.'); real['git'](h,folder,'commit','-m','新增紧急待办隔离样本');real['git'](h,folder,'push','origin','HEAD')
        pr=h.gitea('POST',f"/repos/{repo['repo_name']}/pulls",{'head':branch,'base':'main','title':'紧急待办真实流程验收','body':'仅包含隔离合成样本。'})
        return repo,pr

    repo,pr=new_pr('attention-storage')
    published,_=real['wait_task'](h,repo['id'],pr['number'],pr['head']['sha'])
    record('真实审查发布基线',published['status']=='succeeded',任务=published['id'],PR=h.gitea_url+f"/{repo['repo_name']}/pulls/{pr['number']}")
    stop(); mark_unknown(published['id']); successor=queue(repo,pr)
    record('跨版本阻塞计数',attention()['summary']==dict(pr_count=1,blocked_task_count=1,pending_task_count=1),汇总=attention()['summary'])
    progress=inside(f"from ai_review.progress import ProgressSync\nwith s.tx() as c: c.execute('UPDATE pr_state SET progress_next=0 WHERE repository=? AND number=?',({repo['id']},{pr['number']}))\nprint(json.dumps(ProgressSync(s).sync({repo['id']},{pr['number']})))")
    comments=h.gitea_pages(f"/repos/{repo['repo_name']}/issues/{pr['number']}/comments")
    record('真实 PR 进度显示旧版本阻塞',progress and any('等待管理员核对前次发布结果' in row.get('body','') for row in comments))
    before=len(reviews(repo,pr)); first=request(published['id']);second=request(published['id']);check()
    record('回执丢失只读核对且重复请求合并',first['requested'] and not second['requested'] and task(published['id'])['status']=='succeeded' and len(reviews(repo,pr))==before)
    record('已发布核对解除阻塞',attention()['summary']['pr_count']==0 and not task(successor)['attention']['required'])
    start();next_task,_=real['wait_task'](h,repo['id'],pr['number'],pr['head']['sha'],job_id=successor)
    record('后续 Worker 正常领取完成',next_task['status']=='succeeded',任务=successor)
    stop()
    unknown=queue(repo,pr)
    inside(f"with s.tx() as c:\n c.execute('UPDATE jobs SET snapshot=(SELECT snapshot FROM jobs WHERE id=?),result=(SELECT result FROM jobs WHERE id=?) WHERE id=?',({published['id']!r},{published['id']!r},{unknown!r}))\nprint(json.dumps(True))")
    mark_unknown(unknown); waiting=queue(repo,pr); request(unknown);check()
    record('未发布不会自动误解除',task(unknown)['reconciliation']['state']=='needs_confirmation' and task(waiting)['attention']['required'])
    path=f"/repos/{repo['repo_name']}/pulls/{pr['number']}/reviews"
    draft=h.gitea('POST',path,{'event':'PENDING','commit_id':pr['head']['sha'],'body':'隔离待办验收草稿','comments':[]},actor='bot')
    rejected=False
    try: h.manager('POST',f'/ai-review/tasks/{unknown}/reconcile',{'confirm_unpublished':True})
    except RuntimeError: rejected=True
    record('残留草稿阻止确认未发布',rejected and task(unknown)['status']=='publish_unknown',草稿=draft['id'])
    h.gitea('DELETE',path+'/'+str(draft['id']),actor='bot')
    inside(f"with s.tx() as c:\n c.execute(\"UPDATE repositories SET value=json_set(value,'$.gitea_url','http://gitea:1') WHERE id=?\",({repo['id']},))\nprint(json.dumps(True))")
    try:
        request(unknown);check();failed=task(unknown)
        record('真实连接失败保留待办',failed['status']=='publish_unknown' and failed['reconciliation']['state']=='failed',说明=failed['reconciliation']['message'])
    finally:
        inside(f"with s.tx() as c:\n c.execute(\"UPDATE repositories SET value=json_set(value,'$.gitea_url','http://gitea:3000') WHERE id=?\",({repo['id']},))\nprint(json.dumps(True))")
    inside("with s.tx() as c: c.execute(\"UPDATE health SET updated=0 WHERE name='worker'\")\nprint(json.dumps(True))")
    record('Worker 不可用显示真实心跳状态',not attention()['worker']['available'])
    first=h.manager('POST',f'/ai-review/tasks/{unknown}/reconcile',{'confirm_unpublished':True})
    second=h.manager('POST',f'/ai-review/tasks/{unknown}/reconcile',{'confirm_unpublished':True})
    record('确认未发布及其他会话重复处理',first['task']['status']=='failed' and second['task']['status']=='failed' and attention()['summary']['pr_count']==0)
    start();done,_=real['wait_task'](h,repo['id'],pr['number'],pr['head']['sha'],job_id=waiting)
    record('确认未发布后后续审查完成',done['status']=='succeeded',任务=waiting)
    if args.seed_ui:
        second_repo,second_pr=new_pr('attention-bootloader')
        second_done,_=real['wait_task'](h,second_repo['id'],second_pr['number'],second_pr['head']['sha'])
        record('第二个真实 PR 基线',second_done['status']=='succeeded')
        stop(); mark_unknown(published['id']);mark_unknown(second_done['id'])
        inside(f"for n in range(205):\n ident=s.enqueue({repo['id']},{pr['number']},{{'head':'历史-'+str(n)}},manual=True)\n with s.tx() as c: c.execute(\"UPDATE jobs SET status='superseded' WHERE id=?\",(ident,))\nprint(json.dumps(True))")
        queue(repo,pr);queue(second_repo,second_pr)
        inside("with s.tx() as c: c.execute(\"UPDATE health SET updated=strftime('%s','now') WHERE name IN ('worker','progress','feedback')\")\nprint(json.dumps(True))")
        record('超过 200 条历史末页阻塞仍在全局首位',attention()['summary']['pr_count']==2 and attention()['summary']['blocked_task_count']==2 and h.manager('GET','/ai-review/tasks')['total']>=200,汇总=attention()['summary'],历史总数=h.manager('GET','/ai-review/tasks')['total'])
    print('验收结果：'+str(out),flush=True)

if __name__=='__main__': main()
