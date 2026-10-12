#!/usr/bin/env python3
"""复用本轮隔离实例，通过固定指纹的 SSH 网关验证同一恢复路径。"""
import importlib.util
import os
from pathlib import Path
import time

import paramiko

roots = list(Path('/tmp').glob('operation-acceptance-*/manager.db'))
if len(roots) != 1: raise RuntimeError('无法唯一确定本轮隔离数据库')
os.environ['ACCEPTANCE_ROOT'] = str(roots[0].parent)
spec = importlib.util.spec_from_file_location('acceptance', '/acceptance.py')
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
try:
    with a.app.app_context():
        source = a.db.session.get(a.GiteaServer, 1)
        target = a.db.session.get(a.GiteaServer, 2)
        gateway = a.create('gitea/gitea:1.26.4', 'ssh', entrypoint=['sh', '-c'], command=['sleep 1200'], volumes={'/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}})
        gateway.start(); gateway.reload()
        setup = gateway.exec_run(['sh', '-c', 'apk add --no-cache docker-cli >/tmp/setup.log 2>&1 && ssh-keygen -A && mkdir -p /root/.ssh && chmod 700 /root/.ssh'])
        if setup.exit_code: raise RuntimeError('隔离 SSH 网关依赖准备失败；证据保留于网关 setup.log')
        key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(str(a.root/'ssh-key'))
        a.upload(gateway, '/root/.ssh', 'authorized_keys', key.get_name()+' '+key.get_base64()+'\n')
        result = gateway.exec_run(['/usr/sbin/sshd', '-o', 'PermitRootLogin=prohibit-password', '-o', 'PasswordAuthentication=no', '-o', 'AllowUsers=root'])
        if result.exit_code: raise RuntimeError('隔离 SSH 网关启动失败')
        host = gateway.attrs['NetworkSettings']['Networks']['bridge']['IPAddress']
        pub = gateway.exec_run(['cat', '/etc/ssh/ssh_host_ed25519_key.pub']).output.decode().split()
        (a.root/'known-hosts').write_text(host+' '+pub[0]+' '+pub[1]+'\n')
        for server in (source, target):
            server.is_local = False; server.connection_mode = 'ssh'; server.host = host
        a.db.session.commit()
        sample = a.backup(source, 'ssh-sample.zip')
        a.restore(sample, target)
        # 恢复完成后通过实际 HTTP Git 协议读取私有仓库。
        container = a.client.containers.get(source.gitea_container)
        git = container.exec_run(['git', 'ls-remote', target.gitea_url.replace('http://', 'http://progressadmin:'+source.api_token+'@')+'/progressadmin/sample-0.git'])
        if git.exit_code: raise RuntimeError('SSH 恢复后的私有 Git 协议访问失败')
        a.note('SSH 备份恢复及私有 Git 协议验证通过')
        import json
        Path('/evidence/operation-progress-remote.json').write_text(json.dumps({'说明': '真实 SSH、固定主机指纹与 Docker 容器验收；网关依赖仅用于测试', '检查': a.records}, ensure_ascii=False, indent=2)+'\n')
finally:
    for container in reversed(a.owned):
        try:
            container.reload()
            if container.labels.get('manager.progress.acceptance') == a.marker: container.remove(force=True, v=True)
        except a.docker.errors.NotFound: pass
    a.client.close()
