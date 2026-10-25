#!/usr/bin/env python3
"""从最终包验证新增展示服务的 Manager 升级及中断回退；仅创建独立标记实例。"""
import argparse
import hashlib
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
from urllib.request import build_opener, ProxyHandler, HTTPCookieProcessor, Request

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-dir',type=Path,required=True)
    parser.add_argument('--old-image-tag',default='legacy-initial-20260922')
    parser.add_argument('--fault-after-verify', action='store_true')
    parser.add_argument('--runtime-gate', action='store_true', help='覆盖旧安装器的执行器状态门禁和诊断共享卷')
    parser.add_argument('--stopped-executor-image', default='', help='增加已退出执行器和原令牌，验证升级及回退不会遗漏停止服务')
    parser.add_argument('--skip-fault', action='store_true', help='仅用于缺陷复现，不执行故障注入')
    args=parser.parse_args()
    package=args.package_dir.resolve()
    archive=package/'gitea-manager.tar.gz'
    expected=(package/'gitea-manager.tar.gz.sha256').read_text().split()[0]
    sha=hashlib.sha256()
    with archive.open('rb') as stream:
        for block in iter(lambda:stream.read(1048576),b''):sha.update(block)
    assert sha.hexdigest()==expected,'最终包摘要不匹配'
    ident=time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(4)
    project='gm-legacy-upgrade-'+ident
    folder=ROOT/'work'/('legacy-upgrade-check-'+ident);folder.mkdir(parents=True,mode=0o700)
    docker=shutil.which('docker');password=secrets.token_urlsafe(32)
    report={'说明':'仅本次新建 Manager 实例；已有用户实例和 Gitea 不在操作范围',
            '部署包SHA256':expected,'项目':project,'结果':[]}
    report_path=folder/'acceptance.json'
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
    executor_token = secrets.token_urlsafe(40)
    if args.stopped_executor_image:
        token_file = folder/'executor.token'; token_file.write_text(executor_token); token_file.chmod(0o444)
        compose['services']['review-executor'] = {
            'image':args.stopped_executor_image, 'platform':'linux/amd64',
            'command':['python','-c','pass'], 'labels':{'io.gitea-manager.upgrade-test':'true'},
            'environment':{'AI_REVIEW_EXECUTOR_TOKEN_FILE':'/run/secrets/review_executor'},
            'volumes':[str(token_file)+':/run/secrets/review_executor:ro']}
    if args.runtime_gate:
        runtime_folder=folder/'runtime';runtime_folder.mkdir()
        (runtime_folder/'status.json').write_text(json.dumps({'ready':True,'version':'1.1.51','mode':'legacy'}))
        compose['services']['backend']['environment']['AI_REVIEW_RUNTIME_STATUS']='/run/manager-runtime/status.json'
        compose['services']['backend']['volumes'] += [str(runtime_folder)+':/run/manager-runtime:ro','diagnostic-data:/app/data/diagnostics']
        compose['volumes']['diagnostic-data']={}
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
    conf=folder/'legacy-compose.json';conf.write_text(json.dumps(compose));conf.chmod(0o600)
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
        # 先从原镜像真实登录取得旧 Cookie；随后验证新增认证不会接受旧共享身份。
        cookies=http.cookiejar.CookieJar()
        original_browser=build_opener(ProxyHandler({}),HTTPCookieProcessor(cookies))
        login=Request('http://127.0.0.1:'+port+'/api/login',method='POST',
            data=json.dumps({'password':password}).encode(),headers={'Content-Type':'application/json'})
        with original_browser.open(login,timeout=10) as response:
            assert response.code==200,'旧管理员登录失败'
        original_cookie='; '.join(cookie.name+'='+cookie.value for cookie in cookies)
        assert original_cookie,'未取得真实旧 Cookie'
        original_hash=run([docker,'exec',backend,'python','-c',"import sqlite3; c=sqlite3.connect('/custom/state/gitea-manager.db'); print(c.execute(\"select value from settings where key='admin_password'\").fetchone()[0])"]).strip()
        seed="from pathlib import Path; import sqlite3; p=Path('/custom/state/backups'); p.mkdir(exist_ok=True); (p/'legacy-backup.zip').write_bytes('原有备份必须保留'.encode()); c=sqlite3.connect('/custom/state/gitea-manager.db'); c.execute(\"INSERT INTO settings(key,value) VALUES ('审批升级样本','原有数据')\"); c.commit()"
        run([docker,'exec',backend,'python','-c',seed])
        seed_servers = "from app import create_app; from models import db,GiteaServer,Backup,ScheduledTask,Setting; a=create_app(); ctx=a.app_context(); ctx.push(); db.session.add(Setting(key='host_ip',value='192.0.2.1')); rows=[GiteaServer(name='历史本地',role='primary',host='192.0.2.1',ssh_user='旧账号',gitea_port=3000,gitea_url='http://192.0.2.1:3000',api_token='fixture-local',is_local=True),GiteaServer(name='历史远端',role='backup',host='192.0.2.2',ssh_user='旧账号',gitea_port=3000,gitea_url='http://192.0.2.2:3000',api_token='fixture-remote',is_local=False)]; db.session.add_all(rows); db.session.commit(); db.session.add(Backup(source_server_id=rows[0].id,filename='backup.zip',file_path='/custom/state/backups/legacy-backup.zip',status='success')); db.session.add(ScheduledTask(name='历史计划',enabled=False,source_server_id=rows[0].id,target_ids='[2]')); db.session.commit()"
        run([docker,'exec',backend,'python','-c',seed_servers])
        if args.stopped_executor_image:
            executor_id = run(command+['ps','-aq','review-executor']).strip()
            original_executor = json.loads(run([docker,'inspect',executor_id]))[0]
            assert not original_executor['State']['Running'], '未建立已退出执行器场景'
            failed_exec = subprocess.run([docker,'exec',executor_id,'python','-c','pass'], capture_output=True, text=True)
            assert failed_exec.returncode and 'is not running' in failed_exec.stderr, '未复现停止容器拒绝 exec'
            report['升级前执行器状态'] = '已退出，原 exec 路径拒绝执行'
            report['原执行器令牌摘要'] = hashlib.sha256(executor_token.encode()).hexdigest(); save()

        for fault in ((False,) if args.skip_fault else (True,False)):
            env=os.environ.copy()
            if fault:
                shim=folder/'fault-injection';shim.mkdir(mode=0o700)
                wrapper=shim/'docker'
                wrapper.write_text('#!/usr/bin/env python3\n# 只中断本次升级的新服务启动，随后让真实回退命令继续执行。\nimport os,sys\nargs=sys.argv[1:]\nif "compose" in args and "up" in args and any(a.endswith("新Compose.json") for a in args):\n print("隔离注入：旧服务停止后，新服务启动前中断",file=sys.stderr);sys.exit(42)\nos.execv('+repr(docker)+',["docker",*args])\n')
                if args.fault_after_verify:
                    wrapper.write_text('#!/usr/bin/env python3\n# 新版迁移和健康核对完成后注入故障，验证恢复原历史库。\nimport os,sys,subprocess\nargs=sys.argv[1:]\nif args[-1:]==["verify"] and any(a.endswith("upgrade_runtime.py") for a in args):\n result=subprocess.run(['+repr(docker)+',*args]);sys.exit(42 if result.returncode==0 else result.returncode)\nos.execv('+repr(docker)+',["docker",*args])\n')
                wrapper.chmod(0o700);env['PATH']=str(shim)+os.pathsep+env.get('PATH','')
            label='首次升级失败恢复历史版本' if fault else '历史版本直接升级到最新版'
            began=time.time()
            result=subprocess.run(['bash',str(package/'upgrade.sh'),'--container',info['Name'].lstrip('/'),'--desktop-test'],
                env=env,capture_output=True,text=True,timeout=900)
            row={'场景':label,'退出码':result.returncode,'耗时秒':round(time.time()-began,3),
                 '输出':(result.stdout+'\n'+result.stderr).replace(password,'[隔离口令已隐藏]')}
            report['结果'].append(row);save()
            assert (result.returncode!=0)==fault,'实际升级或受控中断退出结果不符'
            if fault:
                assert '原服务恢复并通过健康检查' in row['输出'], '故障未进入并完成回退，不得认定回退通过'
                if args.fault_after_verify:
                    assert '全部服务、独立审批展示、模型执行器和历史数据核对通过' in row['输出'], '故障未到达迁移后的验证阶段'
            ids=run([docker,'ps','-q','--filter','label=com.docker.compose.project='+project]).split()
            current=json.loads(run([docker,'inspect',*ids]))
            services={item['Config']['Labels']['com.docker.compose.service']:item for item in current}
            assert ({'backend','frontend'} if fault else {'backend','frontend','approval-view','review-worker','review-executor','review-progress','review-feedback'})==set(services),'升级或回退服务集合错误'
            if fault:
                assert services['backend']['Image']==info['Image'],'未恢复原镜像'
                run([docker,'exec',services['backend']['Id'],'python','-c',"import sqlite3; c=sqlite3.connect('/custom/state/gitea-manager.db'); assert 'connection_mode' not in {r[1] for r in c.execute('PRAGMA table_info(gitea_servers)')}; assert c.execute('SELECT is_local FROM gitea_servers ORDER BY id').fetchall()==[(1,),(0,)]; assert c.execute('SELECT count(*) FROM backups').fetchone()[0]==1"])
                if args.stopped_executor_image:
                    restored_id = run(command+['ps','-aq','review-executor']).strip()
                    restored = json.loads(run([docker,'inspect',restored_id]))[0]
                    assert not restored['State']['Running'], '回退擅自启动了原停止执行器'
                    assert restored['Image']==original_executor['Image'], '回退遗漏了原停止执行器镜像'
                    assert restored['Config']['Cmd']==original_executor['Config']['Cmd'], '回退遗漏了原停止执行器命令'
                    row['原停止执行器镜像及停止状态'] = '保持'; save()
                row['结果']='通过';save();continue
            view=services['approval-view']['Id'];new_backend=services['backend']['Id']
            migrated_hash=run([docker,'exec',new_backend,'python','-c',"import sqlite3; c=sqlite3.connect('/custom/state/gitea-manager.db'); print(c.execute(\"select password_hash from access_accounts where subject='local:admin' and source_id is null\").fetchone()[0])"]).strip()
            assert original_hash==migrated_hash,'旧密码哈希未原样迁移'
            new_port=services['backend']['NetworkSettings']['Ports']['5000/tcp'][0]['HostPort']
            new_base='http://127.0.0.1:'+new_port
            with opener.open(Request(new_base+'/api/session',headers={'Cookie':original_cookie}),timeout=10) as response:
                assert not json.load(response)['authenticated'],'升级仍接受旧共享管理员 Cookie'
            browser=build_opener(ProxyHandler({}),HTTPCookieProcessor(http.cookiejar.CookieJar()))
            with browser.open(new_base+'/api/session',timeout=10) as response:csrf=json.load(response)['csrf_token']
            login=Request(new_base+'/api/login',method='POST',data=json.dumps({'password':password}).encode(),
                headers={'Content-Type':'application/json','X-CSRF-Token':csrf})
            with browser.open(login,timeout=10) as response:
                identity=json.load(response)
                assert identity['authenticated'] and identity['user']['is_platform_admin'],'原管理员密码无法登录新版本'
            row.update(旧Cookie失效=True,旧密码哈希原样保留=True,原密码通过新会话登录=True);save()
            run([docker,'exec',view,'python','-c',"import requests; r=requests.get('http://127.0.0.1:5000/health',timeout=3); assert r.status_code==200 and r.json()['role']=='approval-view'; assert requests.get('http://127.0.0.1:5000/api/session',timeout=3).status_code==404"])
            run([docker,'exec',new_backend,'python','-c',"from pathlib import Path; import sqlite3; c=sqlite3.connect('/custom/state/gitea-manager.db'); assert c.execute(\"SELECT value FROM settings WHERE key='审批升级样本'\").fetchone()[0]=='原有数据'; assert Path('/custom/state/backups/legacy-backup.zip').read_bytes()=='原有备份必须保留'.encode()"])
            run([docker,'exec',new_backend,'python','-c',"import sqlite3; c=sqlite3.connect('/custom/state/gitea-manager.db'); assert c.execute('SELECT is_local,connection_mode FROM gitea_servers ORDER BY id').fetchall()==[(1,'local'),(0,'ssh')]; assert c.execute('SELECT count(*) FROM backups').fetchone()[0]==1; assert c.execute('SELECT count(*) FROM scheduled_tasks').fetchone()[0]==1"])
            workspace_worker=next(m['Name'] for m in services['review-worker']['Mounts'] if m['Destination']=='/app/review-workspaces')
            workspace_executor=next(m for m in services['review-executor']['Mounts'] if m['Destination']=='/app/review-workspaces')
            assert workspace_worker==workspace_executor['Name'] and not workspace_executor['RW']
            assert next(m['Name'] for m in services['backend']['Mounts'] if m['Destination']=='/custom/state')==next(m['Name'] for m in info['Mounts'] if m['Destination']=='/custom/state')
            row.update(结果='通过',七个服务运行=True,展示健康=True,原持久卷与数据保持=True);save()
            if args.stopped_executor_image:
                token_check = "from pathlib import Path; import hashlib,os; print(hashlib.sha256(Path(os.environ['AI_REVIEW_EXECUTOR_TOKEN_FILE']).read_bytes()).hexdigest())"
                expected_token = hashlib.sha256(executor_token.encode()).hexdigest()
                for name in ('review-worker','review-executor'):
                    assert run([docker,'exec',services[name]['Id'],'python','-c',token_check]).strip()==expected_token, '升级未保留原执行器令牌'
                row['原执行器令牌'] = 'Worker 与新执行器均逐字节保留'; save()
            if args.runtime_gate:
                run([docker,'exec',new_backend,'python','-c',"from ai_review.runtime_status import status; s=status(); assert s['ready'] and s['version']=='1.1.51'; from pathlib import Path; import os; assert Path(os.environ.get('MANAGEMENT_DIAGNOSTICS','/app/data/diagnostics')).joinpath('diagnostics-'+s['diagnostic_id']+'.tar.gz').is_file()"])
                executor=services['review-executor']['Id']
                run([docker,'stop',executor])
                time.sleep(16)
                run([docker,'exec',new_backend,'python','-c',"from ai_review.runtime_status import available; assert not available()"])
                run([docker,'start',executor])
                deadline=time.monotonic()+90
                while time.monotonic()<deadline:
                    result=subprocess.run([docker,'exec',new_backend,'python','-c',"from ai_review.runtime_status import available; assert available()"],capture_output=True)
                    if result.returncode==0:break
                    time.sleep(3)
                else:raise RuntimeError('执行器重启后运行时门禁未恢复')
                row['运行时门禁']='就绪、停止后拒绝、重启后恢复、诊断共享均通过';save()
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
