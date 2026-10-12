#!/usr/bin/env python3
"""汇集已生成的离线安装包、中文说明与验收证据，并重新计算交付校验值。"""
import hashlib
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'outputs/gitea-oneclick-ubuntu24-amd64-20260921-v2'
TARGET.mkdir(parents=True, exist_ok=True)
for product in ('gitea', 'manager'):
    name = product + '-ubuntu24-amd64-installer.tar.gz'
    for suffix in ('', '.sha256'):
        shutil.copy2(ROOT / 'outputs' / (name + suffix), TARGET / (name + suffix))
for name in ('manager-uninstall-fix.tar.gz', 'manager-uninstall-fix.tar.gz.sha256'):
    source = ROOT / 'outputs' / name
    if source.is_file():
        shutil.copy2(source, TARGET / name)
for source, name in [('docs/oneclick-installation.md', 'README.md'),
                     ('docs/oneclick-v2-acceptance.md', 'acceptance-report.md')]:
    shutil.copy2(ROOT / source, TARGET / name)
evidence = TARGET / 'evidence'
evidence.mkdir(exist_ok=True)
for source in (ROOT / 'docs/evidence').glob('oneclick-v2-*'):
    if source.is_file():
        shutil.copy2(source, evidence / source.name)
diagnostics = evidence / 'diagnostics'
diagnostics.mkdir(exist_ok=True, mode=0o700)
diagnostics.chmod(0o700)
for source in (ROOT / 'work/oneclick-v2-blind-diagnostics-final').glob('*.tar.gz'):
    target = diagnostics / source.name
    shutil.copy2(source, target)
    target.chmod(0o600)
readme = TARGET / 'README.md'
readme.write_text('> 当前为候选包：完整验收尚未通过。请先阅读 acceptance-report.md 的冷重启未通过项和未验证边界。\n\n' + readme.read_text())
checks = []
for file in sorted(TARGET.rglob('*')):
    if not file.is_file() or file.name == 'SHA256SUMS':
        continue
    digest = hashlib.sha256()
    with file.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    checks.append(digest.hexdigest() + '  ' + str(file.relative_to(TARGET)))
(TARGET / 'SHA256SUMS').write_text('\n'.join(checks) + '\n')
print('本机交付目录：' + str(TARGET))
print('U 盘复制与回读校验必须另外完成，生成目录不表示已交付到 U 盘。')
