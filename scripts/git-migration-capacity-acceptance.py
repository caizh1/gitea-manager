#!/usr/bin/env python3
"""使用真实受限文件系统验证接收端容量探测与运行时低水位阻断。"""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
from types import SimpleNamespace

import requests

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from git_migration.capacity import GIB,Monitor,verify
from svn_migration.common import MigrationError


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image',required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    report={'说明':'仅独立临时文件系统，实际写入探测；不填充宿主磁盘','断言':[]}
    def docker(*v):return subprocess.check_output(['docker',*v],text=True).strip()
    def check(value,description):
        assert value,description
        report['断言'].append(description)
        (a.output/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    wanted={'manager_required':4*GIB,'target_required':4*GIB,'inode_required':10000}
    for size,inodes in (('128m',20000),('8g',20000)):
        name='gm-migration-capacity-'+secrets.token_hex(6);password=secrets.token_urlsafe(24)
        docker('run','-d','--platform','linux/amd64','--name',name,'--label','io.gitea-manager.test=git-migration-capacity',
               '--memory','768m','--cpus','2','--tmpfs','/data:rw,size='+size+',nr_inodes='+str(inodes),
               '-p','127.0.0.1::3000','-e','GITEA__database__DB_TYPE=sqlite3','-e','GITEA__security__INSTALL_LOCK=true',
               '-e','GITEA__service__DISABLE_REGISTRATION=true',a.image)
        try:
            base='http://127.0.0.1:'+docker('port',name,'3000/tcp').rsplit(':',1)[1]
            client=requests.Session();client.trust_env=False
            for _ in range(120):
                try:
                    if client.get(base+'/api/v1/version',timeout=2).ok:break
                except requests.RequestException:pass
                time.sleep(1)
            else:raise RuntimeError('容量夹具未启动')
            docker('exec','--user','git',name,'gitea','admin','user','create','--admin','--username','capacity-admin',
                   '--password',password,'--email','capacity@example.test','--must-change-password=false')
            response=client.post(base+'/api/v1/users/capacity-admin/tokens',auth=('capacity-admin',password),json={'name':'容量竞争验收','scopes':['all']},timeout=30)
            response.raise_for_status();client.headers['Authorization']='token '+response.json()['sha1']
            def api(method,path):
                r=client.request(method,base+'/api/v1'+path,timeout=15)
                r.raise_for_status();return r.json()
            receiver=SimpleNamespace(api=api)
            if size=='128m':
                observed=api('GET','/governance/migration-capacity')
                check(observed['database']=='writable' and all(v['free_bytes']<128*1024**2 for v in observed['storage'].values()),'真实小容量磁盘被正确识别，数据库写探测通过')
                try:verify(receiver,a.output,wanted)
                except MigrationError as error:check(error.code=='capacity','容量不足明确阻断预迁移')
                else:raise AssertionError('小磁盘未阻断')
            else:
                evidence=[];monitor=Monitor(receiver,a.output,wanted,lambda:None,evidence.append)
                monitor.tick();check(len(evidence)==1,'运行开始时真实容量和 inode 充足')
                locked=subprocess.Popen(['docker','exec','-i','--user','git',name,'sqlite3','/data/gitea/gitea.db'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
                try:
                    locked.stdin.write('BEGIN IMMEDIATE;\n.shell touch /tmp/capacity-lock-held\n');locked.stdin.flush()
                    for _ in range(50):
                        held=subprocess.run(['docker','exec',name,'test','-f','/tmp/capacity-lock-held'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                        if held.returncode==0:break
                        time.sleep(.1)
                    else:raise AssertionError('未能取得真实数据库写锁')
                    started=time.monotonic();value=api('GET','/governance/migration-capacity?live=true')
                    check(value['database']=='readable' and time.monotonic()-started<5,'数据库写锁持有期间，令牌认证与持续容量探测不争抢写锁')
                finally:
                    locked.stdin.write('ROLLBACK;\n.quit\n');locked.stdin.flush();locked.communicate(timeout=10)
                docker('exec','--user','git',name,'sh','-c','mkdir -p /data/capacity-pressure; i=0; while [ "$i" -lt 11000 ]; do : > /data/capacity-pressure/$i; i=$((i+1)); done')
                monitor.last=0
                try:monitor.tick()
                except MigrationError as error:check(error.code=='capacity','运行中真实 inode 被占用后，持续监测立即阻断')
                else:raise AssertionError('运行中 inode 下降未阻断')
        finally:
            docker('rm','-f',name)
    report['结果']='通过';(a.output/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('真实容量与运行监测验收通过：'+str(a.output/'acceptance.json'))


if __name__=='__main__':main()
