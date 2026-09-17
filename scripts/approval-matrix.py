#!/usr/bin/env python3
"""完整矩阵的真实原生行为分项执行器；只创建新隔离实例，保留每项首次失败。"""
import argparse
import base64
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
from html.parser import HTMLParser
import http.cookiejar
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import time
import traceback
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPCookieProcessor

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('approval_acceptance', ROOT/'scripts/approval-integration.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Matrix(module.Acceptance):
    def __init__(self, image_tag, packaged=False):
        super().__init__(image_tag, packaged)
        self.report['说明'] = '完整矩阵分项实际执行；本文件只证明列出的独立子项，未执行项不能自动通过'
        self.report['矩阵子项'] = []
        self.report['发包后执行'] = ['接入24-A：影子24小时', '接入24-B：隔离强制24小时']
        self.report['首次产品失败'] = []
        self.report['首次产品失败说明'] = '历史兼容字段名；包含测试前置和脚本失败，须单独归因，不能直接当作产品缺陷。'
        sources = sorted(set((ROOT/'scripts').glob('approval-*.py')) |
                         {p for p in (ROOT/'backend/approval').rglob('*') if p.is_file() and '__pycache__' not in p.parts} |
                         {ROOT/p for p in ('backend/models.py','backend/routes/approval_routes.py','docker-compose.yml',
                          'compose.local.yml','compose.review-local.yml','compose.ai-review.images.yml','frontend/nginx.conf',
                          'scripts/upgrade_runtime.py','frontend/src/views/ApprovalRules.vue')})
        self.report['执行源码摘要'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in sources}
        (self.folder/'本轮矩阵执行器.py').write_bytes(Path(__file__).read_bytes())

    def start(self):
        import shutil
        self.report['Docker执行环境']={'客户端路径':shutil.which('docker'),
            '客户端与服务版本':self.run(['docker','version','--format','{{.Client.Version}}/{{.Server.Version}}']).strip()}
        self.save()
        return super().start()

    def compose(self,*args):
        if args and args[0]=='up':
            config=json.loads(self.compose_path.read_text())
            ports=config['services']['gitea']['ports']
            if '127.0.0.1::22' not in ports:ports.append('127.0.0.1::22')
            self.compose_path.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n')
        return super().compose(*args)

    def git_setup(self,transport='HTTP'):
        folder=self.folder/('Git工作区-'+transport)
        env=os.environ.copy()
        env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,GIT_TERMINAL_PROMPT='0')
        if transport=='HTTP':
            credential='Basic '+base64.b64encode(('author:'+self.password).encode()).decode()
            env.update(GIT_CONFIG_COUNT='1',GIT_CONFIG_KEY_0='http.extraHeader',GIT_CONFIG_VALUE_0='Authorization: '+credential)
            url=self.base+'/approval-probe/approval-live.git'
        else:
            key=self.folder/'隔离SSH密钥'
            self.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)])
            self.api('POST','/user/keys',{'title':'本次隔离矩阵','key':key.with_suffix('.pub').read_text()},user='author')
            port=self.verify_owned()['NetworkSettings']['Ports']['22/tcp'][0]['HostPort']
            env['GIT_SSH_COMMAND']=shlex.join(['ssh','-i',str(key),'-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=accept-new',
                                            '-o','UserKnownHostsFile='+str(self.folder/'隔离SSH主机指纹'),'-p',port])
            url='ssh://git@127.0.0.1:'+port+'/approval-probe/approval-live.git'
        result=subprocess.run(['git','clone',url,str(folder)],env=env,capture_output=True,text=True,timeout=60)
        assert result.returncode==0,'隔离Git克隆失败：'+result.stderr[-600:]
        self.git_env,self.git_folder=env,folder
        self.git_command(['config','user.name','隔离矩阵'])
        self.git_command(['config','user.email','matrix@example.invalid'])
        return folder

    def git_command(self,args,expected=(0,)):
        result=subprocess.run(['git',*args],cwd=self.git_folder,env=self.git_env,capture_output=True,text=True,timeout=35)
        if result.returncode not in expected:
            raise AssertionError('Git 操作失败：'+result.stderr[-800:])
        return {'退出码':result.returncode,'输出':result.stdout[-1000:],'错误输出':result.stderr[-1500:]}

    def git_commit(self):
        file=self.git_folder/('隔离-'+secrets.token_hex(4)+'.txt')
        file.write_text('矩阵提交\n')
        self.git_command(['add',file.name]);self.git_command(['commit','-m','测试：矩阵推送'])

    def native_web_login(self,user):
        jar=http.cookiejar.CookieJar()
        browser=build_opener(ProxyHandler({}),HTTPCookieProcessor(jar))
        class Fields(HTMLParser):
            def __init__(self):super().__init__();self.values={}
            def handle_starttag(self,tag,attrs):
                a=dict(attrs)
                if tag=='input' and a.get('name'):self.values[a['name']]=a.get('value','')
        with browser.open(self.base+'/user/login',timeout=15) as response:page=response.read().decode()
        fields=Fields();fields.feed(page)
        assert 'user_name' in fields.values and 'password' in fields.values,'原生登录表单不完整'
        # 官方 1.26.4 已改为同源校验，不再提供旧版本的 _csrf 隐藏字段。
        data=urlencode({'user_name':user,'password':self.password}).encode()
        headers={'Origin':self.base,'Sec-Fetch-Site':'same-origin'}
        with browser.open(Request(self.base+'/user/login',data=data,method='POST',headers=headers),timeout=15) as response:
            page=response.read().decode()
            assert '/user/login' not in response.url,'原生网页登录没有完成'
        return browser,jar

    def web_merge(self,pr,style,expected):
        http,_=self.native_web_login('author')
        url=self.base+'/approval-probe/approval-live/pulls/'+str(pr['number'])
        with http.open(url,timeout=15) as response:page=response.read().decode()
        assert 'pulls/'+str(pr['number']) in page,'登录后原生PR页面不可用'
        # 原生拒绝后会重新安排冲突检查。只读等到就绪，每个断言仅提交一次合并写请求。
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            if self.get(self.path+f'/pulls/{pr["number"]}').get('mergeable'):break
            time.sleep(.25)
        else:raise AssertionError('原生PR合并检查没有就绪')
        before=self.execute('git','--git-dir='+self.repo_dir,'rev-parse','refs/heads/main').strip()
        req=Request(url+'/merge',data=urlencode({'do':style,'head_commit_id':pr['head']['sha'],
                    'merge_title_field':'矩阵原生网页合并'}).encode(),method='POST',
                    headers={'Origin':self.base,'Sec-Fetch-Site':'same-origin'})
        try:response=http.open(req,timeout=20)
        except HTTPError as exc:response=exc
        with response:code=response.code;body=response.read().decode()
        after=self.execute('git','--git-dir='+self.repo_dir,'rev-parse','refs/heads/main').strip()
        current=self.get(self.path+f'/pulls/{pr["number"]}')
        assert (before!=after)==expected and current['merged']==expected, '原生网页合并与实际引用不符：'+body[-700:]
        if not expected:assert '审批' in body or 'approval' in body.lower() or 'hook' in body.lower(), '未进入预期审批拒绝路径：'+body[-700:]
        return {'旧引用':before,'新引用':after,'已合并':current['merged'],'HTTP':code,'原生响应':body[-1200:]}

    def case(self, key, name, action):
        started = time.monotonic()
        try:
            evidence = action()
            row = {'编号': key, '场景': name, '结果': '通过', '证据': evidence}
        except Exception as exc:
            reason = str(exc).replace(self.password, '[隔离口令已隐藏]')
            for token in self.secret_tokens:
                reason = reason.replace(token, '[隔离令牌已隐藏]')
            row = {'编号': key, '场景': name, '结果': '失败', '原因': reason,
                   '异常': type(exc).__name__, '位置': traceback.format_exc().replace(self.password, '[隔离口令已隐藏]')[-3000:]}
            self.report['首次产品失败'].append(copy.deepcopy(row))
        row['耗时秒'] = round(time.monotonic()-started, 3)
        self.report['矩阵子项'].append(row)
        self.save()
        print(key+' '+name+'：'+row['结果'], flush=True)
        return row

    def pr(self, name, voters=()):
        pr = self.create_pr(name)
        for user in voters:
            self.approve(pr, user)
        return pr

    def status_is(self, pr, satisfied, *, count=None, groups=None):
        value = self.check(pr)
        assert value['satisfied'] is satisfied, '审批结论不符：'+json.dumps(value, ensure_ascii=False)
        if count is not None:
            assert value['total']['count'] == count, '有效人数不符：'+json.dumps(value, ensure_ascii=False)
        if groups is not None:
            assert [g['count'] for g in value['groups']] == groups, '逐组人数不符'
        return {'PR': pr['number'], '结论': value}

    def native(self, changes):
        self.api('PATCH', self.path+'/branch_protections/main', changes)

    def restore_native(self):
        payload=copy.deepcopy(self.baseline_rules[0])
        # PATCH 中 null 表示不修改；恢复空列表必须显式发送 []。
        if payload.get('status_check_contexts') is None:payload['status_check_contexts']=[]
        self.native(payload)

    def dismiss(self, pr, user, review_id=None):
        reviews = self.get(self.path+f'/pulls/{pr["number"]}/reviews')
        review = next(r for r in reversed(reviews) if r['user']['login'] == user and (review_id is None or r['id'] == review_id))
        self.api('POST', self.path+f'/pulls/{pr["number"]}/reviews/{review["id"]}/dismissals',
                 {'message': '矩阵验证原生撤回', 'priors': False})
        return review['id']

    def lifecycle(self):
        for count in (0, 1, 2):
            self.case('规则01-'+str(count), '原生总票数'+str(count),
                      lambda count=count: self.status_is(self.pr('总票不足', ('alice','bob')[:count]), False, count=count))
        for key, voters, satisfied, counts in (
                ('规则02', ('bob','carol','dave'), False, [0,1]),
                ('规则03', ('alice','carol','dave'), False, [1,0]),
                ('规则05', ('alice','bob','carol'), True, [1,1])):
            self.case(key, '指定组与合格组外第三票', lambda voters=voters,satisfied=satisfied,counts=counts:
                      self.status_is(self.pr('指定组固定样本', voters), satisfied, count=3, groups=counts))
        def cross(count):
            self.api('PUT', f'/teams/{self.teams["Committer2"]}/members/alice')
            try:
                return self.status_is(self.pr('跨组去重', ('alice','carol','dave')[:count]), count == 3,
                                      count=count, groups=[1,1])
            finally:
                self.api('DELETE', f'/teams/{self.teams["Committer2"]}/members/alice')
        self.case('规则04', '跨组满足两组但总票仅两人', lambda: cross(2))
        self.case('规则06', '跨组第三人补齐，总票不重复', lambda: cross(3))
        def repeated():
            pr = self.pr('同人重复批准', ('alice','bob'))
            self.approve(pr, 'alice')
            return self.status_is(pr, False, count=2)
        self.case('规则07-重复批准', '重复正式批准只算一人', repeated)
        for state in ('COMMENT', 'REQUEST_CHANGES'):
            def decision(state=state):
                pr = self.pr('正式审批后改变决定', ('alice','bob','carol'))
                self.api('POST', self.path+f'/pulls/{pr["number"]}/reviews',
                         {'event': state, 'commit_id': pr['head']['sha'], 'body': '矩阵生命周期'}, user='alice')
                return self.status_is(pr, state=='COMMENT', count=3 if state=='COMMENT' else 2)
            self.case('生命周期02' if state=='COMMENT' else '生命周期01', '批准后'+state, decision)
        def dismissal():
            pr = self.pr('管理员驳回', ('alice','bob','carol'))
            self.dismiss(pr, 'alice')
            return self.status_is(pr, False, count=2)
        self.case('生命周期03-管理员驳回', '原生驳回后指定组票失效', dismissal)
        def old_dismissal():
            pr = self.pr('旧撤回不能取消新审批', ('alice','bob','carol'))
            old = next(r['id'] for r in self.get(self.path+f'/pulls/{pr["number"]}/reviews') if r['user']['login']=='alice')
            self.approve(pr, 'alice')
            self.dismiss(pr, 'alice', old)
            return self.status_is(pr, True, count=3)
        self.case('生命周期04', '新审批后撤回旧审批ID', old_dismissal)
        def commit_update():
            pr = self.pr('新提交审批失效', ('alice','bob','carol'))
            self.api('POST', self.path+'/contents/更新内容-'+secrets.token_hex(4)+'.txt',
                     {'branch': pr['head']['ref'], 'message': '测试：更新代码', 'content': '5paw5Luj56CB'}, user='author')
            result = self.status_is(pr, False, count=0)
            current = self.get(self.path+f'/pulls/{pr["number"]}')
            for user in ('alice','bob','carol'):
                self.approve(current, user)
            result['重新批准'] = self.status_is(current, True, count=3)
            return result
        self.case('生命周期05-新提交', '旧审批失效且新代码重新批准恢复', commit_update)
        def old_close_reopen():
            pr = self.legacy
            self.api('PATCH', self.path+f'/pulls/{pr["number"]}', {'state':'closed'})
            closed = self.check(pr)
            self.api('PATCH', self.path+f'/pulls/{pr["number"]}', {'state':'open'})
            opened = self.check(pr)
            assert closed['grandfathered'] and opened['grandfathered'] and closed['closed'] and not opened['closed']
            return {'关闭':closed,'重开':opened}
        self.case('接入12-C', '存量关闭重开保持稳定豁免', old_close_reopen)
        def permissions(change):
            pr = self.pr('账号资格变化', ('alice','bob','carol'))
            self.api('PATCH','/admin/users/alice',dict(change,login_name='alice'))
            try:
                return self.status_is(pr, False, count=2)
            finally:
                self.api('PATCH','/admin/users/alice',dict({k:False for k in change},login_name='alice'))
        self.case('资格03-禁止登录', '被禁止登录的批准者不能计票', lambda: permissions({'prohibit_login':True}))
        def group_remove():
            pr = self.pr('成员退出', ('alice','bob','carol'))
            self.api('DELETE',f'/teams/{self.teams["Committer1"]}/members/alice')
            try:
                result=self.status_is(pr,False,count=3,groups=[0,1])
            finally:
                self.api('PUT',f'/teams/{self.teams["Committer1"]}/members/alice')
            result['重新加入']=self.status_is(pr,True,count=3,groups=[1,1])
            return result
        self.case('资格02-退出加入', '组资格即时变化且稳定ID不变', group_remove)
        def group_rename():
            pr=self.pr('团队更名',('alice','bob','carol'))
            path=f'/teams/{self.teams["Committer1"]}'
            team=self.get(path)
            new={k:team[k] for k in ('name','description','permission','includes_all_repositories','units_map') if k in team}
            new['name']='Committer1-renamed'
            self.api('PATCH',path,new)
            try:
                result=self.status_is(pr,True,count=3)
                assert result['结论']['groups'][0]['name']=='Committer1-renamed'
                return result
            finally:
                new['name']='Committer1';self.api('PATCH',path,new)
        self.case('资格02-更名', '团队按ID保留规则且展示新名称', group_rename)
        def pool(total, allowed):
            before=self.get(self.path+'/branch_protections')
            self.native({'required_approvals':total,'enable_approvals_whitelist':True,
                         'approvals_whitelist_username':allowed,'approvals_whitelist_teams':[]})
            try:
                result=self.manager('POST',self.prefix+'/preview',{},expected=(200,409))
                after=self.get(self.path+'/branch_protections')
                assert after[0]['required_approvals']==total
                assert set(after[0]['approvals_whitelist_username'])==set(allowed)
                if 'alice' in allowed and 'bob' in allowed:
                    assert 'error' not in result, str(result)
                else:
                    assert 'error' in result, '审批池冲突未被阻断'
                return {'预检':result,'原生保持':after}
            finally:
                self.restore_native()
        for total in (1,3,5):
            self.case(f'接入02-{total}票', '保留原生人数及审批池', lambda total=total:pool(total,['alice','bob','carol','dave','spare']))
        self.case('接入02-D', '原生审批池排除指定组时不接管修复', lambda:pool(3,['bob','carol','dave']))
        def bypass(field,value):
            self.native({field:value})
            try:
                response=self.manager('POST',self.prefix+'/preview',{},expected=(200,409))
                assert 'error' in response, '危险原生绕过路径未阻断预检'
                native=self.get(self.path+'/branch_protections')[0]
                assert native[field]==value
                return {'拒绝原因':response,'原生值':native[field]}
            finally:self.restore_native()
        for field,value in (('enable_push',True),('block_admin_merge_override',False),('unprotected_file_patterns','*.md')):
            self.case('接入03-B-'+field,'原生例外保持且预检拒绝',lambda field=field,value=value:bypass(field,value))
        def pages():
            pr=self.pr('审批跨页',('alice','bob','carol'))
            for i in range(51):
                self.api('POST',self.path+f'/pulls/{pr["number"]}/reviews',
                         {'event':'COMMENT','body':'分页固定样本 '+str(i),'commit_id':pr['head']['sha']},user='dave')
            evidence=self.status_is(pr,True,count=3)
            assert len(self.get(self.path+f'/pulls/{pr["number"]}/reviews?limit=50'))==50
            return evidence
        self.case('生命周期06-多页', '完整原生审批跨页读取且评论不替代批准', pages)

    def installer_cases(self):
        obj=self.installer
        original_footer=obj.read(obj.footer)
        wrappers={phase:obj.read(obj.repo+'/hooks/'+phase) for phase in ('pre-receive','update')}
        def invariant():
            assert all(obj.read(obj.repo+'/hooks/'+phase)==value for phase,value in wrappers.items())
            assert obj.read(self.other_hook)==self.other_content
            assert self.verify_owned()['State']['StartedAt']==self.report['实例']['初始启动时间']
            assert self.get(self.path+'/branch_protections')==self.baseline_rules
            assert '管理员并发新增'.encode() in obj.read(obj.footer)
            return {'包装器及原生规则':'保持','Gitea启动时间':'保持','其他Hook':'保持'}
        def concurrent():
            snapshot=obj.snapshot()
            obj.exec(['mkdir',obj.lock])
            try:
                try:obj.install(snapshot)
                except RuntimeError:pass
                else:raise AssertionError('两个安装任务同时进入')
            finally:obj.exec(['rmdir',obj.lock])
            return invariant()
        self.case('接入08-A','真实目录锁拒绝同时安装',concurrent)
        def modified(kind):
            path=obj.footer if kind=='页脚' else obj.repo+'/hooks/pre-receive.d/gitea-manager-approval'
            snapshot=obj.snapshot();old=obj.read(path);new=old+('\n<!-- 管理员更新 -->\n'.encode() if kind=='页脚' else b'\n# administrator update\n')
            obj.atomic(path,new,0o644 if kind=='页脚' else 0o755)
            try:
                try:obj.install(snapshot)
                except ValueError:pass
                else:raise AssertionError('未拒绝过期预览')
                assert obj.read(path)==new
            finally:obj.atomic(path,old,0o644 if kind=='页脚' else 0o755)
            return invariant()
        self.case('接入07-A','管理员修改Hook后拒绝旧预览且保留',lambda:modified('Hook'))
        self.case('接入07-B','管理员修改模板后拒绝旧预览且保留',lambda:modified('页脚'))
        # 真实子进程非正常终止，不用异常 finally 模拟进程被杀；遗留锁按实物核对后仅清理此实验锁。
        child=self.folder/'安装中断子进程.py'
        child.write_text('''"""只在当前独立容器中制造安装写入边界退出。"""
import importlib.util,json,os,sys
from pathlib import Path
folder,container,target,when,marker=sys.argv[1:]
spec=importlib.util.spec_from_file_location('isolated_installer',Path(folder)/'install.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
import subprocess
def diagnostic(args,input=None):
 result=subprocess.run(args,input=input,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
 if result.returncode:
  # 只收集失败操作的标准错误，不收集 inspect / 配置读取的标准输出或二进制输入。
  secret=json.loads((Path(folder)/'config.json').read_text()).get('token','')
  error=result.stderr.decode(errors='replace')[-2000:]
  if secret:error=error.replace(secret,'[服务凭据已隐藏]')
  Path(marker+'.json').write_text(json.dumps({'失败操作':args[:5],'退出码':result.returncode,'错误输出':error},ensure_ascii=False,indent=2))
  os.chmod(marker+'.json',0o600)
  raise RuntimeError('容器操作失败，诊断已保存在隔离证据目录')
 return result.stdout
obj=module.Installer(container,folder,runner=diagnostic)
original=obj.atomic
counter=0
def atomic(*args,**kwargs):
 global counter
 counter+=1
 if counter==int(target) and when=='before':
  Path(marker).write_text('写入前已退出');os._exit(77)
 value=original(*args,**kwargs)
 if counter==int(target) and when=='after':
  Path(marker).write_text('原子写入后已退出');os._exit(77)
 return value
obj.atomic=atomic
obj.install(obj.snapshot())
''')
        child.chmod(0o600)
        def interrupted(index,when):
            marker=self.folder/f'中断-{index}-{when}.txt'
            result=subprocess.run([sys.executable,str(child),str(obj.root),obj.container,str(index),when,str(marker)],
                                  capture_output=True,text=True,timeout=90)
            assert result.returncode==77 and marker.exists(), '未到达预定写入边界：'+result.stderr[-700:]
            self.exec_assert_lock(obj)
            proof=invariant()
            scan={phase:obj.exec(['find',obj.repo+'/hooks/'+phase+'.d','-maxdepth','1','-type','f','-name','安装中-*']).decode()
                  for phase in ('pre-receive','update')}
            assert not any(scan.values()), '扫描目录内残留安装临时文件'
            proof.update({'写入序号':index,'边界':when,'退出码':result.returncode,'扫描目录临时文件':scan})
            # 核对整个现场后只清理这次非正常退出留下的实验锁，后续完整性核验不重写 Hook。
            obj.exec(['rmdir',obj.lock])
            obj.verify_installed()
            return proof
        for index,label in enumerate(('程序','凭据配置','pre-receive','update','JS','CSS','页脚备份','页脚片段'),1):
            for when in ('before','after'):
                self.case(f'接入06-写入{index}-{when}',label+' 写入边界真实退出',
                          lambda index=index,when=when:interrupted(index,when))
                # 某个失败用例若遗留锁，先记录、核对归属，避免污染后续独立用例。
                obj.exec(['sh','-c','if [ -d "$1" ]; then rmdir "$1"; fi','实验锁清理',obj.lock])
        def half_file():
            stage=obj.owned+'/安装中-半文件样本'
            obj.atomic(stage,b'#!/bin/sh\nexit 79\n',0o755)
            try:
                assert '/hooks/' not in stage
                # 真实 HTTP 推送使原生包装器获得完整 Gitea 身份；不能直接调用它冒充 Git 入口。
                self.git_setup('HTTP');self.git_commit()
                branch='matrix-staging-'+secrets.token_hex(4)
                proof=self.git_command(['push','origin','HEAD:refs/heads/'+branch])
                return dict(invariant(),扫描外样本='可执行且未被调度',真实推送=proof)
            finally:obj.exec(['rm','-f',stage])
        self.case('接入05-C','可执行中间样本位于扫描目录外',half_file)
        def reload_failure():
            previous=obj.exec
            def execute(args,**kwargs):
                if args==obj.reload:raise RuntimeError('隔离注入：模板重载命令失败')
                return previous(args,**kwargs)
            obj.exec=execute
            try:
                try:obj.install(obj.snapshot())
                except RuntimeError as exc:assert '模板重载命令失败' in str(exc)
                else:raise AssertionError('错误报告重载成功')
            finally:obj.exec=previous
            value=invariant()
            obj.verify_installed()
            return dict(value,说明='命令失败后的实物核验；不把本项当作真实模板语法错误已覆盖')
        self.case('接入09-B-命令故障','重载命令失败不报告安装成功',reload_failure)
        def ack_loss():
            original=obj.ack
            def lose(*args,**kwargs):
                original(*args,**kwargs)
                raise RuntimeError('隔离注入：成功回执丢失')
            obj.ack=lose
            try:
                try:obj.install(obj.snapshot())
                except RuntimeError as exc:assert '回执丢失' in str(exc)
                else:raise AssertionError('未触发回执丢失')
            finally:obj.ack=original
            obj.verify_installed()
            return invariant()
        self.case('接入09-D','真实成功回执后丢失响应并只核验恢复',ack_loss)
        def drift():
            target=obj.repo+'/hooks/update.d/gitea-manager-approval'
            old=obj.read(target)
            obj.exec(['rm',target])
            try:
                try:obj.verify_installed()
                except ValueError as exc:assert 'Hook' in str(exc)
                else:raise AssertionError('组件丢失仍报告完整')
            finally:obj.atomic(target,old,0o755)
            return invariant()
        self.case('接入22-B-组件丢失','丢失Hook时完整性检查拒绝正常状态',drift)
        assert obj.read(obj.footer)==original_footer

    def transport_cases(self):
        self.install('isolated_enforced')
        for transport in ('HTTP','SSH'):
            self.case('入口04-'+transport+'-克隆','真实传输拉取已存在私有仓库',
                      lambda transport=transport:{'工作目录':str(self.git_setup(transport))})
            if not hasattr(self,'git_folder') or not self.git_folder.name.endswith(transport):continue
            def push_allowed():
                self.git_commit()
                name='matrix-'+transport.lower()+'-'+secrets.token_hex(4)
                result=self.git_command(['push','origin','HEAD:refs/heads/'+name])
                expected=self.git_command(['rev-parse','HEAD'])['输出'].strip()
                actual=self.execute('git','--git-dir='+self.repo_dir,'rev-parse','refs/heads/'+name).strip()
                assert actual==expected
                return {'推送':result,'实际引用':actual}
            self.case('入口04-'+transport+'-普通分支','非受管分支正常推送',push_allowed)
            for variant,args in (('直接推送',['push','origin','HEAD:refs/heads/main']),
                                 ('强推',['push','--force','origin','HEAD:refs/heads/main']),
                                 ('多引用',['push','--atomic','origin','HEAD:refs/heads/main','HEAD:refs/heads/matrix-multi'])):
                def denied(args=args):
                    before=self.execute('git','--git-dir='+self.repo_dir,'show-ref').strip()
                    response=self.git_command(args,expected=(1,128))
                    after=self.execute('git','--git-dir='+self.repo_dir,'show-ref').strip()
                    assert before==after,'拒绝的推送仍改变实际引用'
                    assert 'protected' in response['错误输出'].lower() or '保护' in response['错误输出'],response
                    return {'实际引用不变':True,'推送':response}
                self.case('入口05-'+transport+'-'+variant,'原生保护与新增门禁不能被绕过',denied)
        self.report['传输检查后原生保持']={
            '规则':self.get(self.path+'/branch_protections')==self.baseline_rules,
            '未重启':self.verify_owned()['State']['StartedAt']==self.report['实例']['初始启动时间']}
        assert all(self.report['传输检查后原生保持'].values())
        self.rollback_cases()

    def native_cases(self):
        self.install('isolated_enforced')
        for style in module.module.STYLES:
            pr=self.pr('原生网页-'+style,('bob','carol','dave'))
            self.case('入口01-'+style+'-拒绝','原生网页入口缺指定组票拒绝',
                      lambda pr=pr,style=style:self.web_merge(pr,style,False))
            self.approve(pr,'alice')
            self.case('入口01-'+style+'-放行','原生网页入口补齐票后合并',
                      lambda pr=pr,style=style:self.web_merge(pr,style,True))
        def stricter():
            self.native({'required_approvals':5})
            self.manager('POST',self.prefix+'/preview',{})
            try:
                pr=self.pr('原生五票优先',('alice','bob','carol'))
                computed=self.status_is(pr,True,count=3)
                self.merge(pr,expected=False)
                for user in ('dave','spare'):self.approve(pr,user)
                self.merge(pr,expected=True)
                return computed
            finally:
                self.restore_native();self.manager('POST',self.prefix+'/preview',{})
        self.case('兼容01-原生五票','新增三票满足仍受原生五票限制',stricter)
        def ci(state):
            self.native({'enable_status_check':True,'status_check_contexts':['matrix/ci']})
            self.manager('POST',self.prefix+'/preview',{})
            try:
                pr=self.pr('原生CI-'+state,('alice','bob','carol'))
                if state!='missing':
                    self.api('POST',self.path+'/statuses/'+pr['head']['sha'],{'state':state,'context':'matrix/ci','description':'矩阵CI状态'})
                self.merge(pr,expected=state=='success')
                return {'原生CI状态':state,'PR':pr['number']}
            finally:
                self.restore_native();self.manager('POST',self.prefix+'/preview',{})
        for state in ('missing','failure','success'):
            self.case('原生01-'+state,'原生CI独立生效',lambda state=state:ci(state))
        def rejected():
            pr=self.pr('原生请求修改',('alice','bob','carol'))
            self.api('POST',self.path+f'/pulls/{pr["number"]}/reviews',{'event':'REQUEST_CHANGES','body':'必须修改','commit_id':pr['head']['sha']},user='dave')
            self.merge(pr,expected=False)
            return {'PR':pr['number'],'结果':'目标分支未写入'}
        self.case('原生02-请求修改','原生请求修改不能被新增三票绕过',rejected)
        def signature():
            self.native({'require_signed_commits':True});self.manager('POST',self.prefix+'/preview',{})
            try:
                pr=self.pr('原生签名要求',('alice','bob','carol'))
                self.merge(pr,expected=False)
                return {'PR':pr['number'],'结果':'未签名提交被原生拒绝'}
            finally:self.restore_native();self.manager('POST',self.prefix+'/preview',{})
        self.case('原生02-签名','原生签名要求保留',signature)
        def third_party():
            pr=self.pr('其他门禁优先',('alice','bob','carol'))
            target=self.installer.repo+'/hooks/update.d/矩阵现有拒绝门禁'
            self.installer.atomic(target,'#!/bin/sh\n# 原有第三方门禁独立拒绝\necho "原有门禁拒绝" >&2\nexit 1\n'.encode(),0o755)
            try:
                self.merge(pr,expected=False)
                return {'PR':pr['number'],'结果':'原有门禁退出码被保留'}
            finally:self.installer.exec(['rm','-f',target])
        self.case('接入04-B','新增组票满足也不能吞掉其他Hook拒绝',third_party)
        current=self.get(self.path+'/branch_protections')
        self.report['原生变更恢复核对']={'基线':self.baseline_rules,'当前':current}
        self.save()
        # 本节主动修改过测试库的原生规则；updated_at 是合法变化，先证明其余字段恢复。
        def comparable(r):
            value={k:v for k,v in r.items() if k!='updated_at'}
            value['status_check_contexts']=value.get('status_check_contexts') or []
            return value
        assert list(map(comparable,current))==list(map(comparable,self.baseline_rules)), '原生测试规则未完整恢复'
        # 回退应保持回退前的真实配置，包括测试期间合法更新的时间戳。
        self.baseline_rules=current
        self.rollback_cases()

    def auto_cases(self):
        self.install('isolated_enforced')
        self.native({'enable_status_check':True,'status_check_contexts':['matrix/auto']})
        self.manager('POST',self.prefix+'/preview',{})
        obj=self.installer
        hook=obj.repo+'/hooks/update.d/gitea-manager-approval'
        original=obj.read(hook)
        try:
            for style in module.module.STYLES:
                def automatic(style=style):
                    pr=self.pr('自动合并-'+style,('bob','carol','dave'))
                    marker=obj.owned+'/自动合并结果-'+str(pr['number'])
                    # 保持真正门禁程序与参数，只在其返回后记录退出码；不改变审批判定。
                    observed=original.replace(b'exec ',b'',1)+(
                        '\nresult=$?\nif [ "$1" = refs/heads/main ]; then printf "%s" "$result" > '+
                        shlex.quote(marker)+'; fi\nexit "$result"\n').encode()
                    obj.atomic(hook,observed,0o755)
                    before=self.execute('git','--git-dir='+self.repo_dir,'rev-parse','refs/heads/main').strip()
                    try:
                        self.api('POST',self.path+f'/pulls/{pr["number"]}/merge',
                            {'do':style,'head_commit_id':pr['head']['sha'],'merge_when_checks_succeed':True},
                            user='author',expected=(201,))
                        self.api('POST',self.path+'/statuses/'+pr['head']['sha'],
                            {'state':'success','context':'matrix/auto','description':'隔离自动合并触发'})
                        deadline=time.monotonic()+45
                        while time.monotonic()<deadline:
                            result=obj.read(marker)
                            if result:break
                            time.sleep(.25)
                        else:raise AssertionError('未观察到原生自动合并实际调用门禁')
                        assert result==b'1','缺组票的自动合并没有被真实门禁拒绝'
                        assert before==self.execute('git','--git-dir='+self.repo_dir,'rev-parse','refs/heads/main').strip()
                        assert not self.get(self.path+f'/pulls/{pr["number"]}')['merged']
                        self.approve(pr,'alice')
                        deadline=time.monotonic()+45
                        while time.monotonic()<deadline:
                            current=self.get(self.path+f'/pulls/{pr["number"]}')
                            if current['merged']:break
                            time.sleep(.25)
                        else:raise AssertionError('补齐指定组票后原生自动合并未完成')
                        after=self.execute('git','--git-dir='+self.repo_dir,'rev-parse','refs/heads/main').strip()
                        assert obj.read(marker)==b'0' and before!=after
                        return {'PR':pr['number'],'第一次门禁退出码':1,'补票后门禁退出码':0,
                                '旧引用':before,'新引用':after,'原生已合并':current['merged']}
                    finally:obj.atomic(hook,original,0o755)
                self.case('入口03-'+style,'自动合并等待CI、缺组拒绝、补票后实际合并',automatic)
        finally:
            self.restore_native();self.manager('POST',self.prefix+'/preview',{})
        self.baseline_rules=self.get(self.path+'/branch_protections')
        self.rollback_cases()

    def qualification_cases(self):
        def author_vote():
            pr=self.pr('作者不能自审')
            code,_=self.api('POST',self.path+f'/pulls/{pr["number"]}/reviews',
                {'event':'APPROVED','commit_id':pr['head']['sha'],'body':'作者自审验证'},user='author',expected=(200,403,422))
            return {'原生HTTP':code,'计算':self.status_is(pr,False,count=0)}
        self.case('规则08-作者本人','作者尝试批准自己不产生有效票',author_vote)
        def read_only():
            self.api('POST','/admin/users',{'username':'reader','password':self.password,'email':'reader@example.invalid',
                                          'must_change_password':False,'send_notify':False})
            self.api('PUT',self.path+'/collaborators/reader',{'permission':'read'})
            pr=self.pr('非合格读者',('alice','bob'))
            self.approve(pr,'reader')
            return self.status_is(pr,False,count=2)
        self.case('规则08-无资格','普通只读用户批准不能补齐第三票',read_only)
        def revoke_permission():
            pr=self.pr('撤销仓库权限',('alice','bob','carol'))
            for name in ('Writers','Committer1'):
                self.api('DELETE',f'/teams/{self.teams[name]}/members/alice')
            try:return self.status_is(pr,False,count=2,groups=[0,1])
            finally:
                for name in ('Writers','Committer1'):self.api('PUT',f'/teams/{self.teams[name]}/members/alice')
        self.case('资格03-仓库权限撤销','历史批准保留但失去权限后不计票',revoke_permission)
        def single_unit():
            team=self.get(f'/teams/{self.teams["Committer1"]}')
            pr=self.pr('分单元审批资格',('alice','bob','carol'))
            payload={k:team[k] for k in ('name','description','permission','includes_all_repositories','units_map')}
            self.api('DELETE',f'/teams/{self.teams["Writers"]}/members/alice')
            self.api('PATCH',f'/teams/{self.teams["Committer1"]}',dict(payload,permission='none',units_map={'repo.pulls':'read'}))
            self.native({'enable_approvals_whitelist':True,'approvals_whitelist_username':['alice','bob','carol','spare']})
            try:
                current=self.get(f'/teams/{self.teams["Committer1"]}')
                assert current['permission']=='none' and current['units_map'].get('repo.pulls')=='read'
                return {'原生团队权限':current['units_map'],'计算':self.status_is(pr,True,count=3)}
            finally:
                self.api('PATCH',f'/teams/{self.teams["Committer1"]}',payload)
                self.api('PUT',f'/teams/{self.teams["Writers"]}/members/alice');self.restore_native()
        self.case('资格04-分单元','审批池中的仅PR读取团队成员按单元资格计票',single_unit)
        def empty_team():
            for user in ('alice','spare'):self.api('DELETE',f'/teams/{self.teams["Committer1"]}/members/'+user)
            try:
                response=self.manager('POST',self.prefix+'/preview',{},expected=(409,))
                assert response.get('allow') is False
                return response
            finally:
                for user in ('alice','spare'):self.api('PUT',f'/teams/{self.teams["Committer1"]}/members/'+user)
        self.case('资格01-空组','必需组为空时阻断预检',empty_team)
        def archived():
            self.api('PATCH',self.path,{'archived':True})
            try:return self.manager('POST',self.prefix+'/preview',{},expected=(409,))
            finally:self.api('PATCH',self.path,{'archived':False})
        self.case('接入03-C-归档','归档仓库不得错误启用',archived)
        def rename():
            self.api('PATCH',self.path,{'name':'approval-renamed'})
            try:
                result=self.manager('POST',self.prefix+'/preview',{},expected=(409,))
                assert self.get('/repositories/'+str(self.repo['id']))['name']=='approval-renamed'
                return result
            finally:self.api('PATCH','/repos/approval-probe/approval-renamed',{'name':'approval-live'})
        self.case('接入16-C-仓库更名','更名后稳定ID核对拒绝旧安装路径',rename)
        def deleted_team():
            self.api('DELETE',f'/teams/{self.teams["Committer1"]}')
            return self.manager('POST',self.prefix+'/preview',{},expected=(409,))
        self.case('资格01-删除团队','删除必需团队后禁止继续预检',deleted_team)

    def inflight_cases(self):
        obj=self.installer
        self.git_setup('HTTP')
        for phase in ('pre-receive','update'):
            def inflight(phase=phase):
                self.git_commit()
                label='在途-'+phase+'-'+secrets.token_hex(4)
                folder=obj.owned+'/'+label
                obj.exec(['mkdir',folder])
                pause=obj.repo+'/hooks/'+phase+'.d/000-矩阵在途暂停'
                script=('#!/bin/sh\n# 隔离推送同步点；最多等待30秒，不修改判定结果。\n'+
                        'touch '+shlex.quote(folder+'/已进入')+'\n'+
                        'deadline=$(($(date +%s)+30))\nwhile [ ! -f '+shlex.quote(folder+'/继续')+' ]; do\n'+
                        ' [ "$(date +%s)" -lt "$deadline" ] || exit 78\n sleep 0.05\ndone\nexit 0\n')
                obj.atomic(pause,script.encode(),0o755)
                managed=obj.repo+'/hooks/'+phase+'.d/gitea-manager-approval'
                if phase=='pre-receive':obj.exec(['rm',managed])
                preview=obj.snapshot()
                try:
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future=pool.submit(self.git_command,['push','origin','HEAD:refs/heads/'+label])
                        try:
                            deadline=time.monotonic()+10
                            while time.monotonic()<deadline:
                                if obj.exec(['sh','-c','if [ -e "$1" ]; then echo 已进入; fi','核对暂停点',folder+'/已进入']).strip():break
                                if future.done():raise AssertionError('推送在同步点之前退出：'+str(future.result()))
                                time.sleep(.1)
                            else:raise AssertionError('推送未进入预定原生Hook阶段')
                            result=obj.install(preview)
                        finally:obj.exec(['touch',folder+'/继续'])
                        pushed=future.result(timeout=15)
                    expected=self.git_command(['rev-parse','HEAD'])['输出'].strip()
                    actual=self.execute('git','--git-dir='+self.repo_dir,'rev-parse','refs/heads/'+label).strip()
                    assert expected==actual
                    assert self.verify_owned()['State']['StartedAt']==self.report['实例']['初始启动时间']
                    assert self.get(self.path+'/branch_protections')==self.baseline_rules
                    return {'暂停阶段':phase,'安装':result,'推送':pushed,'实际引用':actual,'无重启':True}
                finally:obj.exec(['rm','-f',pause])
            self.case('接入05-'+('A' if phase=='pre-receive' else 'B'),'真实在途HTTP推送期间安装或替换受管Hook',inflight)
        def invalid_template():
            target=obj.custom+'/templates/矩阵故障模板.tmpl'
            obj.atomic(target,'{{if}}故障模板{{end}}'.encode())
            before=self.verify_owned()['State']['StartedAt']
            preview=obj.snapshot()
            failure=None
            try:
                try:obj.install(preview)
                except Exception as exc:failure=type(exc).__name__
                try:
                    with build_opener(ProxyHandler({})).open(self.base+'/user/login',timeout=8) as response:
                        code=response.code;body=response.read()
                except Exception as exc:
                    code=getattr(exc,'code',0);body=b''
                current=self.verify_owned()
                evidence={'安装拒绝类型':failure,'真实页面HTTP':code,'页面有内容':bool(body),
                          '主进程持续运行':current['State']['Running'],'启动时间保持':current['State']['StartedAt']==before}
                self.report['模板语法故障原始结果']=evidence;self.save()
                assert failure and code==200 and body and evidence['主进程持续运行'] and evidence['启动时间保持'], '热重载失败影响了已有运行时页面：'+str(evidence)
                return evidence
            finally:
                # 只删除本试验新增的无效模板；不恢复旧数据库或其他人的模板。
                obj.exec(['rm','-f',target])
                obj.exec(obj.reload)
        self.case('接入09-B-真实语法故障','磁盘上存在无效模板时不能破坏当前可用页面',invalid_template)

    def fault_cases(self):
        name=self.manager_name
        self.run(['docker','cp',str(ROOT/'scripts/approval-fault-proxy.py'),name+':/tmp/审批故障代理.py'])
        def control(mode):
            self.run(['docker','exec','-i',name,'python','-c',
                'import pathlib,sys;p=pathlib.Path("/tmp/审批故障代理.json");p.write_bytes(sys.stdin.buffer.read())'],
                input=json.dumps({'故障':mode},ensure_ascii=False))
        control('正常')
        self.run(['docker','exec','-d',name,'python','/tmp/审批故障代理.py'])
        def url(value):
            self.run(['docker','exec','-i',name,'python','-c',
                'from app import create_app;from models import db,GiteaServer;import sys\napp=create_app()\n'
                'with app.app_context():\n s=db.session.get(GiteaServer,1);s.gitea_url=sys.stdin.read();db.session.commit()'],input=value)
        url('http://127.0.0.1:18765')
        try:
            for fault in ('超时','500','损坏JSON'):
                def shadow(fault=fault):
                    pr=self.pr('影子故障-'+fault,('bob','carol','dave'))
                    control(fault)
                    try:
                        self.merge(pr,expected=True)
                        return {'PR':pr['number'],'故障':fault,'结果':'原生合并成功，影子Hook没有等待服务'}
                    finally:control('正常')
                self.case('接入11-C-'+fault,'影子模式不增加原生API故障依赖',shadow)
            self.install('isolated_enforced')
            for fault in ('403','404','429','500','损坏JSON','重定向','超时'):
                def enforced(fault=fault):
                    pr=self.pr('强制故障-'+fault,('alice','bob','carol'))
                    control(fault)
                    started=time.monotonic()
                    try:
                        self.merge(pr,expected=False)
                        elapsed=time.monotonic()-started
                        assert elapsed<10,'门禁故障超过硬超时'
                        return {'PR':pr['number'],'故障':fault,'实际合并被拒绝':True,'含核对耗时秒':round(elapsed,3)}
                    finally:control('正常')
                self.case('故障01-'+fault,'真实合并期间原生读取异常必须阻断',enforced)
        finally:url('http://gitea:3000')
        self.rollback_cases()

    def timeline_metadata_cases(self):
        postgres=self.compose('ps','-q','postgres').strip()
        info=json.loads(self.run(['docker','inspect',postgres]))[0]
        assert info['Config']['Labels'].get(module.module.LABEL)==self.run_id
        def metadata(number):
            assert type(number) is int and number>0
            # 固定整数参数、只读事务、短语句与锁超时；不读取评论正文或任何凭据。
            query='''BEGIN READ ONLY;
SET LOCAL statement_timeout = '1000ms';
SET LOCAL lock_timeout = '100ms';
WITH subject AS (
 SELECT p.id AS pr_id, i.id AS issue_id, i.repo_id, i.index
 FROM pull_request p JOIN issue i ON i.id=p.issue_id
 WHERE i.repo_id=%d AND i.index=%d
), ordered AS (
 SELECT c.id,c.type,c.review_id, row_number() OVER (ORDER BY c.created_unix,c.id) AS ordinal
 FROM comment c JOIN subject s ON c.issue_id=s.issue_id
)
SELECT json_build_object('pr_id',(SELECT pr_id FROM subject),'repo_id',(SELECT repo_id FROM subject),
 'number',(SELECT index FROM subject),'count',(SELECT count(*) FROM ordered),
 'max_id',(SELECT coalesce(max(id),0) FROM ordered),
 'events',(SELECT coalesce(json_agg(json_build_object('id',id,'type',type,'review_id',review_id,'ordinal',ordinal) ORDER BY ordinal),'[]')
 FROM ordered WHERE type IN (22,25,29)))::text;
ROLLBACK;
''' % (self.repo['id'],number)
            started=time.monotonic()
            result=self.run(['docker','exec','-i',postgres,'psql','-X','-q','-A','-t','-v','ON_ERROR_STOP=1','-U','gitea','-d','gitea'],input=query)
            assert len(result)<1024*1024
            value=json.loads(result.strip())
            value['采集耗时秒']=round(time.monotonic()-started,4)
            return value
        def full_pages():
            pr=self.pr('多页只读元数据',('alice','bob','carol'))
            for i in range(51):
                self.api('POST',self.path+f'/pulls/{pr["number"]}/reviews',
                    {'event':'COMMENT','body':'多页元数据固定样本 '+str(i),'commit_id':pr['head']['sha']},user='dave')
            before=metadata(pr['number'])
            rows=[]
            for page in range(1,(before['count']+49)//50+1):
                rows.extend(self.get(self.path+f'/issues/{pr["number"]}/timeline?page={page}&limit=50') or [])
            after=metadata(pr['number'])
            assert {e['id'] for e in before['events']}=={r['id'] for r in rows if r['type'] in ('review','change_target_branch','pull_push')}
            assert {k:v for k,v in before.items() if k!='采集耗时秒'}=={k:v for k,v in after.items() if k!='采集耗时秒'}
            # 只读事务必须拒绝写入；无目标行的 UPDATE 也应在执行前被 PostgreSQL 拒绝。
            result=subprocess.run(['docker','exec','-i',postgres,'psql','-X','-q','-v','ON_ERROR_STOP=1','-U','gitea','-d','gitea'],
                input='BEGIN READ ONLY; UPDATE comment SET id=id WHERE false; ROLLBACK;',capture_output=True,text=True,timeout=5)
            assert result.returncode!=0 and 'read-only transaction' in result.stderr
            return {'PR':pr['number'],'只读完整性元数据':before,'API事件数':len(rows),
                    '前后元数据一致':True,'数据库拒绝事务内写入':True,'说明':'仅隔离 PostgreSQL 可行性，不代表生产适配已实现'}
        self.case('分页修复预研-多页只读元数据','用只读记录标记确认原生时间线多页完整性',full_pages)
        self.install('isolated_enforced')
        def hidden_aba():
            pr=self.pr('过滤页之后的强推事件',('alice','bob','carol'))
            comments=[{'path':pr['head']['ref']+'.txt','new_position':1,'body':'整页行内评论固定样本 '+str(i)} for i in range(100)]
            self.api('POST',self.path+f'/pulls/{pr["number"]}/reviews',
                {'event':'COMMENT','body':'制造完整过滤页','commit_id':pr['head']['sha'],'comments':comments},user='dave')
            self.git_setup('HTTP')
            self.git_command(['checkout','-b','matrix-aba','origin/'+pr['head']['ref']])
            self.git_commit()
            self.git_command(['push','origin','HEAD:refs/heads/'+pr['head']['ref']])
            self.git_command(['reset','--hard',pr['head']['sha']])
            self.git_command(['push','--force','origin','HEAD:refs/heads/'+pr['head']['ref']])
            deadline=time.monotonic()+30
            pages=[]
            while time.monotonic()<deadline:
                meta=metadata(pr['number'])
                pages=[self.get(self.path+f'/issues/{pr["number"]}/timeline?page={page}&limit=50') or []
                       for page in range(1,(meta['count']+49)//50+1)]
                rows=[row for page in pages for row in page]
                forces=[r for r in rows if r['type']=='pull_push' and json.loads(r['body'])['is_force_push']]
                if forces:break
                time.sleep(.25)
            else:raise AssertionError('原生强推事件没有完成，不能判断本用例')
            assert pages[0] and not pages[1] and any(pages[2:]), '未形成夹在可见事件之间的完整过滤页'
            current=self.check(pr)
            self.report['过滤页强推原始证据']={'PR':pr['number'],'各页可见条数':[len(p) for p in pages],
                '只读完整性标记':meta,'强推事件':forces,'当前审批计算':current,
                '原生审批':self.get(self.path+f'/pulls/{pr["number"]}/reviews')}
            self.save()
            self.merge(pr,expected=False)
            assert current['satisfied'] is False, '遗漏强推事件导致复用旧批准'
            return self.report['过滤页强推原始证据']
        self.case('生命周期05-过滤页ABA','完整过滤页后存在强推事件仍必须使旧批准失效',hidden_aba)

    @staticmethod
    def exec_assert_lock(obj):
        obj.exec(['test','-d',obj.lock])

    def run_matrix(self,section):
        self.start();self.fixtures();self.start_manager();self.create_repo();self.setup_policy()
        self.install('shadow')
        if section in ('all','lifecycle'):self.lifecycle()
        if section in ('all','install'):self.installer_cases()
        if section in ('all','transport'):self.transport_cases()
        if section in ('all','native'):self.native_cases()
        if section in ('all','auto'):self.auto_cases()
        if section in ('all','qualification'):self.qualification_cases()
        if section in ('all','inflight'):self.inflight_cases()
        if section in ('all','fault'):self.fault_cases()
        if section=='timeline':self.timeline_metadata_cases()
        self.report['状态']='本轮分项执行完成，完整矩阵仍按所有分项证据判定'
        self.save()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-tag',default='approval-candidate-20260913')
    parser.add_argument('--packaged',action='store_true')
    parser.add_argument('--section',choices=('all','lifecycle','install','transport','native','auto','qualification','inflight','fault','timeline'),default='all')
    args=parser.parse_args()
    sections=['lifecycle','install','transport','native','auto','qualification','inflight','fault','timeline'] if args.section=='all' else [args.section]
    failed=False
    for section in sections:
        # 有删除团队、回退及修改规则的用例，各节使用不同实例，不能共享已破坏的前置状态。
        run=Matrix(args.image_tag,args.packaged)
        run.report['分项']=section
        try:
            run.run_matrix(section)
        except Exception as exc:
            failed=True
            run.report['状态']='基础设施或前置失败，保留现场'
            run.report['首次失败'].append({'原因':str(exc).replace(run.password,'[隔离口令已隐藏]'),'位置':traceback.format_exc()[-3000:]})
            run.save()
        finally:
            run.cleanup()
            print('矩阵分项证据：'+str(run.report_path),flush=True)
        failed |= bool(run.report['首次产品失败'])
    return 1 if failed else 0


if __name__=='__main__':
    sys.exit(main())
