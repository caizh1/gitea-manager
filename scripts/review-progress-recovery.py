#!/usr/bin/env python3
"""独立真实 PR 的连续提交、Worker 重启、关闭和开关验收。"""
import base64
import importlib.util
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]
s = importlib.util.spec_from_file_location('本机', ROOT/'scripts/review-local.py')
m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
h = m.Harness()
if 'progress' not in h.values.get('REVIEW_PROJECT',''): raise RuntimeError('禁止操作用户手动测试环境')
h.login(); repo = next(r for r in h.manager('GET','/ai-review/repositories') if r['repo_name']=='review-admin/progress-acceptance')
prefix = '/repos/'+repo['repo_name']; records=[]

def record(label, value):
    records.append({'项目':label,'结果':value}); print(label+'：'+json.dumps(value,ensure_ascii=False),flush=True)

def task(job): return h.manager('GET','/ai-review/tasks/'+job)

def wait(predicate, timeout=180):
    until=time.time()+timeout
    while time.time()<until:
        value=predicate()
        if value:return value
        time.sleep(.5)
    raise RuntimeError('等待验收条件超时')

def status():
    rows=h.gitea('GET',prefix+'/issues/1/comments',actor='developer')
    owned=[r for r in rows if r['user']['id']==repo['bot_id'] and r['body'].startswith('<!-- ai-review:summary:v1 -->')]
    assert len(owned)==1
    return json.loads(base64.urlsafe_b64decode(owned[0]['body'].split('<!-- ai-review:progress:v1:')[1].split(' -->')[0]))

wait(lambda:not any(j['status'] in ('queued','running','publishing','recovering') for j in h.manager('GET','/ai-review/tasks')['items']))
fixture='http://127.0.0.1:'+h.values['REVIEW_FIXTURE_PORT']
h.request('POST',fixture+'/test/control',{'mode':'normal','delay':5},{'Authorization':'Bearer '+h.values['INTEGRATION_TOKEN']})
old=h.manager('POST',f"/ai-review/repositories/{repo['id']}/rerun",{'number':1})['job']
wait(lambda:task(old)['progress']['stage']=='analysis')
pr=h.gitea('GET',prefix+'/pulls/1');branch=pr['head']['ref']
source=h.gitea('GET',prefix+'/contents/progress.c?ref='+branch)
content=base64.b64decode(source['content']).decode()+'\nint newly_added(int value) { return value > 8 ? 8 : value; }\n'
head=h.gitea('PUT',prefix+'/contents/progress.c',{'branch':branch,'sha':source['sha'],'content':base64.b64encode(content.encode()).decode(),'message':'连续提交进度验收'})['commit']['sha']
wait(lambda:task(old)['status']=='superseded')
latest=wait(lambda:next((j for j in h.manager('GET',f"/ai-review/tasks?repository={repo['id']}")['items'] if j['number']==1 and j['id']!=old and j['progress']['head']==head),None))
record('新提交隔离旧任务',{'旧任务':old,'最新任务':latest['id'],'最新SHA':head})
wait(lambda:task(latest['id'])['progress']['stage']=='analysis')
m.compose('stop','-t','1','review-worker')
# 仅将租约时间快进以缩短验收等待；工作进程本身确实已经被停止。
code="import sqlite3,time; c=sqlite3.connect('/app/review/review.db'); c.execute(\"UPDATE jobs SET lease=? WHERE id=? AND status='running'\",(time.time()-1,"+repr(latest['id'])+")); c.execute('UPDATE pr_state SET progress_seq=progress_seq+1'); c.commit()"
m.compose('exec','-T','backend','python','-c',code)
wait(lambda:status()['status']=='interrupted',timeout=15)
record('真实Worker中断状态',{'任务':latest['id'],'状态':status()['status'],'说明':'实际停止进程，租约时间通过独立数据库快进。'})
m.compose('start','review-worker')
wait(lambda:task(latest['id'])['status']=='succeeded')
wait(lambda:status()['status']=='succeeded')
record('Worker重启恢复',{'任务':latest['id'],'执行尝试':task(latest['id'])['attempts'],'完成单元':status()['units_completed']})
h.gitea('PATCH',prefix+'/pulls/1',{'state':'closed'})
wait(lambda:status()['status']=='stopped',timeout=15)
record('PR关闭',{'状态':status()['status']})
# 对已结束任务关闭全局开关，验证仍会更新已存在的状态评论。
config=h.manager('GET','/ai-review/config')['config'];config.pop('api_key_configured');config['enabled']=False
h.manager('PUT','/ai-review/config',config)
wait(lambda:not status()['enabled'],timeout=15)
record('全局关闭',{'状态':status()['status'],'enabled':status()['enabled']})
path=m.STATE/'进度恢复验收.json';path.write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n')
print('验收记录：'+str(path),flush=True)
