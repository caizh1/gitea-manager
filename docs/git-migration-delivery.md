> 2026-11-06 主线合并说明：经用户明确要求，迁移分支截至 `ff53c9a` 的历史及本地改动合入 `master`，同时保留主线的 AI Review、侧栏、门禁与安装器修复。此操作为源码合并，不代表发布新安装包或完成生产验收。下文镜像、包摘要和旧交付清单属于各自原始构建，不能当作本次合并后源码的打包证明。后续打包应从已同步主线重新构建，并包含配套 Git Worker 及对应接收端。

> 当前试用包采用协议八，包含制品隔离、完整清单及终止查询错误修复，见 [真实环境试用说明](git-migration-trial.md) 与 [制品隔离说明](git-migration-package-isolation.md)。以下旧版验收记录保留其原有范围；当前提交和镜像身份以包内来源清单为准。

> 本轮新增 [讨论历史状态与差异确认](git-migration-discussion-history.md)。第六版采用已确认的目标原生讨论行为，旧文中的讨论强制门禁仅描述旧协议。

# Git 迁移隔离测试交付说明

正式开放约束见 [正式开放说明](git-migration-publication.md)。完整性和合并条件说明见 [迁移补齐说明](git-migration-completion.md)，新增人员自动匹配与批量确认见 [人员匹配说明](git-migration-account-matching.md)。本包用于固定基准的隔离链路测试，不构成全部版本或生产能力认证。已知未支持资料会阻断最终交接，详细范围见 `git-migration-implementation.md`。

## 组成

- Manager 后端、前端、Git Worker、SVN Worker 固定标签：`discussion-20261029-r4`。
- 原有 AI 执行镜像沿用已验收的 `backup-icons-20261026-r1` 内容，没有增加迁移管理权限。
- 配套增强版接收镜像：`gm-migration-receiver:seal-manifest-20261029-r3`，包内导出名 `gitea-migration-receiver:offline`；第六版协议包含最终形态预演、整批隔离、容量探测与原子开放。
- 独立接收端源码：`offline/git-migration-receiver.bundle`。分支 `codex/git-migration-receiver`，实现提交 `ebe16fc`，基线 `1c0dab7`。讨论历史完整补丁保存在 `docs/patches/migration-receiver-discussion-history.patch`；上一轮最终形态补丁保存在 `docs/patches/migration-receiver-final-form.patch`，字段修复补丁位于 `docs/patches/migration-receiver-field-integrity.patch`，上一轮人员权限补丁继续保留，不修改原增强版工作区。该基线包含用户指定的当前增强版源码快照，不能将提交直接应用到未知版本并宣称兼容。

## 使用顺序

1. 在独立测试机器解压完整包，按现有安装/升级流程启动 Manager 与 Git Worker。保留原数据卷、迁移密钥和任务检查点。
2. 向独立测试接收实例装载配套增强版镜像。此包不会自动替换已有目标 Gitea；生产升级及回退需要另行验证。
3. 在 Manager 登记目标管理员连接和来源读取身份。连接路径必须是实际最终实例地址，不能依赖认证重定向；同一物理实例只登记一条目标连接。
4. 在“GitLab / Gitea 迁移”中发现项目、完成盘点和代码核验，选择目标已有群组，进入人员匹配页面。可整批确认全部自动匹配，或只确认跨页勾选的人员；普通歧义和未匹配人员可经影响预览后统一保留来源身份。有效权限例外必须补齐，全部处理方式确认后运行预检查和最终形态演练，再确认来源停写并复检，系统通过后才创建正式任务。
5. 核对执行详情。成功要求代码、对象、文件、规则和最终群组交接全部完成。隔离、待确认或中断不是成功；可在处理原因后继续核对同一任务。

接收端源码可使用 `git clone -b codex/git-migration-receiver offline/git-migration-receiver.bundle receiver-source` 单独检出。不要覆盖用户当前增强版工作区，也不要复用既有生产数据库做测试。

## 验收入口

- `scripts/git-migration-write-acceptance.py`：真实来源到接收端的模块全链路和重复导入。
- `scripts/git-migration-manager-acceptance.py`：固定镜像 Manager、独立 Worker、完整交接、维护重启；`--interrupt-once` 验证写入中断恢复。
- `scripts/git-migration-account-acceptance.py`：真实人员名单的批量确认、补齐账号、百分之百门槛和迁移后负责人核验。
- `scripts/git-migration-retention-acceptance.py`：真实多选、来源身份保留、有效权限例外、原生规则和完整交接后的逐项核对。
- `scripts/git-migration-rich-fixture.py` 与 `scripts/gitlab-migration-rich-fixture.py`：仅向已标记的独立来源建立代表性测试资料。

凭据和原始测试数据仅保存在本机 `work/`，不进入交付包。包内源码、文档、依赖和镜像清单用于复核，不能把本地 API 验收当作 EE 许可、完整版本矩阵或人工节省百分之八十的证明。

## 高级功能验收入口

- `git-migration-advanced-fixture.py` 建立真实依赖仓库、重叠规则和五种原生制品；`git-migration-ci-fixture.py` 建立真实 C 编译、成功与失败日志、归档及原始报告。
- `git-migration-rule-fixture.py` 为 GitLab 增加现存保护分支，并核对来源有效身份下的 Nobody 合并拒绝。
- `git-migration-advanced-acceptance.py` 下载全部盘点文件、核对五种协议、执行已知 C 基准二进制，并实际合并跨仓库请求。
- `git-migration-rule-acceptance.py` 执行真实允许与拒绝行为，拒绝必须有明确保护或权限原因，网络错误不能计为拒绝通过。
- `git-migration-recovery-acceptance.py` 保留旧 Worker，替换固定镜像后从原任务与仓库编号继续；不能用于生产实例。

来源要求 CI 成功、讨论解决及已支持合并方式时，使用配套接收端的最终门禁；讨论限制按第六版差异确认处理；可信 CI 账号需提前指定。历史成功不能代替当前提交的 CI 回执。无法等价转换的额外条件继续阻断交接。

旧版 Gitea Actions 制品和部分 GitLab 文件需要来源专用迁移管理员的网页会话，令牌不能完成时会明确阻断。源码和任务凭据不能进入命令输出或发布包。OCI／容器镜像暂不包含；EE 付费能力保持未验证。

配套接收端还需声明 npm 注册表元数据能力，缺少该能力会在创建目标仓库前拒绝执行。`git-migration-registry-fixture.py` 补充多版本、注册表说明和归档依赖不一致的固定基准。

最终包使用 `git-migration-final-acceptance.py` 从包内源码与固定镜像建立空白环境，执行二十七项目、二十三合并变体、六类协议、全部文件与规则行为验收，并记录最终包 SHA-256。来源凭据失效、目标令牌失效及安全终止均包含在该链路中。详细回执位置见补齐说明。

## 预迁移门禁

人员确认后先完成独立预迁移与检查，随后确认停写并复检，服务端才创建正式任务。操作、检查范围与保证边界见 [预迁移说明](git-migration-preflight.md)。检查通过不等于未来故障概率为零；最终交接仍须逐项回读。

最终形态与恢复说明见 [整批开放说明](git-migration-final-form.md)。新增 `git-migration-final-form-acceptance.py` 验证附件与开放回执丢失、进程崩溃、隔离可见性和取消回收；`git-migration-capacity-acceptance.py` 使用真实受限文件系统核验字节容量和运行中 inode 下降。实际通过范围及失败记录以本轮验收报告为准。

## 2026-11-06 主线合并验证

- 保留主线现有 AI Review 通知验证、失败接入移除及侧栏样式，接入迁移菜单、权限清单、数据库模型、独立 Worker、安装和升级流程。
- 修复从新版本回退到尚无 Git Worker 的旧版时，健康检查错误索引不存在服务的问题；原版有该 Worker 时仍必须检查其健康。
- 安装器测试补齐 Git Worker 清单及仅挂载 Manager 数据卷的断言。备份测试改为从仓库定位入口，Git 快照测试改为验证当前凭据助手的仓库路径与来源限制，没有取消凭据隔离检查。
- 本地累计 387 项自动化测试通过，1 项 Linux 孤儿进程回收测试因当前为 macOS 跳过。范围为迁移 173 项、门禁与组织权限及 Review 接入 90 项、安装升级 40 项、接口权限和 SVN 与备份 52 项、前端 32 项。修正过期测试后已重跑受影响模块。
- 前端生产构建、改动 Python 文件语法检查、隔离数据库连续启动两次及迁移路由注册检查通过。

VERDICT: PASS

P0/P1 BLOCKERS: 本次合并与兼容性检查未发现未修复的 P0/P1 阻塞问题。旧版回退缺陷已修复，并覆盖有、无原 Git Worker 的回归路径。

UNVERIFIED RISKS: 未重新执行全部历史外部服务矩阵、实际 Docker 安装升级、Linux 孤儿进程回收、生产及人工效率验收。历史交付包与接收端补丁仍保留各自原始验证范围，不能据此宣称当前合并提交已经打包或部署。

NON-BLOCKING FINDINGS: 前端保留既有第三方注释与大资源块构建提示；接收端补丁文件中的上下文空格按补丁格式保留。
