"""导出不含凭据的实例进度组件；所有依赖随包提供。"""
import hashlib
import io
import json
from pathlib import Path
import zipfile

from .common import ReviewError


def build_assets(repositories):
    bots = sorted({r['bot_id'] for r in repositories if type(r.get('bot_id')) is int and r['bot_id'] > 0})
    if not bots:
        raise ReviewError('请先为该实例配置至少一个 Review 仓库及可信机器人')
    root = Path(__file__).with_name('assets')
    files = {}
    assets = {}
    for kind in ('js', 'css'):
        content = (root / ('progress.' + kind)).read_bytes()
        name = 'progress.' + hashlib.sha256(content).hexdigest()[:16] + '.' + kind
        files[name] = content; assets[kind] = name
    fragment = ('<link rel="stylesheet" href="{{AppSubUrl}}/assets/ai-review-progress/' + assets['css'] + '">\n'
                + '<script defer src="{{AppSubUrl}}/assets/ai-review-progress/' + assets['js']
                + '" data-review-base="{{AppSubUrl}}" data-review-bots="' + ','.join(map(str, bots)) + '"></script>\n')
    # 1.26.4 REST API 不接受浏览器会话。由原生 PR 页面权限保护这段模板数据，
    # 只导出可信机器人的普通评论；源码评论经 Go 模板转义，不进入执行上下文。
    condition = 'or ' + ' '.join('(eq .PosterID ' + str(bot) + ')' for bot in bots)
    fragment += ('{{if and .Issue .Issue.IsPull}}\n<!-- gitea-manager:progress:data:begin -->\n'
        + '<template id="ai-review-page-data" data-repository-id="{{.Repository.ID}}" data-repository="{{.Repository.FullName}}" '
        + 'data-number="{{.Issue.Index}}" data-closed="{{.Issue.IsClosed}}" '
        + 'data-head="{{if .HeadBranchCommitID}}{{.HeadBranchCommitID}}{{else}}{{.PullHeadCommitID}}{{end}}">\n'
        + '{{range .Issue.Comments}}{{if and (eq .Type 0) (' + condition + ')}}'
        + '<div data-comment-id="{{.ID}}" data-author-id="{{.PosterID}}">{{.Content}}</div>'
        + '{{end}}{{end}}\n</template>\n<!-- gitea-manager:progress:data:end -->\n{{end}}\n')
    files['footer.fragment.tmpl'] = fragment.encode()
    files['install.py'] = (root / 'install.py').read_bytes()
    files['安装说明.md'] = '''# 已有 Gitea 容器接入 PR 进度

适配 Gitea 1.26.4（包含 review.1 兼容版本）；增强版 1.27.3（含构建后缀）已放开准入，待内网实际验收。服务器需已有 Python 3 与 Docker CLI；全程无需联网安装依赖。
资源由现有 Gitea 服务提供，浏览器使用自己的登录会话，不包含模型密钥或机器人令牌。

1. 将本包复制至 Gitea 所在服务器，解压至独立目录。
2. 检查：`python3 install.py check --container 实际容器名`。
3. 安装或升级：`python3 install.py install --container 实际容器名`。
4. 重新打开 PR 的对话、提交或文件变更页面，检查进度卡片与浏览器网络请求。
5. 卸载：`python3 install.py uninstall --container 实际容器名`。

自定义镜像、rootless、自定义启动参数请核对站点管理中的 CustomPath 和 app.ini，再传入
`--custom-path /实际目录 --config /实际配置路径 --user 实际运行用户`。脚本要求目录位于可写持久卷，
不会改挂载或重新创建容器；普通官方镜像通常使用 /data/gitea，rootless 通常使用 /var/lib/gitea/custom。

安装器保留已有 footer.tmpl，只管理有标记的组件片段。备份位于 CustomPath/ai-review-progress-backups，
不通过网页提供下载。激活使用 Gitea 原生 manager reload-templates，失败时恢复原模板，不自动重启服务。
若已有自定义 base/footer.tmpl 未调用 custom/footer，请先由维护者恢复这个扩展入口。
若代理单独托管 /assets，需让 /assets/ai-review-progress/ 回源到 Gitea；已有 CSP 需允许本站外部脚本和样式，
不能通过关闭安全策略处理。Gitea 升级后需重新做页面兼容检查。

更换或新增机器人后，重新导出并安装组件包以更新可信机器人 ID。
组件每 5 秒读取一次原生 PR 对话页面，仅解析页脚模板数据，不刷新当前页面或执行返回 HTML。
Gitea 1.26.4 REST API 不接受浏览器登录会话，因此不通过浏览器调用 REST API。
同时部署 Manager 的 review-progress 服务，状态才会持续写回；模型 Worker 不负责状态评论写入。
仅一个实例连接一套 Review 数据库；同一实例请保留一套状态同步服务。
''' .encode()
    manifest = {'说明': '固定文件 SHA-256，不包含任何访问凭据', 'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    files['manifest.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    output.seek(0)
    return output
