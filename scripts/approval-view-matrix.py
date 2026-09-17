#!/usr/bin/env python3
"""真实原生登录、独立展示进程及最终 nginx 配置的权限与失效矩阵；仅操作本次隔离实例。"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request,build_opener,ProxyHandler

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('capacity',ROOT/'scripts/approval-capacity-probe.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class Views(module.Capacity):
    def start_frontend(self):
        self.front_name=self.project+'-frontend'
        self.run(['docker','run','-d','--name',self.front_name,'--platform','linux/amd64',
            '--label',module.module.module.module.LABEL+'='+self.run_id,'--network',self.project+'_default',
            '--memory','128m','--cpus','0.5','-p','127.0.0.1::80',
            *([] if self.packaged else ['-v',str(ROOT/'frontend/nginx.conf')+':/etc/nginx/conf.d/default.conf:ro']),
            'gitea-review-frontend:'+self.image_tag])
        info=json.loads(self.run(['docker','inspect',self.front_name]))[0]
        self.front_base='http://127.0.0.1:'+info['NetworkSettings']['Ports']['80/tcp'][0]['HostPort']
        self.endpoint=self.front_base+f'/api/approval-view/{self.server_id}/{self.repo["id"]}/{self.pr_data["number"]}'
        self.run(['docker','exec',self.front_name,'nginx','-t'])
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            try:
                # 容器创建和 nginx -t 均不代表监听已就绪；权限入口返回401后才开始业务断言。
                if self.fetch()['HTTP']==401:break
            except Exception:pass
            time.sleep(.2)
        else:raise AssertionError('最终 nginx 配置的展示代理尚未就绪，未开始业务矩阵')

    def cleanup(self):
        if getattr(self,'front_name',None):
            try:
                info=json.loads(self.run(['docker','inspect',self.front_name]))[0]
                if info['Config']['Labels'].get(module.module.module.module.LABEL)==self.run_id:
                    self.run(['docker','stop',self.front_name])
            except Exception:pass
        super().cleanup()

    def fetch(self,account=None,headers=None,method='GET',target=None):
        combined={'Accept':'application/json',**({'Cookie':account['cookie']} if account else {}),**(headers or {})}
        try:r=build_opener(ProxyHandler({})).open(Request(target or self.endpoint,headers=combined,method=method),timeout=12)
        except HTTPError as exc:r=exc
        with r:
            raw=r.read(200000);code=r.code;cache=r.headers.get('Cache-Control')
        try:value=json.loads(raw)
        except ValueError:value={'说明':'非JSON错误响应'}
        for secret in (*self.secret_tokens,self.password):
            assert secret.encode() not in raw,'页面响应暴露了服务凭据'
        return {'HTTP':code,'缓存控制':cache,'内容':value}

    def allow(self,account):
        result=self.fetch(account)
        assert result['HTTP']==200 and result['内容'].get('viewer_id')==account['id'],json.dumps(result,ensure_ascii=False)
        assert result['缓存控制']=='no-store'
        return result

    def deny(self,account=None,headers=None,method='GET',target=None):
        result=self.fetch(account,headers,method,target)
        assert result['HTTP'] in (401,403,404,405,409,502,503,504),json.dumps(result,ensure_ascii=False)
        assert not result['内容'].get('satisfied') and 'groups' not in result['内容'],'错误响应泄露审批名单或通过结论'
        return result

    def scenarios(self):
        self.start();self.fixtures();self.start_manager();self.create_repo();self.setup_policy();self.install('shadow')
        self.start_view()
        _,team=self.api('POST','/orgs/approval-probe/teams',{'name':'PrivateReaders','permission':'read',
            'includes_all_repositories':True,'units_map':{'repo.code':'read','repo.pulls':'read'}})
        self.reader_team=team['id'];self.accounts_data=[];self.accounts(4)
        self.pr_data=self.pr('展示身份与权限固定样本',('alice','bob','carol'))
        self.start_frontend()
        def identities():
            first=self.allow(self.accounts_data[0]);second=self.allow(self.accounts_data[1])
            assert first['内容']['satisfied'] and second['内容']['satisfied']
            assert first['内容']['viewer_id']!=second['内容']['viewer_id']
            return {'首次':first,'换账户':second}
        self.case('页面03-独立身份','同一私有PR复用状态但不复用浏览器身份',identities)
        self.case('安全02-无会话','无Cookie不能读取私有审批规则',lambda:self.deny())
        self.case('安全02-伪造身份头','伪造身份头不能代替原生会话',lambda:self.deny(headers={
            'X-Forwarded-User':'probe-admin','X-Approval-Viewer':'1','X-Remote-User':'probe-admin'}))
        manager_cookie='; '.join(c.name+'='+c.value for handler in self.manager_http.handlers
                                  if hasattr(handler,'cookiejar') for c in handler.cookiejar)
        self.case('安全02-Manager会话','Manager管理员会话不能读取原生私有PR',lambda:self.deny(headers={'Cookie':manager_cookie}))
        self.case('安全02-跨站来源','跨站来源不能读取审批规则',lambda:self.deny(self.accounts_data[0],{'Origin':'https://untrusted.invalid'}))
        self.case('安全02-错误仓库','仓库稳定ID不匹配不能串用状态',lambda:self.deny(self.accounts_data[0],
            target=self.front_base+f'/api/approval-view/{self.server_id}/999999/{self.pr_data["number"]}'))
        self.case('安全02-写方法','同源展示代理不能调用写接口',lambda:self.deny(self.accounts_data[0],method='POST'))
        def revoke():
            user=self.accounts_data[0];before=self.allow(user)
            self.api('DELETE',f'/teams/{self.reader_team}/members/{user["name"]}')
            denied=self.deny(user)
            self.api('PUT',f'/teams/{self.reader_team}/members/{user["name"]}')
            after=self.allow(user)
            return {'撤权前':before,'撤权后':denied,'恢复后':after}
        self.case('安全02-权限撤销','已预热展示缓存也必须阻止撤权用户',revoke)
        def suspended(field):
            user=self.accounts_data[1];self.allow(user)
            self.api('PATCH','/admin/users/'+user['name'],{'source_id':0,'login_name':user['name'],field:field=='prohibit_login'})
            try:return self.deny(user)
            finally:self.api('PATCH','/admin/users/'+user['name'],{'source_id':0,'login_name':user['name'],field:field!='prohibit_login'})
        for field in ('prohibit_login','active'):
            self.case('页面03-账户停用-'+field,'原生账户'+field+'变化立即拒绝已有Cookie',lambda field=field:suspended(field))
        def logout():
            user=self.accounts_data[2];self.allow(user)
            req=Request(self.base+'/user/logout',method='GET',headers={
                'Cookie':user['cookie'],'Origin':self.base,'Sec-Fetch-Site':'same-origin'})
            with build_opener(ProxyHandler({})).open(req,timeout=8) as r:r.read()
            return self.deny(user)
        self.case('页面03-退出登录','原生退出后旧Cookie不能读取已有缓存',logout)
        def changed_code():
            user=self.accounts_data[3];before=self.allow(user)
            self.api('POST',self.path+'/contents/展示代码更新.txt',{'branch':self.pr_data['head']['ref'],
                'message':'测试：展示代码失效','content':'5paw5Luj56CB'},user='author')
            # 两秒是明确声明的展示复用上限；时间到后必须重新核验，不能续期旧通过。
            time.sleep(2.05)
            after=self.allow(user)
            current=self.get(self.path+f'/pulls/{self.pr_data["number"]}')
            assert after['内容']['head']==current['head']['sha']!=before['内容']['head']
            assert not after['内容']['satisfied'] and after['内容']['total']['count']==0
            return {'更新前':before,'更新后':after}
        self.case('页面01-新代码','代码更新后的展示不能续用旧批准',changed_code)
        def closed():
            self.api('PATCH',self.path+f'/pulls/{self.pr_data["number"]}',{'state':'closed'})
            value=self.allow(self.accounts_data[3]);assert value['内容']['closed'] and not value['内容']['satisfied']
            return value
        self.case('页面01-关闭PR','关闭PR的展示不能继续显示新增要求满足',closed)
        def outage():
            self.run(['docker','stop',self.view_name])
            try:
                result=self.deny(self.accounts_data[3])
                # 独立展示停机不阻断主 Manager 和原生浏览页面。
                assert self.manager('GET','/api/session')['authenticated']
                with build_opener(ProxyHandler({})).open(self.base+'/user/login',timeout=3) as r:assert r.code==200
                return result
            finally:self.run(['docker','start',self.view_name])
        self.case('页面04-展示停机','真实nginx代理失联时只显示不可用，主服务正常',outage)
        def missing_marker():
            self.installer.uninstall(True)
            return self.deny(self.accounts_data[3])
        self.case('接入19-身份组件移除','原生身份标记不存在时不得显示通过',missing_marker)
        self.record('展示矩阵没有改原生规则或重启',self.get(self.path+'/branch_protections')==self.baseline_rules and
                    self.verify_owned()['State']['StartedAt']==self.report['实例']['初始启动时间'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-tag',default='offline')
    parser.add_argument('--packaged',action='store_true',help='完全使用交付镜像内的后端、前端和代理配置')
    args=parser.parse_args()
    run=Views(args.image_tag,reference=True,packaged=args.packaged)
    try:run.scenarios()
    except Exception as exc:
        run.report['首次失败'].append({'原因':str(exc).replace(run.password,'[隔离口令已隐藏]')});run.save();raise
    finally:
        run.cleanup();run.report.update(状态='展示分项结束，完整矩阵仍待验收',结束时间=time.time());run.save()
        print('真实展示矩阵证据：'+str(run.report_path),flush=True)
    sys.exit(1 if run.report['首次产品失败'] else 0)
