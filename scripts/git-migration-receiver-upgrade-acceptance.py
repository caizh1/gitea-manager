#!/usr/bin/env python3
"""只替换带隔离标记的接收端镜像，保留原容器、原卷、配置、地址和凭据。"""
import argparse,json,secrets,subprocess,time
from pathlib import Path
import requests

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state',type=Path,required=True);p.add_argument('--image',required=True);a=p.parse_args()
    s=json.loads(a.state.read_text());n=s['name'];run=lambda *v:subprocess.check_output(['docker',*v],text=True).strip();info=json.loads(run('inspect',n))[0]
    assert info['Config']['Labels'].get('io.gitea-manager.test')=='git-migration-receiver'
    assert len(info['Mounts'])==1 and info['Mounts'][0]['Name']==n and info['Mounts'][0]['Destination']=='/data'
    assert info['HostConfig']['PortBindings']['3000/tcp']==[{'HostIp':'127.0.0.1','HostPort':s['base'].rsplit(':',1)[1]}]
    run('stop','-t','20',n);run('rename',n,n+'-before-'+secrets.token_hex(3))
    args=['run','-d','--platform','linux/amd64','--name',n,'--label','io.gitea-manager.test=git-migration-receiver','--memory','768m','--cpus','2','-v',n+':/data','-p','127.0.0.1:'+s['base'].rsplit(':',1)[1]+':3000']
    for e in info['Config']['Env']:
        if e.startswith('GITEA__'):args+=['-e',e]
    run(*args,a.image)
    client=requests.Session();client.trust_env=False
    for _ in range(90):
        try:
            if client.get(s['base']+'/api/v1/version',timeout=2).ok:break
        except requests.RequestException:pass
        time.sleep(1)
    else:raise RuntimeError('升级后隔离接收端未就绪')
    print('隔离接收端已替换固定镜像，原卷和原容器均保留')

if __name__=='__main__':main()
