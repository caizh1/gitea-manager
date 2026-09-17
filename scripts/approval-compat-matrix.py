#!/usr/bin/env python3
"""真实原生兼容、评审删除、目标分支与同 SHA 身份隔离用例。"""
import importlib.util
import json
from pathlib import Path
import secrets
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('approval_matrix',ROOT/'scripts/approval-matrix.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class Compatibility(module.Matrix):
    def scenarios(self):
        self.start();self.fixtures();self.start_manager();self.create_repo();self.setup_policy()
        self.install('shadow');self.install('isolated_enforced')
        def delete_review(old=False):
            pr=self.pr('本人删除审批',('alice','bob','carol'))
            review=next(r for r in self.get(self.path+f'/pulls/{pr["number"]}/reviews') if r['user']['login']=='alice')
            if old:self.approve(pr,'alice')
            self.api('DELETE',self.path+f'/pulls/{pr["number"]}/reviews/{review["id"]}',user='alice')
            status=self.status_is(pr,old,count=3 if old else 2)
            self.merge(pr,expected=old)
            return status
        self.case('生命周期03-本人撤回','本人删除唯一批准后指定组票失效',lambda:delete_review(False))
        self.case('生命周期04-删除旧记录','删除旧审批ID不能取消已经产生的新批准',lambda:delete_review(True))
        def wildcard(higher):
            self.native({'priority':10 if higher else 1})
            self.api('POST',self.path+'/branch_protections',{'rule_name':'ma*','priority':1,'enable_push':False,
                'required_approvals':5,'block_admin_merge_override':True,'block_on_rejected_reviews':True})
            try:
                preview=self.manager('POST',self.prefix+'/preview',{})
                pr=self.pr('原生通配优先级',('alice','bob','carol'))
                status=self.check(pr)
                assert status['native_required']==(5 if higher else 3), '没有选中真实优先级对应的原生要求'
                self.merge(pr,expected=not higher)
                if higher:
                    for user in ('dave','spare'):self.approve(pr,user)
                    self.merge(pr,expected=True)
                return {'原生规则':preview['native_rules'],'实际采用要求':status['native_required']}
            finally:
                # api() 统一编码路径；传入原始星号，避免 % 再次编码导致清理 404。
                try:self.api('DELETE',self.path+'/branch_protections/ma*')
                finally:self.restore_native()
                assert len(self.get(self.path+'/branch_protections'))==1, '隔离规则清理未完成，不能继续兼容断言'
                self.manager('POST',self.prefix+'/preview',{})
        self.case('接入03-A-通配优先','优先级更高的通配五票不能被精确三票覆盖',lambda:wildcard(True))
        self.case('接入03-A-精确优先','优先级相同时精确规则优先且与实际合并一致',lambda:wildcard(False))
        def administrator():
            pr=self.pr('管理员仍需指定组票',('bob','carol','dave'))
            results=[]
            for satisfied in (False,True):
                if satisfied:self.approve(pr,'alice')
                deadline=time.monotonic()+40
                while time.monotonic()<deadline:
                    if self.get(self.path+f'/pulls/{pr["number"]}').get('mergeable'):break
                    time.sleep(.25)
                else:raise AssertionError('原生合并检查未就绪，未提交合并请求')
                before=self.execute('git','--git-dir='+self.repo_dir,'rev-parse','refs/heads/main').strip()
                code,response=self.api('POST',self.path+f'/pulls/{pr["number"]}/merge',
                    {'do':'merge','head_commit_id':pr['head']['sha'],'force_merge':True},expected=(200,403,405,409,500))
                after=self.execute('git','--git-dir='+self.repo_dir,'rev-parse','refs/heads/main').strip()
                results.append({'新增要求满足':satisfied,'HTTP':code,'旧引用':before,'新引用':after,'响应':response})
                self.report['管理员入口原始结果']=results;self.save()
                assert (before!=after)==satisfied and self.get(self.path+f'/pulls/{pr["number"]}')['merged']==satisfied, json.dumps(results[-1],ensure_ascii=False)
            return results
        self.case('入口06-管理员覆盖请求','管理员 force_merge 仍不能跳过指定组要求',administrator)
        def same_sha():
            first=self.pr('相同SHA有指定组票',('alice','bob','carol'))
            branch='same-sha-'+secrets.token_hex(4)
            self.api('POST',self.path+'/branches',{'new_branch_name':branch,'old_ref_name':first['head']['ref']},user='author')
            _,second=self.api('POST',self.path+'/pulls',{'title':'相同SHA缺指定组票','head':branch,'base':'main'},user='author')
            assert first['head']['sha']==second['head']['sha'] and first['id']!=second['id']
            for user in ('bob','carol','dave'):self.approve(second,user)
            no=self.status_is(second,False,count=3,groups=[0,1]);yes=self.status_is(first,True,count=3)
            self.merge(second,expected=False);self.merge(first,expected=True)
            return {'同一SHA':first['head']['sha'],'拒绝的PR':no,'通过的PR':yes}
        self.case('并发04-同SHA不同PR','同一代码SHA不能共用不同PR的批准结果',same_sha)
        def target_change():
            branch='unmanaged-'+secrets.token_hex(4)
            self.api('POST',self.path+'/branches',{'new_branch_name':branch,'old_ref_name':'main'},user='author')
            pr=self.pr('修改目标分支',('alice','bob','carol'))
            self.api('PATCH',self.path+f'/pulls/{pr["number"]}',{'base':branch})
            out=self.check(pr);assert out['applicable'] is False
            self.api('PATCH',self.path+f'/pulls/{pr["number"]}',{'base':'main'})
            reset=self.status_is(pr,False,count=0)
            for user in ('alice','bob','carol'):self.approve(pr,user)
            renewed=self.status_is(pr,True,count=3)
            self.merge(pr,expected=True)
            return {'非受管目标':out,'返回受管目标旧票失效':reset,'重新批准':renewed}
        self.case('兼容04-目标切换','PR改目标后重新匹配，返回受管目标需要新批准',target_change)
        # 本节主动调整过原生规则，回退保持最终实物及其合法更新时间。
        self.baseline_rules=self.get(self.path+'/branch_protections')
        self.rollback_cases()


if __name__=='__main__':
    run=Compatibility('approval-candidate-20260913')
    try:run.scenarios()
    except Exception as exc:
        run.report['首次失败'].append({'原因':str(exc).replace(run.password,'[隔离口令已隐藏]')});run.save()
        raise
    finally:run.cleanup();print('原生兼容矩阵证据：'+str(run.report_path),flush=True)
    sys.exit(1 if run.report['首次产品失败'] else 0)
