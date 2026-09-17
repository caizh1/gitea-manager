#!/usr/bin/env python3
"""从交付包启动全新断网验收实例；不复用现有容器、数据卷或凭据。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import tarfile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',required=True,type=Path)
    parser.add_argument('--destination',required=True,type=Path)
    parser.add_argument('--state',required=True,type=Path)
    parser.add_argument('--project',required=True)
    parser.add_argument('--gitea-port',required=True,type=int)
    parser.add_argument('--manager-port',required=True,type=int)
    parser.add_argument('--fixture-port',required=True,type=int)
    parser.add_argument('--network-start',required=True,type=int,help='使用 10.253.N.0/24 起的三个空闲网段')
    parser.add_argument('--phases',default='boundaries,cumulative,duplicates')
    args=parser.parse_args()
    if not args.project.startswith('gitea-review-direct-') or not 1<=args.network_start<=252:
        parser.error('只允许独立直接发布验收实例和有效网段')
    dest=args.destination.resolve();state=args.state.resolve();archive=args.archive.resolve()
    if dest.exists() or state.exists():
        raise RuntimeError('验收必须使用不存在的解压目录和状态目录，未覆盖现场')
    for kind in ('container','volume','network'):
        command=['docker',kind,'ls','-q','--filter','label=com.docker.compose.project='+args.project]
        if subprocess.check_output(command,text=True).strip():
            raise RuntimeError('已有同名容器、卷或网络，不能宣称干净部署')
    digest=hashlib.sha256()
    with archive.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    sha=digest.hexdigest()
    expected=Path(str(archive)+'.sha256').read_text().split()[0]
    if sha!=expected:raise RuntimeError('部署包 SHA-256 不匹配')
    dest.mkdir(parents=True)
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            target=(dest/member.name).resolve()
            if not target.is_relative_to(dest) or not (member.isfile() or member.isdir()):
                raise RuntimeError('部署包包含不安全路径或特殊文件')
        bundle.extractall(dest)
    manifest=json.loads((dest/'docs/AIReview交付清单.json').read_text())
    for row in manifest['文件']:
        digest=hashlib.sha256()
        with (dest/row['文件']).open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
        if digest.hexdigest()!=row['SHA256']:raise RuntimeError('文件级校验失败：'+row['文件'])
    subprocess.run(['docker','load','-i',str(dest/'offline/运行镜像.tar')],check=True)
    os.environ['REVIEW_STATE_DIR']=str(state)
    local=runpy.run_path(str(dest/'scripts/review-local.py'))
    values=local['values']();override=state/'断网配置.yml';n=args.network_start
    override.write_text('''# 全新离线验收；仅入口代理连接宿主机，其余服务无公网出口。
services:
  gitea:
    ports: !reset []
  frontend:
    ports: !reset []
  review-model-fixture:
    ports: !reset []
networks:
  default:
    internal: true
    ipam:
      config: [{subnet: 10.253.%d.0/24}]
  review-execution:
    internal: true
    ipam:
      config: [{subnet: 10.253.%d.0/24}]
  test-entry:
    ipam:
      config: [{subnet: 10.253.%d.0/24}]
''' % (n,n+1,n+2))
    values.update(REVIEW_PROJECT=args.project,REVIEW_OFFLINE='true',REVIEW_IMAGE_TAG='offline',
                  REVIEW_GITEA_PORT=str(args.gitea_port),REVIEW_MANAGER_PORT=str(args.manager_port),
                  REVIEW_FIXTURE_PORT=str(args.fixture_port),REVIEW_MODEL_HOSTS='review-model-fixture',
                  COMPOSE_PROFILES='offline,fixture',REVIEW_COMPOSE_OVERRIDE=str(override))
    local['save'](values)
    local['compose']('up','-d','--no-build','--pull','never',capture=False)
    h=local['Harness']();h.initialize()
    initial=h.manager('GET','/ai-review/config')['config']
    if initial['enabled'] or initial['api_key_configured']:raise RuntimeError('干净部署未默认关闭或带入了密钥')
    h.configure_model('http://review-model-fixture:5053/v1','review-fixture','opencode_cli',values['INTEGRATION_TOKEN'])
    subprocess.run(['python3',str(dest/'scripts/review-boundary-acceptance.py'),'--name','package-fixtures','--phases',args.phases],check=True,cwd=dest)
    # 在实际 Worker 内检查外部 TCP 连接被断网配置阻断。
    probe="import socket\ntry:\n socket.create_connection(('1.1.1.1',443),timeout=2).close(); raise SystemExit(1)\nexcept OSError: print('公网 TCP 连接被阻止')"
    local['compose']('exec','-T','review-worker','python','-c',probe)
    report={'通过':True,'说明':'从指定 SHA-256 离线包导入镜像并在全新原生卷上冷启动；真实 Git/PR/Webhook/OpenCode/Review，模型为本机协议替身。',
            '部署包SHA256':sha,'项目':args.project,'Gitea版本':h.gitea('GET','/version')['version'],
            '默认全局关闭':True,'默认没有模型密钥':True,'Worker公网TCP被阻止':True,'测试阶段':args.phases,
            'Manager':h.manager_url,'Gitea':h.gitea_url,'限制':'不代表内部模型质量或请求处理期间并发 Push 的位置保证。'}
    (state/'离线包部署验收.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('离线包干净部署验收通过：'+str(state/'离线包部署验收.json'))


if __name__=='__main__':main()
