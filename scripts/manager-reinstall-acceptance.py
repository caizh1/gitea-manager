"""隔离验收新包卸载、旧目录缺失、业务记录清空及重新安装，不接触已有实例。"""
import argparse
import hashlib
import http.cookiejar
import ipaddress
import json
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tarfile
import time
from urllib.request import build_opener, ProxyHandler, HTTPCookieProcessor, Request

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--package', type=Path, required=True)
parser.add_argument('--old-package', type=Path, help='可选的历史安装包，用于真实旧版本卸载验收')
parser.add_argument('--directory', type=Path, required=True)
parser.add_argument('--helper-image', default='gitea-manager-reinstall-helper:20261024-r1')
args = parser.parse_args()
folder = args.directory.resolve()
assert folder.is_relative_to(ROOT/'work') and not folder.exists(), '只接受本任务工作目录中的新隔离目录'
folder.mkdir(parents=True)
checks = []


def docker(*argv):
    result = subprocess.run(['docker', *argv], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('隔离 Docker 操作失败：' + result.stderr[-800:])
    return result.stdout.strip()


def check(name, valid):
    checks.append({'检查': name, '通过': bool(valid)})
    (folder/'acceptance.json').write_text(json.dumps({'说明': '独立 Docker Desktop 的原安装与新包卸载入口验收，不代表 Ubuntu systemd 真机', '检查': checks}, ensure_ascii=False, indent=2)+'\n')
    assert valid, name
    print('通过：'+name, flush=True)


def extract(directory, package=None):
    directory.mkdir()
    with tarfile.open(package or args.package) as archive:
        assert all((directory/member.name).resolve().is_relative_to(directory) and (member.isfile() or member.isdir()) for member in archive)
        archive.extractall(directory)


def networks(project):
    ids = docker('network', 'ls', '-q').split()
    used = [ipaddress.ip_network(c['Subnet']) for n in json.loads(docker('network', 'inspect', *ids))
            for c in (n.get('IPAM', {}).get('Config') or []) if c.get('Subnet')]
    for name in ('default', 'review-execution'):
        subnet = next(n for n in ipaddress.ip_network('10.249.0.0/16').subnets(new_prefix=24)
                      if not any(n.version == u.version and n.overlaps(u) for u in used))
        command = ['network','create','--subnet',str(subnet),'--label','com.docker.compose.project='+project,
                   '--label','com.docker.compose.network='+name,'--label','io.gitea-manager.test=manager-reinstall']
        if name == 'review-execution': command.append('--internal')
        docker(*command, project+'_'+name)
        used.append(subnet)


def install(directory, port):
    process = subprocess.Popen(['bash','install.sh','--desktop-test','--host','127.0.0.1','--port',str(port)], cwd=directory)
    prepared = False
    try:
        while process.poll() is None:
            path = directory/'state/installation.json'
            if path.exists() and not prepared:
                state = json.loads(path.read_text())
                assert state['directory'] == str(directory) and state['product'] == 'manager'
                networks(state['project']); prepared = True
            time.sleep(.3)
        assert process.returncode == 0, '原安装入口失败，保留隔离诊断'
    finally:
        if process.poll() is None: process.terminate(); process.wait()
    state = json.loads((directory/'state/installation.json').read_text())
    assert state['complete']
    return state


def ids(project):
    return docker('ps','-aq','--filter','label=com.docker.compose.project='+project).split()


def backend(project):
    return docker('ps','-q','--filter','label=com.docker.compose.project='+project,'--filter','label=com.docker.compose.service=backend')


def records(project):
    code = "import sqlite3,json;c=sqlite3.connect('file:/app/data/gitea-manager.db?mode=ro',uri=True);print(json.dumps({t:c.execute('select count(*) from '+t).fetchone()[0] for t in ('gitea_servers','svn_sources','svn_repositories','svn_migration_plans')}))"
    return json.loads(docker('exec',backend(project),'python','-c',code))


def projects():
    values = docker('ps','-a','--format','{{.Label "com.docker.compose.project"}}').splitlines()
    for kind in ('volume','network'):
        values += docker(kind,'ls','--format','{{.Label "com.docker.compose.project"}}').splitlines()
    return sorted({v for v in values if re.fullmatch(r'gitea-manager-[0-9a-f]{10}',v)})


def uninstall(directory, project, first='UNINSTALL', second=None):
    choices = projects(); assert project in choices
    answers = ([str(choices.index(project)+1)] if len(choices)>1 else [])+[first]
    if first == 'UNINSTALL': answers += [second if second is not None else 'DELETE '+project]
    result = subprocess.run(['docker','run','--rm','-i','--platform','linux/amd64',
        '--label','io.gitea-manager.test=manager-reinstall-helper',
        '-v','/var/run/docker.sock:/var/run/docker.sock','-v',str(directory)+':/package',
        args.helper_image,'bash','uninstall.sh'], input='\n'.join(answers)+'\n', capture_output=True,text=True,timeout=180)
    (folder/('uninstall-'+str(time.time_ns())+'.json')).write_text(json.dumps({
        '说明':'原卸载入口的受限输出，不含凭据；执行只允许本次明确确认的隔离项目',
        '项目':project,'退出码':result.returncode,'输出':result.stdout[-16000:],'错误':result.stderr[-4000:]},ensure_ascii=False,indent=2)+'\n')
    return result


old = folder/'old-installation'; fresh = folder/'new-package'
extract(old, args.old_package); extract(fresh)
with socket.socket() as probe: probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
old_state = install(old, port); project = old_state['project']
check(('历史版安装包' if args.old_package else '完整新包')+'通过原首次安装入口，全部核心及后台服务就绪',len(ids(project))==8)
# 通过真实 HTTP 管理入口写入来源、仓库和草稿，不能只验证空实例卸载。
client=build_opener(ProxyHandler({}),HTTPCookieProcessor(http.cookiejar.CookieJar()));csrf=''
def api(method,path,value=None):
    data=json.dumps(value).encode() if value is not None else None
    request=Request(old_state['url']+'/api'+path,data=data,method=method,
                    headers={'Content-Type':'application/json','X-CSRF-Token':csrf})
    with client.open(request,timeout=30) as response:return json.load(response)
csrf=api('GET','/session')['csrf_token']
login=api('POST','/login',{'username':'admin','password':old_state['admin_password']})
csrf=login.get('csrf_token') or api('GET','/session')['csrf_token']
if login.get('user',{}).get('must_change_password'):
    changed=secrets.token_urlsafe(32)
    login=api('POST','/access/password',{'current_password':old_state['admin_password'],'new_password':changed})
    csrf=login.get('csrf_token') or api('GET','/session')['csrf_token']
api('POST','/settings',{'host_ip':'127.0.0.1'})
target=api('POST','/servers',{'name':'隔离管理目标','host':'127.0.0.1','ssh_user':'test','gitea_url':'http://127.0.0.1:1','api_token':secrets.token_hex(20),'is_local':False})
source=api('POST','/svn-migrations/sources',{'name':'隔离 SVN','slug':'reinstall-source','url':'svn://127.0.0.1/fixture','username':'reader','password':secrets.token_urlsafe(20)})
repo=api('POST',f'/svn-migrations/sources/{source["id"]}/repositories',{'name':'fixture','url':'svn://127.0.0.1/fixture/project'})[0]
api('POST','/svn-migrations/plans',{'name':'重装前草稿','server_id':target['id'],'repositories':[{'id':repo['id']}]})
check('旧隔离实例含真实管理记录与 SVN 配置及草稿',all(value>0 for value in records(project).values()))
# 旧目录不可用；仅保留私有备份供失败诊断，卸载器无法通过原路径访问它。
keeper=folder/'old-private-keeper';old.rename(keeper)
check('原安装目录已不存在',not old.exists())
# 创建本测试的独立 Gitea 旁观实例，用于确认产品边界。
bystander='gitea-gitea-'+secrets.token_hex(5)
other=docker('run','-d','--platform','linux/amd64','--label','com.docker.compose.project='+bystander,
             '--label','com.docker.compose.service=gitea','--label','io.gitea-manager.test=manager-reinstall-bystander',
             'python:3.11-slim','sleep','1800')
try:
    before=ids(project)
    result=uninstall(fresh,project,first='取消')
    check('第一次确认取消，全部旧 Manager 容器保留',result.returncode==0 and ids(project)==before)
    result=uninstall(fresh,project,second='DELETE '+bystander)
    check('第二次确认实例不符，不删除任何 Manager 容器',result.returncode==0 and ids(project)==before)
    result=uninstall(fresh,project)
    check('新包在旧目录缺失且已有业务记录时完成双确认卸载',result.returncode==0 and 'Manager 实例卸载完成：'+project in result.stdout and not ids(project))
    for kind in ('volume','network'):
        check('旧 Manager 专属'+('卷' if kind=='volume' else '网络')+'无残留',not docker(kind,'ls','-q','--filter','label=com.docker.compose.project='+project))
    check('独立 Gitea 标记的旁观容器保持运行',docker('inspect','--format','{{.State.Running}}',other)=='true')
    check('旧宿主机绑定目录保留且新包没有旧安装状态',keeper.exists() and not (fresh/'state').exists() and not (fresh/'compose.install.json').exists())
    fresh_state=install(fresh,port)
    check('同一新包目录在原端口重新安装成功',fresh_state['complete'] and fresh_state['port']==port and len(ids(fresh_state['project']))==8)
    check('新安装凭据文件明确给出管理员账号', '管理员：admin' in (fresh/'state/access.txt').read_text())
    check('新账号和密钥重新生成，旧 Manager 业务配置已清空',fresh_state['project']!=project and fresh_state['admin_password']!=old_state['admin_password'] and fresh_state['master_key']!=old_state['master_key'] and not any(records(fresh_state['project']).values()))
    worker=docker('ps','-q','--filter','label=com.docker.compose.project='+fresh_state['project'],'--filter','label=com.docker.compose.service=svn-migration-worker')
    docker('exec',worker,'python','-m','svn_migration.worker','--check')
    code="import sqlite3,json;c=sqlite3.connect('/app/data/gitea-manager.db');print(json.dumps(c.execute(\"select count(*) from sqlite_master where name in ('svn_layout_detections','svn_discoveries')\").fetchone()[0]))"
    check('新安装包含 SVN 自动检测表和独立执行器',json.loads(docker('exec',backend(fresh_state['project']),'python','-c',code))==2)
    docker('stop','--time','15',*ids(fresh_state['project']))
finally:
    info=json.loads(docker('inspect',other))[0]
    assert info['Config']['Labels'].get('io.gitea-manager.test')=='manager-reinstall-bystander'
    docker('rm','-f',other)
print('完整一键安装、旧目录缺失卸载及重新安装闭环通过：'+str(folder/'acceptance.json'),flush=True)
