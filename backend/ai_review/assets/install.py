#!/usr/bin/env python3
"""在已有 Gitea Docker 持久卷中安装、检测或卸载 PR 进度组件；不下载依赖。"""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import time

BEGIN = '<!-- gitea-manager:progress:begin -->'
END = '<!-- gitea-manager:progress:end -->'


def merged_footer(existing, fragment=None):
    if existing.count(BEGIN) != existing.count(END) or existing.count(BEGIN) > 1:
        raise ValueError('自定义模板中的受管理片段不完整，请先人工核对')
    pattern = re.compile(re.escape(BEGIN) + r'.*?' + re.escape(END), re.S)
    if BEGIN in existing:
        if not pattern.search(existing):
            raise ValueError('自定义模板标记顺序无效')
        return pattern.sub(lambda _: BEGIN + '\n' + fragment.rstrip() + '\n' + END if fragment else '', existing)
    return existing + ('\n' if existing and not existing.endswith('\n') else '') + (BEGIN + '\n' + fragment.rstrip() + '\n' + END + '\n' if fragment else '')


def run(args, binary=False, input=None, check=True):
    p = subprocess.run(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode:
        raise RuntimeError('容器操作失败；请检查 Docker 权限、路径和运行用户。')
    return p.stdout if binary else p.stdout.decode('utf-8', errors='strict')


class Installer:
    def __init__(self, args, runner=run, asset_root=None):
        self.args = args
        self.run = runner
        self.asset_root = asset_root
        info = json.loads(self.run(['docker', 'inspect', args.container]))[0]
        if not info['State']['Running']:
            raise ValueError('请先启动目标 Gitea 容器')
        # 不输出完整容器环境，避免泄露数据库密码等部署秘密。
        env = dict(v.split('=', 1) for v in info['Config'].get('Env', []) if '=' in v)
        self.user = args.user or (info['Config'].get('User') if info['Config'].get('User') not in ('', 'root', '0') else 'git')
        self.custom = args.custom_path or env.get('GITEA_CUSTOM')
        self.config = args.config or env.get('GITEA_APP_INI')
        help_text = self.exec(['gitea', '--help'])
        if not self.custom:
            m = re.search(r'CustomPath:\s*(/[^\r\n]+)', help_text)
            self.custom = m[1].strip() if m else None
        if not self.config:
            m = re.search(r'ConfigFile:\s*(/[^\r\n]+)', help_text)
            self.config = m[1].strip() if m else None
        if not self.custom or not self.config or any(not p.startswith('/') or '..' in PurePosixPath(p).parts for p in (self.custom, self.config)):
            raise ValueError('无法确定实际 CustomPath 或 app.ini，请指定 --custom-path 与 --config')
        self.custom = self.custom.rstrip('/')
        mounts = [m for m in info['Mounts'] if self.custom == m['Destination'].rstrip('/') or self.custom.startswith(m['Destination'].rstrip('/') + '/')]
        if not mounts or not max(mounts, key=lambda m: len(m['Destination'])).get('RW'):
            raise ValueError('CustomPath 必须位于可写持久卷中；本脚本不会修改容器挂载或重建容器')
        self.exec(['test', '-r', self.config])
        version = self.exec(['gitea', '--version'])
        if not re.search(r'(?<![\w.+-])(?:1\.26\.4(?:\+review\.1)?|1\.27\.3(?:\+[0-9A-Za-z.-]+)?)(?![\w.+-])', version):
            raise ValueError('当前组件允许 Gitea 1.26.4（含 review.1）及增强版 1.27.3（待内网验收）；其他版本需先完成页面兼容测试')
        self.reload_args = ['gitea', '--config', self.config, '--custom-path', self.custom, 'manager', 'reload-templates']
        help_reload = self.exec(self.reload_args + ['--help'])
        if 'reload-templates' not in help_reload:
            raise ValueError('当前 Gitea 不支持模板热重载；请安排维护窗口后使用人工安装方式')
        self.footer = self.custom + '/templates/custom/footer.tmpl'
        self.folder = self.custom + '/public/assets/ai-review-progress'
        self.backups = self.custom + '/ai-review-progress-backups'

    def exec(self, args, **kwargs):
        return self.run(['docker', 'exec', '--user', self.user, self.args.container, *args], **kwargs)

    def read(self, path):
        return self.exec(['sh', '-c', 'if [ -L "$1" ]; then exit 2; elif [ -f "$1" ]; then cat "$1"; fi', '读取模板', path])

    def write(self, path, content):
        # docker cp 写入现有持久卷内的暂存文件，再以运行用户原子替换。
        target = PurePosixPath(path)
        self.exec(['mkdir', '-p', str(target.parent)])
        staging = target.name + '.install-' + str(time.time_ns())
        raw = content.encode() if isinstance(content, str) else content
        with io.BytesIO() as stream:
            with tarfile.open(fileobj=stream, mode='w') as archive:
                meta = tarfile.TarInfo(staging); meta.size = len(raw); meta.mode = 0o644
                meta.uid = int(self.exec(['id', '-u']).strip()); meta.gid = int(self.exec(['id', '-g']).strip())
                archive.addfile(meta, io.BytesIO(raw))
            self.run(['docker', 'cp', '-a', '-', self.args.container + ':' + str(target.parent)], input=stream.getvalue())
        self.exec(['mv', str(target.parent / staging), path])

    def execute(self):
        print('目标容器：' + self.args.container + '\n自定义目录：' + self.custom + '\n配置文件：' + self.config)
        existing = self.read(self.footer)
        if self.args.action == 'check':
            print('组件状态：' + ('已安装' if BEGIN in existing else '未安装') + '；持久卷与模板热重载能力已确认')
            return
        folder = Path(self.asset_root) if self.asset_root else Path(__file__).resolve().parent
        manifest = json.loads((folder / 'manifest.json').read_text())
        for name, expected in manifest['files'].items():
            if PurePosixPath(name).name != name or hashlib.sha256((folder / name).read_bytes()).hexdigest() != expected:
                raise ValueError('安装包文件校验失败')
        fragment = (folder / 'footer.fragment.tmpl').read_text() if self.args.action == 'install' else None
        modified = merged_footer(existing, fragment)
        # 备份放在非公开目录中；不覆盖已有用户自定义内容。
        self.exec(['mkdir', '-p', self.backups]); self.exec(['chmod', '700', self.backups])
        self.write(self.backups + '/footer-' + str(time.time_ns()) + '.bak', existing)
        if fragment:
            for name in manifest['files']:
                if name.endswith(('.js', '.css')):
                    self.write(self.folder + '/' + name, (folder / name).read_bytes())
        self.write(self.footer, modified)
        try:
            self.exec(self.reload_args)
        except Exception:
            self.write(self.footer, existing)
            self.exec(self.reload_args, check=False)
            raise RuntimeError('模板热重载失败，已恢复原模板；未重启 Gitea') from None
        if not fragment:
            # 仅删除安装器命名的组件资源，保留其他用户文件。
            self.exec(['sh', '-c', 'for f in "$1"/progress.*.js "$1"/progress.*.css; do [ ! -f "$f" ] || rm "$f"; done', '卸载组件资源', self.folder])
        print('已' + ('安装' if fragment else '卸载') + '并热重载模板。请重新打开 PR 页面验证。')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['check', 'install', 'uninstall'])
    p.add_argument('--container', required=True, help='已有 Gitea 容器名称')
    p.add_argument('--custom-path', help='实际 CustomPath；自定义启动参数时显式指定')
    p.add_argument('--config', help='实际 app.ini 路径')
    p.add_argument('--user', help='容器内 Gitea 的运行用户或 UID')
    args = p.parse_args()
    try:
        Installer(args).execute()
    except Exception as exc:
        print('安装未完成：' + str(exc), file=sys.stderr); sys.exit(1)


if __name__ == '__main__':
    main()
