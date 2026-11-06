#!/usr/bin/env python3
"""对已标记隔离环境注入回执丢失与执行器崩溃，验证整批开放和恢复。"""
import argparse
import base64
import hashlib
import inspect
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import subprocess
import threading
import textwrap
import time
from urllib.parse import quote
import requests


def direct_request(method,url,**options):
    # 本地验收流量不能继承桌面代理，避免把隔离实例请求送往外部。
    with requests.Session() as client:
        client.trust_env=False
        return client.request(method,url,**options)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('manager-state','target-state','source-state','output'):p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--container-proxy', action='store_true', help='在已标记隔离容器网络内运行故障代理，避免宿主转发干扰')
    p.add_argument('--probe-expiry', action='store_true', help='仅在隔离夹具缩短有效期，验证已接受的复检可跨过截止时间')
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    manager,target,source=(json.loads(path.read_text()) for path in (a.manager_state,a.target_state,a.source_state))
    def docker(*args):return subprocess.check_output(['docker',*args],text=True).strip()
    for name,label in ((manager['name']+'-backend','git-migration-manager'),(target['name'],'git-migration-receiver'),(source['name'],'git-migration-source')):
        assert json.loads(docker('inspect',name))[0]['Config']['Labels']['io.gitea-manager.test']==label
    report={'说明':'真实隔离环境回执丢失、崩溃与整批可见性验收，不操作生产','断言':[]}
    def save(): (a.output/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    def check(ok,name):
        if not ok:raise AssertionError(name)
        report['断言'].append(name);save()
    flags={'asset':True,'publish':False,'lost_asset':0,'lost_publish':0,'publish_calls':0,
           'publication_only':False,'unexpected_writes':[]}
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def handle_request(self):
            authorization=self.headers.get('Authorization')
            if authorization is None:
                self.send_response(401);self.send_header('WWW-Authenticate','Basic realm="migration-test"');self.send_header('Content-Length','0');self.end_headers();return
            if authorization not in ('token '+target['token'],'Basic '+base64.b64encode(('migration-admin:'+target['token']).encode()).decode()):
                self.send_error(403);return
            if flags['publication_only'] and self.command != 'GET' and self.path.startswith('/api/v1/') and not self.path.endswith('/publish'):
                flags['unexpected_writes'].append({'方法':self.command,'路径':self.path.split('?')[0]})
                self.send_response(409);self.send_header('Content-Length','0');self.end_headers();return
            if self.headers.get('Transfer-Encoding','').lower()=='chunked':
                chunks=[]
                while True:
                    size=int(self.rfile.readline().split(b';',1)[0].strip(),16)
                    if not size:
                        while self.rfile.readline().strip():pass
                        break
                    chunks.append(self.rfile.read(size));assert self.rfile.read(2)==b'\r\n'
                body=b''.join(chunks)
            else:body=self.rfile.read(int(self.headers.get('Content-Length','0')))
            # 保留调用端 Host，让 LFS 返回相同代理地址，避免绕过注入边界。
            headers={k:v for k,v in self.headers.items() if k.lower() not in ('content-length','connection','transfer-encoding')}
            r=direct_request(self.command,target['base']+self.path,headers=headers,data=body,timeout=120,allow_redirects=False)
            kind='asset' if self.command=='POST' and self.path.endswith('/assets') else 'publish' if self.command=='POST' and self.path.endswith('/publish') else None
            if kind=='publish':flags['publish_calls']+=1
            if kind and flags[kind] and r.ok:
                flags[kind]=False;flags['lost_'+kind]+=1
                self.send_response(502);self.end_headers();return
            self.send_response(r.status_code)
            for k,v in r.headers.items():
                if k.lower() not in ('transfer-encoding','content-encoding','connection','content-length'):self.send_header(k,v)
            self.send_header('Content-Length',str(len(r.content)));self.end_headers()
            try:self.wfile.write(r.content)
            except (BrokenPipeError,ConnectionResetError):pass
        do_GET=handle_request;do_POST=handle_request;do_PUT=handle_request;do_PATCH=handle_request;do_DELETE=handle_request
    if a.container_proxy:
        # 代理只转发本次隔离目标的固定测试身份，不挂载 Docker socket。
        container_name=manager['name']+'-fault-proxy'
        control_key=secrets.token_urlsafe(32)
        config=a.output/'proxy-config.json'
        config.write_text(json.dumps({'target':dict(target,base='http://'+target['name']+':3000'),'control_key':control_key}))
        config.chmod(0o600)
        support="import base64,json,requests\nfrom http.server import BaseHTTPRequestHandler,ThreadingHTTPServer\nfrom pathlib import Path\nconfig=json.loads(Path('/config.json').read_text());target=config['target'];control_key=config['control_key']\n"
        support+='flags='+repr(flags)+'\n'+textwrap.dedent(inspect.getsource(direct_request))+'\n'+textwrap.dedent(inspect.getsource(Proxy))
        support+='''
original=Proxy.handle_request
def dispatch(self):
    if self.path=='/__fault_control' and self.headers.get('X-Fault-Control')==control_key:
        if self.command=='POST':
            change=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
            assert set(change)<={'asset','publish','publication_only'}
            flags.update(change)
        data=json.dumps(flags,ensure_ascii=False).encode()
        self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
    return original(self)
Proxy.do_GET=Proxy.do_POST=Proxy.do_PUT=Proxy.do_PATCH=Proxy.do_DELETE=dispatch
ThreadingHTTPServer(('0.0.0.0',8080),Proxy).serve_forever()
'''
        source_file=a.output/'proxy-server.py';source_file.write_text(support)
        network=json.loads(docker('network','inspect',manager['name']))[0]
        assert network['Labels']['io.gitea-manager.test']=='git-migration-manager'
        docker('network','connect',manager['name'],target['name'])
        docker('run','-d','--platform','linux/amd64','--name',container_name,
               '--label','io.gitea-manager.test=git-migration-fault-proxy','--network',manager['name'],
               '--network-alias','migration-fault-proxy','--read-only','--memory','256m','--pids-limit','128',
               '-p','127.0.0.1::8080','--mount','type=bind,src='+str(source_file.resolve())+',dst=/proxy.py,readonly',
               '--mount','type=bind,src='+str(config.resolve())+',dst=/config.json,readonly',
               '--entrypoint','python','gitea-review-backend:offline','-u','/proxy.py')
        port=docker('port',container_name,'8080/tcp').rsplit(':',1)[1]
        class RemoteFlags:
            def __getitem__(self,key):return self.request('GET')[key]
            def __setitem__(self,key,value):self.request('POST',{key:value})
            def request(self,method,data=None):
                response=direct_request(method,'http://127.0.0.1:'+port+'/__fault_control',headers={'X-Fault-Control':control_key},json=data,timeout=5)
                response.raise_for_status();return response.json()
        flags=RemoteFlags()
        for _ in range(30):
            try:flags['publish_calls'];break
            except requests.RequestException:time.sleep(.2)
        else:raise RuntimeError('隔离故障代理未启动')
        class ContainerProxy:
            server_port=8080
            def shutdown(self):docker('stop',container_name)
        proxy=ContainerProxy()
        proxy_url='http://migration-fault-proxy:8080'
        report['代理模式']='独立容器网络'
    else:
        proxy=ThreadingHTTPServer(('0.0.0.0',0),Proxy);threading.Thread(target=proxy.serve_forever,daemon=True).start()
        proxy_url='http://host.docker.internal:'+str(proxy.server_port)
        report['代理模式']='宿主机网络'
    session=requests.Session();session.trust_env=False
    def call(method,path,data=None):
        r=session.request(method,manager['base']+'/api'+path,json=data,timeout=120)
        if not r.ok:raise RuntimeError('隔离管理接口失败 '+path+' '+r.text[:300])
        return r.json()
    session.headers['X-CSRF-Token']=call('GET','/session')['csrf_token'];session.headers['X-CSRF-Token']=call('POST','/login',{'password':manager['password']})['csrf_token']
    def poll(path,states=('queued','running'),seconds=1800):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            value=call('GET',path)
            if value['status'] not in states:return value
            time.sleep(.3)
        raise RuntimeError('等待超时 '+path)
    def ready_with_receipt_recovery(path):
        value=poll(path)
        for attempt in range(2):
            if value['status']!='blocked' or '回执丢失' not in value.get('error',''):break
            report.setdefault('额外网络恢复',[]).append({'预迁移':value['id'],'原因':value['error'],'尝试':attempt+1});save()
            call('POST',path+'/resume',{});value=poll(path)
        return value
    def native(method,path,data=None,auth=None):
        r=direct_request(method,target['base']+'/api/v1'+path,json=data,headers={'Authorization':'token '+target['token']} if auth is None else {},auth=auth,timeout=60)
        if not r.ok:raise RuntimeError('隔离接收端失败 '+path+' '+r.text[:150])
        return r.json() if r.content else None
    sources=call('GET','/git-migrations/sources');src=next(s for s in sources if s['url'].replace('host.docker.internal','127.0.0.1')==source['base'])
    servers=call('GET','/servers');server=next(s for s in servers if s['gitea_url'].replace('host.docker.internal','127.0.0.1')==target['base'])
    original=server['gitea_url']
    call('PUT','/servers/'+str(server['id']),{'gitea_url':proxy_url})
    observer='observer-'+secrets.token_hex(4);password=secrets.token_urlsafe(24)
    user=native('POST','/admin/users',{'username':observer,'password':password,'email':observer+'@example.test','must_change_password':False})
    group=native('POST','/governance/groups',{'parent_id':0,'path':'fault-'+secrets.token_hex(6),'name':'整批开放故障验收','visibility':2})
    current=native('GET','/governance/groups/'+str(group['id']))
    native('PUT',f"/governance/groups/{group['id']}/members/{user['id']}",{'revision':current['revision'],'role':30})
    discovery=call('POST',f"/git-migrations/sources/{src['id']}/discoveries",{'source_version':src['version'],'request_key':secrets.token_hex(16)})
    discovery=poll('/git-migrations/discoveries/'+str(discovery['id']));assert discovery['status']=='succeeded'
    names=['rich-migration-b2cec501','private-project']
    selected=[v for v in discovery['result']['projects'] if v['path'].split('/')[-1] in names]
    check(len(selected)==2,'真实双仓库批次就绪')
    # 确保空样本也具有可核验的 Wiki 历史。
    for v in selected:
        r=direct_request('GET',source['base']+'/api/v1/repos/'+v['path']+'/wiki/page/Home',auth=('source-admin',source['password']),timeout=60)
        if r.status_code==404:
            r=direct_request('POST',source['base']+'/api/v1/repos/'+v['path']+'/wiki/new',auth=('source-admin',source['password']),json={'title':'Home','content_base64':base64.b64encode('故障恢复验收'.encode()).decode(),'message':'补齐固定 Wiki'},timeout=60);r.raise_for_status()
    inventory=call('POST',f"/git-migrations/sources/{src['id']}/inventories",{'discovery_id':discovery['id'],'project_ids':[v['id'] for v in selected],'capture_code':True,'request_key':secrets.token_hex(16)})
    inventory=poll('/git-migrations/inventories/'+str(inventory['id']));check(inventory['status']=='succeeded','双仓库完整盘点成功')
    request={'inventory_id':inventory['id'],'server_id':server['id'],'group_id':group['id'],'request_key':secrets.token_hex(16)}
    required=call('GET',f"/git-migrations/inventories/{inventory['id']}/principals")
    if required:
        mapping=call('POST','/git-migrations/account-mappings',request);path='/git-migrations/account-mappings/'+str(mapping['id']);mapping=poll(path)
        for row in mapping['rows']:
            mapping=call('PATCH',path,{'revision':mapping['revision'],'action':'select','key':row['key'],'target_ids':[1]})
            mapping=call('PATCH',path,{'revision':mapping['revision'],'action':'confirm','key':row['key']})
        check(mapping['ready'],'故障夹具人员映射已确认');request['mapping_id']=mapping['id']
    trial=call('POST','/git-migrations/preflights',request);path='/git-migrations/preflights/'+str(trial['id']);trial=poll(path)
    check(trial['status']=='blocked' and flags['lost_asset']==1,'服务端完成附件写入后丢失回执，任务保留隔离并阻断')
    check(trial['run_id'] is None,'不确定写入没有创建正式任务')
    call('POST',path+'/resume',{})
    for _ in range(200):
        value=call('GET',path)
        if value['status']=='running':break
        time.sleep(.1)
    check(value['status']=='running','同一预迁移已从检查点领取')
    docker('kill','--signal','KILL',manager['name']+'-worker');docker('start',manager['name']+'-worker')
    value=poll(path);check(value['status']=='interrupted','执行器崩溃后回收为中断，没有伪造成功')
    call('POST',path+'/resume',{});trial=poll(path);check(trial['status']=='ready','重启后同一双仓库批次恢复并完成最终形态演练')
    private=json.loads(docker('exec',manager['name']+'-backend','python','-c',"import sqlite3;print(sqlite3.connect('/app/data/gitea-manager.db').execute('select checkpoint_json from git_migration_preflights where id=?',("+str(trial['id'])+",)).fetchone()[0])"))
    repos=[v for k,v in private.items() if k.isdigit()];check(len(repos)==2 and all(v.get('prepared') for v in repos),'两个仓库都已准备最终形态')
    auth=(observer,password)
    for v in repos:
        r=direct_request('GET',target['base']+'/api/v1/repos/'+v['final_path'],auth=auth,timeout=60)
        check(r.status_code in (403,404),'发布前业务账号无法读取仓库 '+str(v['repo_id']))
    search=native('GET','/repos/search?q=rich-migration',auth=auth)
    check(not any(x['id'] in [v['repo_id'] for v in repos] for x in search['data']),'发布前搜索结果不泄露隔离仓库')
    downloads=[]
    for v in repos:
        repo=native('GET','/repositories/'+str(v['repo_id']))
        for item in v.get('package_files',[]):
            package,file=item['package'],item['file']
            if package['type']!='generic':continue
            url=target['base']+'/api/packages/'+quote(repo['owner']['login'],safe='')+'/generic/'+ '/'.join(quote(x,safe='') for x in (package['name'],package['version'],file['name']))
            check(direct_request('GET',url,auth=auth,timeout=60).status_code in (403,404),'发布前业务账号无法下载原生制品')
            downloads.append((url,item['sha256']))
    flags['publish']=True
    flags['publication_only']=True
    if a.probe_expiry:
        script="from datetime import datetime,timedelta;import sqlite3;d=sqlite3.connect('/app/data/gitea-manager.db');v=datetime.utcnow()+timedelta(seconds=2);d.execute('update git_migration_preflights set expires_at=? where id=?',(v.strftime('%Y-%m-%d %H:%M:%S.%f'),"+str(trial['id'])+"));d.commit();print(v.isoformat()+'Z')"
        report['缩短后的启动截止时间']=docker('exec',manager['name']+'-backend','python','-c',script)
        save()
    call('POST',path+'/start',{'source_frozen':True,'request_key':secrets.token_hex(16)})
    trial=poll(path);check(trial['run_id'] is not None,'通过复检后创建唯一正式任务')
    if a.probe_expiry:
        from datetime import datetime, timezone
        check(datetime.now(timezone.utc)>datetime.fromisoformat(report['缩短后的启动截止时间'].replace('Z','+00:00')),
              '在有效期内接受启动，复检跨过截止时间后仍完成')
    runpath='/git-migrations/runs/'+str(trial['run_id']);run=poll(runpath)
    check(run['status']=='uncertain' and flags['lost_publish']==1,'整批提交成功后回执丢失，显示待核对状态')
    for v in repos:
        r=direct_request('GET',target['base']+'/api/v1/repos/'+v['final_path'],auth=auth,timeout=60)
        check(r.status_code==200,'提交后业务账号可读取仓库 '+str(v['repo_id']))
    for url,digest in downloads:
        r=direct_request('GET',url,auth=auth,timeout=60);check(r.ok and hashlib.sha256(r.content).hexdigest()==digest,'发布后业务账号可下载并核对原生制品')
    check(native('GET','/governance/migration-batches/'+private['key'])['state']=='published','接收端整批开放状态已持久化')
    call('POST',runpath+'/resume',{});run=poll(runpath)
    check(run['status']=='succeeded','根据原批次提交回执恢复成功')
    check(flags['publish_calls']==1,'恢复没有二次发布或重复迁移')
    check(not flags['unexpected_writes'],'从启动复检到完成仅有整批开放写入，没有重复交接或创建制品')
    check(sorted(v['repo_id'] for v in run['checkpoint'].values())==sorted(v['repo_id'] for v in repos),'恢复保持原仓库编号')
    flags['publication_only']=False
    cancel_group=native('POST','/governance/groups',{'parent_id':0,'path':'cancel-'+secrets.token_hex(6),'name':'预演取消与路径回收验收','visibility':2})
    cancel_request=dict(request,group_id=cancel_group['id'],request_key=secrets.token_hex(16))
    if required:
        cancel_request.pop('mapping_id',None)
        mapping=call('POST','/git-migrations/account-mappings',cancel_request);mp='/git-migrations/account-mappings/'+str(mapping['id']);mapping=poll(mp)
        for row in mapping['rows']:
            mapping=call('PATCH',mp,{'revision':mapping['revision'],'action':'select','key':row['key'],'target_ids':[1]})
            mapping=call('PATCH',mp,{'revision':mapping['revision'],'action':'confirm','key':row['key']})
        cancel_request['mapping_id']=mapping['id']
    cancelled=call('POST','/git-migrations/preflights',cancel_request);cp='/git-migrations/preflights/'+str(cancelled['id']);cancelled=ready_with_receipt_recovery(cp)
    check(cancelled['status']=='ready','取消夹具已完成真实最终形态演练')
    before_cancel=json.loads(docker('exec',manager['name']+'-backend','python','-c',"import sqlite3;print(sqlite3.connect('/app/data/gitea-manager.db').execute('select checkpoint_json from git_migration_preflights where id=?',("+str(cancelled['id'])+",)).fetchone()[0])"))
    call('POST',cp+'/cancel',{});cancelled=poll(cp)
    check(cancelled['status']=='cancelled','最终形态取消成功且保留归档副本')
    for key,v in before_cancel.items():
        if not key.isdigit():continue
        repo=native('GET','/repositories/'+str(v['repo_id']))
        check(repo['archived'] and repo['private'] and repo['owner']['login']=='migration-admin','取消后原仓库编号保留并回到管理员归档空间')
        check(native('GET','/governance/migration-sessions/'+v['key']+'/status')['state']=='abandoned','已取消会话永久禁止重新写入')
        r=direct_request('GET',target['base']+'/api/v1/repos/'+v['final_path'],headers={'Authorization':'token '+target['token']},allow_redirects=False,timeout=30)
        check(r.status_code==404,'取消后释放最终路径及迁移别名')
    again=call('POST','/git-migrations/preflights',dict(cancel_request,request_key=secrets.token_hex(16)));ap='/git-migrations/preflights/'+str(again['id']);again=ready_with_receipt_recovery(ap)
    check(again['status']=='ready','原选择与群组可重新预迁移，不接管旧归档仓库')
    call('POST',ap+'/cancel',{});check(poll(ap)['status']=='cancelled','重建预演同样可安全结束')
    call('PUT','/servers/'+str(server['id']),{'gitea_url':original})
    proxy.shutdown();report.update(结果='通过',预迁移=trial['id'],正式迁移=run['id'],仓库数=2);save()
    print('整批开放及故障恢复验收通过：'+str(a.output/'acceptance.json'),flush=True)

if __name__=='__main__':main()
