#!/usr/bin/env python3
"""在联网准备机生成两套独立 Ubuntu 24.04 amd64 离线安装包。"""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gitea-image', default='gitea-governance-combined:review-20260916-r5')
    parser.add_argument('--manager-tag', default='oneclick-v2')
    parser.add_argument('--executor-image', default='gitea-review-executor:oneclick-v2')
    parser.add_argument('--product', choices=['gitea', 'manager', 'both'], default='both')
    parser.add_argument('--executor-legacy-image', default='gitea-review-executor:oneclick-v2-legacy')
    parser.add_argument('--output-dir', type=Path, default=ROOT/'outputs')
    parser.add_argument('--provenance-dir', type=Path, default=ROOT/'work/oneclick-v2-build')
    args = parser.parse_args()
    deps = ROOT / 'work/oneclick-build/debs'
    if not (deps / 'Packages').is_file():
        raise RuntimeError('请先运行 bash deployment/prepare-ubuntu-deps.sh')
    lock = (deps.parent / 'package.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('另一安装包构建正在运行，未覆盖输出') from None
    products = ['gitea', 'manager'] if args.product == 'both' else [args.product]
    for product in products:
        directory = args.output_dir / (product + '-ubuntu24-amd64-installer')
        directory.mkdir(parents=True, exist_ok=True)
        if (directory / 'state').exists():
            raise RuntimeError('输出目录包含运行状态，拒绝覆盖')
        (directory / 'deployment').mkdir(exist_ok=True)
        (directory / 'images').mkdir(exist_ok=True)
        shutil.copy2(ROOT / 'install.sh', directory / 'install.sh')
        shutil.copy2(ROOT / 'uninstall.sh', directory / 'uninstall.sh')
        shutil.copy2(ROOT / 'deployment/uninstall.py', directory / 'deployment/uninstall.py')
        shutil.copy2(ROOT / 'deployment/manager_uninstall.py', directory / 'deployment/manager_uninstall.py')
        shutil.copy2(ROOT / 'deployment/install.py', directory / 'deployment/install.py')
        shutil.copy2(ROOT / 'backend/services/diagnostics.py', directory / 'deployment/diagnostics.py')
        shutil.copytree(deps, directory / 'runtime/debs', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('partial', 'lock'))
        tags = ({'gitea': args.gitea_image, 'database': 'postgres:17.11-bookworm'} if product == 'gitea' else
                {key: 'gitea-review-' + key + ':' + args.manager_tag for key in ('backend', 'frontend', 'executor', 'svn')})
        if product == 'manager':
            tags['executor'] = args.executor_image
            tags['executor_legacy'] = args.executor_legacy_image
        images = {}
        for key, tag in tags.items():
            info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', '--platform', 'linux/amd64', tag], text=True))[0]
            if info['Architecture'] != 'amd64' or info['Os'] != 'linux':
                raise RuntimeError('镜像架构不匹配：' + tag)
            images[key] = {'tag': tag, 'ids': [info['Id']]}
        subprocess.run(['docker', 'image', 'save', '--platform', 'linux/amd64', '-o', str(directory / 'images/runtime.tar'), *tags.values()], check=True)
        with tarfile.open(directory / 'images/runtime.tar') as archive:
            records = json.load(archive.extractfile('manifest.json'))
            index = json.load(archive.extractfile('index.json')) if 'index.json' in archive.getnames() else {'manifests': []}
            for item in images.values():
                row = next(row for row in records if item['tag'] in row.get('RepoTags', []))
                config_id = 'sha256:' + hashlib.sha256(archive.extractfile(row['Config']).read()).hexdigest()
                descriptor = next((row for row in index['manifests'] if row.get('annotations', {}).get('io.containerd.image.name', '').endswith('/' + item['tag'])), {})
                if item['ids'][0] not in (config_id, descriptor.get('digest')):
                    raise RuntimeError('导出期间镜像身份发生变化，拒绝生成混合版本安装包：' + item['tag'])
                item['ids'].append(config_id)
        if product=='manager':
            provenance=directory/'build-provenance';provenance.mkdir(exist_ok=True)
            for filename in ('base-images.json','source-manifest.json'):
                shutil.copy2(args.provenance_dir/filename,provenance/filename)
        manifest = {'说明': '固定 Linux amd64 镜像；首次安装不构建、不拉取镜像。', 'product': product, 'images': images, 'opencode_versions': ['1.18.7','1.1.51'] if product=='manager' else []}
        (directory / 'installer-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        shutil.copy2(ROOT / 'docs/oneclick-installation.md', directory / 'README.md')
        if product == 'manager':
            shutil.copy2(ROOT / 'docs/organization-access-ldap.md', directory / 'organization-access-ldap.md')
            shutil.copy2(ROOT / 'docs/svn-migration.md', directory / 'svn-migration.md')
            shutil.copy2(ROOT / 'docs/svn-layout-detection.md', directory / 'svn-layout-detection.md')
            shutil.copy2(ROOT / 'docs/svn-project-discovery.md', directory / 'svn-project-discovery.md')
            shutil.copy2(ROOT / 'docs/svn-target-groups.md', directory / 'svn-target-groups.md')
            evidence = directory / 'docs/evidence'
            evidence.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / 'docs/evidence/svn-layout-detection-release.json', evidence / 'svn-layout-detection-release.json')
            shutil.copy2(ROOT / 'docs/evidence/svn-project-discovery-release.json', evidence / 'svn-project-discovery-release.json')
            shutil.copy2(ROOT / 'docs/evidence/svn-target-groups-release.json', evidence / 'svn-target-groups-release.json')
        files = sorted(path for path in directory.rglob('*') if path.is_file() and path.name != 'SHA256SUMS')
        (directory / 'SHA256SUMS').write_text(''.join(sha(path) + '  ' + str(path.relative_to(directory)) + '\n' for path in files))
        target = directory.with_suffix('.tar.gz')
        temporary = target.with_suffix('.tmp')
        with tarfile.open(temporary, 'w:gz', compresslevel=3) as archive:
            for path in files + [directory / 'SHA256SUMS']:
                archive.add(path, arcname=str(path.relative_to(directory)), recursive=False)
        temporary.replace(target)
        Path(str(target) + '.sha256').write_text(sha(target) + '  ' + target.name + '\n')
        print('已生成安装包：' + str(target), flush=True)


if __name__ == '__main__':
    main()
