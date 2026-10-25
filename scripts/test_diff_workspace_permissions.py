#!/usr/bin/env python3
"""用真实交付镜像、不同 UID 及只读共享卷验证新建与旧目录的 OpenCode 启动。"""
import argparse
from pathlib import Path
import subprocess
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-image', required=True)
    parser.add_argument('--executor-image', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    volume = 'gm-diff-permission-test-' + uuid.uuid4().hex[:12]

    def run(*command): subprocess.run(command, check=True)

    run('docker', 'volume', 'create', '--label', 'io.gitea-manager.test=diff-permission', volume)
    try:
        run('docker', 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
            '-v', volume+':/app/review-workspaces', '--entrypoint', 'python', args.backend_image,
            '-c', '''import os
from pathlib import Path
from ai_review.diff_review import prepare
assert os.getuid() == 0, '使用交付 Worker 的 root 用户'
root = Path('/app/review-workspaces')
old = root / 'existing'
old.mkdir(mode=0o700)
os.umask(0o077)
for name in ('fresh', 'existing'):
    path = Path(prepare(str(root/name)))
    assert path.stat().st_uid == 0 and path.stat().st_mode & 0o777 == 0o755
print('root 新建目录及旧 0700 目录均已准备为 0755；严格 umask 不影响结果', flush=True)
''')
        for name in ('fresh', 'existing'):
            print('开始验证跨用户只读目录：'+name, flush=True)
            run('docker', 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
                '-v', volume+':/app/review-workspaces:ro',
                '-v', str(root/'deployment/test_diff_review_binary.py')+':/test_diff_review_binary.py:ro',
                '-v', str(root/'backend/ai_review')+':/test-backend/ai_review:ro',
                '-e', 'DIFF_TEST_WORKSPACE=/app/review-workspaces/'+name,
                '--entrypoint', 'python', args.executor_image, '/test_diff_review_binary.py')
    finally:
        run('docker', 'volume', 'rm', volume)


if __name__ == '__main__': main()
