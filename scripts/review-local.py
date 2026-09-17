#!/usr/bin/env python3
"""独立本机真实验收环境。凭据只放工作目录；模型密钥经隐藏输入提交管理接口。"""
import argparse
import base64
import getpass
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get('REVIEW_STATE_DIR', str(ROOT/'work/AIReview本机')))


def run(args, capture=True, env=None, cwd=None, input=None, check=True):
    environment = os.environ.copy()
    environment['PATH'] = '/Applications/Docker.app/Contents/Resources/bin:' + environment.get('PATH', '')
    if env:
        environment.update(env)
    p = subprocess.run(args, cwd=cwd or ROOT, env=environment, input=input, text=True,
                       stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None)
    if p.returncode and check:
        raise RuntimeError(f'{args[0]} 执行失败，退出码 {p.returncode}；命令参数可能含本机凭据，未输出')
    return p.stdout or ''


def values():
    STATE.mkdir(parents=True, exist_ok=True); STATE.chmod(0o700)
    path = STATE/'.env'
    if not path.exists():
        data = {k: secrets.token_urlsafe(32) for k in ('PG_PASSWORD','MANAGER_SECRET','MANAGER_PASSWORD','INTEGRATION_TOKEN',
                                                      'GITEA_PASSWORD','BOT_PASSWORD','DEVELOPER_PASSWORD')}
        data.update(REVIEW_STATE_DIR=str(STATE), REVIEW_MODEL_HOSTS='api.deepseek.com,review-model-fixture')
        path.write_text('# 本机合成代码验收专用配置，禁止提交或打入部署包。\n' + '\n'.join(k+'='+v for k,v in data.items())+'\n')
        path.chmod(0o600)
    secret_dir = STATE/'secrets'; secret_dir.mkdir(exist_ok=True); secret_dir.chmod(0o700)
    master = secret_dir/'master.key'; executor = secret_dir/'executor.token'
    if not master.exists(): master.write_text(base64.urlsafe_b64encode(os.urandom(32)).decode()); master.chmod(0o600)
    if not executor.exists(): executor.write_text(secrets.token_urlsafe(32)); executor.chmod(0o444)
    return dict(line.split('=',1) for line in path.read_text().splitlines() if '=' in line and not line.startswith('#'))


def save(values):
    path = STATE/'.env'
    path.write_text('# 本机合成代码验收专用配置，禁止提交或打入部署包。\n'+'\n'.join(k+'='+v for k,v in values.items())+'\n')
    path.chmod(0o600)


def compose(*args, capture=True):
    options = ['docker','compose','--env-file',str(STATE/'.env'),'-f',str(ROOT/'compose.review-local.yml')]
    override = values().get('REVIEW_COMPOSE_OVERRIDE')
    if override: options += ['-f', override]
    return run([*options,*args],capture=capture)


class Harness:
    def __init__(self):
        self.values = values()
        self.gitea_url = 'http://127.0.0.1:'+self.values.get('REVIEW_GITEA_PORT','13000')
        self.manager_url = 'http://127.0.0.1:'+self.values.get('REVIEW_MANAGER_PORT','15480')
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = ''

    def request(self, method, url, data=None, headers=None, opener=None, raw=False):
        req = urllib.request.Request(url, method=method, data=json.dumps(data,ensure_ascii=False).encode() if data is not None else None,
                                     headers=dict({'Content-Type':'application/json'}, **(headers or {})))
        try:
            with (opener or urllib.request.build_opener()).open(req,timeout=60) as response:
                content = response.read()
                return content if raw else json.loads(content) if content else None
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'本机接口返回 HTTP {exc.code}：{method} {urllib.parse.urlsplit(url).path}') from None

    def gitea(self, method, path, data=None, actor='admin', raw=False):
        token = self.values[{'admin':'GITEA_TOKEN','bot':'BOT_TOKEN','developer':'DEVELOPER_TOKEN'}[actor]]
        return self.request(method,self.gitea_url+'/api/v1'+path,data,{'Authorization':'token '+token},raw=raw)

    def gitea_pages(self, path, actor='admin'):
        result=[]
        for page in range(1,101):
            rows=self.gitea('GET',path+('&' if '?' in path else '?')+f'page={page}&limit=50',actor=actor)
            result.extend(rows)
            if len(rows)<50:return result
        raise RuntimeError('验收读取超过分页上限，不能使用截断列表')

    def manager(self, method, path, data=None):
        headers = {'X-Review-CSRF': self.csrf} if self.csrf else {}
        return self.request(method,self.manager_url+'/api'+path,data,headers,self.opener)

    def login(self):
        self.manager('POST','/login',{'password':self.values['MANAGER_PASSWORD']})
        self.csrf = self.manager('GET','/ai-review/csrf')['token']

    def wait(self, url):
        for _ in range(120):
            try:
                self.request('GET',url); return
            except Exception:
                time.sleep(1)
        raise RuntimeError('本机服务未就绪：'+url)

    def initialize(self):
        self.wait(self.gitea_url+'/api/v1/version'); self.wait(self.manager_url+'/api/session')
        prefix = ['exec','-T','--user','git','gitea','gitea','--config','/data/gitea/conf/app.ini','admin','user']
        users = compose(*prefix,'list')
        for user,password,key,admin in [('review-admin','GITEA_PASSWORD','GITEA_TOKEN',True),
                                         ('review-bot','BOT_PASSWORD','BOT_TOKEN',False),
                                         ('review-developer','DEVELOPER_PASSWORD','DEVELOPER_TOKEN',False)]:
            if user not in users:
                args = ['create','--username',user,'--password',self.values[password],'--email',user+'@example.invalid','--must-change-password=false']
                if admin: args += ['--admin']
                compose(*prefix,*args)
            if key not in self.values or not admin and self.values.get('REVIEW_SCOPE_VERSION') != '2':
                token = compose(*prefix,'generate-access-token','--username',user,'--token-name','review-local-'+secrets.token_hex(4),
                                '--scopes','all' if admin else 'read:user,write:repository,write:issue','--raw').strip().splitlines()[-1]
                self.values[key] = token
        self.values['REVIEW_SCOPE_VERSION'] = '2'
        save(self.values); self.login()
        self.manager('POST','/settings',{'host_ip':'127.0.0.1'})
        servers = self.manager('GET','/servers')
        if not any(s['name']=='本机 AI Review 验收' for s in servers):
            self.manager('POST','/servers',{'name':'本机 AI Review 验收','role':'primary','host':'127.0.0.1','ssh_user':'git',
                'gitea_container':'仅本机审查验收','pg_container':'仅本机审查数据库','pg_dbname':'gitea','pg_user':'gitea',
                'gitea_port':3000,'gitea_url':'http://gitea:3000','api_token':self.values['GITEA_TOKEN']})
        info = STATE/'本机访问说明.txt'
        info.write_text('独立 AI Review 验收环境\n\nManager：'+self.manager_url+'\n管理员密码：'+self.values['MANAGER_PASSWORD']
                        +'\n\nGitea：'+self.gitea_url+'\n普通开发者：review-developer\n密码：'+self.values['DEVELOPER_PASSWORD']
                        +'\n\n管理员：review-admin\n密码：'+self.values['GITEA_PASSWORD']+'\n\n只包含本机合成验收数据；禁止提交或分享凭据。\n')
        info.chmod(0o600)
        print('本机环境已初始化。访问说明：'+str(info))

    def create_repo(self, name='review-fixtures'):
        repos = self.gitea('GET','/user/repos?limit=100')
        full = 'review-admin/'+name
        if not any(r['full_name']==full for r in repos):
            self.gitea('POST','/user/repos',{'name':name,'private':True,'auto_init':True,'default_branch':'main','description':'仅包含合成 C/C++ 验收代码'})
        for user in ('review-bot','review-developer'):
            self.gitea('PUT',f'/repos/{full}/collaborators/{user}',{'permission':'read'})
        configured = self.manager('GET','/ai-review/repositories')
        repo = next((r for r in configured if r['repo_name']==full), None)
        if not repo:
            target = next(s for s in self.manager('GET','/ai-review/servers') if s['name']=='本机 AI Review 验收')
            repo = self.manager('POST','/ai-review/repositories',{'server_id':target['id'],'repo_name':full,'enabled':True,'bot_token':self.values['BOT_TOKEN']})
        self.manager('POST',f"/ai-review/repositories/{repo['id']}/webhook",{'manager_url':'http://backend:5000'})
        return repo

    def configure_model(self, url, model, engine, key):
        cfg = self.manager('GET','/ai-review/config')['config']; cfg.pop('api_key_configured',None)
        cfg.update(api_base_url=url,model=model,engine=engine,enabled=True,api_key=key,debounce_seconds=2)
        self.manager('PUT','/ai-review/config',cfg)
        result = self.manager('POST','/ai-review/connection-test',{})
        if not result['ok']: raise RuntimeError(result['message'])
        print('模型连接及所选模型已验证；未输出或写入明文模型密钥。')


def main():
    parser=argparse.ArgumentParser(description='管理本机 AI Review 真实测试环境')
    parser.add_argument('action',choices=['start','init','status','stop','configure-model','clear-model','repository'])
    parser.add_argument('--url');parser.add_argument('--model');parser.add_argument('--engine',default='opencode_cli',choices=['opencode_cli','new_api_http'])
    args=parser.parse_args(); harness=Harness()
    if args.action=='start':
        run(['npm','--prefix','frontend','run','build'],capture=False)
        compose('up','-d','--build',capture=False);harness.initialize()
    elif args.action=='init':harness.initialize()
    elif args.action=='status':print(compose('ps'))
    elif args.action=='stop':compose('stop',capture=False)
    else:
        harness.login()
        if args.action=='configure-model':
            if not args.url or not args.model:parser.error('需要 --url 和 --model')
            key=getpass.getpass('临时模型 API Key（隐藏输入）：')
            harness.configure_model(args.url,args.model,args.engine,key)
        elif args.action=='clear-model':
            cfg=harness.manager('GET','/ai-review/config')['config'];cfg.pop('api_key_configured',None)
            cfg.update(enabled=False,api_key='');harness.manager('PUT','/ai-review/config',cfg)
            print('已关闭本机模型调用并清除配置中的模型密钥。')
        else:
            repo=harness.create_repo();print('合成测试仓库与真实 Webhook 已就绪：'+repo['repo_name'])


if __name__=='__main__':main()
