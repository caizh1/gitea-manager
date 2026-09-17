#!/usr/bin/env python3
"""短时容量校准：独立身份、真实会话与状态读取；失败即停升档，不替代 24 小时或现场测试。"""
from concurrent.futures import ThreadPoolExecutor
import argparse
import importlib.util
import json
import math
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request,build_opener,ProxyHandler

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('approval_matrix',ROOT/'scripts/approval-matrix.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class Capacity(module.Matrix):
    def __init__(self,image_tag,reference=False,max_users=1000,diagnostics=False,threads=8,packaged=False):
        super().__init__(image_tag,packaged);self.reference_profile=reference;self.max_users=max_users;self.diagnostics=diagnostics;self.threads=threads

    def compose(self,*args):
        if args and args[0]=='up' and self.reference_profile:
            config=json.loads(self.compose_path.read_text())
            config['services']['gitea'].update(cpus=4.0,mem_limit='1536m')
            config['services']['postgres'].update(cpus=2.0,mem_limit='512m')
            self.compose_path.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n')
        return super().compose(*args)

    def start_view(self):
        self.view_name=self.project+'-approval-view'
        extra=(['-v',str(ROOT/'scripts/approval-view-probe.py')+':/probe/approval_probe.py:ro','-e','PYTHONPATH=/app:/probe']
               if self.diagnostics else [])
        self.run(['docker','run','-d','--platform','linux/amd64','--name',self.view_name,
            '--label',module.module.module.LABEL+'='+self.run_id,'--network',self.project+'_default',
            '--network-alias','approval-view','--memory','512m','--cpus','2' if self.reference_profile else '1','-p','127.0.0.1::5000',
            *([] if self.packaged else ['-v',str(ROOT/'backend')+':/app:ro']),'-v',self.manager_volume+':/testdata',
            '-v','/var/run/docker.sock:/var/run/docker.sock','-e','DATABASE_URL=sqlite:////testdata/manager.db',
            *extra,'gitea-review-backend:'+self.image_tag,'gunicorn','--bind','0.0.0.0:5000','--workers','2',
            '--threads',str(self.threads),'--timeout','30',('approval_probe:create_app()' if self.diagnostics else 'approval.view_app:create_app()')])
        info=json.loads(self.run(['docker','inspect',self.view_name]))[0]
        self.view_base='http://127.0.0.1:'+info['NetworkSettings']['Ports']['5000/tcp'][0]['HostPort']
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            try:
                with build_opener(ProxyHandler({})).open(self.view_base+'/health',timeout=1) as response:
                    if json.load(response).get('ok'):break
            except Exception:time.sleep(.2)
        else:raise AssertionError('独立展示进程未就绪')

    def cleanup(self):
        if getattr(self,'view_name',None):
            try:
                info=json.loads(self.run(['docker','inspect',self.view_name]))[0]
                if info['Config']['Labels'].get(module.module.module.LABEL)==self.run_id:
                    if self.diagnostics:
                        log=self.run(['docker','logs',self.view_name])
                        (self.folder/'隔离展示逐请求计时.jsonl').write_text('\n'.join(
                            line for line in log.splitlines() if line.startswith('{"类型": "隔离展示计时"'))+'\n')
                    self.run(['docker','stop',self.view_name])
            except Exception:pass
        super().cleanup()

    def accounts(self,count):
        while len(self.accounts_data)<count:
            name='viewer'+str(len(self.accounts_data)+1)
            _,user=self.api('POST','/admin/users',{'username':name,'password':self.password,'email':name+'@example.invalid',
                'must_change_password':False,'send_notify':False})
            self.api('PUT',f'/teams/{self.reader_team}/members/{name}')
            _,jar=self.native_web_login(name)
            cookie='; '.join(c.name+'='+c.value for c in jar)
            assert cookie,'原生独立身份没有取得会话'
            self.accounts_data.append({'name':name,'id':user['id'],'cookie':cookie})
        assert len({a['id'] for a in self.accounts_data[:count]})==count

    def burst(self,count):
        self.accounts(count)
        def baseline(account):
            began=time.monotonic()
            try:
                req=Request(self.base+f'/approval-probe/approval-live/pulls/{self.pr_data["number"]}/info',headers={'Cookie':account['cookie']})
                with build_opener(ProxyHandler({})).open(req,timeout=4) as r:
                    value=json.load(r);ok=r.code==200 and value.get('convertedIssue',{}).get('number')==self.pr_data['number']
                return {'成功':ok,'耗时秒':round(time.monotonic()-began,4)}
            except Exception as exc:return {'成功':False,'耗时秒':round(time.monotonic()-began,4),'异常':type(exc).__name__}
        with ThreadPoolExecutor(max_workers=min(count,self.threads*2)) as pool:base_rows=list(pool.map(baseline,self.accounts_data[:count]))
        self.report.setdefault('原生信息接口并发对照',[]).append({'人数':count,'并发上限':min(count,self.threads*2),
            '说明':'同一原生实例、同一身份、无审批状态读取；尚不等于完整未接入混合业务基线',
            '成功数':sum(r['成功'] for r in base_rows),'逐请求':base_rows});self.save()
        assert all(r['成功'] for r in base_rows),'原生信息接口并发对照已有失败，当前资源不能用于审批容量通过结论'
        target=self.view_base+f'/api/approval-view/{self.server_id}/{self.repo["id"]}/{self.pr_data["number"]}'
        start=time.monotonic()+.5
        def fetch(account):
            while time.monotonic()<start:time.sleep(.002)
            began=time.monotonic()
            result={'用户ID':account['id'],'计划延误秒':round(began-start,4)}
            try:
                req=Request(target,headers={'Cookie':account['cookie']})
                try:r=build_opener(ProxyHandler({})).open(req,timeout=12)
                except HTTPError as exc:r=exc
                with r:
                    data=json.loads(r.read(4*1024*1024));result['HTTP']=r.code
                result['身份正确']=data.get('viewer_id')==account['id']
                result['成功']=r.code==200 and result['身份正确'] and data.get('satisfied') is True
                if not result['成功']:result['原因']=data.get('error','状态或身份不符')
            except Exception as exc:result.update(成功=False,异常=type(exc).__name__)
            result['耗时秒']=round(time.monotonic()-began,4)
            return result
        with ThreadPoolExecutor(max_workers=count) as pool:rows=list(pool.map(fetch,self.accounts_data[:count]))
        times=sorted(r['耗时秒'] for r in rows)
        result={'独立会话数':count,'实际请求数':len(rows),'完成成功数':sum(r['成功'] for r in rows),
                'P95秒':times[math.ceil(len(times)*.95)-1],'最大秒':max(times),'逐请求':rows}
        self.report.setdefault('容量原始结果',[]).append(result);self.save()
        assert all(r['成功'] for r in rows),'独立页面突发有失败，停止升档：'+json.dumps({k:v for k,v in result.items() if k!='逐请求'},ensure_ascii=False)
        return result

    def scenarios(self):
        self.start();self.fixtures();self.start_manager();self.create_repo();self.setup_policy()
        self.install('shadow')
        self.start_view()
        _,team=self.api('POST','/orgs/approval-probe/teams',{'name':'CapacityReaders','permission':'read',
            'includes_all_repositories':True,'units_map':{'repo.code':'read','repo.pulls':'read'}})
        self.reader_team=team['id'];self.accounts_data=[]
        self.pr_data=self.pr('页面容量热点',('alice','bob','carol'))
        self.report['容量边界']={'类型':'短时热点页面突发，真实独立登录身份',
            '配额':('主 Manager 1 CPU/512 MiB，展示 2 CPU/512 MiB；Gitea 4 CPU/1536 MiB，PostgreSQL 2 CPU/512 MiB' if self.reference_profile else
                    '主 Manager 与独立展示各 1 CPU/512 MiB；Gitea 1 CPU/768 MiB，PostgreSQL 0.5 CPU/256 MiB'),
            '展示工作进程':f'2 进程×{self.threads}线程；只调整本次新建隔离实例',
            '限制':'本机与其他开发实例共享资源；不等于现场硬件，也不等于完整混合流量或 24 小时结果',
            '保护':'只从 1 页面起逐档校准，出现失败停止升档；不调整用户既有容器资源'}
        self.save()
        for count in (1,50,200,500,1000):
            if count>self.max_users:break
            row=self.case('容量校准-热点突发-'+str(count),'独立登录身份同时读取审批页面',lambda count=count:self.burst(count))
            if row['结果']!='通过':
                self.report['未执行升档']=[n for n in (1,50,200,500,1000) if n>count];self.save();break
        self.record('容量校准没有重启原生服务',self.verify_owned()['State']['StartedAt']==self.report['实例']['初始启动时间'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference',action='store_true',help='新建隔离实例采用明确的较高配额，不修改现有容器')
    parser.add_argument('--max-users',type=int,choices=(50,200,500,1000),default=1000)
    parser.add_argument('--diagnostics',action='store_true',help='仅在新建展示实例加载无凭据计时探针')
    parser.add_argument('--threads',type=int,choices=(8,16,32),default=8,help='只调整本次隔离展示进程的有界工作线程')
    args=parser.parse_args()
    run=Capacity('approval-candidate-20260913',args.reference,args.max_users,args.diagnostics,args.threads)
    try:run.scenarios()
    except Exception as exc:
        run.report['首次失败'].append({'原因':str(exc).replace(run.password,'[隔离口令已隐藏]')});run.save();raise
    finally:
        run.cleanup()
        run.report.update(状态='分项执行结束，完整矩阵仍待验收',结束时间=time.time());run.save()
        print('短时容量校准证据：'+str(run.report_path),flush=True)
    sys.exit(1 if run.report['首次产品失败'] else 0)
