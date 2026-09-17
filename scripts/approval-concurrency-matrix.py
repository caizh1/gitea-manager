#!/usr/bin/env python3
"""在实际 Manager 读取中设置同步点，以真实 Git 写入判定并发结果。"""
import base64
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('approval_matrix',ROOT/'scripts/approval-matrix.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class Concurrency(module.Matrix):
    def set_control(self,path,data):
        self.run(['docker','exec','-i',self.manager_name,'python','-c',
            'from pathlib import Path;import sys;p=Path(sys.argv[1]);p.parent.mkdir(exist_ok=True);p.write_text(sys.stdin.read())',
            '/tmp/审批读取同步点/'+path],input=data)

    def change_url(self,url):
        self.run(['docker','exec','-i',self.manager_name,'python','-c',
            'from app import create_app;from models import db,GiteaServer;import sys\napp=create_app()\n'
            'with app.app_context():\n s=db.session.get(GiteaServer,1);s.gitea_url=sys.stdin.read();db.session.commit()'],input=url)

    def wait_point(self,key):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            r=subprocess.run(['docker','exec',self.manager_name,'test','-f','/tmp/审批读取同步点/'+key+'.到达'],capture_output=True)
            if r.returncode==0:return
            time.sleep(.03)
        raise AssertionError('指定读取同步点未到达，不能当作并发用例通过')

    def race(self,kind):
        pr=self.pr('读取中变化-'+kind,('alice','bob','carol','dave'))
        key=secrets.token_hex(5)
        # 团队读取发生在审批和时间线快照完成之后，但在最终状态返回之前。
        self.set_control('配置.json',json.dumps({'标识':key,'路径':'/api/v1'+self.path+'/teams','第几次':1},ensure_ascii=False))
        before=self.execute('git','--git-dir='+self.repo_dir,'rev-parse','refs/heads/main').strip()
        with ThreadPoolExecutor(max_workers=1) as pool:
            task=pool.submit(self.api,'POST',self.path+f'/pulls/{pr["number"]}/merge',
                {'do':'merge','head_commit_id':pr['head']['sha']},'author',(200,403,405,409,500))
            try:
                self.wait_point(key)
                if kind=='撤回':
                    self.dismiss(pr,'alice')
                    reviews=self.get(self.path+f'/pulls/{pr["number"]}/reviews')
                    assert any(r['user']['login']=='alice' and r['dismissed'] for r in reviews)
                elif kind=='代码':
                    branch=pr['head']['ref']
                    self.api('POST',self.path+'/contents/并发新增.txt',{'branch':branch,'message':'测试：读取期间更新代码',
                        'content':base64.b64encode('未获批准的新代码\n'.encode()).decode()},user='author')
                    assert self.get(self.path+f'/pulls/{pr["number"]}')['head']['sha']!=pr['head']['sha']
                elif kind=='成员':
                    self.api('DELETE',f'/teams/{self.teams["Committer1"]}/members/alice')
                    assert all(u['login']!='alice' for u in self.get(f'/teams/{self.teams["Committer1"]}/members'))
                unchanged=self.execute('git','--git-dir='+self.repo_dir,'rev-parse','refs/heads/main').strip()==before
                assert unchanged,'同步点没有位于目标引用写入之前'
            finally:self.set_control(key+'.继续','继续')
            code,response=task.result(timeout=15)
        after=self.execute('git','--git-dir='+self.repo_dir,'rev-parse','refs/heads/main').strip()
        merged=self.get(self.path+f'/pulls/{pr["number"]}')['merged']
        evidence={'PR':pr['number'],'变化':kind,'同步阶段':'时间线完整性核验后、最终审批结果返回前',
                  'HTTP':code,'响应':response,'旧引用':before,'新引用':after,'已合并':merged}
        self.report.setdefault('并发原始结果',[]).append(evidence);self.save()
        self.set_control('配置.json','{}')
        if kind=='成员':self.api('PUT',f'/teams/{self.teams["Committer1"]}/members/alice')
        assert before==after and not merged,'读取期间条件失效后仍写入：'+json.dumps(evidence,ensure_ascii=False)
        return evidence

    def scenarios(self):
        self.start();self.fixtures();self.start_manager();self.create_repo();self.setup_policy()
        self.install('shadow');self.install('isolated_enforced')
        self.set_control('配置.json','{}')
        self.run(['docker','cp',str(ROOT/'scripts/approval-pause-proxy.py'),self.manager_name+':/tmp/审批读取代理.py'])
        self.run(['docker','exec','-d',self.manager_name,'python','/tmp/审批读取代理.py'])
        self.change_url('http://127.0.0.1:18765')
        try:
            for kind in ('撤回','代码','成员'):
                self.case('并发读取-'+kind,'最终结果返回前原生'+kind+'变化必须阻断',lambda kind=kind:self.race(kind))
        finally:
            self.change_url('http://gitea:3000')
        self.rollback_cases()


if __name__=='__main__':
    run=Concurrency('approval-candidate-20260913')
    try:run.scenarios()
    except Exception as exc:
        run.report['首次失败'].append({'原因':str(exc).replace(run.password,'[隔离口令已隐藏]')});run.save()
        raise
    finally:run.cleanup();print('实际读取并发证据：'+str(run.report_path),flush=True)
    sys.exit(1 if run.report['首次产品失败'] else 0)
