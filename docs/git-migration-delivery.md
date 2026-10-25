# Git 迁移隔离测试交付说明

本包用于完整固定基准的隔离链路测试，不构成全部版本或生产能力认证。已知未支持资料会阻断最终交接，详细范围见 `git-migration-implementation.md`。

## 组成

- Manager 后端、前端、Git Worker、SVN Worker 固定标签：`git-migration-final-20261025-r13`。
- 原有 AI 执行镜像沿用已验收的 `git-migration-20261025-r5` 内容，没有增加迁移管理权限。
- 配套增强版接收镜像：`gm-migration-receiver:20261025-r24`，包内导出名 `gitea-migration-receiver:offline`。
- 独立接收端源码：`offline/git-migration-receiver.bundle`。分支 `codex/git-migration-receiver`，实现提交 `e89eacf`，基线 `1c0dab7`。该基线包含用户指定的当前增强版源码快照，不能将提交直接应用到未知版本并宣称兼容。

## 使用顺序

1. 在独立测试机器解压完整包，按现有安装/升级流程启动 Manager 与 Git Worker。保留原数据卷、迁移密钥和任务检查点。
2. 向独立测试接收实例装载配套增强版镜像。此包不会自动替换已有目标 Gitea；生产升级及回退需要另行验证。
3. 在 Manager 登记目标管理员连接和来源读取身份。连接路径必须是实际最终实例地址，不能依赖认证重定向；同一物理实例只登记一条目标连接。
4. 在“GitLab / Gitea 迁移”中发现项目、完成盘点和代码核验，选择目标已有群组、配置规则主体、确认来源停写，然后执行。
5. 核对执行详情。成功要求代码、对象、文件、规则和最终群组交接全部完成。隔离、待确认或中断不是成功；可在处理原因后继续核对同一任务。

接收端源码可使用 `git clone offline/git-migration-receiver.bundle receiver-source` 单独检出。不要覆盖用户当前增强版工作区，也不要复用既有生产数据库做测试。

## 验收入口

- `scripts/git-migration-write-acceptance.py`：真实来源到接收端的模块全链路和重复导入。
- `scripts/git-migration-manager-acceptance.py`：固定镜像 Manager、独立 Worker、完整交接、维护重启；`--interrupt-once` 验证写入中断恢复。
- `scripts/git-migration-rich-fixture.py` 与 `scripts/gitlab-migration-rich-fixture.py`：仅向已标记的独立来源建立代表性测试资料。

凭据和原始测试数据仅保存在本机 `work/`，不进入交付包。包内源码、文档、依赖和镜像清单用于复核，不能把本地 API 验收当作 EE 许可、完整版本矩阵或人工节省百分之八十的证明。

## 高级功能验收入口

- `git-migration-advanced-fixture.py` 建立真实依赖仓库、重叠规则和五种原生制品；`git-migration-ci-fixture.py` 建立真实 C 编译、成功与失败日志、归档及原始报告。
- `git-migration-rule-fixture.py` 为 GitLab 增加现存保护分支，并核对来源有效身份下的 Nobody 合并拒绝。
- `git-migration-advanced-acceptance.py` 下载全部盘点文件、核对五种协议、执行已知 C 基准二进制，并实际合并跨仓库请求。
- `git-migration-rule-acceptance.py` 执行真实允许与拒绝行为，拒绝必须有明确保护或权限原因，网络错误不能计为拒绝通过。
- `git-migration-recovery-acceptance.py` 保留旧 Worker，替换固定镜像后从原任务与仓库编号继续；不能用于生产实例。

源端要求强制 CI 成功、讨论解决或不兼容合并方式时，目前会保留待处理并阻断交接，需要确认等价目标门禁；不能把迁入的历史成功记录当作当前 CI 通过。

旧版 Gitea Actions 制品和部分 GitLab 文件需要来源专用迁移管理员的网页会话，令牌不能完成时会明确阻断。源码和任务凭据不能进入命令输出或发布包。OCI／容器镜像暂不包含；EE 付费能力保持未验证。

配套接收端还需声明 npm 注册表元数据能力，缺少该能力会在创建目标仓库前拒绝执行。`git-migration-registry-fixture.py` 补充多版本、注册表说明和归档依赖不一致的固定基准。
