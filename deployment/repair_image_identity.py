#!/usr/bin/env python3
"""为指定原版安装包应用镜像身份兼容补丁，保留安装清单与全部运行状态。"""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path, data, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def apply_repair(target, bundle):
    target, bundle = target.resolve(), bundle.resolve()
    manifest = json.loads((bundle / 'repair-manifest.json').read_text())
    allowed = {'deployment/install.py', 'deployment/image_identity.py', 'SHA256SUMS'}
    if {row['路径'] for row in manifest['文件']} != allowed:
        raise RuntimeError('补丁文件范围不正确')
    if sha(target / 'installer-manifest.json') != manifest['目标清单SHA256']:
        raise RuntimeError('不是本补丁对应的原版安装包，未修改任何文件')
    with ExitStack() as stack:
        locks = [target / '.lifecycle.lock']
        if (target / 'state').is_dir():
            locks.append(target / 'state/install.lock')
        for path in locks:
            stream = stack.enter_context(path.open('a'))
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('安装或卸载正在运行，请等待退出后再修复') from None
        pending = []
        for row in manifest['文件']:
            path = target / row['路径']
            source = bundle / 'payload' / row['路径']
            if path.is_symlink() or path.parent.is_symlink() or source.is_symlink():
                raise RuntimeError('补丁目标或来源不能为符号链接：' + row['路径'])
            if sha(source) != row['新SHA256']:
                raise RuntimeError('补丁文件校验失败：' + row['路径'])
            actual = sha(path) if path.exists() else None
            if actual not in (row['原SHA256'], row['新SHA256']):
                raise RuntimeError('原文件已被修改，保留现场：' + row['路径'])
            pending.append((path, source.read_bytes(), path.stat().st_mode & 0o777 if path.exists() else 0o644))
        # 校验全部通过后才修改；校验表最后替换，异常中断后可重跑同一补丁。
        for path, data, mode in sorted(pending, key=lambda entry: entry[0].name == 'SHA256SUMS'):
            atomic_write(path, data, mode)
    print('镜像身份兼容补丁已应用；安装清单、账号、密钥和运行数据均未修改。')
    print('请在原安装目录重新执行：sudo bash install.sh')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='之前安装失败的原目录')
    args = parser.parse_args()
    apply_repair(args.directory, Path(__file__).resolve().parent)
