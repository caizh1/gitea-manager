#!/usr/bin/env python3
"""审批组件安全安装器：预览、逐文件校验、独立锁、原子替换、有限范围回退。"""
import argparse
import configparser
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import subprocess
import sys
import tarfile
import time
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

BEGIN = '<!-- gitea-manager:approval:begin -->'
END = '<!-- gitea-manager:approval:end -->'
HOOK = 'gitea-manager-approval'


def merged_footer(existing, fragment):
    if existing.count(BEGIN) != existing.count(END) or existing.count(BEGIN) > 1:
        raise ValueError('审批页脚标记不完整，未修改模板')
    pattern = re.compile(re.escape(BEGIN)+r'.*?'+re.escape(END), re.S)
    replacement = BEGIN+'\n'+fragment.rstrip()+'\n'+END if fragment else ''
    if BEGIN in existing:
        if not pattern.search(existing):
            raise ValueError('页脚标记顺序错误')
        return pattern.sub(lambda _: replacement, existing)
    return existing + ('\n' if existing and not existing.endswith('\n') else '') + (replacement+'\n' if fragment else '')


def run(args, input=None):
    result = subprocess.run(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode:
        raise RuntimeError('容器操作未完成（退出码 '+str(result.returncode)+'）；未输出可能含凭据的命令或响应')
    return result.stdout


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('回执接口发生重定向，未转发凭据')


class Installer:
    def __init__(self, container, root, runner=run):
        self.container, self.root, self.run = container, Path(root), runner
        self.manifest = json.loads((self.root/'manifest.json').read_text())['files']
        for name, sha in self.manifest.items():
            file = self.root/name
            if PurePosixPath(name).name != name or file.is_symlink() or hashlib.sha256(file.read_bytes()).hexdigest() != sha:
                raise ValueError('安装包文件摘要不匹配')
        self.config = json.loads((self.root/'config.json').read_text())
        info = json.loads(runner(['docker', 'inspect', container]))[0]
        if not info['State']['Running']:
            raise ValueError('Gitea 必须已经运行')
        image = json.loads(runner(['docker', 'image', 'inspect', info['Image']]))[0]
        if image['Architecture'] != 'amd64' or not any(x.startswith('gitea/gitea@sha256:') for x in image.get('RepoDigests', [])):
            raise ValueError('仅支持有官方摘要的 Gitea Linux amd64 镜像')
        if self.config['mode'] == 'isolated_enforced' and (not self.config['isolation'] or
                info['Config'].get('Labels', {}).get('com.gitea-manager.approval-feasibility') != self.config['isolation']):
            raise ValueError('此强制配置不属于本次独立验收实例')
        self.started = info['State']['StartedAt']
        self.mounts = info['Mounts']
        self.user = 'git'
        version = self.exec(['gitea', '--version']).decode()
        if not re.search(r'version 1\.26\.4(?:\s|$)', version):
            raise ValueError('需要官方 Gitea 1.26.4，不能使用兼容修改版本')
        text = self.exec(['gitea', '--help']).decode()
        def field(name):
            match = re.search(name+r':\s*(/[^\r\n]+)', text)
            if not match:
                raise ValueError('无法确认 Gitea 的 '+name)
            return match[1].strip()
        self.custom, self.ini = field('CustomPath'), field('ConfigFile')
        self.safe_path(self.custom)
        ini = configparser.RawConfigParser(strict=False)
        # Gitea INI 允许首个节之前的 APP_NAME 等全局键；单独收纳，不继承到仓库节。
        ini.read_string('[gitea_global]\n'+self.exec(['cat', self.ini]).decode())
        repo_root = ini.get('repository', 'ROOT', fallback='')
        if not repo_root.startswith('/'):
            raise ValueError('仓库根目录未明确配置为绝对路径，未猜测生产路径')
        name = self.config['repo_name'].lower()
        if not re.fullmatch(r'[a-z0-9_.-]+/[a-z0-9_.-]+', name) or '..' in name:
            raise ValueError('仓库路径不在已验收范围内')
        self.repo = repo_root.rstrip('/')+'/'+name+'.git'
        self.safe_path(self.repo)
        self.exec(['test', '-d', self.repo])
        self.owned = self.repo+'/.gitea-manager-approval'
        self.footer = self.custom+'/templates/custom/footer.tmpl'
        self.staging = self.custom+'/.gitea-manager-approval-staging'
        self.lock = self.custom+'/.gitea-manager-approval-install.lock'
        self.reload = ['gitea', '--config', self.ini, '--custom-path', self.custom, 'manager', 'reload-templates']
        for phase in ('pre-receive', 'update'):
            wrapper = self.read(self.repo+'/hooks/'+phase)
            if not wrapper or not ((phase+'.d').encode() in wrapper or
                    (b'hookname=$(basename $0)' in wrapper and b'/hooks/${hookname}.d/*' in wrapper)):
                raise ValueError('原生 Hook 包装器缺失或不支持子目录，未重建 Hook')

    def exec(self, args, **kwargs):
        return self.run(['docker', 'exec', '--user', self.user, self.container, *args], **kwargs)

    def safe_path(self, path):
        if not path.startswith('/') or '..' in PurePosixPath(path).parts:
            raise ValueError('路径格式错误')
        matches = [m for m in self.mounts if path == m['Destination'] or path.startswith(m['Destination'].rstrip('/')+'/')]
        if not matches or not max(matches, key=lambda m: len(m['Destination']))['RW']:
            raise ValueError('目标路径不是可写持久卷，不修改挂载或重建容器')
        self.exec(['sh', '-c', 'p=$1; while [ "$p" != / ]; do [ ! -L "$p" ] || exit 3; p=${p%/*}; [ -n "$p" ] || p=/; done', '校验路径', path])

    def read(self, path):
        self.safe_path(path)
        return self.exec(['sh', '-c', 'if [ -e "$1" ]; then [ -f "$1" ] || exit 4; cat "$1"; fi', '读取受管文件', path])

    def snapshot(self):
        paths = [self.footer, self.owned+'/config.json']
        paths += [self.repo+'/hooks/'+phase+suffix for phase in ('pre-receive', 'update') for suffix in ('', '.d/'+HOOK)]
        return {'容器': self.container, '启动时间': self.started, '仓库': self.repo, '自定义目录': self.custom,
                '包摘要': hashlib.sha256((self.root/'manifest.json').read_bytes()).hexdigest(),
                '文件': {p: hashlib.sha256(self.read(p)).hexdigest() for p in paths},
                '属性': {p: self.exec(['sh','-c','if [ -e "$1" ]; then stat -c "%a:%u:%g" "$1"; else echo 不存在; fi','读取权限',p]).decode().strip() for p in paths},
                '说明': '安装只增加受管资源、两个 Hook 和页脚片段，不修改原生规则、包装器或代理；仍需真实页面验收'}

    def atomic(self, path, content, mode=0o644, expected=None):
        self.safe_path(path)
        stage_dir = self.owned if path.startswith(self.repo+'/') else self.staging
        self.exec(['mkdir', '-p', stage_dir, str(PurePosixPath(path).parent)])
        self.exec(['chmod', '700', stage_dir])
        name = '安装中-'+secrets.token_hex(16)
        stage = stage_dir+'/'+name
        uid = int(self.exec(['id', '-u']).strip())
        gid = int(self.exec(['id', '-g']).strip())
        with io.BytesIO() as stream:
            with tarfile.open(fileobj=stream, mode='w') as archive:
                meta = tarfile.TarInfo(name); meta.size=len(content);meta.mode=mode;meta.uid=uid;meta.gid=gid
                archive.addfile(meta, io.BytesIO(content))
            self.run(['docker', 'cp', '-a', '-', self.container+':'+stage_dir], input=stream.getvalue())
        try:
            expected = expected if expected is not None else hashlib.sha256(self.read(path)).hexdigest()
            # 暂存始终在扫描目录外；不同设备拒绝，防止 mv 退化为复制半文件。
            self.exec(['sh', '-c', '''set -eu
[ "$(stat -c %d "$1")" = "$(stat -c %d "$(dirname "$2")")" ]
if [ -e "$2" ]; then actual=$(sha256sum "$2"); actual=${actual%% *}; else actual=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855; fi
[ "$actual" = "$3" ] || exit 8
[ ! -L "$2" ]
mv -f "$1" "$2"
''', '原子替换受管文件', stage, path, expected])
        finally:
            self.exec(['rm', '-f', stage])

    def install(self, preview):
        self.exec(['mkdir', self.lock])  # 原子目录锁；中断遗留锁需核对现场后人工解除。
        try:
            if self.snapshot() != preview:
                raise ValueError('预览后现场或安装包变化，保留管理员修改，未继续安装')
            current = self.read(self.owned+'/config.json')
            if current and json.loads(current).get('mode') == 'isolated_enforced':
                raise ValueError('强制组件已存在，请先明确停用，不能直接覆盖')
            existing = self.read(self.footer)
            fragment = (self.root/'footer.fragment.tmpl').read_text()
            # 只允许本组件命名的受管 Hook，不接管同名但无法证明归属的文件。
            for phase in ('pre-receive', 'update'):
                path = self.repo+'/hooks/'+phase+'.d/'+HOOK
                old = self.read(path)
                if old and not old.startswith(b'#!/bin/sh\n# gitea-manager:approval:v1\n'):
                    raise ValueError('同名 Hook 不属于本组件')
            self.atomic(self.owned+'/gate', (self.root/'approval-hook-linux-amd64').read_bytes(), 0o755)
            self.atomic(self.owned+'/config.json', (self.root/'config.json').read_bytes(), 0o600,
                        preview['文件'][self.owned+'/config.json'])
            for phase in ('pre-receive', 'update'):
                header = '#!/bin/sh\n# gitea-manager:approval:v1\n'
                command = ('exit 0\n' if self.config['mode'] == 'shadow' else
                           'exec '+shlex.quote(self.owned+'/gate')+' '+shlex.quote(self.owned+'/config.json')+' '+phase+' "$@"\n')
                path = self.repo+'/hooks/'+phase+'.d/'+HOOK
                self.atomic(path, (header+command).encode(), 0o755, preview['文件'][path])
            for name in self.manifest:
                if re.fullmatch(r'widget\.[0-9a-f]{16}\.(js|css)', name):
                    self.atomic(self.custom+'/public/assets/manager-approval/'+name, (self.root/name).read_bytes())
            self.atomic(self.staging+'/页脚备份-'+str(time.time_ns()), existing, 0o600)
            self.atomic(self.footer, merged_footer(existing.decode(), fragment).encode(), expected=preview['文件'][self.footer])
            self.exec(self.reload)
            # 热重载命令成功仍需读真实运行时；登录页标记验证全局模板，PR 身份另做真实页面验收。
            page = self.exec(['wget', '-q', '-O', '-', 'http://127.0.0.1:3000/user/login'])
            if b'content="gm-approval-v1"' not in page:
                raise ValueError('磁盘已更新但真实页面未出现组件版本，未报告安装成功')
            for phase in ('pre-receive', 'update'):
                path = self.repo+'/hooks/'+phase
                if hashlib.sha256(self.read(path)).hexdigest() != preview['文件'][path]:
                    raise ValueError('安装期间原生包装器发生变化，请核对')
            info = json.loads(self.run(['docker', 'inspect', self.container]))[0]
            if info['State']['StartedAt'] != self.started:
                raise ValueError('安装期间 Gitea 被重启，不能通过无感接入验收')
            self.ack()
            return {'说明': '已核对 Hook 完整性和运行时模板；真实 PR 及持续流量仍需验收', '模式': self.config['mode']}
        finally:
            self.exec(['rmdir', self.lock])

    def ack(self, removal=False, ready=False):
        data = {'repo_id': self.config['repo_id'], 'revision': self.config['revision'],
                'nonce': secrets.token_hex(16), 'timestamp': int(time.time()),
                'runtime_marker': 'gm-approval-v1', 'hooks_verified': True, 'removal': removal}
        endpoint = '/uninstall-ready' if ready else '/installed'
        req = Request(self.config['gate_url']+'/'+str(self.config['policy_id'])+endpoint, method='POST',
                      data=json.dumps(data).encode(), headers={'Authorization': 'Bearer '+self.config['token'], 'Content-Type': 'application/json'})
        with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=8) as response:
            result = json.load(response)
            if result.get('nonce') != data['nonce'] or result.get('ok') is not True:
                raise ValueError('安装回执不一致，先核对现场，不能盲目重装')

    def verify_installed(self):
        """回执丢失时仅核验并补回执，不重复安装，不更换服务凭据。"""
        self.exec(['mkdir',self.lock])
        try:
            if self.read(self.owned+'/config.json') != (self.root/'config.json').read_bytes():
                raise ValueError('现场配置与此包不一致，未补报成功')
            if self.read(self.owned+'/gate') != (self.root/'approval-hook-linux-amd64').read_bytes():
                raise ValueError('现场程序与此包不一致')
            for phase in ('pre-receive','update'):
                command=('exit 0\n' if self.config['mode']=='shadow' else
                         'exec '+shlex.quote(self.owned+'/gate')+' '+shlex.quote(self.owned+'/config.json')+' '+phase+' "$@"\n')
                path=self.repo+'/hooks/'+phase+'.d/'+HOOK
                if self.read(path) != ('#!/bin/sh\n# gitea-manager:approval:v1\n'+command).encode():
                    raise ValueError('现场 Hook 不完整，未补报成功')
                self.exec(['test','-x',path])
            page=self.exec(['wget','-q','-O','-','http://127.0.0.1:3000/user/login'])
            if b'content="gm-approval-v1"' not in page:raise ValueError('运行时模板缺失')
            self.ack()
            return {'说明':'完整性复核与回执完成，没有重新写入 Hook'}
        finally:self.exec(['rmdir',self.lock])

    def uninstall(self, ui):
        self.exec(['mkdir', self.lock])
        try:
            if ui:
                old = self.read(self.footer)
                new = merged_footer(old.decode(), None).encode()
                self.atomic(self.footer, new, expected=hashlib.sha256(old).hexdigest())
                self.exec(self.reload)
                return {'说明': '已移除页面片段；门禁状态未改变'}
            self.ack(ready=True)
            for phase in ('pre-receive', 'update'):
                path = self.repo+'/hooks/'+phase+'.d/'+HOOK
                old = self.read(path)
                if old and not old.startswith(b'#!/bin/sh\n# gitea-manager:approval:v1\n'):
                    raise ValueError('Hook 已被替换，未删除不明内容')
                expected = hashlib.sha256(old).hexdigest()
                self.exec(['sh', '-c', 'if [ -f "$1" ]; then v=$(sha256sum "$1"); [ "${v%% *}" = "$2" ] && rm "$1"; fi', '移除受管门禁', path, expected])
            self.ack(removal=True)
            config_path=self.owned+'/config.json'
            current=self.read(config_path)
            if current:
                if current != (self.root/'config.json').read_bytes():raise ValueError('配置已变更，未清理他人配置')
                self.exec(['sh','-c','v=$(sha256sum "$1"); [ "${v%% *}" = "$2" ] && rm "$1"',
                           '清理已停用凭据',config_path,hashlib.sha256(current).hexdigest()])
            return {'说明': '仅移除本组件 Hook；原生规则、历史数据和页面保持'}
        finally:
            self.exec(['rmdir', self.lock])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check','install','verify-installed','uninstall-ui','uninstall-gate'))
    parser.add_argument('--container', required=True)
    parser.add_argument('--preview', default='安装预览.json')
    args = parser.parse_args()
    try:
        installer = Installer(args.container, Path(__file__).resolve().parent)
        preview = Path(args.preview)
        if args.action == 'check':
            result = installer.snapshot()
            preview.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
            preview.chmod(0o600)
        elif args.action == 'install':
            result = installer.install(json.loads(preview.read_text()))
        elif args.action == 'verify-installed':
            result = installer.verify_installed()
        else:
            result = installer.uninstall(args.action == 'uninstall-ui')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print('安装未完成：'+type(exc).__name__+'；请保留预览与现场核对，不自动重启或覆盖恢复。', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
