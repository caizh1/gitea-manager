# Gitea PR 实时进度使用与部署

本组件适配 Gitea 1.26.4 和已交付的 1.26.4+review.1。进度显示使用官方自定义模板、静态资源和模板热重载，不修改或重新编译 Gitea。当前行内评论使用本地校验后的直接 REST 发布，同样不依赖 Gitea 补丁。

## 开发者使用

安装后首次重新打开 PR 的对话、提交或文件变更页，标题下会出现 AI Code Review 卡片。随后每 5 秒更新卡片，保留正在输入的评论；切到后台标签页暂停读取，回来立即读取。排队显示接收时间与目标 SHA；执行显示开始时间、耗时、阶段和真实完成单元数；完成显示结束时间、问题数、覆盖情况与正式 Review 链接。不估算百分比和剩余时间。

分析与反证共同完成后才增加完成计数；达到调用预算而跳过的单元不会计入完成。缺少证据、失败或部分覆盖不表示全部代码通过。Worker 租约失效显示“工作进程中断，等待恢复”，发布阶段中断显示待核对。新 SHA 到达后，旧结果明确作为历史版本；关闭 PR 或开关显示停止。

没有安装组件也可以刷新 PR，读取同一条机器人状态评论。组件未收到可信评论时显示“尚未收到审查状态”，读取失败时提示当前状态可能已过期。卡片只提供辅助信息，不添加合并门禁。

## 给已有内网 Gitea 容器安装

先部署新 Manager、Worker 和独立 `review-progress` 服务。生产配置已加入 `compose.ai-review.yml`，镜像映射加入 `compose.ai-review.images.yml`。同步进程复用后端镜像和 Review SQLite 持久卷，不挂载 Manager 数据库、Docker socket 或执行器凭据，不调用模型。升级前等待当前任务结束，再协调替换旧 Worker，避免旧版 Publisher 和新进度进程同时维护总体评论。

1. Manager → AI Code Review → 仓库管理 → **下载 PR 进度组件**。同一实例导出的包包含该实例已配置机器人的数字 ID，不含任何密钥。
2. 将 ZIP 带入 Gitea 所在服务器并解压。服务器需预装 Python 3 和 Docker CLI，不运行 pip/npm，不需要公网。
3. 将下面 `gitea` 替换为已经运行的实际容器名，先检查再安装：

```bash
python3 install.py check --container gitea
python3 install.py install --container gitea
```

安装器自动识别 CustomPath 和 app.ini，要求 CustomPath 位于现有可写持久卷。普通官方镜像通常是 `/data/gitea`。脚本增量维护 `templates/custom/footer.tmpl` 中带标记的片段，将哈希命名的 JS/CSS 放入 `public/assets/ai-review-progress/`，保留原自定义内容，再执行 `gitea manager reload-templates`。不重新创建容器、不改卷、不停止 Gitea；首次检查失败时也不会自动改部署架构。

自定义启动路径可显式传入：

```bash
python3 install.py install --container gitea \
  --custom-path /实际自定义目录 --config /实际配置路径 --user git
```

备份位于 CustomPath 下非公开的 `ai-review-progress-backups` 目录，权限 700。模板热重载失败会恢复原模板。可重复安装升级；换机器人后重新导出安装。卸载只移除受管理片段和组件资源：

```bash
python3 install.py uninstall --container gitea
```

如果容器尚无持久化的 CustomPath，脚本会拒绝安装；需先由现有部署维护者添加持久挂载，这种情况不能承诺无需维护窗口。rootless 和自定义镜像需要显式核对路径与权限，本轮没有完成这些环境的实机验收。多个 Gitea Web 副本需逐副本安装/热重载，并维持同版本资源。

已有自定义 `base/footer.tmpl` 必须保留官方 `custom/footer` 调用。反向代理若单独托管 `/assets`，将 `/assets/ai-review-progress/` 回源到 Gitea；部署在子路径时前面加实际子路径。模板使用 `AppSubUrl` 生成地址。已有 CSP 应允许本站的外部脚本和样式；不使用内联脚本，不建议关闭 CSP。Gitea 升级后先做兼容验证，安装器当前拒绝未验收的版本。

## 会话、身份与状态一致性

Gitea 1.26.4 REST API 不接受普通浏览器登录 Cookie。真实页面测试确认这一点后，组件改为用当前会话读取本 PR 的**原生对话页面**，由 Gitea 自身执行仓库/PR 权限检查。受管理页脚仅提供当前仓库、PR、实际 head 及可信机器人普通评论的转义文本。组件只解析该模板元素，绝不插入或执行整页 HTML，不向浏览器下发机器人 Token，也不开放匿名 Manager 状态接口。

状态评论包含简短中文说明与版本 1 的机器数据：任务和执行代次、仓库/PR、SHA、阶段、计数、时间和正式 Review 引用。浏览器再次校验机器人、身份、格式、版本和 SHA，使用文本节点展示，只允许当前 PR 的 `issuecomment-N` 结果链接。普通开发者复制机器数据也不能伪造机器人作者。

数据库触发器在任务状态变化的同一事务递增待同步序号。独立进程按 PR 加锁并合并中间变化，成功写入后至少间隔 5 秒；运行心跳最多每 30 秒同步一次。发布前重新读取代次，写入期间新变化保留待同步。状态同步失败单独退避重试，既不改变正式 Review 的成功状态，也不阻塞模型或 Webhook。

创建评论前持久化“不确定创建”标记。响应丢失或进程退出时只回读接管，不盲目再 POST；明确的 4xx 拒绝可重试。如果原请求结果始终无法确认，会保留同步错误，由管理员先核对 Gitea 评论和服务端记录；不能直接清空标记来碰运气。旧总体评论可按机器人和 PR 归属接管，开发者评论不会被覆盖。历史任务缺失的开始时间不会编造，源码保留期后缺失的问题统计也不会显示成零问题。

原生页面读取比独立 JSON 接口更重，每个前台 PR 标签页约 0.2 次请求/秒，单次有 4.5 秒超时、4 MiB 响应上限和 256 KiB 模板片段上限。超大讨论会安全停止更新并提示失败。大规模并发、实际反向代理、rootless 和企业自定义主题需在目标环境另验，不能用本机小样证明生产容量。

## 运维与复核

Manager 任务详情展示结构化进度、开始/结束时间及独立状态同步错误；全局页展示进度进程心跳异常。优先检查 `docker compose logs review-progress`、仓库开关、Webhook 和可信机器人。日志不输出 Token/Key。

- `scripts/test-progress-widget.cjs`：身份、版本、危险链接和浏览器状态规则。
- `backend/tests/test_ai_review_progress.py`：事务通知、租约、同步失败、版本竞争、评论归属及不确定写入。
- `scripts/review-progress-acceptance.py`：仅独立 progress 环境的真实 Gitea 生命周期、安装和协议异常。
- `scripts/review-progress-recovery.py`：真实 Push、Worker 停启、PR 关闭及功能禁用。

测试结论与证据见 [进度验收报告](AIReview进度验收报告.md)。本组件工程验收不替代内部 New API 的模型效果验收，也没有新的人工 80% 节省测量。

官方依据：[自定义入口](https://docs.gitea.com/administration/customizing-gitea/)、[1.26.4 API 认证组](https://github.com/go-gitea/gitea/blob/v1.26.4/routers/api/v1/api.go)、[PR 原生页面](https://github.com/go-gitea/gitea/blob/v1.26.4/routers/web/repo/issue_view.go)、[模板热重载](https://github.com/go-gitea/gitea/blob/v1.26.4/cmd/manager.go)。
