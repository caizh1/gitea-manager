"""启动三个隔离 SVN、增强版 Gitea 与 Manager，运行内容迁移验收。"""
import argparse
import json
import ipaddress
from pathlib import Path
import secrets
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--image', default='gitea-review-svn:svn-20261022')
parser.add_argument('--gitea-image', default='gitea-enhanced:enhanced-20261020-r9')
parser.add_argument('--prefix', default='gm-svn-' + str(int(time.time())))
parser.add_argument('--reuse', action='store_true')
args=parser.parse_args()
folder=ROOT/'work'/'svn-acceptance'/args.prefix
folder.mkdir(parents=True,exist_ok=True)


def docker(*argv, data=None, timeout=120):
    result=subprocess.run(['docker',*argv],input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    if result.returncode:
        raise RuntimeError('隔离验收 Docker 命令失败：'+str(argv[:3])+'；'+result.stderr.decode(errors='replace')[-1200:])
    return result.stdout.decode().strip()


state_path=folder/'resources.json'
if not args.reuse:
    state={'说明':'仅为本次隔离验收创建的资源和随机凭据，请勿作为业务配置','prefix':args.prefix,
           'manager_password':secrets.token_urlsafe(24),'gitea_password':secrets.token_urlsafe(24),
           'svn_password':secrets.token_urlsafe(24),'secret':secrets.token_urlsafe(48),'image':args.image}
    state_path.write_text(json.dumps(state,ensure_ascii=False,indent=2));state_path.chmod(0o600)
    networks=json.loads(docker('network','inspect',*docker('network','ls','-q').split()))
    used=[ipaddress.ip_network(row['Subnet']) for network in networks for row in (network.get('IPAM',{}).get('Config') or []) if row.get('Subnet')]
    subnet=next(str(net) for net in ipaddress.ip_network('10.248.0.0/16').subnets(new_prefix=24)
                if not any(net.version==other.version and net.overlaps(other) for other in used))
    docker('network','create','--subnet',subnet,'--label','io.gitea-manager.test=svn',args.prefix)
    for name in ('manager','gitea','source-a','source-b','source-c'):
        docker('volume','create','--label','io.gitea-manager.test=svn',args.prefix+'-'+name)
    docker('run','-d','--platform','linux/amd64','--name',args.prefix+'-gitea','--network',args.prefix,'--network-alias','gitea',
           '--label','io.gitea-manager.test=svn','-p','127.0.0.1::3000','-v',args.prefix+'-gitea:/data',
           '-e','GITEA__database__DB_TYPE=sqlite3','-e','GITEA__security__INSTALL_LOCK=true',
           '-e','GITEA__server__ROOT_URL=http://gitea:3000/','-e','GITEA__service__DISABLE_REGISTRATION=true',args.gitea_image)
    for label in ('a','b','c'):
        name=args.prefix+'-source-'+label
        docker('run','-d','--platform','linux/amd64','--name',name,'--network',args.prefix,'--network-alias','source-'+label,
               '--label','io.gitea-manager.test=svn','-e','SVN_TEST_PASSWORD='+state['svn_password'],'-e','SVN_TEST_SOURCE='+label,
               '-v',args.prefix+'-source-'+label+':/svn','--entrypoint','sh',args.image,'-c','sleep infinity')
        docker('exec','-i',name,'python','-',data=(ROOT/'scripts/svn-migration-fixture.py').read_bytes(),timeout=300)
        docker('exec','-d',name,'svnserve','--daemon','--foreground','--root','/svn')
    for attempt in range(60):
        probe=subprocess.run(['docker','exec',args.prefix+'-gitea','sh','-c','test -f /data/gitea/conf/app.ini'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if probe.returncode==0:break
        time.sleep(1)
    # 隔离账号的随机密码经标准输入传入，不进入主机命令行或测试报告。
    docker('exec','-i','-u','git',args.prefix+'-gitea','sh',data=('gitea --config /data/gitea/conf/app.ini admin user create --username migration-admin --password '+state['gitea_password']+' --email migration@example.test --admin --must-change-password=false\n').encode())
    token=docker('exec','-u','git',args.prefix+'-gitea','gitea','--config','/data/gitea/conf/app.ini','admin','user','generate-access-token','--username','migration-admin','--token-name','svn-test','--scopes','all','--raw')
    state['gitea_token']=token.splitlines()[-1]
    state_path.write_text(json.dumps(state,ensure_ascii=False,indent=2))
    common=['--platform','linux/amd64','--network',args.prefix,'--label','io.gitea-manager.test=svn',
            '-v',args.prefix+'-manager:/app/data','-e','DATABASE_URL=sqlite:////app/data/gitea-manager.db',
            '-e','INIT_PASSWORD='+state['manager_password'],'-e','SECRET_KEY='+state['secret'],'-e','DISABLE_BACKGROUND_TASKS=1',
            '-e','PYTHONDONTWRITEBYTECODE=1']
    docker('run','-d','--name',args.prefix+'-manager','--network-alias','manager','-p','127.0.0.1::5000',*common,args.image,
           'gunicorn','--bind','0.0.0.0:5000','--workers','2','--timeout','60','app:create_app()')
    docker('run','-d','--name',args.prefix+'-worker',*common,args.image)
else:
    state=json.loads(state_path.read_text())
command=['docker','run','--rm','--platform','linux/amd64','--network',args.prefix,'--label','io.gitea-manager.test=svn',
         '-v',str(ROOT)+'/scripts:/tests:ro','-v',str(folder)+':/evidence',args.image,'python','/tests/svn-migration-scenarios.py']
driver=subprocess.Popen(command)
try:
    while driver.poll() is None:
        request=folder/'worker-restart.request'
        if request.exists():
            request.unlink()
            name=args.prefix+'-worker'
            labels=json.loads(docker('inspect','--format','{{json .Config.Labels}}',name))
            if labels.get('io.gitea-manager.test')!='svn':raise RuntimeError('拒绝重启没有隔离标签的容器')
            docker('kill','--signal','KILL',name)
            docker('start',name)
        time.sleep(.2)
    if driver.returncode:raise RuntimeError('隔离迁移场景未全部通过，保留现场和结果')
finally:
    if driver.poll() is None:driver.terminate();driver.wait()
print('隔离验收结果：'+str(folder/'acceptance.json'))
