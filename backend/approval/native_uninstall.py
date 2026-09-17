"""为已切换原生治理的项目生成专用旧审批 Hook 卸载包。"""
import hashlib
import io
import json
import zipfile


SCRIPT = r'''#!/usr/bin/env python3
import argparse, configparser, hashlib, json, re, shlex, subprocess, time
from pathlib import Path, PurePosixPath
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

HOOK='gitea-manager-approval'
class NoRedirect(HTTPRedirectHandler):
 def redirect_request(self,*a,**k): raise ValueError('回执接口发生重定向')
def run(args):
 r=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
 if r.returncode: raise RuntimeError('容器命令失败，退出码 '+str(r.returncode))
 return r.stdout
def ack(c,removal):
 data={'repo_id':c['repo_id'],'revision':c['revision'],'nonce':hashlib.sha256((str(time.time_ns())+c['token']).encode()).hexdigest()[:32],
       'timestamp':int(time.time()),'runtime_marker':'gm-approval-v1','hooks_verified':True,'removal':removal}
 endpoint='/installed' if removal else '/uninstall-ready'
 req=Request(c['gate_url']+'/'+str(c['policy_id'])+endpoint,method='POST',data=json.dumps(data).encode(),
             headers={'Authorization':'Bearer '+c['token'],'Content-Type':'application/json'})
 with build_opener(ProxyHandler({}),NoRedirect()).open(req,timeout=8) as response:
  value=json.load(response)
  if value.get('ok') is not True or value.get('nonce')!=data['nonce']: raise ValueError('卸载回执不一致')
def main():
 p=argparse.ArgumentParser();p.add_argument('--container',required=True);p.add_argument('--config',default='config.json');a=p.parse_args()
 root=Path(__file__).resolve().parent; manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))['files']
 for name,digest in manifest.items():
  file=root/name
  if PurePosixPath(name).name!=name or file.is_symlink() or hashlib.sha256(file.read_bytes()).hexdigest()!=digest: raise ValueError('卸载包文件摘要不匹配')
 c=json.load(open(a.config,encoding='utf-8'))
 version=run(['docker','exec','--user','git',a.container,'gitea','--version']).decode()
 if not re.search(r'version 1\.27\.3\+governance(?:\.|\s|$)',version): raise ValueError('仅用于已核对的1.27.3原生治理实例')
 raw=run(['docker','exec','--user','git',a.container,'gitea','admin','governance','repository-storage','--repository-id',str(c['repo_id'])])
 identity=json.loads(raw); path=identity.get('storage_path','')
 if identity.get('repository_id')!=c['repo_id'] or not path.startswith('/') or '..' in PurePosixPath(path).parts: raise ValueError('稳定项目身份核对失败')
 help_text=run(['docker','exec','--user','git',a.container,'gitea','--help']).decode()
 match=re.search(r'CustomPath:\s*(/[^\r\n]+)',help_text)
 ini_match=re.search(r'ConfigFile:\s*(/[^\r\n]+)',help_text)
 if not match or not ini_match: raise ValueError('无法确认Gitea配置目录')
 ini=configparser.RawConfigParser(strict=False); ini.read_string('[gitea_global]\n'+run(['docker','exec','--user','git',a.container,'cat',ini_match.group(1).strip()]).decode())
 if ini.get('server','ROOT_URL',fallback='').rstrip('/')!=c['expected_root_url'].rstrip('/'): raise ValueError('目标容器不属于此Gitea实例')
 if identity.get('full_path')!=c['expected_full_path']: raise ValueError('目标项目完整路径与导出时不一致')
 custom=match.group(1).strip(); lock=custom+'/.gitea-manager-approval-install.lock'; owned=path+'/.gitea-manager-approval'
 run(['docker','exec','--user','git',a.container,'mkdir',lock])
 try:
  onsite_raw=run(['docker','exec','--user','git',a.container,'sh','-c','if [ ! -e "$1" ]; then exit 0; fi; [ -f "$1" ] && [ ! -L "$1" ] || exit 4; cat "$1"','读取现场配置',owned+'/config.json'])
  if not onsite_raw:
   present=[]
   for phase in ('pre-receive','update'):
    target=path+'/hooks/'+phase+'.d/'+HOOK
    exists=run(['docker','exec','--user','git',a.container,'sh','-c','if [ -e "$1" ]; then echo yes; fi','核对受管Hook是否存在',target])
    if exists: present.append(target)
   if present: raise ValueError('现场配置缺失但受管 Hook 仍存在，拒绝推断归属')
   ack(c,False); ack(c,True)
   print(json.dumps({'完成':True,'项目ID':c['repo_id'],'说明':'已确认组件配置与两个受管Hook均不存在，并完成幂等回执'},ensure_ascii=False)); return
  onsite=json.loads(onsite_raw)
  if any(onsite.get(k)!=c[k] for k in ('policy_id','repo_id','revision')): raise ValueError('现场组件不属于此策略或安装版本')
  if hashlib.sha256(str(onsite.get('token','')).encode()).hexdigest()!=c['expected_hook_token_hash']: raise ValueError('现场组件凭据身份不匹配')
  expected={}
  for phase in ('pre-receive','update'):
   target=path+'/hooks/'+phase+'.d/'+HOOK
   command=('exit 0\n' if onsite.get('mode')=='shadow' else 'exec '+shlex.quote(owned+'/gate')+' '+shlex.quote(owned+'/config.json')+' '+phase+' "$@"\n')
   expected[target]=hashlib.sha256(('#!/bin/sh\n# gitea-manager:approval:v1\n'+command).encode()).hexdigest()
   actual=run(['docker','exec','--user','git',a.container,'sh','-c','if [ -e "$1" ]; then [ -f "$1" ] && [ ! -L "$1" ] || exit 4; sha256sum "$1" | cut -d" " -f1; fi','核对受管Hook',target]).decode().strip()
   if actual and actual!=expected[target]: raise ValueError('现场 Hook 内容与此策略不一致，未删除未知内容')
  ack(c,False)
  for target,digest in expected.items():
   script='f=$1; expected=$2; [ ! -e "$f" ] && exit 0; [ -f "$f" ] && [ ! -L "$f" ] || exit 4; actual=$(sha256sum "$f"); actual=${actual%% *}; [ "$actual" = "$expected" ] || exit 5; rm "$f"'
   run(['docker','exec','--user','git',a.container,'sh','-c',script,'条件删除受管Hook',target,digest])
  ack(c,True)
 finally:
  run(['docker','exec','--user','git',a.container,'rmdir',lock])
 print(json.dumps({'完成':True,'项目ID':c['repo_id'],'说明':'已按稳定项目ID移除本组件旧Hook并完成回执'},ensure_ascii=False))
if __name__=='__main__': main()
'''


def build_native_uninstall_bundle(policy, token, gate_url, expected_root_url):
    preview = json.loads(policy.preview_json or '{}')
    config = {'policy_id':policy.id, 'repo_id':policy.repo_id, 'revision':policy.revision,
              'token':token, 'gate_url':gate_url.rstrip('/'),
              'expected_hook_token_hash':policy.hook_token_hash,
              'expected_root_url':expected_root_url.rstrip('/'),
              'expected_full_path':preview.get('full_path') or policy.repo_name}
    files = {'卸载旧审批Hook.py':SCRIPT.encode(), 'config.json':json.dumps(config, ensure_ascii=False, separators=(',',':')).encode()}
    manifest = {'files':{name:hashlib.sha256(value).hexdigest() for name,value in files.items()},
                '说明':'仅用于原生治理切换后，按稳定项目ID移除Gitea Manager旧审批Hook'}
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,value in files.items(): archive.writestr(name,value)
        archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode())
    stream.seek(0)
    return stream
