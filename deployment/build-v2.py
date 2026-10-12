#!/usr/bin/env python3
"""从固定已交付基础镜像及可审查源码重建；不发送运行数据与凭据。"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT=Path(__file__).resolve().parents[1]
ASSETS=ROOT/'work/oneclick-v2-assets'
BUILD=ROOT/'work/oneclick-v2-build'


def command(*args):
    subprocess.run(args,cwd=ROOT,check=True)


def main():
    global BUILD
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag',default='oneclick-v2')
    parser.add_argument('--build-dir',type=Path,default=BUILD)
    args=parser.parse_args()
    BUILD=args.build_dir.resolve()
    if BUILD.exists() and any(BUILD.iterdir()):
        raise RuntimeError('构建目录非空，请使用新的目录，避免混入已删除的旧源码')
    archive=ASSETS/'opencode-1.1.51.tar.gz'
    if hashlib.sha256(archive.read_bytes()).hexdigest()!='4b2c38b0a9af96598422c545b8b951ad91866c3673b688414064d2c979b605f3':
        raise RuntimeError('官方旧版 OpenCode 校验失败')
    BUILD.mkdir(parents=True,exist_ok=True)
    bases={}
    for tag in ('gitea-review-executor:oneclick-20260921','gitea-review-backend:integration-default-off-20260921','gitea-review-frontend:integration-default-off-20260921'):
        bases[tag]=json.loads(subprocess.check_output(['docker','image','inspect',tag]))[0]['Id']
    (BUILD/'base-images.json').write_text(json.dumps({'说明':'本次可复核构建使用的固定基础镜像身份','镜像':bases},ensure_ascii=False,indent=2)+'\n')
    runtime=BUILD/'runtime';runtime.mkdir(exist_ok=True)
    for p in (ROOT/'review-runtime').iterdir():
        if p.suffix in ('.py','.js','.json') and p.is_file():shutil.copyfile(p,runtime/p.name)
    shutil.copytree(ASSETS/'legacy-deps',BUILD/'legacy-deps',dirs_exist_ok=True)
    with tarfile.open(archive) as t:
        members=[m for m in t if m.isfile() and Path(m.name).name=='opencode']
        if len(members)!=1:raise RuntimeError('旧版归档结构不匹配')
        (BUILD/'opencode-legacy').write_bytes(t.extractfile(members[0]).read())
    (BUILD/'Dockerfile').write_text('''FROM gitea-review-executor:oneclick-20260921
USER 0:0
RUN rm -f /opt/review/*.py /opt/review/*.js /opt/review/*.json
COPY runtime/ /opt/review/
COPY legacy-deps/ /opt/review/legacy-deps/
ENV PYTHONDONTWRITEBYTECODE=1 OPENCODE_EXPECTED_VERSION=1.18.7
USER 10001:10001
''')
    command('docker','build','--platform','linux/amd64','-t','gitea-review-executor:'+args.tag,str(BUILD))
    (BUILD/'Dockerfile.legacy').write_text('''FROM gitea-review-executor:'''+args.tag+'''
USER 0:0
COPY --chmod=555 opencode-legacy /opt/review/opencode
ENV OPENCODE_EXPECTED_VERSION=1.1.51
USER 10001:10001
''')
    command('docker','build','--platform','linux/amd64','-f',str(BUILD/'Dockerfile.legacy'),'-t','gitea-review-executor:'+args.tag+'-legacy',str(BUILD))
    shutil.copytree(ROOT/'backend',BUILD/'backend',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.db','*.log','data','wheels','tests'))
    shutil.copytree(ROOT/'backend/wheels',BUILD/'wheels',dirs_exist_ok=True)
    (BUILD/'Dockerfile.backend').write_text('''FROM gitea-review-backend:integration-default-off-20260921
RUN rm -rf /app && mkdir /app
COPY backend/ /app/
COPY wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-index --find-links=/tmp/wheels -r /app/requirements.txt && rm -rf /tmp/wheels
''')
    command('docker','build','--platform','linux/amd64','-f',str(BUILD/'Dockerfile.backend'),'-t','gitea-review-backend:'+args.tag,str(BUILD))
    if (BUILD/'dist').exists():shutil.rmtree(BUILD/'dist')
    shutil.copytree(ROOT/'frontend/dist',BUILD/'dist')
    for name in ('nginx.conf','nginx-https.conf','start-nginx.sh'):
        shutil.copyfile(ROOT/'frontend'/name,BUILD/name)
    (BUILD/'Dockerfile.frontend').write_text('''FROM gitea-review-frontend:integration-default-off-20260921
RUN rm -rf /usr/share/nginx/html/*
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY nginx-https.conf /etc/nginx/manager-https.conf
COPY start-nginx.sh /usr/local/bin/start-manager-nginx
CMD ["sh", "/usr/local/bin/start-manager-nginx"]
COPY dist/ /usr/share/nginx/html/
''')
    command('docker','build','--platform','linux/amd64','-f',str(BUILD/'Dockerfile.frontend'),'-t','gitea-review-frontend:'+args.tag,str(BUILD))
    records={str(p.relative_to(BUILD)):hashlib.sha256(p.read_bytes()).hexdigest() for p in BUILD.rglob('*') if p.is_file() and p.name!='source-manifest.json'}
    (BUILD/'source-manifest.json').write_text(json.dumps({'说明':'构建输入的 SHA-256 清单','文件':records},ensure_ascii=False,indent=2)+'\n')
    for tag,identity in bases.items():
        if json.loads(subprocess.check_output(['docker','image','inspect',tag]))[0]['Id']!=identity:raise RuntimeError('构建期间基础镜像身份变化')


if __name__=='__main__':main()
