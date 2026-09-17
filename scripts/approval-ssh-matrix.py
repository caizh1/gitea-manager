#!/usr/bin/env python3
"""验证远程只读审批的 SSH 协议、实际 Docker 命令与故障拒绝；不代表目标宿主机验收。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('approval_matrix',ROOT/'scripts/approval-matrix.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class SSHMatrix(module.Matrix):
    def remote_cases(self):
        self.start();self.fixtures();self.start_manager();self.create_repo();self.setup_policy()
        self.install('shadow');self.install('isolated_enforced')
        folder=self.folder/'SSH协议夹具';folder.mkdir(mode=0o700)
        postgres=self.compose('ps','-q','postgres').strip()
        (folder/'允许容器.json').write_text(json.dumps([self.container,postgres],ensure_ascii=False))
        def control(value):
            p=folder/'故障控制.json';temp=p.with_suffix('.准备中')
            temp.write_text(json.dumps({'模式':value},ensure_ascii=False));temp.replace(p)
        control('正常')
        environment=os.environ.copy()
        environment['PYTHONPATH']=str(ROOT/'work/审批SSH测试依赖')
        interpreter=os.environ.get('GM_MATRIX_SSH_PYTHON',sys.executable)
        log=(folder/'夹具诊断.txt').open('w')
        process=subprocess.Popen([interpreter,str(ROOT/'scripts/approval-ssh-fixture.py'),str(folder)],
                                 env=environment,stdout=log,stderr=log)
        def connection(remote=False,port=22):
            payload={'remote':remote,'port':port}
            self.run(['docker','exec','-i',self.manager_name,'python','-c',
                'from app import create_app;from models import db,GiteaServer;import sys,json\nd=json.load(sys.stdin);app=create_app()\n'
                'with app.app_context():\n s=db.session.get(GiteaServer,1);s.is_local=not d["remote"];s.host="host.docker.internal" if d["remote"] else "localhost";s.ssh_port=d["port"];s.ssh_user="approval-matrix" if d["remote"] else "root";db.session.commit()'],
                input=json.dumps(payload))
        try:
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                if (folder/'服务就绪.json').exists():break
                if process.poll() is not None:raise RuntimeError('SSH 协议夹具未启动；见隔离诊断文件')
                time.sleep(.1)
            else:raise RuntimeError('SSH 协议夹具未就绪')
            port=json.loads((folder/'服务就绪.json').read_text())['端口']
            self.run(['docker','exec',self.manager_name,'mkdir','-p','/root/.ssh'])
            self.run(['docker','cp',str(folder/'客户端测试密钥'),self.manager_name+':/root/.ssh/id_rsa'])
            self.run(['docker','exec',self.manager_name,'chmod','600','/root/.ssh/id_rsa'])
            connection(True,port)
            def normal():
                pr=self.pr('远程只读连接正常',('alice','bob','carol'))
                status=self.status_is(pr,True,count=3)
                self.merge(pr,expected=True)
                return status
            row=self.case('远程01-真实SSH','远程只读核验经过SSH握手、实际容器读取及真实合并',normal)
            if row['结果']!='通过':return
            for fault in ('拒绝认证','丢弃连接','持续无输出','过量输出'):
                def reject(fault=fault):
                    pr=self.pr('远程故障-'+fault,('alice','bob','carol'))
                    control(fault)
                    try:
                        self.merge(pr,expected=False)
                        return {'PR':pr['number'],'实际目标引用':'保持','故障':fault}
                    finally:control('正常')
                self.case('远程02-'+fault,'远程元数据失败不能放行新PR',reject)
            def grandfather():
                control('持续无输出')
                try:
                    self.merge(self.legacy,expected=True)
                    return {'PR':self.legacy['number'],'说明':'受信静态存量清单不调用远程元数据服务'}
                finally:control('正常')
            self.case('接入15-远程故障存量','SSH故障时存量PR不增加远程依赖',grandfather)
        finally:
            connection(False)
            process.terminate();process.wait(timeout=5);log.close()
        self.rollback_cases()


if __name__=='__main__':
    run=SSHMatrix('approval-candidate-20260913')
    try:run.remote_cases()
    except Exception as exc:
        run.report['首次失败'].append({'原因':str(exc).replace(run.password,'[隔离口令已隐藏]')});run.save()
        raise
    finally:run.cleanup();print('SSH 矩阵证据：'+str(run.report_path),flush=True)
    sys.exit(1 if run.report['首次产品失败'] else 0)
