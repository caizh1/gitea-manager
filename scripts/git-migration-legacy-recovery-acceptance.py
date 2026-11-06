#!/usr/bin/env python3
"""复制旧协议隔离夹具，验证发布回执丢失后的升级恢复；不修改原夹具或目标规则。"""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import time
import requests

p=argparse.ArgumentParser(description=__doc__)
for name in ('old-manager','old-target','image-tag','receiver-image','output'):p.add_argument('--'+name,required=True)
a=p.parse_args();out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=False)
def docker(*args):return subprocess.check_output(['docker',*args],text=True).strip()
def info(name):return json.loads(docker('inspect',name))[0]
assert info(a.old_manager+'-backend')['Config']['Labels']['io.gitea-manager.test']=='git-migration-manager'
assert info(a.old_target)['Config']['Labels']['io.gitea-manager.test']=='git-migration-receiver'
name='gm-legacy-recovery-'+secrets.token_hex(4)
for suffix in ('-manager','-target'):docker('volume','create','--label','io.gitea-manager.test=git-migration-recovery',name+suffix)
paused=[]
try:
 for n in (a.old_manager+'-worker',a.old_manager+'-backend',a.old_target):
  if info(n)['State']['Running']:docker('pause',n);paused.append(n)
 for source,destination,path in [(a.old_manager+'-backend',name+'-manager','/app/data'),(a.old_target,name+'-target','/data')]:
  docker('run','--rm','--network','none','--volumes-from',source+':ro','-v',destination+':/snapshot','--entrypoint','sh','gitea-review-git:'+a.image_tag,'-c','cp -a '+path+'/. /snapshot/')
finally:
 for n in reversed(paused):docker('unpause',n)
docker('run','-d','--platform','linux/amd64','--label','io.gitea-manager.test=git-migration-receiver','--name',name,'-p','127.0.0.1::3000','-v',name+'-target:/data',a.receiver_image)
port=info(name)['NetworkSettings']['Ports']['3000/tcp'][0]['HostPort']
url='http://host.docker.internal:'+port
for _ in range(120):
 try:
  if requests.get('http://127.0.0.1:'+port+'/api/v1/version',timeout=2).ok:break
 except requests.RequestException:pass
 time.sleep(1)
else:raise RuntimeError('复制接收端未就绪')
inside='''
import json,sys
from flask import Flask
from models import db,GiteaServer
from git_migration.models import GitMigrationRun,GitMigrationInventory
from git_migration import runs
from svn_migration.common import encode,digest,MigrationError
from svn_migration.guards import target_guard
app=Flask(__name__);app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:////app/data/gitea-manager.db',SQLALCHEMY_TRACK_MODIFICATIONS=False);db.init_app(app)
with app.app_context():
 row=db.session.get(GitMigrationRun,1);plan=json.loads(row.plan_json);cp=json.loads(row.checkpoint_json)
 assert plan['preflight_protocol']==5 and cp['batch_prepared'] and cp['batch_size']>0
 inv=db.session.get(GitMigrationInventory,row.inventory_id);frozen=json.loads(inv.result_json)
 from git_migration.discussion_history import differences
 assert differences(frozen) and not plan.get('compatibility_acknowledgement')
 server=db.session.get(GiteaServer,row.server_id);server.gitea_url=sys.argv[1]
 plan['target']=runs.target_identity(server);row.plan_json=encode(plan)
 row.status='uncertain';row.completed_at=None
 for key,value in cp.items():
  if key.isdigit():value['handed_off']=False
 row.checkpoint_json=encode(cp);db.session.commit()
 try:
  with target_guard(row.server_id):pass
  raise AssertionError('未恢复前未持有互斥')
 except MigrationError:pass
 writes=[];original=runs.Receiver.api
 def api(self,method,*args,**kwargs):
  if method!='GET':writes.append(method);raise AssertionError('恢复阶段出现目标写入')
  return original(self,method,*args,**kwargs)
 runs.Receiver.api=api
 runs.resume(row.id)
 if sys.argv[2]=='before':
  try:runs.execute(row,None,lambda:None)
  except MigrationError as error:
   assert error.code=='compatibility_required';print(json.dumps({'结果':'复现原失败','原因':str(error)},ensure_ascii=False))
  else:raise AssertionError('原版本没有复现阻断')
 else:
  runs.execute(row,None,lambda:None)
  assert row.status=='succeeded' and not runs.public(row)['discussion_history_verified']
  with target_guard(row.server_id):pass
  runs.execute(row,None,lambda:None)
  assert not writes
  print(json.dumps({'结果':'通过','仓库数':cp['batch_size'],'目标写入次数':len(writes),'实例互斥':'已解除','旧历史状态':'未核验'},ensure_ascii=False))
'''
script=out/'recover_fixture.py';script.write_text(inside)
def histories():return hashlib.sha256(docker('exec',name,'sqlite3','-json','/data/gitea/gitea.db','SELECT repo_id,kind,data FROM governance_migration_history ORDER BY id').encode()).hexdigest()
before=histories();results=[]
for phase,tag in [('before','discussion-20261029-r4'),('after',a.image_tag)]:
 result=docker('run','--rm','--platform','linux/amd64','--label','io.gitea-manager.test=git-migration-recovery','-e','PYTHONPATH=/app','-v',name+'-manager:/app/data','-v',str(script)+':/tmp/recover_fixture.py:ro','gitea-review-git:'+tag,'python','/tmp/recover_fixture.py',url,phase)
 (out/(phase+'.log')).write_text(result+'\n');results.append(json.loads(result.splitlines()[-1]))
assert before==histories(),'恢复过程改写了来源历史或旧规则'
(out/'acceptance.json').write_text(json.dumps({'说明':'复制真实旧协议五、20仓库已发布夹具，仅改变克隆环境连接地址并模拟成功回执尚未保存；原夹具保持不变','结果':'通过','检查':results,'原始历史及规则摘要未变':True,'复制接收端':name},ensure_ascii=False,indent=2)+'\n')
print('旧协议真实持久化夹具恢复验收通过：'+str(out/'acceptance.json'))
