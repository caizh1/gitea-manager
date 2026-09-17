#!/usr/bin/env python3
"""从最终包验证新增展示服务的 Manager 升级及中断回退；仅创建独立标记实例。"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
from urllib.request import build_opener, ProxyHandler

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-dir',type=Path,required=True)
    parser.add_argument('--old-image-tag',default='approval-candidate-20260913')
    args=parser.parse_args()
    package=args.package_dir.resolve()
    archive=package/'gitea-manager.tar.gz'
    expected=(package/'gitea-manager.tar.gz.sha256').read_text().split()[0]
    sha=hashlib.sha256()
    with archive.open('rb') as stream:
        for block in iter(lambda:stream.read(1048576),b''):sha.update(block)
    assert sha.hexdigest()==expected,'最终包摘要不匹配'
    ident=time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(4)
    project='gm-approval-upgrade-'+ident
    folder=ROOT/'work'/('审批升级复验-'+ident);folder.mkdir(parents=True,mode=0o700)
    docker=shutil.which('docker');password=secrets.token_urlsafe(32)
    report={'说明':'仅本次新建 Manager 实例；已有用户实例和 Gitea 不在操作范围',
            '部署包SHA256':expected,'项目':project,'结果':[]}
    report_path=folder/'升级复验.json'
    def save():report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    def run(argv,timeout=120):
        value=subprocess.run(argv,capture_output=True,text=True,timeout=timeout)
        if value.returncode:raise RuntimeError('隔离命令未完成：'+value.stderr.replace(password,'[隔离口令已隐藏]')[-900:])
        return value.stdout
    compose={'services':{
        'backend':{'image':'gitea-review-backend:'+args.old_image_tag,'platform':'linux/amd64',
            'labels':{'io.gitea-manager.upgrade-test':'true'},'ports':['127.0.0.1::5000'],
            'mem_limit':'512m','cpus':1,'environment':{'DATABASE_URL':'sqlite:////custom/state/gitea-manager.db',
                'BACKUP_DIR':'/custom/state/backups','INIT_PASSWORD':password,'SECRET_KEY':secrets.token_urlsafe(32),
                'DISABLE_BACKGROUND_TASKS':'1'},
            'volumes':['manager-data:/custom/state','/var/run/docker.sock:/var/run/docker.sock']},
        'frontend':{'image':'gitea-review-frontend:'+args.old_image_tag,'platform':'linux/amd64',
            'ports':['127.0.0.1::80'],'mem_limit':'128m','cpus':.5}},'volumes':{'manager-data':{}}}
    # 测试机可能已有许多保留现场；指定不冲突的小网段，不清理任何既有网络。
    network_ids=run([docker,'network','ls','-q']).split()
    networks=json.loads(run([docker,'network','inspect',*network_ids]))
    used=[ipaddress.ip_network(c['Subnet']) for n in networks
          for c in (n.get('IPAM',{}).get('Config') or []) if c.get('Subnet')]
    free=[str(n) for n in ipaddress.ip_network('10.253.224.0/20').subnets(new_prefix=28)
          if not any(v.version==4 and n.overlaps(v) for v in used)][:2]
    if len(free)!=2:raise RuntimeError('没有独立升级验收网段，未改动其他网络')
    compose['networks']={'default':{'ipam':{'config':[{'subnet':free[0]}]}},
        'gm-review-execution':{'internal':True,'ipam':{'config':[{'subnet':free[1]}]}}}
    report['独立测试网段']=free
    # Compose 会过滤旧服务未使用的网络；先创建独立内部网，升级器按名称发现并复用。
    run([docker,'network','create','--internal','--subnet',free[1],
         '--label','com.docker.compose.project='+project,'--label','io.gitea-manager.upgrade-test=true',
         project+'_gm-review-execution'])
    conf=folder/'隔离旧部署.json';conf.write_text(json.dumps(compose));conf.chmod(0o600)
    command=[docker,'compose','-p',project,'-f',str(conf)]
    try:
        run(command+['up','-d','--no-build','--pull','never'])
        backend=run(command+['ps','-q','backend']).strip()
        info=json.loads(run([docker,'inspect',backend]))[0]
        port=info['NetworkSettings']['Ports']['5000/tcp'][0]['HostPort']
        opener=build_opener(ProxyHandler({}))
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            try:
                with opener.open('http://127.0.0.1:'+port+'/api/session',timeout=2) as response:
                    if response.code==200:break
            except Exception:time.sleep(.3)
        else:raise RuntimeError('隔离旧 Manager 未就绪')
        seed="from pathlib import Path; import sqlite3; p=Path('/custom/state/backups'); p.mkdir(exist_ok=True); (p/'旧备份.zip').write_bytes('原有备份必须保留'.encode()); c=sqlite3.connect('/custom/state/gitea-manager.db'); c.execute(\"INSERT INTO settings(key,value) VALUES ('审批升级样本','原有数据')\"); c.commit()"
        run([docker,'exec',backend,'python','-c',seed])
        for fault in (False,True):
            env=os.environ.copy()
            if fault:
                shim=folder/'受控中断';shim.mkdir(mode=0o700)
                wrapper=shim/'docker'
                wrapper.write_text('#!/usr/bin/env python3\n# 只中断本次升级的新服务启动，随后让真实回退命令继续执行。\nimport os,sys\nargs=sys.argv[1:]\nif "compose" in args and "up" in args and any(a.endswith("新Compose.json") for a in args):\n print("隔离注入：旧服务停止后，新服务启动前中断",file=sys.stderr);sys.exit(42)\nos.execv('+repr(docker)+',["docker",*args])\n')
                wrapper.chmod(0o700);env['PATH']=str(shim)+os.pathsep+env.get('PATH','')
            label='中断后恢复已有展示服务' if fault else '新增独立展示服务的实际升级'
            began=time.time()
            result=subprocess.run(['bash',str(package/'upgrade.sh'),'--container',info['Name'].lstrip('/'),'--desktop-test'],
                env=env,capture_output=True,text=True,timeout=900)
            row={'场景':label,'退出码':result.returncode,'耗时秒':round(time.time()-began,3),
                 '输出':(result.stdout+'\n'+result.stderr).replace(password,'[隔离口令已隐藏]')}
            report['结果'].append(row);save()
            assert (result.returncode!=0)==fault,'实际升级或受控中断退出结果不符'
            ids=run([docker,'ps','-q','--filter','label=com.docker.compose.project='+project]).split()
            current=json.loads(run([docker,'inspect',*ids]))
            services={item['Config']['Labels']['com.docker.compose.service']:item for item in current}
            assert {'backend','frontend','approval-view','review-worker','review-executor','review-progress','review-feedback'}<=set(services),'升级或回退遗漏服务'
            view=services['approval-view']['Id'];new_backend=services['backend']['Id']
            run([docker,'exec',view,'python','-c',"import requests; r=requests.get('http://127.0.0.1:5000/health',timeout=3); assert r.status_code==200 and r.json()['role']=='approval-view'; assert requests.get('http://127.0.0.1:5000/api/session',timeout=3).status_code==404"])
            run([docker,'exec',new_backend,'python','-c',"from pathlib import Path; import sqlite3; c=sqlite3.connect('/custom/state/gitea-manager.db'); assert c.execute(\"SELECT value FROM settings WHERE key='审批升级样本'\").fetchone()[0]=='原有数据'; assert Path('/custom/state/backups/旧备份.zip').read_bytes()=='原有备份必须保留'.encode()"])
            assert next(m['Name'] for m in services['backend']['Mounts'] if m['Destination']=='/custom/state')==next(m['Name'] for m in info['Mounts'] if m['Destination']=='/custom/state')
            row.update(结果='通过',七个服务运行=True,展示健康=True,原持久卷与数据保持=True);save()
            print(label+'：通过',flush=True)
        report['状态']='本次最终包升级和中断回退通过';save()
    except Exception as exc:
        report.update(状态='失败，保留首次现场',首次失败=str(exc));save();raise
    finally:
        ids=run([docker,'ps','-q','--filter','label=com.docker.compose.project='+project]).split()
        for cid in ids:
            current=json.loads(run([docker,'inspect',cid]))[0]
            if current['Config']['Labels'].get('com.docker.compose.project')==project:run([docker,'stop',cid])
        print('隔离升级证据：'+str(report_path),flush=True)


if __name__=='__main__':main()
