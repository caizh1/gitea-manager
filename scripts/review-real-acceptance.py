#!/usr/bin/env python3
"""真实 Git → Gitea Webhook → 独立执行器 → 真实模型 → Review 回写验收。"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import time


def module(name, filename):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(filename))
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod


local=module('review_local','review-local.py')
fixtures=module('review_fixtures','review-fixtures.py')


def git(harness, folder, *args):
    helper=local.STATE/'git-askpass.sh'
    if not helper.exists():
        helper.write_text('#!/bin/sh\n# 仅从本次子进程环境读取本机测试令牌。\ncase "$1" in *Username*) printf "%s" "review-admin";; *) printf "%s" "$REVIEW_GIT_TOKEN";; esac\n')
        helper.chmod(0o700)
    return local.run(['git',*args],cwd=folder,env={'GIT_ASKPASS':str(helper),'GIT_TERMINAL_PROMPT':'0',
                    'REVIEW_GIT_TOKEN':harness.values['GITEA_TOKEN']})


def clone(harness, name):
    folder=local.STATE/name
    if not (folder/'.git').exists():
        git(harness,local.STATE,'clone',harness.gitea_url+'/review-admin/'+name+'.git',str(folder))
        git(harness,folder,'config','user.name','本机验收开发者')
        git(harness,folder,'config','user.email','review-local@example.invalid')
    return folder


def wait_task(harness, repository, number, sha, timeout=600, job_id=None):
    start=time.monotonic()
    while time.monotonic()-start<timeout:
        data=harness.manager('GET',f'/ai-review/tasks?repository={repository}')
        tasks=[t for t in data['items'] if t['number']==number]
        for row in tasks:
            if job_id and row['id'] != job_id: continue
            if row['status'] in ('succeeded','failed','publish_unknown'):
                task=harness.manager('GET','/ai-review/tasks/'+row['id'])
                if task.get('snapshot') and task['snapshot']['head']!=sha:continue
                return task,time.monotonic()-start
        time.sleep(1)
    raise RuntimeError(f'PR #{number} 未在 {timeout} 秒内完成')


def execute(harness, configured, folder, case, suffix, resume=False):
    full=configured['repo_name']; branch=case['id']+'-'+suffix
    existing=harness.gitea('GET',f'/repos/{full}/pulls?state=all&limit=100') if resume else []
    pr=next((p for p in existing if p['title']=='合成审查样本 '+case['id']),None)
    if pr:
        sha=pr['head']['sha']
    else:
        git(harness,folder,'switch','main')
        git(harness,folder,'switch','-c',branch)
        (folder/case['path']).write_text(case['code'])
        git(harness,folder,'add',case['path']);git(harness,folder,'commit','-m','新增合成代码验收样本 '+case['id'])
        sha=git(harness,folder,'rev-parse','HEAD').strip()
        git(harness,folder,'push','origin',branch)
        pr=harness.gitea('POST',f'/repos/{full}/pulls',{'head':branch,'base':'main','title':'合成审查样本 '+case['id'],
                    'body':'仅包含公开构造的合成 C/C++ 代码，用于本机功能验收。'})
    task,elapsed=wait_task(harness,configured['id'],pr['number'],sha)
    report={'样本':case['id'],'名称':case['名称'],'PR':harness.gitea_url+f'/{full}/pulls/{pr["number"]}',
            'PR编号':pr['number'],'Head':sha,'任务':task['id'],'状态':task['status'],'耗时秒':round(elapsed,2),
            '错误':task['error'],'存在缺陷':case['存在缺陷'],'允许问题行':case['允许问题行'],'行内评论':[],
            '问题':task.get('result',{}).get('findings',[]) if task.get('result') else []}
    if task['status']=='succeeded' and task.get('review_id'):
        review=harness.gitea('GET',f'/repos/{full}/pulls/{pr["number"]}/reviews/{task["review_id"]}',actor='developer')
        comments=harness.gitea('GET',f'/repos/{full}/pulls/{pr["number"]}/reviews/{task["review_id"]}/comments',actor='developer')
        report['Review版本正确']=review['commit_id']==sha
        report['普通开发者可见']=review['state']=='COMMENT'
        summaries=harness.gitea('GET',f'/repos/{full}/issues/{pr["number"]}/comments',actor='developer')
        summaries=[s for s in summaries if s.get('user',{}).get('login')=='review-bot' and s.get('body','').startswith('<!-- ai-review:summary:v1 -->')]
        report['总体评论正确']=len(summaries)==1 and sha in summaries[0]['body']
        timeline=harness.gitea_pages(f'/repos/{full}/issues/{pr["number"]}/timeline',actor='developer')
        report['Review动态唯一']=sum(t.get('type')=='review' and t.get('review_id')==task['review_id'] for t in timeline)==1
        for comment in comments:
            line=comment.get('position') or comment.get('original_position')
            report['行内评论'].append({'文件':comment['path'],'新侧':comment.get('position'),'旧侧':comment.get('original_position'),
                                       '位置正确':comment['path']==case['path'] and line in case['允许问题行'],
                                       '页面':comment.get('html_url')})
    report['链路通过']=task['status']=='succeeded' and all(report.get(k,False) for k in ['Review版本正确','普通开发者可见','总体评论正确','Review动态唯一'])
    report['定位通过']=all(c['位置正确'] for c in report['行内评论'])
    return report


def main():
    parser=argparse.ArgumentParser(description='运行真实模型与真实 Gitea 合成基准')
    parser.add_argument('--count',type=int,default=50)
    parser.add_argument('--start',type=int,default=1)
    parser.add_argument('--name',default='review-fixtures')
    parser.add_argument('--resume-existing',action='store_true',help='只回读已经存在的样本 PR，不重复创建或调用模型')
    args=parser.parse_args();harness=local.Harness();harness.login()
    cfg=harness.manager('GET','/ai-review/config')['config']
    if not cfg['enabled']:raise RuntimeError('请先配置并启用测试模型')
    repo=harness.create_repo(args.name);folder=clone(harness,args.name)
    suffix=str(int(time.time()));reports=[]
    out=local.STATE/f'真实模型验收-{suffix}.json'
    for case in fixtures.cases()[args.start-1:args.start-1+args.count]:
        row=execute(harness,repo,folder,case,suffix,args.resume_existing);reports.append(row)
        out.write_text(json.dumps({'说明':'真实 Gitea、真实 Git、真实模型的合成代码验收；不代表公司真实 PR 或内部模型质量通过。',
                                  '引擎':cfg['engine'],'模型':cfg['model'],'结果':reports},ensure_ascii=False,indent=2)+'\n')
        print(f"{row['样本']}：{row['状态']}，问题 {len(row['问题'])}，行内 {len(row['行内评论'])}，位置 {'通过' if row['定位通过'] else '失败'}，{row['耗时秒']} 秒",flush=True)
        if not row['链路通过']:
            print('保留首次失败证据，停止基准批次以排查原因。',flush=True);break
    print('验收记录：'+str(out))


if __name__=='__main__':main()
