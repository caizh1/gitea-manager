#!/usr/bin/env python3
"""连续真实运行 OpenCode，验证动态库和模型任务临时数据不会累积。"""
import argparse
import json
from pathlib import Path
import runpy
import time
import re

parser=argparse.ArgumentParser();parser.add_argument('--runs',type=int,default=40);parser.add_argument('--resume-pr',type=int);args=parser.parse_args()
m=runpy.run_path(str(Path(__file__).with_name('review-boundary-acceptance.py')))
s=m['Suite']('endurance-fixtures');s.control()
if args.resume_pr:
    pr=s.h.gitea('GET',f'/repos/{s.full}/pulls/{args.resume_pr}')
    m['git'](s.h,s.folder,'switch',pr['head']['ref'])
    reviews=s.h.gitea_pages(f'/repos/{s.full}/pulls/{pr["number"]}/reviews',actor='developer')
    completed=len(reviews)
    latest=max(reviews,key=lambda r:r['id'])
    previous_job=re.search(r'ai-review:v1:job:([0-9a-f]{32})',latest['body']).group(1)
    first=s.check('回读既有连续执行结果',pr,[],job_id=previous_job)
else:
    s.prepare();pr=s.branch('连续执行',{'endurance.c':b'int endurance(void) {\n    return 1; /* REVIEW_TEST_TARGET */\n}\n'})
    first=s.check('连续执行第 1 次',pr,[('endurance.c','new',2)]);completed=1
local=m['local'];project=s.h.values.get('REVIEW_PROJECT','gitea-review-local');observations=[]
probe='import os,json\ns=os.statvfs("/tmp");print(json.dumps({"used":(s.f_blocks-s.f_bfree)*s.f_frsize,"entries":len(os.listdir("/tmp"))}))'
calls=first['model_calls'];out=local.STATE/('OpenCode连续执行-'+str(int(time.time()))+'.json')
for index in range(completed-1,args.runs):
    if index>=completed:
        job=s.h.manager('POST',f'/ai-review/repositories/{s.repo["id"]}/rerun',{'number':pr['number']})['job']
        task=s.check('连续执行第 '+str(index+1)+' 次',pr,[],job_id=job);calls+=task['model_calls']
    raw=local.run(['docker','exec',project+'-review-executor-1','python','-c',probe])
    observations.append(json.loads(raw))
    out.write_text(json.dumps({'说明':'真实 Linux OpenCode；协议替身。恢复时不重复已完成任务，当前记录仅包含恢复后的实际空间采样。',
        '起始已完成任务':completed,'累计Review数量':index+1,'本段模型调用':calls,'临时空间采样':observations,
        '通过':all(r['used']<=1024*1024 for r in observations)},ensure_ascii=False,indent=2)+'\n')
    if observations[-1]['used']>1024*1024:raise RuntimeError('任务结束后临时空间仍超过 1 MB，停止并保留证据')
print('验收记录：'+str(out))
