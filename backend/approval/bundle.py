"""导出离线资源；服务令牌仅在经过认证的安装包中出现。"""
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import zipfile

from .rules import ApprovalError, encode


def build_bundle(policy, token, gate_url, isolation, mode):
    u = urlsplit(gate_url)
    if (u.scheme not in ('http', 'https') or not u.netloc or u.username or u.password or u.query or u.fragment
            or u.path.rstrip('/') != '/api/approval-internal'):
        raise ApprovalError('请输入包含 /api/approval-internal 的门禁地址')
    if u.scheme != 'https' and not isolation:
        raise ApprovalError('正式环境的安装回执必须通过 HTTPS，不能明文传输服务令牌')
    root = Path(__file__).parent / 'assets'
    binary = root / 'approval-hook-linux-amd64'
    if not binary.is_file():
        raise ApprovalError('安装包缺少已构建的 Linux amd64 Hook，请先完成候选构建')
    files = {'approval-hook-linux-amd64': binary.read_bytes(), 'install.py': (root/'install.py').read_bytes()}
    names = {}
    for kind in ('js', 'css'):
        content = (root/('widget.'+kind)).read_bytes()
        name = 'widget.'+hashlib.sha256(content).hexdigest()[:16]+'.'+kind
        files[name] = content
        names[kind] = name
    fragment = '<meta name="gm-approval-version" content="gm-approval-v1">\n'
    fragment += '<meta name="gm-approval-viewer" content="{{if .IsSigned}}{{.SignedUser.ID}}{{else}}0{{end}}">\n'
    fragment += ('{{if and .Issue .Issue.IsPull}}\n'
                 '<template id="gm-approval-context" data-repo="{{.Repository.ID}}" data-number="{{.Issue.Index}}" '
                 'data-viewer="{{if .IsSigned}}{{.SignedUser.ID}}{{else}}0{{end}}" data-closed="{{.Issue.IsClosed}}" '
                 'data-head="{{if .HeadBranchCommitID}}{{.HeadBranchCommitID}}{{else}}{{.PullHeadCommitID}}{{end}}"></template>\n'
                 '<link rel="stylesheet" href="{{AppSubUrl}}/assets/manager-approval/'+names['css']+'">\n'
                 '<script defer src="{{AppSubUrl}}/assets/manager-approval/'+names['js']+'" '
                 'data-base="{{AppSubUrl}}" data-server="'+str(policy.server_id)+'"></script>\n{{end}}\n')
    files['footer.fragment.tmpl'] = fragment.encode()
    files['config.json'] = encode({'说明': '包含 Hook 服务凭据；权限必须为 0600，不提交、不进入公开资源目录',
                                  'policy_id': policy.id, 'repo_id': policy.repo_id, 'revision': policy.revision,
                                  'repo_name': policy.repo_name, 'token': token, 'gate_url': gate_url.rstrip('/'),
                                  'mode': mode, 'isolation': isolation,
                                  'branches': json.loads(policy.rule_json)['branches'],
                                  'grandfather': json.loads(policy.grandfather_json)}).encode()
    files['安装说明.md'] = '''# 审批组件候选安装包

本包未完成生产强制审批验收。APR-RACE-001（P2）尚未修复。
宿主机需 Python 3 和 Docker CLI；只支持官方 Gitea 1.26.4 Linux amd64。
安装包含服务令牌，只能放在非公开目录并设置目录 0700、文件 0600。

先执行 `python3 install.py check --container 实际容器名`，确认变更预览；
再执行 `python3 install.py install --container 实际容器名 --preview 安装预览.json`。
只卸载页面用 `uninstall-ui`；停用门禁后用 `uninstall-gate`，不能以页面失败自动解除保护。
回执丢失先执行 check 核对，禁止盲目重新生成令牌或重复覆盖。

页面数据需要同源反向代理：将 Gitea 站点的 `/-/manager-approval/` 转发到
Manager 前端入口的 `/api/approval-view/`，由其 nginx 转发到独立展示服务；保留 Cookie，禁用代理缓存。此路径仅接受 GET。
现有代理必须能够不中断连接地重载；本安装器不改代理配置、不重启 Gitea。
Manager 通过原生 PR 信息接口验证权限、原生个人设置页身份标记验证当前登录人，不向浏览器暴露服务或管理令牌。
未配置此路径时组件显示“状态不可用”，不会宣称规则通过。

强制模式仅供有独立标记的隔离实例测试，存量 PR 清单只适用于本次导出边界。
并发创建 PR 的正式切换边界尚未验收，禁止把此安装包用于生产强制接入。
''' .encode()
    files['manifest.json'] = encode({'说明': '文件级 SHA-256；配置包含凭据，不得公开',
                                    'files': {n: hashlib.sha256(v).hexdigest() for n, v in files.items()}}).encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o100600 << 16
            archive.writestr(info, value)
    stream.seek(0)
    return stream
