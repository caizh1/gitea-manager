#!/usr/bin/env python3
"""生成可断网部署的平铺交付包；不打入运行数据、凭据或开发缓存。"""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import time

ROOT=Path(__file__).resolve().parents[1]
(ROOT/'work').mkdir(exist_ok=True)
package_lock=(ROOT/'work/AIReview打包.lock').open('a')
try: fcntl.flock(package_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError: raise RuntimeError('另一次离线打包正在执行，未覆盖其临时文件') from None
parser=argparse.ArgumentParser();parser.add_argument('--export-images',action='store_true')
parser.add_argument('--source-image-tag',default='local',help='导出已固定构建的镜像标签，避免并行开发改写 local 标签')
parser.add_argument('--include-legacy-executor',action='store_true')
parser.add_argument('--upgrade-only',action='store_true',help='仅包含应用镜像与升级回退必需文件')
args=parser.parse_args()
def run(command):subprocess.run(command,cwd=ROOT,check=True)
if not args.upgrade_only: run(['python3','scripts/review-runtime-prepare.py'])
offline=ROOT/'offline';offline.mkdir(exist_ok=True)
images=['gitea-review-backend:offline','gitea-review-frontend:offline','gitea-review-executor:offline','gitea-review-svn:offline',
        'python:3.11-slim','nginx:1.31.1','gitea/gitea:1.26.4','postgres:17.11-bookworm']
if args.upgrade_only: images=images[:4]
if args.include_legacy_executor: images.append('gitea-review-executor-legacy:offline')
expected_images={}
if args.export_images:
    for component in (('backend','frontend','executor','svn','executor-legacy') if args.include_legacy_executor else ('backend','frontend','executor','svn')):
        source=('gitea-review-executor:'+args.source_image_tag+'-legacy') if component=='executor-legacy' else 'gitea-review-'+component+':'+args.source_image_tag
        if component.startswith('executor'):
            run(['python3', 'scripts/executor-image-acceptance.py', source,
                 str(ROOT/'work'/('package-'+component+'-acceptance.json'))])
        dest='gitea-review-'+component+':offline'
        expected_images[dest]=json.loads(subprocess.check_output(['docker','image','inspect','--platform','linux/amd64',source],text=True))[0]['Id']
        run(['docker','tag',source,dest])
    run(['docker','image','save','--platform','linux/amd64','-o',str(offline/'运行镜像.tar'),*images])
if not (offline/'运行镜像.tar').is_file():raise RuntimeError('请先用 --export-images 导出已构建的离线镜像')
manifest=[]
with tarfile.open(offline/'运行镜像.tar') as archive:
    exported=json.load(archive.extractfile('manifest.json'))
    index=json.load(archive.extractfile('index.json')) if 'index.json' in archive.getnames() else {'manifests':[]}
    for name in images:
        record=next(r for r in exported if name in r.get('RepoTags',[]))
        raw=archive.extractfile(record['Config']).read();value=json.loads(raw)
        config_id='sha256:'+hashlib.sha256(raw).hexdigest()
        descriptor=next((r for r in index['manifests'] if r.get('annotations',{}).get('io.containerd.image.name','').endswith('/'+name)),None)
        if value['architecture']!='amd64' or value['os']!='linux':raise RuntimeError('离线镜像架构与验收范围不符')
        # 部分 Docker 版本返回 OCI 索引 ID，另一些返回配置 ID；两者均来自该次固定构建。
        if name in expected_images and expected_images[name] not in (config_id, descriptor['digest'] if descriptor else ''):
            raise RuntimeError('导出期间镜像标签发生变化，未生成混合版本交付包：'+name)
        manifest.append({'镜像':name,'ID':descriptor['digest'] if descriptor else config_id,'配置ID':config_id,
                         '架构':value['architecture'],'系统':value['os']})
(offline/'镜像清单.json').write_text(json.dumps({'说明':'本交付包固定镜像；启动使用 --no-build --pull never。','镜像':manifest},ensure_ascii=False,indent=2)+'\n')
roots=['frontend','backend','review-runtime','docs','scripts','offline','docker-compose.yml','compose.ai-review.yml',
       'compose.ai-review.images.yml','compose.tls.yml','compose.review-local.yml','compose.local.yml','compose.remote-backup.yml','deploy.sh','upgrade.sh','install.sh','uninstall.sh','deployment','AGENTS.md','README.md','.dockerignore','.gitignore']
excluded={'node_modules','__pycache__','.git','.DS_Store','.env','work','data'}
if args.upgrade_only:
    roots=['offline/运行镜像.tar','offline/镜像清单.json','scripts/upgrade_runtime.py',
           'scripts/upgrade_volume.py','upgrade.sh']
files=[]
for name in roots:
    path=ROOT/name
    for item in (path.rglob('*') if path.is_dir() else [path]):
        rel=item.relative_to(ROOT)
        if any(p in excluded for p in rel.parts) or not item.is_file():continue
        if item.is_symlink() or item.suffix in ('.pyc','.db','.key','.log') or item.name.endswith(('.db-wal','.db-shm')):continue
        files.append(item)
records=[]
for file in sorted(files):
    sha=hashlib.sha256()
    with file.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):sha.update(block)
    records.append({'文件':str(file.relative_to(ROOT)),'字节':file.stat().st_size,'SHA256':sha.hexdigest()})
manifest_path=ROOT/'docs/AIReview交付清单.json'
# 清单不包含自身，避免自引用哈希；生成包的 SHA-256 单独交付。
records=[r for r in records if r['文件']!='docs/AIReview交付清单.json']
manifest_path.write_text(json.dumps({'说明':'文件级固定交付清单，排除本清单自身。','生成时间UTC':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'文件':records},ensure_ascii=False,indent=2)+'\n')
files=[f for f in files if f!=manifest_path]+[manifest_path]
target=ROOT/'gitea-manager.tar.gz';temp=ROOT/'work/AIReview打包中.tar.gz';temp.parent.mkdir(exist_ok=True)
with tarfile.open(temp,'w:gz',compresslevel=6) as archive:
    for item in sorted(files):archive.add(item,arcname=str(item.relative_to(ROOT)),recursive=False)
    info=tarfile.TarInfo('data');info.type=tarfile.DIRTYPE;info.mode=0o755;archive.addfile(info)
# 工作区可被其他任务修改；实际归档内容必须逐文件符合先前固定的清单。
expected={r['文件']:r['SHA256'] for r in records}
with tarfile.open(temp) as archive:
    for member in archive:
        if not member.isfile() or member.name not in expected: continue
        sha=hashlib.sha256()
        with archive.extractfile(member) as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):sha.update(block)
        if sha.hexdigest()!=expected[member.name]:
            raise RuntimeError('打包期间源码发生变化，未发布混合版本：'+member.name)
temp.replace(target)
sha=hashlib.sha256()
with target.open('rb') as stream:
    for block in iter(lambda:stream.read(1024*1024),b''):sha.update(block)
(ROOT/'gitea-manager.tar.gz.sha256').write_text(sha.hexdigest()+'  gitea-manager.tar.gz\n')
print('离线部署包：'+str(target));print('SHA-256：'+sha.hexdigest());print('字节数：'+str(target.stat().st_size))
