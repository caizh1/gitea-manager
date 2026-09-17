#!/usr/bin/env python3
"""确定性接入边界反证：原生PR在预检或安装准备期间创建；保留首次失败，不改变原断言。"""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import secrets
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('concurrency',ROOT/'scripts/approval-concurrency-matrix.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class Onboarding(module.Concurrency):
    def scenarios(self):
        self.start();self.fixtures();self.start_manager();self.create_repo();self.setup_policy();self.install('shadow')
        self.set_control('配置.json','{}')
        self.run(['docker','cp',str(ROOT/'scripts/approval-pause-proxy.py'),self.manager_name+':/tmp/审批读取代理.py'])
        self.run(['docker','exec','-d',self.manager_name,'python','/tmp/审批读取代理.py'])
        self.change_url('http://127.0.0.1:18765')
        def preview_creation():
            key=secrets.token_hex(5)
            self.set_control('配置.json',json.dumps({'标识':key,'路径':'/api/v1'+self.path+'/pulls','第几次':1},ensure_ascii=False))
            with ThreadPoolExecutor(max_workers=1) as pool:
                task=pool.submit(self.manager,'POST',self.prefix+'/preview',{},False,(200,409))
                try:
                    self.wait_point(key)
                    created=self.pr('预检两次读取之间创建',())
                finally:self.set_control(key+'.继续','继续')
                result=task.result(timeout=15)
            self.set_control('配置.json','{}')
            assert 'PR 创建边界变化' in result.get('error',''),json.dumps(result,ensure_ascii=False)
            assert self.manager('GET','/api/approval-rules/policies')['items'][0]['mode']=='shadow'
            return {'PR':created['number'],'拒绝结果':result,'模式':'shadow'}
        try:self.case('接入13-B-预检读取中创建','第一次PR列表读取后创建PR，预检必须拒绝不完整边界',preview_creation)
        finally:self.change_url('http://gitea:3000')
        def after_bundle():
            original=self.manager;captured={}
            def intercept(method,path,*args,**kwargs):
                result=original(method,path,*args,**kwargs)
                if method=='POST' and path==self.prefix+'/bundle' and not captured:
                    captured['导出后时间']=time.time()
                    captured['PR']=self.pr('导出完成但尚未安装时创建',('bob','carol','dave'))
                    captured['创建完成时间']=time.time()
                return result
            self.manager=intercept
            try:
                try:self.install('isolated_enforced')
                except Exception as exc:captured['安装拒绝']=type(exc).__name__+': '+str(exc)
            finally:self.manager=original
            assert 'PR' in captured,'没有实际到达导出后、安装前同步点'
            policy=self.manager('GET','/api/approval-rules/policies')['items'][0]
            state=self.check(captured['PR'])
            evidence={'导出后时间':captured['导出后时间'],'创建完成时间':captured['创建完成时间'],
                '检查时间':time.time(),'PR':captured['PR']['number'],'模式':policy['mode'],'存量豁免':state['grandfathered'],
                '新增条件':state['satisfied'],'安装拒绝':captured.get('安装拒绝')}
            self.report['切换边界原始证据']=evidence;self.save()
            # 实例一直存在业务；无法确认边界时必须拒绝完成启用，不能将其错误纳入新规则。
            assert policy['mode']!='isolated_enforced' or state['grandfathered'],json.dumps(evidence,ensure_ascii=False)
            return evidence
        self.case('接入13-B-安装准备中创建','导出后且正式激活前创建PR，必须保持存量或拒绝启用',after_bundle)
        self.record('接入边界测试未改原生规则或重启',self.get(self.path+'/branch_protections')==self.baseline_rules and
                    self.verify_owned()['State']['StartedAt']==self.report['实例']['初始启动时间'])


if __name__=='__main__':
    run=Onboarding('approval-candidate-20260913')
    try:run.scenarios()
    except Exception as exc:
        run.report['首次失败'].append({'原因':str(exc).replace(run.password,'[隔离口令已隐藏]')});run.save();raise
    finally:
        run.cleanup();run.report.update(状态='接入并发分项结束，完整矩阵仍待验收',结束时间=time.time());run.save()
        print('接入并发边界证据：'+str(run.report_path),flush=True)
    sys.exit(1 if run.report['首次产品失败'] else 0)
