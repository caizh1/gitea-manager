"""仅在独立容器复现遗漏存储、外置启动秘密和恢复中断的反例。"""
import importlib.util,io,json,os,signal,time,zipfile,secrets,base64
from pathlib import Path
from unittest.mock import patch
import paramiko
spec=importlib.util.spec_from_file_location('phase1','/scripts/backup-phase1-acceptance.py')
p1=importlib.util.module_from_spec(spec);spec.loader.exec_module(p1);m=p1.m
from services.backup_consistency import database_sql,execute
from services.backup_manifest import identity
from services.restore_secrets import read_file,put_file
from services.restore_isolation import IsolatedGitea
from services.restore_recovery import recover,STATE
from services.gitea_service import do_restore
from models import RestoreTask,Backup
records=[]
def note(label,**values):
 row={'检查':label,**values};records.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
 Path('/evidence/review-results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
def create(image,suffix,**kwargs):
 if image.startswith('gitea/gitea:'):
  kwargs['environment']['GITEA__actions.artifacts__PATH']='/data/actions-external'
  if suffix=='source':
   kwargs['environment'].update({'GITEA__oauth2__JWT_SECRET_URI':'file:/external-config/oauth-jwt','GITEA__oauth2__JWT_SECRET':''})
   kwargs['volumes']={m.marker+'-external': {'bind':'/external-config','mode':'rw'}}
 value=p1.create(image,suffix,**kwargs)
 if image.startswith('gitea/gitea:'):
  m.client.containers.run(value.attrs['Image'],entrypoint=['sh','-ec'],command=['mkdir -p /data/actions-external; chown 1000:1000 /data/actions-external'],volumes_from=[value.id],network_mode='none',remove=True)
 if image.startswith('gitea/gitea:') and suffix=='source':
  put_file(value,'/external-config/oauth-jwt',secrets.token_urlsafe(32).encode(),0o600,1000,1000)
 return value
m.create=create
try:
 with m.app.app_context():
  m.db.create_all()
  source,src,_=m.node('source');target,dst,_=m.node('target')
  keys={}
  for label,server in [('source',source),('target',target)]:
   key=paramiko.RSAKey.generate(3072);keys[label]=key
   p1.api(server,'POST','/user/keys',json={'title':'审查独立密钥','key':key.get_name()+' '+key.get_base64()})
  p1.api(source,'POST','/user/repos',json={'name':'source-data','auto_init':True,'private':True})
  p1.api(target,'POST','/user/repos',json={'name':'old-target','auto_init':True,'private':True})
  execute(src,['mkdir','-p','/data/actions-external'])
  body='审查用 Actions 制品正文'.encode();put_file(src,'/data/actions-external/review-proof.bin',body,0o600,1000,1000)
  original_body='目标原有制品正文'.encode();put_file(dst,'/data/actions-external/old-target.bin',original_body,0o600,1000,1000)
  database=m.client.containers.get(source.pg_container)
  database_sql(database,source,"INSERT INTO action_artifact (run_id,run_attempt_id,repo_id,owner_id,storage_path,file_size,artifact_path,artifact_name,status,expired_unix) VALUES (1,1,1,1,'review-proof.bin',"+str(len(body))+",'review-proof.bin','审查制品',2,4102444800)")
  backup=m.backup(source,'review.zip')
  with zipfile.ZipFile(backup.file_path) as archive:
   manifest=json.loads(archive.read('manager-manifest.json'))
   missing=all('review-proof.bin' not in name for name in archive.namelist())
   assert not missing
   assert 'actions.artifacts' in manifest['准入']['存储']
   assert 'manager-startup-files.json' in archive.namelist()
   external_secret_absent=all('oauth-jwt' not in name for name in archive.namelist())
  note('Actions 兼容配置与正文已纳入备份',备份状态=backup.status,制品正文遗漏=missing,清单包含制品表='action_artifact' in manifest['数据表'],准入遗漏兼容配置='actions.artifacts' not in manifest['准入']['存储'])
  note('外置 OAuth 启动秘密已归档',备份状态=backup.status,存在启动依赖快照=True)
  from services.disaster_bundle import export
  if not os.environ.get('SKIP_EXPORT'):
   export(backup,'/evidence/review-bundle.tar')
   note('含启动依赖的完整离线包已导出')
  task=RestoreTask(backup_id=backup.id,target_server_id=target.id,target_server_name=target.name,status='running');m.db.session.add(task);m.db.session.commit()
  task_id=task.id;target_id=target.id;source_id=source.id;backup_id=backup.id
  m.db.session.remove()
  child=os.fork()
  if child==0:
   with patch.object(IsolatedGitea,'start',lambda self:os.kill(os.getpid(),signal.SIGKILL)):
    do_restore(task_id)
   os._exit(3)
  _,status=os.waitpid(child,0);assert os.WIFSIGNALED(status)
  m.db.session.remove();target=m.db.session.get(m.GiteaServer,target_id);source=m.db.session.get(m.GiteaServer,source_id)
  if os.environ.get('LEGACY_JOURNAL'):
   current=m.client.containers.get(target.gitea_container)
   state=json.loads(read_file(current,STATE)[0]);state.pop('SSH快照')
   put_file(current,STATE,json.dumps(state,ensure_ascii=False).encode(),0o600,0,0)
   note('按旧版本中断日志执行接管，未使用新增 SSH 快照')
  from services import commit_service
  health=commit_service._require_target_health
  def fresh(server): p1.wait_ready(server);return health(server)
  with patch.object(commit_service,'_require_target_health',fresh): message=recover(target)
  dst=m.client.containers.get(target.gitea_container)
  authorized=read_file(dst,'/data/git/.ssh/authorized_keys')[0].decode()
  note('SSH 授权改写后中断并回退',恢复结果=message,原目标API可用=p1.api(target,'GET','/repos/progressadmin/old-target').status_code==200,源公钥仍在授权文件=keys['source'].get_base64() in authorized,目标公钥仍在授权文件=keys['target'].get_base64() in authorized)
  host=dst.attrs['NetworkSettings']['Networks'][os.environ['ACCEPTANCE_NETWORK']]['IPAddress']
  hostkey=read_file(dst,'/data/ssh/ssh_host_ed25519_key.pub')[0].decode().split()
  results={}
  for label,key in keys.items():
   client=paramiko.SSHClient();client.get_host_keys().add(host,hostkey[0],paramiko.Ed25519Key(data=base64.b64decode(hostkey[1])))
   try:
    client.connect(host,username='git',pkey=key,look_for_keys=False,allow_agent=False,timeout=10)
    stdin,stdout,stderr=client.exec_command("git-upload-pack 'progressadmin/old-target.git'",timeout=15)
    data=stdout.channel.recv(2048)
    results[label]={'认证成功':True,'收到目标仓库引用':b'HEAD' in data or b'refs/heads/' in data}
   except Exception as exc: results[label]={'认证成功':False,'异常类型':type(exc).__name__}
   finally: client.close()
  note('回退后实际 SSH 访问检查',结果=results)
  assert not results['source']['认证成功']
  assert results['target']['收到目标仓库引用']
  assert keys['source'].get_base64() not in authorized
  assert keys['target'].get_base64() in authorized
  assert read_file(dst,'/data/actions-external/old-target.bin')[0]==original_body
  assert dst.exec_run(['test','-e','/data/actions-external/review-proof.bin']).exit_code==1
  from services import backup_actions
  verify=backup_actions.verify_artifacts
  def corrupt(helper,snapshot):
   put_file(helper,'/data/actions-external/review-proof.bin','故障注入的损坏正文'.encode(),0o600,1000,1000)
   return verify(helper,snapshot)
  backup=m.db.session.get(Backup,backup_id)
  task=RestoreTask(backup_id=backup_id,target_server_id=target.id,target_server_name=target.name,status='running')
  m.db.session.add(task);m.db.session.commit()
  with patch.object(backup_actions,'verify_artifacts',corrupt): do_restore(task.id)
  assert task.status=='failed' and 'Actions 正文与停写点不一致' in task.error_msg
  dst=m.client.containers.get(target.gitea_container);p1.wait_ready(target)
  assert read_file(dst,'/data/actions-external/old-target.bin')[0]==original_body
  assert keys['target'].get_base64() in read_file(dst,'/data/git/.ssh/authorized_keys')[0].decode()
  assert p1.api(target,'GET','/repos/progressadmin/old-target').status_code==200
  note('实际写入损坏被正文摘要拦截并回退',原目标数据库可用=True,原制品正文一致=True,原SSH授权一致=True)
  task=m.restore(backup,target)
  dst=m.client.containers.get(target.gitea_container)
  assert read_file(dst,'/data/actions-external/review-proof.bin')[0]==body
  count=database_sql(m.client.containers.get(target.pg_container),target,"SELECT count(*) FROM action_artifact WHERE storage_path='review-proof.bin'")
  assert count=='1'
  note('完整恢复保留 Actions 正文与数据库记录',恢复状态=task.status,记录数=int(count),正文一致=True)

finally:
 sources=set()
 for container in m.owned:
  try: container.reload();sources.update(x['Source'] for x in container.attrs['Mounts'])
  except Exception: pass
 for container in m.client.containers.list(all=True,filters={'label':'gitea.manager.operation'}):
  if any(x['Source'] in sources for x in container.attrs.get('Mounts',[])): container.remove(force=True)
 for container in reversed(m.owned):
  try: container.remove(force=True,v=True)
  except Exception: pass

 try: m.client.volumes.get(m.marker+'-external').remove()
 except Exception: pass
