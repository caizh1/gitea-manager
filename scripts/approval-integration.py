#!/usr/bin/env python3
"""真实集成验收：只创建本次标记实例，保留存量与首次失败，禁止传入生产地址。"""
import base64
import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import traceback
import threading
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPCookieProcessor
import http.cookiejar
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('feasibility', ROOT/'scripts/approval-feasibility.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class Acceptance(module.Probe):
    def __init__(self, image_tag='approval-candidate-20260913', packaged=False):
        super().__init__()
        if not re.fullmatch('[a-zA-Z0-9_.-]+',image_tag):raise ValueError('验收镜像标签无效')
        self.image_tag,self.packaged=image_tag,packaged
        self.report = {'说明': '实际功能服务、原生审批和官方镜像隔离集成；不代表完整生产验收',
                       '运行标识': self.run_id, '结果': [], '首次失败': [],
                       '未执行': ['全部生产接入矩阵', '各24小时长测', '现场峰值和两倍峰值', '人工精力指标', '目标现场验收'],
                       '已知缺陷': ['APR-RACE-001：P2，暂缓修复，原反证断言和证据保留']}
        self.manager_name = self.project+'-manager'
        self.manager_volume = self.project+'-manager-data'
        self.manager_http = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.manager_base = None
        self.csrf = ''
        self.access_csrf = ''
        self.pid = None
        self.secret_tokens = []

    def run(self, args, **kwargs):
        try:
            return super().run(args, **kwargs)
        except RuntimeError as exc:
            message = str(exc)
            for token in self.secret_tokens:
                message = message.replace(token, '[隔离服务令牌已隐藏]')
            raise RuntimeError(message) from None

    def manager(self, method, path, data=None, raw=False, expected=(200, 201)):
        req = Request(self.manager_base+path, method=method,
                      headers={'Content-Type': 'application/json', 'X-Approval-CSRF': self.csrf,
                               'X-CSRF-Token': self.access_csrf},
                      data=json.dumps(data).encode() if data is not None else None)
        try: response = self.manager_http.open(req, timeout=35)
        except HTTPError as exc: response = exc
        with response:
            content = response.read()
            if response.code not in expected:
                try: value = json.loads(content) if content else {}
                except ValueError: value = {'error': '服务端未返回 JSON，请检查本次独立容器日志'}
                raise RuntimeError('Manager '+path+' 返回 '+str(response.code)+'：'+str(value.get('error', '接口未成功')))
            value = content if raw else json.loads(content)
            if isinstance(value, dict) and value.get('csrf_token'):
                self.access_csrf = value['csrf_token']
            return value

    def start_manager(self):
        _, token = self.api('POST', '/users/probe-admin/tokens', {'name': '审批隔离验收', 'scopes': ['all']})
        self.secret_tokens.append(token['sha1'])
        name = self.manager_name
        self.run(['docker', 'run', '-d', '--platform', 'linux/amd64', '--name', name,
                  '--label', module.LABEL+'='+self.run_id, '--network', self.project+'_default', '--network-alias', 'manager', '--network-alias', 'backend',
                  '--memory', '512m', '--cpus', '1', '-p', '127.0.0.1::5000',
                  *([] if self.packaged else ['-v', str(ROOT/'backend')+':/app:ro']), '-v', self.manager_volume+':/testdata',
                  '-v', '/var/run/docker.sock:/var/run/docker.sock',
                  '-e', 'DATABASE_URL=sqlite:////testdata/manager.db', '-e', 'DISABLE_BACKGROUND_TASKS=1',
                  '-e', 'INIT_PASSWORD='+self.password, '-e', 'SECRET_KEY='+secrets.token_urlsafe(32),
                  '-e', 'GM_APPROVAL_ISOLATION_ID='+self.run_id,
                  'gitea-review-backend:'+self.image_tag])
        info = json.loads(self.run(['docker', 'inspect', name]))[0]
        self.manager_base = 'http://127.0.0.1:'+info['NetworkSettings']['Ports']['5000/tcp'][0]['HostPort']
        deadline = time.monotonic()+50
        while time.monotonic() < deadline:
            try:
                self.manager('GET', '/api/session'); break
            except Exception: time.sleep(0.3)
        else: raise RuntimeError('独立 Manager 未就绪')
        self.manager('POST', '/api/login', {'password': self.password})
        self.csrf = self.manager('GET', '/api/approval-rules/csrf')['token']
        # 写入的只是本次新建 Manager 数据库，模拟“已配置现有 Gitea 实例”。
        init = '''import json,sys
sys.path.insert(0,"/app")
from app import create_app
from models import db,GiteaServer
d=json.load(sys.stdin)
app=create_app()
with app.app_context():
 s=GiteaServer(name="审批隔离实例",host="localhost",ssh_user="root",gitea_port=3000,gitea_url="http://gitea:3000",api_token=d["token"],is_local=True,gitea_container=d["container"],pg_container=d["postgres"])
 db.session.add(s);db.session.commit()
 print(s.id)
'''
        init_path = self.folder/'初始化隔离Manager.py'; init_path.write_text(init)
        self.run(['docker', 'cp', str(init_path), name+':/tmp/初始化.py'])
        self.server_id = int(self.run(['docker', 'exec', '-i', name, 'python', '/tmp/初始化.py'],
                            input=json.dumps({'token': token['sha1'], 'container': self.container,
                                              'postgres': self.compose('ps','-q','postgres').strip()})).strip().splitlines()[-1])

    def create_repo(self):
        self.path = '/repos/approval-probe/approval-live'
        _, self.repo = self.api('POST', '/orgs/approval-probe/repos', {'name': 'approval-live', 'private': True,
                             'auto_init': True, 'default_branch': 'main', 'description': '已有数据审批接入样本'})
        self.api('PATCH', self.path, {'allow_merge_commits': True, 'allow_rebase': True, 'allow_rebase_explicit': True,
                                     'allow_squash_merge': True, 'allow_fast_forward_only_merge': True, 'allow_manual_merge': False})
        self.repo_dir = '/data/git/repositories/approval-probe/approval-live.git'
        self.api('POST', self.path+'/branch_protections', {'rule_name': 'main', 'priority': 1, 'enable_push': False,
                    'required_approvals': 3, 'block_admin_merge_override': True, 'block_on_rejected_reviews': True})
        self.baseline_rules = self.get(self.path+'/branch_protections')
        self.baseline_hooks = {p: self.execute('sha256sum', self.repo_dir+'/hooks/'+p).split()[0] for p in ('pre-receive', 'update')}
        self.legacy = self.create_pr('存量PR')
        for user in ('alice', 'bob', 'carol', 'dave'): self.approve(self.legacy, user)

    def create_pr(self, label):
        branch = 'feature-'+secrets.token_hex(5)
        self.api('POST', self.path+'/branches', {'new_branch_name': branch, 'old_ref_name': 'main'}, user='author')
        self.api('POST', self.path+'/contents/'+branch+'.txt', {'branch': branch, 'message': '测试：'+label,
                 'content': base64.b64encode((label+'\n').encode()).decode()}, user='author')
        _, pr = self.api('POST', self.path+'/pulls', {'title': label, 'head': branch, 'base': 'main'}, user='author')
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            pr = self.get(self.path+'/pulls/'+str(pr['number']))
            if pr['mergeable']: return pr
            time.sleep(0.1)
        raise RuntimeError('PR 未准备好')

    def approve(self, pr, user):
        return self.api('POST', self.path+'/pulls/'+str(pr['number'])+'/reviews', {'event': 'APPROVED',
                        'commit_id': pr['head']['sha'], 'body': '隔离固定样本审批'}, user=user)[1]

    def setup_policy(self):
        policy = self.manager('POST', '/api/approval-rules/policies', {'server_id': self.server_id, 'repo_id': self.repo['id'],
                    'rule': {'name': '三人及双组审批', 'branches': ['main'], 'total': 3,
                             'groups': [{'team_id': self.teams['Committer1'], 'required': 1},
                                        {'team_id': self.teams['Committer2'], 'required': 1}]}})
        self.pid = policy['id']
        self.prefix = '/api/approval-rules/policies/'+str(self.pid)
        preview = self.manager('POST', self.prefix+'/preview', {})
        self.report['预检'] = preview
        self.record('接入01-只读预检', self.get(self.path+'/branch_protections') == self.baseline_rules)

    def record(self, name, passed, **details):
        self.report['结果'].append({'场景': name, '结果': '通过' if passed else '失败', **details})
        self.save()
        print(name+'：'+('通过' if passed else '失败'), flush=True)
        if not passed: raise RuntimeError(name+' 断言失败')

    def install(self, mode):
        self.manager('POST', self.prefix+'/preview', {})
        raw = self.manager('POST', self.prefix+'/bundle', {'mode': mode, 'gate_url': 'http://manager:5000/api/approval-internal'}, raw=True)
        folder = self.folder/('组件-'+mode); folder.mkdir(mode=0o700)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if any(Path(n).name != n for n in archive.namelist()): raise RuntimeError('导出包路径异常')
            archive.extractall(folder)
        for file in folder.iterdir(): file.chmod(0o600)
        spec = importlib.util.spec_from_file_location('installer_'+mode, folder/'install.py')
        install = importlib.util.module_from_spec(spec); spec.loader.exec_module(install)
        config=json.loads((folder/'config.json').read_text())
        self.secret_tokens.append(config['token'])
        def diagnostic_runner(args,input=None):
            result=subprocess.run(args,input=input,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
            if result.returncode:
                detail=result.stderr.decode(errors='replace')[-1800:].replace(self.password,'[隔离口令已隐藏]')
                for secret in self.secret_tokens:detail=detail.replace(secret,'[隔离令牌已隐藏]')
                evidence={'类别':args[:2],'退出码':result.returncode,'错误输出':detail,'时间':time.time()}
                self.report.setdefault('容器操作首次失败',[]).append(evidence);self.save()
                raise RuntimeError('隔离容器操作失败：'+json.dumps(evidence,ensure_ascii=False))
            return result.stdout
        obj = install.Installer(self.container, folder,runner=diagnostic_runner)
        # 宿主机和容器使用不同 DNS 地址指向同一真实服务，只适配回执连接地址。
        original = obj.ack
        def ack(*args, **kwargs):
            saved = obj.config['gate_url']; obj.config['gate_url'] = self.manager_base+'/api/approval-internal'
            try: return original(*args, **kwargs)
            finally: obj.config['gate_url'] = saved
        obj.ack = ack
        if mode=='shadow':
            # 预置其他管理员维护的内容；测试原样保留及并发修改后的拒绝。
            self.other_hook=obj.repo+'/hooks/pre-receive.d/现有检查'
            self.other_content=b'#!/bin/sh\n# '+ '原有独立门禁样本\n'.encode()+b'exit 0\n'
            obj.atomic(self.other_hook,self.other_content,0o755)
            obj.atomic(obj.footer,'<!-- 现有自定义页面与其他组件占位样本 -->\n'.encode())
            old_preview=obj.snapshot()
            changed=obj.read(obj.footer)+'<!-- 管理员并发新增：必须保留 -->\n'.encode()
            obj.atomic(obj.footer,changed)
            try:obj.install(old_preview)
            except ValueError:self.record('接入07-模板并发修改拒绝且保留',obj.read(obj.footer)==changed)
            else:raise RuntimeError('安装器错误接受过期模板预览')
        snapshot = obj.snapshot()
        module.write_json(folder/'安装预览.json', snapshot)
        with self.traffic('安装-'+mode):result = obj.install(snapshot)
        obj.verify_installed()
        self.record('接入-安装-'+mode, True, 说明=result)
        self.record('接入04-其他Hook和模板保持-'+mode,obj.read(self.other_hook)==self.other_content and
                    '管理员并发新增'.encode() in obj.read(obj.footer))
        self.installer = obj

    def traffic(self, label):
        """实际读取流量与连接复用的有限演练，不能代替全部 SSH/写流量或现场峰值。"""
        from contextlib import contextmanager
        import http.client
        from urllib.parse import urlsplit
        @contextmanager
        def active():
            stop=threading.Event();rows=[]
            u=urlsplit(self.base);conn=http.client.HTTPConnection(u.hostname,u.port,timeout=5)
            def workload():
                while not stop.is_set():
                    started=time.monotonic()
                    try:
                        conn.request('GET','/user/login');response=conn.getresponse();content=response.read()
                        rows.append({'成功':response.status==200 and bool(content),'耗时秒':round(time.monotonic()-started,4)})
                    except Exception as exc:rows.append({'成功':False,'原因':type(exc).__name__})
                    stop.wait(0.2)
            thread=threading.Thread(target=workload,daemon=True);thread.start()
            try:yield
            finally:
                stop.set();thread.join(7);conn.close()
                self.report.setdefault('有限持续流量',{})[label]=rows;self.save()
                self.record('接入-持续页面读取-'+label,bool(rows) and all(r['成功'] for r in rows))
        return active()

    def rollback_cases(self):
        obj=self.installer
        data=self.manager('GET','/api/approval-rules/policies')['items'][0]
        with self.traffic('仅页面回退'):
            obj.uninstall(True)
        self.record('接入19-页面卸载不取消门禁',
                    self.manager('GET','/api/approval-rules/policies')['items'][0]['mode']=='isolated_enforced' and
                    bool(obj.read(obj.repo+'/hooks/update.d/gitea-manager-approval')))
        before=self.execute('git','--git-dir='+self.repo_dir,'show-ref').strip()
        self.manager('POST',self.prefix+'/disable',{'revision':data['revision'],'reason':'独立验收正常回退'})
        with self.traffic('门禁卸载'):
            obj.uninstall(False)
        self.record('接入18-回退只移除组件且保留接入后数据',
                    before==self.execute('git','--git-dir='+self.repo_dir,'show-ref').strip() and
                    obj.read(self.other_hook)==self.other_content and '管理员并发新增'.encode() in obj.read(obj.footer) and
                    not obj.read(obj.repo+'/hooks/update.d/gitea-manager-approval') and
                    self.get(self.path+'/branch_protections')==self.baseline_rules)
        obj.uninstall(False)
        self.record('接入08-重复卸载回执可核对',self.manager('GET','/api/approval-rules/policies')['items'][0]['mode']=='disabled')
        self.record('接入-回退后没有重启',self.verify_owned()['State']['StartedAt']==self.report['实例']['初始启动时间'])

    def check(self, pr):
        return self.manager('GET', self.prefix+'/status/'+str(pr['number']))

    def merge(self, pr, style='merge', expected=True):
        # 上次拒绝后原生冲突检查会重新入队；只读等待明确就绪，每次用例仅发送一次合并写请求。
        ready_started=time.monotonic()
        while time.monotonic()-ready_started<60:
            ready=self.get(self.path+'/pulls/'+str(pr['number']))
            if ready.get('mergeable'):break
            time.sleep(0.5)
        else:raise RuntimeError('原生冲突检查在一分钟内未就绪，没有重试合并写请求')
        before = self.execute('git', '--git-dir='+self.repo_dir, 'rev-parse', 'refs/heads/main').strip()
        started = time.monotonic()
        code, value = self.api('POST', self.path+'/pulls/'+str(pr['number'])+'/merge',
                               {'do': style, 'head_commit_id': pr['head']['sha']}, user='author', expected=(200,403,405,409,500))
        elapsed = time.monotonic()-started
        after = self.execute('git', '--git-dir='+self.repo_dir, 'rev-parse', 'refs/heads/main').strip()
        actual = self.get(self.path+'/pulls/'+str(pr['number']))
        self.record('合并-'+style+'-'+pr['title'], (after != before) == expected and bool(actual['merged']) == expected,
                    合并HTTP=code, 合并响应=value, 原生合并就绪=actual.get('mergeable'),
                    原生就绪等待秒=round(time.monotonic()-ready_started-elapsed,3),
                    旧引用=before, 新引用=after, 耗时秒=round(elapsed, 3))

    def execute_cases(self):
        self.start(); self.fixtures(); self.start_manager(); self.create_repo(); self.setup_policy()
        self.install('shadow')
        shadow = self.create_pr('影子组票不足仍走原生流程')
        for user in ('bob','carol','dave'): self.approve(shadow, user)
        self.record('规则-影子缺组显示', not self.check(shadow)['satisfied'])
        self.run(['docker', 'pause', self.manager_name])
        try: self.merge(shadow)
        finally: self.run(['docker', 'unpause', self.manager_name])
        self.install('isolated_enforced')
        # 旧 PR 撤回关键组票后仍有三票，离线豁免必须保留原流程。
        reviews = self.get(self.path+'/pulls/'+str(self.legacy['number'])+'/reviews')
        alice = next(r for r in reviews if r['user']['login']=='alice')
        self.api('POST', self.path+'/pulls/'+str(self.legacy['number'])+'/reviews/'+str(alice['id'])+'/dismissals', {'message': '存量保持原流程', 'priors': False})
        self.run(['docker','pause',self.manager_name])
        try: self.merge(self.legacy)
        finally: self.run(['docker','unpause',self.manager_name])
        for style in module.STYLES:
            pr = self.create_pr('新PR缺组票')
            for user in ('bob','carol','dave'): self.approve(pr,user)
            self.record('规则-'+style+'缺组', not self.check(pr)['satisfied'])
            self.merge(pr, style, False)
            self.approve(pr, 'alice')
            self.record('规则-'+style+'全部满足', self.check(pr)['satisfied'])
            self.merge(pr, style, True)
        outage = self.create_pr('强制服务故障阻断')
        for user in ('alice','bob','carol'): self.approve(outage,user)
        self.run(['docker','pause',self.manager_name])
        try: self.merge(outage, expected=False)
        finally: self.run(['docker','unpause',self.manager_name])
        self.record('原生规则与包装器保持', self.get(self.path+'/branch_protections') == self.baseline_rules and
                    all(self.execute('sha256sum',self.repo_dir+'/hooks/'+p).split()[0] == h for p,h in self.baseline_hooks.items()))
        self.record('Gitea没有重启', self.verify_owned()['State']['StartedAt'] == self.report['实例']['初始启动时间'])
        self.report['状态'] = '本轮有限集成通过；完整验收未完成'; self.save()

    def cleanup(self):
        # 保留卷和证据，仅停止本次标签归属的容器，不能清理其他开发环境。
        for attr in ('view_name','ui_proxy_name'):
            name=getattr(self,attr,None)
            if not name:continue
            try:
                info=json.loads(self.run(['docker','inspect',name]))[0]
                if info['Config']['Labels'].get(module.LABEL)==self.run_id and info['State']['Running']:
                    self.run(['docker','stop',name])
            except Exception:pass
        try:
            info = json.loads(self.run(['docker','inspect',self.manager_name]))[0]
            if info['Config']['Labels'].get(module.LABEL)==self.run_id:
                if info['State'].get('Paused'): self.run(['docker','unpause',self.manager_name])
                self.run(['docker','stop',self.manager_name])
        except Exception: pass
        if self.container:
            self.verify_owned(); self.compose('stop')

    def ui_window(self, seconds):
        """给真实浏览器留出有上限的验收窗口；到期或写入结束标记后只停止本次实例。"""
        proxy = self.project+'-ui-proxy';self.ui_proxy_name=proxy
        # 人工窗口也使用独立展示镜像，避免误测主 Manager 展示入口。
        view_spec=importlib.util.spec_from_file_location('window_capacity',ROOT/'scripts/approval-capacity-probe.py')
        view_module=importlib.util.module_from_spec(view_spec);view_spec.loader.exec_module(view_module)
        self.diagnostics=False;self.reference_profile=False;self.threads=8
        view_module.Capacity.start_view(self)
        self.manager('POST','/api/settings',{'host_ip':'127.0.0.1'})
        config = '''# 独立审批验收代理，不修改现有 Gitea 配置。
events {}
http {
 include /etc/nginx/mime.types;
 server { listen 80; root /usr/share/nginx/html;
  location /api/ { proxy_pass http://manager:5000; proxy_set_header Host $http_host; }
  location / { try_files $uri $uri/ /index.html; }
 }
 server { listen 81;
  location /-/manager-approval/ { proxy_pass http://approval-view:5000/api/approval-view/; proxy_set_header Host $http_host; proxy_set_header Cookie $http_cookie; add_header Cache-Control no-store; }
  location / { proxy_pass http://gitea:3000; proxy_set_header Host $http_host; }
 }
}
'''
        conf = self.folder/'页面代理.conf'; conf.write_text(config)
        self.run(['docker','run','-d','--name',proxy,'--label',module.LABEL+'='+self.run_id,
                  '--network',self.project+'_default','--memory','128m','--cpus','0.5',
                  '-p','127.0.0.1::80','-p','127.0.0.1::81',
                  '-v',str(conf)+':/etc/nginx/nginx.conf:ro',
                  *([] if self.packaged else ['-v',str(ROOT/'frontend/dist')+':/usr/share/nginx/html:ro']),
                  'gitea-review-frontend:'+self.image_tag])
        try:
            info=json.loads(self.run(['docker','inspect',proxy]))[0]
            urls={label:'http://127.0.0.1:'+info['NetworkSettings']['Ports'][str(port)+'/tcp'][0]['HostPort']
                  for label,port in [('Manager',80),('Gitea',81)]}
            module.write_json(self.folder/'页面验收入口.json',dict(urls,账号=['probe-admin','author','alice','bob','carol','dave'],口令=self.password,说明='仅本次隔离测试账号；停止后保留卷，禁止将此文件分享或打包'))
            (self.folder/'页面验收入口.json').chmod(0o600)
            self.report['页面入口']=urls;self.save()
            print('页面窗口：'+json.dumps(urls,ensure_ascii=False),flush=True)
            deadline=time.monotonic()+seconds
            while time.monotonic()<deadline and not (self.folder/'页面验收结束').exists():time.sleep(1)
        finally:
            info=json.loads(self.run(['docker','inspect',proxy]))[0]
            if info['Config']['Labels'].get(module.LABEL)==self.run_id:self.run(['docker','stop',proxy])
            info=json.loads(self.run(['docker','inspect',self.view_name]))[0]
            if info['Config']['Labels'].get(module.LABEL)==self.run_id:self.run(['docker','stop',self.view_name])


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ui-window-seconds',type=int,default=0)
    parser.add_argument('--image-tag',default='approval-candidate-20260913')
    parser.add_argument('--packaged',action='store_true',help='从交付镜像运行，不挂载工作区源码')
    args=parser.parse_args()
    run = Acceptance(args.image_tag,args.packaged)
    try:
        run.execute_cases()
        if args.ui_window_seconds:run.ui_window(min(args.ui_window_seconds,7200))
        run.rollback_cases()
    except Exception as exc:
        run.report['状态']='失败，保留首次现场'
        run.report['首次失败'].append({'异常':type(exc).__name__, '原因':str(exc).replace(run.password,'[隐藏]'), '调用位置':traceback.format_exc().replace(run.password,'[隐藏]')})
        run.save(); print('验收未通过：'+str(exc),flush=True); sys.exit(1)
    finally: run.cleanup(); print('证据：'+str(run.report_path),flush=True)
