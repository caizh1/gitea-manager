"""独立接收端的完整清单故障注入与发布竞争；保留全部反例现场。"""
import argparse,base64,hashlib,json,secrets,subprocess,sys,time,threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import requests
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'backend'))
from git_migration.manifest import summary,metadata_digest

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--target-state',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 state=json.loads(a.target_state.read_text());name=state['name']
 info=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
 assert info['Config']['Labels'].get('io.gitea-manager.test')=='git-migration-receiver'
 s=requests.Session();s.trust_env=False;s.headers['Authorization']='token '+state['token'];base=state['base']+'/api/v1'
 def api(method,path,body=None,ok=True):
  r=s.request(method,base+path,json=body,timeout=120)
  if ok:assert 200<=r.status_code<300,(method,path,r.status_code,r.text[:300])
  return r.json() if ok and r.content else r
 def sql(command):
  if state.get('database')=='postgres':cmd=['docker','exec',name+'-db','psql','-U','gitea','-d','gitea','-v','ON_ERROR_STOP=1','-c',command]
  else:cmd=['docker','exec',name,'sqlite3','-cmd','.timeout 30000','/data/gitea/gitea.db',command]
  subprocess.run(cmd,check=True,stdout=subprocess.DEVNULL)
 group=api('POST','/governance/groups',{'parent_id':0,'path':'manifest-'+secrets.token_hex(4),'name':'完整清单隔离验收','visibility':2})
 actor=api('GET','/user');results=[]
 def fixture(phase):
  key=secrets.token_hex(16);path='/governance/migration-sessions/'+key;repo=api('POST','/user/repos',{'name':'gm-migrate-'+key,'private':True,'auto_init':True})
  api('PATCH','/repos/'+actor['login']+'/'+repo['name'],{'has_actions':False})
  session=api('POST','/governance/migration-sessions',{'protocol':8,'key':key,'repo_id':repo['id'],'source_digest':'a'*64,'batch_key':key,'batch_size':1})
  objects=[{'kind':'issue','source_id':'4','number':1,'title':'清单验收议题','state':'open','author':'来源作者','created':1600000000,'updated':1600000000},
   {'kind':'comment','source_id':'8','parent_kind':'issue','parent_id':'4','content':'待解决讨论','author':'来源作者','created':1600000000,'updated':1600000000},
   {'kind':'discussion_snapshot','source_id':'thread','parent_kind':'issue','parent_id':'4','title':'来源线程','created':1600000000,'history':{'thread_id':'thread','parent_kind':'issue','parent_source_id':'4','notes':[{'source_id':'8','kind':'comment','resolvable':True,'resolved':False}]}}]
  receipts=api('POST',path+'/objects',{'objects':objects})
  content=b'fixed manifest asset';meta={'source_id':'file','name':'review.txt','parent_kind':'comment','parent_id':'8','sha256':hashlib.sha256(content).hexdigest(),'size':len(content),'created':1600000000}
  r=s.post(base+path+'/assets',data=content,headers={'X-Migration-Metadata':base64.b64encode(json.dumps(meta).encode()).decode(),'Content-Type':'application/octet-stream'},timeout=30);assert r.status_code==200,r.text
  asset=r.json();assert asset['metadata_digest']==metadata_digest(meta)
  local={'objects':{v['kind']+':'+v['source_id']:v for v in receipts},'assets':{'file':asset}}
  expected=summary(local)
  assert api('GET',path+'/manifest')['digest']==expected['digest']
  body={'objects':len(receipts),'assets':1,'manifest':expected}
  if phase=='loading':return key,path,session,repo,body
  api('POST',path+'/seal',body);api('POST',path+'/seal',body)
  if phase=='prepared':
   api('PATCH','/repos/'+actor['login']+'/'+repo['name'],{'archived':True})
   api('GET',path+'/manifest')
   api('POST',path+'/handoff',{'owner_id':group['id'],'name':'repo-'+key[:8]})
   revision=api('GET','/governance/groups/'+str(group['id']))['revision']
   api('POST','/governance/migration-batches/'+key+'/prepare',{'repositories':[{'key':key,'repo_id':repo['id'],'group_revision':revision,'archived':False}]})
  return key,path,session,repo,body
 mutations={
  '删除快照回执':lambda sid:"DELETE FROM governance_migration_receipt WHERE session_id=%d AND kind='discussion_snapshot'"%sid,
  '删除回执及历史':lambda sid:"DELETE FROM governance_migration_receipt WHERE session_id=%d AND kind='discussion_snapshot'; DELETE FROM governance_migration_history WHERE session_id=%d"%(sid,sid),
  '删除全部回执':lambda sid:'DELETE FROM governance_migration_receipt WHERE session_id=%d'%sid,
  '等量来源替换':lambda sid:"UPDATE governance_migration_receipt SET source_id='other' WHERE session_id=%d AND kind='discussion_snapshot'"%sid,
  '替换目标关联':lambda sid:"UPDATE governance_migration_receipt SET target_id=987654321 WHERE session_id=%d AND kind='comment'"%sid,
  '修改载荷摘要':lambda sid:"UPDATE governance_migration_receipt SET digest='%s' WHERE session_id=%d AND kind='discussion_snapshot'"%('b'*64,sid),
  '删除附件回执':lambda sid:'DELETE FROM governance_migration_asset WHERE session_id=%d'%sid,
  '修改附件元数据':lambda sid:"UPDATE governance_migration_asset SET metadata='{}' WHERE session_id=%d"%sid,
 }
 for phase in ('sealed','prepared'):
  for mutation,change in mutations.items():
   key,path,session,repo,body=fixture(phase);sql(change(session['id']))
   check=api('GET',path+'/manifest',ok=False);assert check.status_code>=400,(phase,mutation,'清单错误放行')
   final=api('POST',path+'/seal',body,ok=False) if phase=='sealed' else api('POST','/governance/migration-batches/'+key+'/publish',{},ok=False)
   assert final.status_code>=400,(phase,mutation,'重签或发布错误放行')
   assert api('GET','/governance/migration-batches/'+key)['state']!='published'
   results.append({'阶段':phase,'注入':mutation,'清单HTTP':check.status_code,'后续HTTP':final.status_code,'结果':'阻断'})
   print('已阻断：'+phase+' / '+mutation,flush=True)
 # 真实并发发布与终止必须仅有一个终态，重复发布不得使终止批次复活。
 key,path,session,repo,body=fixture('prepared')
 with ThreadPoolExecutor(2) as pool:
  futures=[pool.submit(api,'POST','/governance/migration-batches/'+key+'/publish',{},False),pool.submit(api,'POST',path+'/abort',{'repo_id':repo['id'],'marker':''},False)]
  codes=[f.result().status_code for f in futures]
 terminal=api('GET','/governance/migration-batches/'+key)['state'];assert terminal in ('published','abandoned')
 if terminal=='abandoned':assert api('POST','/governance/migration-batches/'+key+'/publish',{},ok=False).status_code>=400
 results.append({'阶段':'并发','注入':'发布与终止竞争','HTTP':codes,'终态':terminal,'结果':'通过'})
 # 在途附件上传与封存：已保存上传意图但正文尚未传完时必须拒绝封存。
 key,path,session,repo,body=fixture('loading')
 release=threading.Event();content=b'x'*4096
 meta={'source_id':'slow-file','name':'slow.txt','parent_kind':'comment','parent_id':'8','sha256':hashlib.sha256(content).hexdigest(),'size':len(content),'created':1600000000}
 class SlowBody:
  def __init__(self):self.position=0
  def __len__(self):return len(content)
  def read(self,size=-1):
   if self.position==0:self.position=1;return content[:1]
   if self.position==len(content):return b''
   assert release.wait(30),'测试上传等待超时'
   self.position=len(content);return content[1:]
 def upload():
  return s.post(base+path+'/assets',data=SlowBody(),headers={'X-Migration-Metadata':base64.b64encode(json.dumps(meta).encode()).decode(),'Content-Type':'application/octet-stream'},timeout=60)
 with ThreadPoolExecutor(1) as pool:
  future=pool.submit(upload)
  try:
   for _ in range(100):
    if api('GET',path+'/manifest',ok=False).status_code>=400:break
    time.sleep(.05)
   else:raise AssertionError('未进入在途上传状态')
   assert api('POST',path+'/seal',body,ok=False).status_code>=400
  finally:release.set()
  assert future.result().status_code==200
 results.append({'阶段':'装载','注入':'附件在途时封存','结果':'阻断且上传可完成'})
 # 回执超过一页时，逐页完整性校验必须覆盖所有对象。
 key,path,session,repo,body=fixture('loading')
 for start in (100,150):
  api('POST',path+'/objects',{'objects':[{'kind':'comment','source_id':str(i),'parent_kind':'issue','parent_id':'4','content':'分页评论','author':'来源作者','created':1600000000,'updated':1600000000} for i in range(start,start+50)]})
 pages=[];after=0
 while True:
  page=api('GET',path+'?after_id='+str(after));pages.extend(page['objects'])
  if not page['next_id']:break
  assert page['next_id']>after;after=page['next_id']
 assert len(pages)==103
 results.append({'阶段':'装载','注入':'跨页回执','对象数':103,'结果':'通过'})
 a.output.write_text(json.dumps({'说明':'独立接收端真实 HTTP 与数据库故障注入，未操作生产','数据库':state.get('database','sqlite'),'结果':'通过','场景':results},ensure_ascii=False,indent=2)+'\n')
if __name__=='__main__':main()
