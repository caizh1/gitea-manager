#!/usr/bin/env python3
"""独立接收端制品隔离、完整清单、终止及 HTTP 响应丢失验收；保留全部现场。"""
import argparse,base64,hashlib,http.server,io,json,secrets,subprocess,sys,tarfile,threading,zipfile
from pathlib import Path
from datetime import datetime,timezone,timedelta
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor
import requests
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from git_migration.manifest import summary

def archive(files,kind='zip'):
 out=io.BytesIO()
 if kind=='zip':
  with zipfile.ZipFile(out,'w') as z:
   for k,v in files.items():z.writestr(k,v)
 else:
  with tarfile.open(fileobj=out,mode='w:gz') as t:
   for k,v in files.items():
    v=v.encode() if isinstance(v,str) else v
    a=tarfile.TarInfo(k);a.size=len(v);a.mtime=1600000000;t.addfile(a,io.BytesIO(v))
 return out.getvalue()

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--target-state',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 state=json.loads(a.target_state.read_text());target=state['name'];info=json.loads(subprocess.check_output(['docker','inspect',target]))[0]
 assert info['Config']['Labels'].get('io.gitea-manager.test')=='git-migration-receiver'
 c=requests.Session();c.trust_env=False;c.headers['Authorization']='token '+state['token'];base=state['base'];evidence=[]
 def req(method,path,body=None,client=c,ok=True,**kw):
  r=client.request(method,base+path,json=body,timeout=90,**kw)
  if ok:assert r.ok,(method,path,r.status_code,r.text[:500])
  return r
 def api(method,path,body=None,**kw):
  r=req(method,'/api/v1'+path,body,**kw);return r.json() if r.content else None
 actor=api('GET','/user');owner=actor['login'];cap=api('GET','/governance/migration-capabilities');assert cap['protocol']==8 and cap['native_package_manifest']
 credentials={}
 for role in ('read','write','none','owner'):
  login='package-'+role+'-'+secrets.token_hex(3);pw=secrets.token_urlsafe(20)
  user=api('POST','/admin/users',{'username':login,'password':pw,'email':login+'@example.invalid','must_change_password':False})
  client=requests.Session();client.trust_env=False;client.auth=(login,pw);credentials[role]=(user,client)
 anonymous=requests.Session();anonymous.trust_env=False;credentials['anonymous']=(None,anonymous)
 def payload(kind,name):
  version='1.2.3'
  if kind=='generic':return {'payload.bin':b'fixed payload'}
  if kind=='nuget':
   spec=('<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd"><metadata><id>'+name+'</id><version>'+version+'</version><authors>来源作者</authors><description>未发布资料</description></metadata></package>').encode()
   return {name.lower()+'.1.2.3.nupkg':archive({name+'.nuspec':spec})}
  if kind=='npm':return {name+'-1.2.3.tgz':archive({'package/package.json':json.dumps({'name':name,'version':version}).encode()},'tar')}
  if kind=='maven':return {name.split(':')[1]+'-1.2.3.pom':('<project><modelVersion>4.0.0</modelVersion><groupId>local</groupId><artifactId>'+name.split(':')[1]+'</artifactId><version>1.2.3</version></project>').encode()}
  if kind=='pypi':return {name+'-1.2.3-py3-none-any.whl':archive({name+'-1.2.3.dist-info/METADATA':('Metadata-Version: 2.1\nName: '+name+'\nVersion: 1.2.3\n\n').encode()})}
  return {name+'-1.2.3.tgz':archive({name+'/Chart.yaml':('apiVersion: v2\nname: '+name+'\nversion: 1.2.3\n').encode()},'tar')}
 def fixture(kind,with_spec=False):
  key=secrets.token_hex(16);name='p'+key[:10];name='local:'+name if kind=='maven' else name
  group=api('POST','/governance/groups',{'parent_id':0,'path':'pkg-'+key[:10],'name':'制品隔离验收','visibility':2})
  for role,value in [('read',20),('write',40),('owner',50)]:
   current=api('GET','/governance/groups/'+str(group['id']));api('PUT',f"/governance/groups/{group['id']}/members/{credentials[role][0]['id']}",{'revision':current['revision'],'role':value})
  repo=api('POST','/user/repos',{'name':'gm-migrate-'+key,'private':True,'auto_init':True})
  api('PATCH','/repos/'+owner+'/'+repo['name'],{'has_actions':False})
  path='/governance/migration-sessions/'+key
  session=api('POST','/governance/migration-sessions',{'protocol':8,'key':key,'batch_key':key,'batch_size':1,'repo_id':repo['id'],'source_digest':'a'*64})
  receipt=api('POST',path+'/objects',{'objects':[{'kind':'package','source_id':'package','title':name,'created':1600000000,'history':{'type':kind,'name':name,'version':'1.2.3'}}]})[0]
  files=payload(kind,name)
  if with_spec:
   with zipfile.ZipFile(io.BytesIO(next(iter(files.values())))) as z:files[name.lower()+'.nuspec']=z.read(name+'.nuspec')
  assets={}
  for filename,data in files.items():
   meta={'source_id':filename,'name':filename,'parent_kind':'package','parent_id':'package','sha256':hashlib.sha256(data).hexdigest(),'size':len(data),'created':1600000000}
   response=c.post(base+'/api/v1'+path+'/assets',data=data,headers={'X-Migration-Metadata':base64.b64encode(json.dumps(meta).encode()).decode(),'Content-Type':'application/octet-stream'},timeout=90);assert response.ok,response.text;assets[filename]=response.json()
  local={'objects':{'package':receipt},'assets':assets};manifest=summary(local)
  api('POST',path+'/seal',{'objects':1,'assets':len(assets),'manifest':manifest})
  api('PATCH','/repos/'+owner+'/'+repo['name'],{'archived':True})
  api('POST',path+'/handoff',{'owner_id':group['id'],'name':'result'})
  first=api('GET',path+'/status');api('POST',path+'/handoff',{'owner_id':group['id'],'name':'result'});second=api('GET',path+'/status');assert first['package_manifest_digest']==second['package_manifest_digest']
  current=api('GET','/governance/groups/'+str(group['id']))
  api('POST','/governance/migration-batches/'+key+'/prepare',{'repositories':[{'key':key,'repo_id':repo['id'],'group_revision':current['revision'],'archived':False}]})
  native=api('GET',path+'/manifest');assert native['native_version']==1 and native['native_count']==1
  return {'key':key,'path':path,'session':session,'repo':repo,'group':group,'name':name,'files':files,'assets':assets,'native':native}
 def location(f,kind):
  root='/api/packages/'+f['group']['full_path']
  name=f['name'];filename=next(iter(f['files']))
  return {'generic':root+'/generic/'+name+'/1.2.3/'+filename,'nuget':root+'/nuget/query?q='+name,'npm':root+'/npm/'+name,'maven':root+'/maven/local/'+name.split(':')[-1]+'/1.2.3/'+filename,'pypi':root+'/pypi/simple/'+name+'/','helm':root+'/helm/index.yaml'}[kind]
 for kind in ('generic','npm','maven','pypi','nuget','helm'):
  f=fixture(kind);route=location(f,kind)
  assert req('GET',route).ok, '管理员应能核验隔离制品'
  now=datetime.now(timezone.utc)
  query=urlencode({'scope_type':'group','scope_id':f['group']['id'],'from':(now-timedelta(hours=1)).isoformat(),'to':(now+timedelta(seconds=1)).isoformat(),'event_type':'package.version_published'})
  audit=req('GET','/api/v1/governance/audit-events?'+query,client=credentials['owner'][1]);assert not audit.json()['events'], '群组审计泄露隔离制品'

  for role,(_,client) in credentials.items():
   response=req('GET',route,client=client,ok=False)
   if kind in ('nuget','helm') and role!='none':assert f['name'] not in response.text,(kind,role,response.text)
   else:assert response.status_code>=400,(kind,role,response.status_code)
  if kind=='generic':
   for client in (c,credentials['write'][1]):assert req('PUT',route+'-extra',client=client,ok=False,data=b'extra').status_code>=400
  if kind=='nuget':
   api('POST',f['path']+'/abort',{'repo_id':f['repo']['id']});api('POST',f['path']+'/abort',{'repo_id':f['repo']['id']})
   assert api('GET',f['path']+'/status')['state']=='abandoned'
  else:
   api('POST','/governance/migration-batches/'+f['key']+'/publish',{})
   assert req('GET',route,client=credentials['read'][1]).ok
  evidence.append({'格式':kind,'权限及生命周期':'通过'})
 f=fixture('nuget',True);api('POST',f['path']+'/abort',{'repo_id':f['repo']['id']});evidence.append({'场景':'来源同时带 nuspec 的终止','结果':'通过'})
 # 真正丢弃已提交的 HTTP 回执，重启接收端后按同一任务恢复。
 f=fixture('nuget')
 class Drop(http.server.BaseHTTPRequestHandler):
  def do_POST(self):
   r=c.post(base+self.path,data=self.rfile.read(int(self.headers.get('Content-Length','0'))),headers={'Content-Type':'application/json'},timeout=90)
   assert r.ok,r.text
   self.close_connection=True
  def log_message(self,*args):pass
 server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Drop);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 try:
  try:requests.post('http://127.0.0.1:'+str(server.server_port)+'/api/v1'+f['path']+'/abort',json={'repo_id':f['repo']['id']},timeout=90)
  except requests.RequestException:pass
  else:raise AssertionError('响应未被丢弃')
 finally:server.shutdown();server.server_close()
 subprocess.run(['docker','restart',target],check=True,stdout=subprocess.DEVNULL)
 import time
 for _ in range(90):
  try:
   if c.get(base+'/api/v1/version',timeout=1).ok:break
  except requests.RequestException:pass
  time.sleep(1)
 api('POST',f['path']+'/abort',{'repo_id':f['repo']['id']});assert api('GET',f['path']+'/status')['state']=='abandoned'
 evidence.append({'场景':'终止回执丢失及接收端重启','结果':'通过'})
 # 发布和终止竞争不允许出现两个成功结果。
 f=fixture('generic');barrier=threading.Barrier(2)
 def race(path,body):barrier.wait();return req('POST','/api/v1'+path,body,ok=False).status_code
 with ThreadPoolExecutor(2) as pool:
  one=pool.submit(race,'/governance/migration-batches/'+f['key']+'/publish',{});two=pool.submit(race,f['path']+'/abort',{'repo_id':f['repo']['id']});codes=[one.result(),two.result()]
 assert sum(code<300 for code in codes)==1,codes
 evidence.append({'场景':'发布与终止竞争','HTTP':codes,'结果':'通过'})
 a.output.write_text(json.dumps({'数据库':state['database'],'结果':'通过','场景':evidence},ensure_ascii=False,indent=2)+'\n')
 print(state['database']+' 制品隔离验收通过',flush=True)
if __name__=='__main__':main()
