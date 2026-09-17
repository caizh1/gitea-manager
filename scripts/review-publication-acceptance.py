#!/usr/bin/env python3
"""真实 Gitea 直接发布、版本变化、响应丢失与残留草稿的隔离验收。"""
import json
from pathlib import Path
import runpy
import time
import argparse

parser=argparse.ArgumentParser();parser.add_argument('--modes',default='normal,diff_race,before_publish,response_lost,partial_write,rejected,expired_owner');args=parser.parse_args()
real=runpy.run_path(str(Path(__file__).with_name('review-real-acceptance.py')))
local=real['local'];h=local.Harness();h.login();name='publication-fixtures'
repo=h.create_repo(name)
h.manager('PUT',f'/ai-review/repositories/{repo["id"]}',dict(enabled=False,revision=repo['revision']))
folder=real['clone'](h,name);git=real['git'];suffix=str(int(time.time()));results=[]
for mode in args.modes.split(','):
    branch=mode+'-'+suffix
    git(h,folder,'switch','main');git(h,folder,'switch','-c',branch)
    (folder/'probe.c').write_text('int probe(void) {\n    return 1; /* REVIEW_TEST_TARGET */\n}\n')
    git(h,folder,'add','.');git(h,folder,'commit','-m','合成发布竞争测试');git(h,folder,'push','origin','HEAD')
    pr=h.gitea('POST',f'/repos/{repo["repo_name"]}/pulls',{'head':branch,'base':'main','title':'发布竞争 '+mode})
    h.gitea('POST',f'/repos/{repo["repo_name"]}/issues/{pr["number"]}/comments',{'body':'开发者原有讨论，请保留。'},actor='developer')
    payload={'mode':mode,'repo':repo['repo_name'],'repo_id':repo['repo_id'],'number':pr['number'],'branch':branch,
             'admin':h.values['GITEA_TOKEN'],'bot':h.values['BOT_TOKEN']}
    network=h.values.get('REVIEW_PROJECT','gitea-review-local')+'_default'
    output=local.run(['docker','run','--rm','-i','--pull=never','--platform','linux/amd64','--network',network,
        '-e','PYTHONDONTWRITEBYTECODE=1','-v',str(local.ROOT/'backend')+':/app:ro','-w','/app',
        'gitea-review-backend:'+h.values.get('REVIEW_IMAGE_TAG','local'),'python','-m','tests.live_review_driver'],input=json.dumps(payload),check=False)
    row=json.loads(output);row['PR']=h.gitea_url+f'/{repo["repo_name"]}/pulls/{pr["number"]}';results.append(row)
    out=local.STATE/('真实发布竞争-'+suffix+'.json');out.write_text(json.dumps({'说明':'真实 Gitea；注入提交与模拟丢弃成功响应的控制器仅用于合成验收。','结果':results},ensure_ascii=False,indent=2)+'\n')
    print(mode+'：'+('通过' if row['通过'] else '失败'),flush=True)
    if not row['通过']:raise RuntimeError('发布竞争验收失败，记录：'+str(out))
print('验收记录：'+str(out))
