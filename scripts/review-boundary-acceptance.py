#!/usr/bin/env python3
"""真实 Git、Webhook、Worker、Linux OpenCode 与协议替身的边界和异常验收。"""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import runpy
import time
import urllib.request

real = runpy.run_path(str(Path(__file__).with_name('review-real-acceptance.py')))
local = real['local']; git = real['git']; wait_task = real['wait_task']


class Suite:
    def __init__(self, name):
        self.h = local.Harness(); self.h.login()
        if self.h.manager('GET', '/ai-review/config')['config']['model'] != 'review-fixture':
            raise RuntimeError('本脚本只允许连接隔离协议替身，不能替代真实模型验收')
        self.repo = self.h.create_repo(name); self.full = self.repo['repo_name']
        self.folder = real['clone'](self.h, name); self.suffix = str(int(time.time()))
        self.results = []; self.out = local.STATE / ('边界与异常验收-'+self.suffix+'.json')
        self.base_files = {
            'modify.c': b'int change(void) {\n    return 0;\n}\n',
            'delete.c': b'int removed(void) {\n    int value = (1 + 0); /* REVIEW_TEST_TARGET */\n    return value;\n}\n',
            'rename.c': b'/* '+'\n * 合成重命名上下文'.encode()*15+b'\n */\nint moved(void) {\n    return 0;\n}\n',
            'deleted-file.c': b'int deleted(void) {\n    return 1; /* REVIEW_TEST_TARGET */\n}\n',
            'multi.c': b'\n'.join(b'int function_%d(void) {\n    return %d;\n}\n' % (i,i) for i in range(8)),
        }

    def control(self, mode='normal', delay=0):
        return self.h.request('POST', 'http://127.0.0.1:'+self.h.values.get('REVIEW_FIXTURE_PORT','23053')+'/test/control',
                              {'mode':mode,'delay':delay}, {'Authorization':'Bearer '+self.h.values['INTEGRATION_TOKEN']})

    def record(self, name, value):
        self.results.append(dict(场景=name, **value))
        self.out.write_text(json.dumps({'说明':'真实 Gitea 与执行器，模型为协议替身；不计入模型准确率。', '结果':self.results},ensure_ascii=False,indent=2)+'\n')
        print(name+'：'+('通过' if value.get('通过') else '失败'),flush=True)
        if not value.get('通过'): raise RuntimeError('保留首次失败证据：'+str(self.out))

    def prepare(self):
        git(self.h,self.folder,'switch','main')
        for path, content in self.base_files.items(): (self.folder/path).write_bytes(content)
        git(self.h,self.folder,'add','.');git(self.h,self.folder,'commit','--allow-empty','-m','准备合成边界基线')
        git(self.h,self.folder,'push','origin','main')

    def branch(self, name, changes):
        git(self.h,self.folder,'switch','main');git(self.h,self.folder,'switch','-c',name+'-'+self.suffix)
        self.change(changes)
        git(self.h,self.folder,'push','origin','HEAD')
        return self.h.gitea('POST',f'/repos/{self.full}/pulls',{'head':name+'-'+self.suffix,'base':'main','title':'协议验收 '+name,'body':'合成代码，非真实模型质量验收。'})

    def change(self, changes):
        for path, content in changes.items():
            target=self.folder/path
            if content is None: target.unlink()
            else: target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content)
        git(self.h,self.folder,'add','-A');git(self.h,self.folder,'commit','--allow-empty','-m','更新合成边界代码')

    def check(self, name, pr, expected, job_id=None):
        sha=git(self.h,self.folder,'rev-parse','HEAD').strip()
        task, elapsed=wait_task(self.h,self.repo['id'],pr['number'],sha,job_id=job_id)
        comments=[]; timeline=[]; review={}
        if task['status']=='succeeded':
            base=f'/repos/{self.full}/pulls/{pr["number"]}/reviews/{task["review_id"]}'
            review=self.h.gitea('GET',base,actor='developer')
            comments=self.h.gitea('GET',base+'/comments',actor='developer')
            timeline=self.h.gitea_pages(f'/repos/{self.full}/issues/{pr["number"]}/timeline',actor='developer')
        actual=sorted((c['path'],'new' if c['position'] else 'old', c['position'] or c['original_position']) for c in comments)
        unique=sum(t.get('type')=='review' and t.get('review_id')==task['review_id'] for t in timeline)==1
        self.record(name, {'通过':task['status']=='succeeded' and actual==sorted(expected) and review.get('commit_id')==sha and unique,
            'PR':self.h.gitea_url+f'/{self.full}/pulls/{pr["number"]}', '任务':task['id'],'Head':sha,'状态':task['status'],
            '预期位置':expected,'实际位置':actual,'动态唯一':unique,'耗时秒':round(elapsed,2),'错误':task['error'],
            '降级原因':[f['fallback_reason'] for f in (task.get('result') or {}).get('findings',[]) if not f.get('anchor')]})
        return task

    def boundaries(self):
        target=b'int added(void) {\n    return 1; /* REVIEW_TEST_TARGET */\n}\n'
        cases=[('新增',{'added.c':target},[('added.c','new',2)]),
            ('修改',{'modify.c':target.replace(b'added',b'change')},[('modify.c','new',2)]),
            ('删除行',{'delete.c':b'int removed(void) {\n    return 0;\n}\n'},[('delete.c','old',2)]),
            ('删除文件',{'deleted-file.c':None},[('deleted-file.c','old',2)]),
            ('重命名特殊路径',{'rename.c':None,'目录/带 空格名称.c':self.base_files['rename.c'].replace(b'return 0;',b'return 1; /* REVIEW_TEST_TARGET */')},[('目录/带 空格名称.c','new',19)]),
            ('CRLF',{'crlf.c':target.replace(b'\n',b'\r\n')},[('crlf.c','new',2)]),
            ('末尾无换行',{'no-eol.c':target.rstrip(b'\n')},[('no-eol.c','new',2)]),
            ('多个修改区域',{'multi.c':self.base_files['multi.c'].replace(b'return 0;',b'return 10; /* REVIEW_TEST_TARGET */').replace(b'return 7;',b'return 17; /* REVIEW_TEST_TARGET */')},[('multi.c','new',2),('multi.c','new',30)]),
            ('重复函数片段',{'similar.c':target+target.replace(b'added',b'another')},[('similar.c','new',2),('similar.c','new',5)]),
            ('函数内重复原文降级',{'ambiguous.c':b'void repeated(void) {\n    helper(); /* REVIEW_TEST_TARGET */\n    helper(); /* REVIEW_TEST_TARGET */\n}\n'},[]),
        ]
        for index,(name,changes,expected) in enumerate(cases):
            self.control();pr=self.branch('boundary-'+str(index),changes);self.check(name,pr,expected)
        for mode in ('wrong_anchor','wrong_quote','unlocated'):
            self.control(mode);pr=self.branch(mode,{'fallback.c':target});self.check(mode+' 安全降级',pr,[])

    def faults(self):
        target=b'int probe(void) {\n    return 1; /* REVIEW_TEST_TARGET */\n}\n'
        for mode in ('429','500','invalid_json','tools','truncated'):
            self.control(mode);pr=self.branch('fault-'+mode,{'fault.c':target})
            sha=git(self.h,self.folder,'rev-parse','HEAD').strip()
            task,elapsed=wait_task(self.h,self.repo['id'],pr['number'],sha)
            reviews=self.h.gitea('GET',f'/repos/{self.full}/pulls/{pr["number"]}/reviews',actor='developer')
            responsive=self.h.manager('GET','/session')['authenticated']
            self.record('模型异常 '+mode,{'通过':task['status']=='failed' and not reviews and responsive,'任务':task['id'],'状态':task['status'],'错误':task['error'],'耗时秒':round(elapsed,2)})
        self.control()

    def push_race(self):
        self.control(delay=8)
        code=b'int racing(void) {\n    return 1; /* REVIEW_TEST_TARGET */\n}\n'
        pr=self.branch('连续提交竞争',{'race.c':code});old=git(self.h,self.folder,'rev-parse','HEAD').strip()
        for _ in range(100):
            tasks=self.h.manager('GET',f'/ai-review/tasks?repository={self.repo["id"]}')['items']
            if any(t['number']==pr['number'] and t['status']=='running' and t['stage'].startswith('分析') for t in tasks):break
            time.sleep(.25)
        else: raise RuntimeError('未进入模型执行阶段，不能宣称竞争测试成立')
        self.change({'race.c':'/* 新提交使问题位置下移 */\n'.encode()+code});git(self.h,self.folder,'push','origin','HEAD')
        self.control()
        self.check('模型执行期间 Push 只发布最新版本',pr,[('race.c','new',3)])
        rows=self.h.gitea('GET',f'/repos/{self.full}/pulls/{pr["number"]}/reviews',actor='developer')
        self.record('旧版本无正式评论',{'通过':all(r['commit_id']!=old for r in rows),'旧Head':old,'正式Review数量':len(rows)})

    def reliability(self):
        self.control(delay=20)
        code=b'int restarted(void) {\n    return 1; /* REVIEW_TEST_TARGET */\n}\n'
        pr=self.branch('重启恢复',{'restart.c':code});original=None
        for _ in range(120):
            tasks=self.h.manager('GET',f'/ai-review/tasks?repository={self.repo["id"]}')['items']
            original=next((t for t in tasks if t['number']==pr['number'] and t['status']=='running' and t['stage'].startswith('分析')),None)
            if original:break
            time.sleep(.25)
        if not original:raise RuntimeError('任务未进入模型阶段，无法执行真实重启测试')
        local.compose('stop','-t','0','review-worker');self.control();local.compose('start','review-worker')
        task=self.check('Worker 强制中断后租约恢复',pr,[('restart.c','new',2)],job_id=original['id'])
        self.record('重启不重复创建任务与 Review',{'通过':task['id']==original['id'] and task['model_calls']>1,'任务':task['id'],'模型调用':task['model_calls']})
        # 固定模型标题和问题未变化时，应重新核对新锚点，同时引用已有讨论。
        self.change({'restart.c':'/* 后续提交移动代码位置 */\n'.encode()+code});git(self.h,self.folder,'push','origin','HEAD')
        updated=self.check('后续提交重新定位并保留历史讨论',pr,[])
        self.record('新版本锚点确实重新计算',{'通过':updated['result']['findings'][0]['anchor']['line']==3 and bool(updated['result']['findings'][0].get('existing_review_id')),
                    '新行号':updated['result']['findings'][0]['anchor']['line']})
        current=git(self.h,self.folder,'branch','--show-current').strip()
        git(self.h,self.folder,'switch','main');self.change({'base-new.txt':'合成基线推进\n'.encode()});git(self.h,self.folder,'push','origin','main')
        git(self.h,self.folder,'switch',current);git(self.h,self.folder,'rebase','main');git(self.h,self.folder,'push','--force-with-lease','origin','HEAD')
        self.check('Rebase 与 force-push 绑定新 SHA',pr,[])
        latest=self.h.manager('POST',f'/ai-review/repositories/{self.repo["id"]}/rerun',{'number':pr['number']})
        manual=self.check('人工重新审查创建独立执行代次',pr,[],job_id=latest['job'])
        self.record('人工任务独立',{'通过':manual['manual']==1 and manual['id']!=original['id'],'任务':manual['id']})

    def cumulative(self):
        self.control()
        code=b'int cumulative(void) {\n    return 1; /* REVIEW_TEST_TARGET */\n}\n'
        # PR 创建前存在两次提交，第一条提交的问题仍必须被审查。
        branch='多提交-'+self.suffix
        git(self.h,self.folder,'switch','main');git(self.h,self.folder,'switch','-c',branch)
        self.change({'cumulative.c':code});self.change({'uncovered.txt':'明确记录尚未覆盖的文本文件\n'.encode()})
        git(self.h,self.folder,'push','origin','HEAD')
        pr=self.h.gitea('POST',f'/repos/{self.full}/pulls',{'head':branch,'base':'main','title':'完整 PR 累计 diff 验收'})
        task=self.check('多 Commit 审查整个 PR',pr,[('cumulative.c','new',2)])
        self.record('部分覆盖不能伪装完整通过',{'通过':not task['result']['coverage']['complete'],'覆盖范围':task['result']['coverage']})

    def duplicates(self):
        rows=self.h.manager('GET',f'/ai-review/tasks?repository={self.repo["id"]}')['items']
        completed=next(r for r in rows if r['status']=='succeeded')
        pr=self.h.gitea('GET',f'/repos/{self.full}/pulls/{completed["number"]}')
        before=self.h.gitea('GET',f'/repos/{self.full}/pulls/{pr["number"]}/reviews')
        total=self.h.manager('GET',f'/ai-review/tasks?repository={self.repo["id"]}')['total']
        # 凭据只经过子进程内存，不写报告，也不放在命令参数中。
        secret=local.compose('exec','-T','review-worker','python','-c',
            f"from ai_review.store import Store; s=Store(); print(s.unseal(s.repo({self.repo['id']})['webhook_secret']))").strip()
        raw=json.dumps({'action':'synchronized','repository':{'id':self.repo['repo_id'],'full_name':self.full},'pull_request':pr}).encode()
        signature=hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest();del secret
        responses=[]
        for _ in range(20):
            req=urllib.request.Request(self.h.manager_url+f'/api/ai-review/webhooks/{self.repo["id"]}',data=raw,method='POST',
                headers={'Content-Type':'application/json','X-Gitea-Signature':signature,'X-Gitea-Event':'pull_request','X-Gitea-Delivery':'duplicate-'+self.suffix})
            with urllib.request.urlopen(req,timeout=5) as response: responses.append(response.status)
        after=self.h.gitea('GET',f'/repos/{self.full}/pulls/{pr["number"]}/reviews')
        count=self.h.manager('GET',f'/ai-review/tasks?repository={self.repo["id"]}')['total']
        self.record('同一真实 PR 重复事件不增加任务或 Review',{'通过':responses==[202]*20 and count==total and [r['id'] for r in before]==[r['id'] for r in after],
            '事件次数':20,'任务数之前':total,'任务数之后':count,'Review数':len(after),'PR编号':pr['number']})

    def run(self, phases):
        self.prepare()
        for phase in phases.split(','):getattr(self,phase)()
        print('验收记录：'+str(self.out))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='必须指定独立断网测试环境的 REVIEW_STATE_DIR')
    parser.add_argument('--name',default='boundary-fixtures')
    parser.add_argument('--phases',default='boundaries,faults,push_race,reliability,cumulative,duplicates');args=parser.parse_args()
    if set(args.phases.split(','))-{'boundaries','faults','push_race','reliability','cumulative','duplicates'}:parser.error('未知测试阶段')
    Suite(args.name).run(args.phases)
