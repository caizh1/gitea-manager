#!/usr/bin/env python3
"""向独立 Gitea 夹具写入分支、标签与 LFS 历史，再验证只读代码快照。"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from git_migration.providers import Provider
from git_migration.transport import Transport
from git_migration.git_snapshot import GitSnapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-state', type=Path, required=True)
    args = parser.parse_args()
    state = json.loads(args.source_state.read_text())
    spec = importlib.util.spec_from_file_location('fixture', Path(__file__).with_name('git-migration-source-acceptance.py'))
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    container = json.loads(fixture.docker('inspect', state['name']))[0]
    if state['kind'] != 'gitea' or container['Config']['Labels'].get('io.gitea-manager.test') != 'git-migration-source':
        raise RuntimeError('只接受本项目创建的隔离 Gitea 夹具')
    folder = args.source_state.parent / ('code-acceptance-' + str(time.time_ns()))
    folder.mkdir(mode=0o700)
    http = Transport(state['base'])
    try:
        provider = Provider(http, 'gitea', 'source-admin', state['password'])
        project = next(p for p in provider.discover()['projects'] if p['path'].endswith('/private-project'))
        snapshot = GitSnapshot(provider, project, folder / 'snapshot')
        with tempfile.TemporaryDirectory(dir=folder) as name:
            working = Path(name)
            snapshot.git('clone', '--', snapshot.url, str(working))
            snapshot.git('config', 'user.name', '迁移验收作者', cwd=working)
            snapshot.git('config', 'user.email', 'migration@example.test', cwd=working)
            snapshot.git('lfs', 'install', '--local', cwd=working)
            (working / '.gitattributes').write_text('*.bin filter=lfs diff=lfs merge=lfs -text\n')
            original = bytes(range(256)) * 1024
            (working / 'artifact.bin').write_bytes(original)
            snapshot.git('add', '.', cwd=working)
            snapshot.git('commit', '-m', '加入历史构建样本', cwd=working)
            snapshot.git('tag', '-a', 'migration-v1', '-m', '迁移验收标记', cwd=working)
            snapshot.git('checkout', '-b', 'migration-feature', cwd=working)
            updated = original + '第二版构建内容'.encode()
            (working / 'artifact.bin').write_bytes(updated)
            snapshot.git('add', '.', cwd=working)
            snapshot.git('commit', '-m', '更新构建样本', cwd=working)
            snapshot.git('push', '--all', 'origin', cwd=working)
            snapshot.git('push', '--tags', 'origin', cwd=working)
        result = snapshot.capture()
        actual = {r['oid'] for r in result['lfs_objects']}
        expected = {hashlib.sha256(original).hexdigest(), hashlib.sha256(updated).hexdigest()}
        if not expected <= actual or 'refs/heads/migration-feature' not in result['refs'] or 'refs/tags/migration-v1' not in result['refs']:
            raise RuntimeError('代码快照遗漏历史 LFS、分支或标签')
        evidence = {'说明': '真实私有仓库的 Git 与历史 LFS 内容验证，尚非目标迁移验证', '结果': '通过',
                    '引用': result['refs'], '历史LFS对象': result['lfs_objects']}
        (folder / 'acceptance.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
        print('历史 LFS、分支和标签核验通过：' + str(folder / 'acceptance.json'))
    finally:
        http.close()


if __name__ == '__main__':
    main()
