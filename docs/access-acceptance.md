# 组织权限与目录工程验收记录

本报告记录 2026-10-11 的真实隔离环境执行证据。公司真实目录、最终离线包安装升级回退、真实宿主机和人工精力指标必须分别判定；不能由本报告的工程通过结果替代。

## 环境与结果

独立项目使用 OpenLDAP、Samba AD DC、官方 Gitea 1.26.4、已交付增强版 Gitea 和 HTTPS Manager。两个 Gitea 各有两个组织；仓库使用实际 Git 数据，部门管理员、跨实例多组织管理员、未授权用户和停用账号均实际登录。Manager 使用同一原生持久卷和两个 Gunicorn Worker。

| 验收层 | 实际结果 | 证据文件 |
|---|---|---|
| 真实 LDAP 与两版 Gitea 认证差分 | 66/66 通过 | `work/access-acceptance/ldap-results.json`、`ldap-report.md` |
| 两版 Gitea 首次网页登录属性差分 | 4/4 通过 | `work/access-acceptance/ldap-attribute-results.json` |
| Linux Manager 内真实目录集成测试 | 6/6 通过 | `work/access-acceptance/ldap-linux-unittest.log` |
| Manager HTTPS 业务与组织隔离 | 120/120 通过 | `work/access-acceptance/manager-results.json`、`manager-report.md` |
| 实际 Git 推送和 PR 合并门禁 | 10/10 通过 | `work/access-acceptance/hook-results.json`、`hook-report.md` |
| HTTPS 会话与真实目录账号生命周期 | 26/26 通过 | `work/access-acceptance/session-results.json`、`session-report.md` |
| 真实后台任务领取前撤权 | 10/10 通过 | `work/access-acceptance/task-results.json`、`task-report.md` |

各目录建立 1,501 个固定账号。OpenLDAP 分页返回 1,501 个唯一稳定身份；AD 中有一个禁用账号，可授权查询应返回 1,500 个。未分页超出服务端限制时，Manager 拒绝使用部分结果。临时属性夹具执行完删除，测试凭据和私钥仅留在忽略的工作目录。

镜像证据保存在 `images.json`。本次实际摘要：

| 服务 | 镜像内容摘要 |
|---|---|
| OpenLDAP / Samba 测试镜像 | `sha256:95364a38c42ede763c7e04d39e8437a943805a88586b3db92db9c7e91ad1396a` |
| 官方 Gitea 1.26.4 | `sha256:ebe815a4e594bb9a180dc7b3a7946e11cb7389f8ae2daa2beb31287416c94849` |
| 已交付增强版 Gitea | `sha256:01d658c9c852636c3a048035f1d97ca2f42f86e60a9ddd119608b0582b28bdad` |

本环境端口只绑定 `127.0.0.1`。脚本检查项目所有权标记，使用独立网络、随机密码、测试 CA 和卷，不操作其他现有项目。只有 Manager 后端为现有 Hook 安装路径挂载 Docker socket，目录探针和 Worker 没有获得管理挂载。

## 已完成的真实路径

LDAP 差分覆盖 BindDN、simple auth、匿名查询，三种安全协议与 SkipVerify，错误和空密码，多条匹配、重复 `%[1]s`、邮箱登录、用户名属性留空、群组匹配、属性读取身份、分页、账号禁用和到期、目录中断恢复、改名和同名重建。可信、过期、主机名不匹配和不可信证书均通过真实 LDAPS 与 StartTLS 验证。连接失败没有自动降级。

属性对照使用 Gitea 首次网页登录实际创建目录账号，然后通过 API 回读姓名、邮箱和用户名，与 Manager 的同一配置结果比较。只在 BindDN 上下文可读的私有属性分别验证了开启和关闭行为。预创建 Gitea 账号的旧资料不作为属性同步证据。

Manager 验收通过真实 HTTPS 请求完成目录保存和刷新回读、密码不回显、连接和登录测试、全量分页查询、个人登录、待授权状态、授权和撤权。一个账号可以管理两个实例中的组织。组织业务列表、策略模板、提交规则、审批配置和审计、统计作者明细、AI 已接入仓库开关及样本导出均验证了合法读取与跨组织拒绝。平台设置、接入安装、机器人密钥及原始诊断对组织管理员关闭。

仓库策略由真实后台 Worker 预览、执行并回读 Gitea 分支保护；记录实际个人身份。预览后直接修改真实 Gitea 规则，后台正确拒绝过期配置并产生组织归属告警，另一组织的列表、摘要和清除接口均不能访问。撤销授权后，32 条并发独立连接的后续请求全部拒绝。

真实转移仓库后，原组织不能继续读取仓库和旧批次，新组织不能继承旧批次；新组织可按新的实际归属读取转入仓库。切换目录配置使旧会话失效。真实 Samba AD 账号禁用后，通过推进隔离会话的检查时间触发五分钟复核，已有会话被拒绝。目录状态、认证查询和 HTTP 鉴权没有用 Mock 替换。

最后一轮组织和仓库出生时间防护加载后，120 项 Manager 矩阵再次完整通过。补充会话测试验证了默认八小时数据库期限、Secure/HttpOnly/SameSite Cookie、篡改 Cookie、CSRF、账号停用、闲置到期、绝对到期、管理员撤销和主动退出。真实 LDAP 改名保留原账号和组织授权；同名删除重建产生新账号且无任何继承权限，旧会话复核失败。没有后台查询凭据的 simple auth 在目录认证满五分钟后要求重新登录，实际 HTTP 入口拒绝 LDAP 登录。

后台撤权测试使用现有实例文件锁暂停领取，先通过真实 HTTP 提交应用任务，再撤销操作者的组织授权后释放锁。真实 Worker 将目标记录标为阻断，Gitea 回读没有新增保护规则；同组织另一管理员仍可正常操作。此用例验证领取前的确定性时序，不冒充所有外部请求进行中断或回执丢失的组合均已实测。

Git 验收由部门管理员通过 Manager 安装 Hook，再实际 clone、commit、push：不合规提交被服务器拒绝，合规提交成功。真实 PR 合并不合规标题得到 409，合规标题合并成功。其他组织不能移除 Hook；本组织移除后回读成功。

## 失败记录与修复复验

| 早期失败 | 根因及处理 | 保留证据 |
|---|---|---|
| AD 禁用账号的无密码会话复核错误放行 | 认证查询未读取 AD 控制状态。实现补充 `userAccountControl`、计算状态与 `accountExpires`，禁用及过期真实复核重新通过。 | `ldap-revalidation-initial.json` |
| AD 分页数量与预期不一致 | 原夹具过滤器包含系统账号和群组；改成测试用户范围，并保留一个已禁用用户的预期排除。 | `ldap-initial-results.json` |
| 未分页上限断言不适用于 Samba 默认配置 | Samba 本环境未强制一千条上限；使用已设置一千条服务端上限的真实 OpenLDAP 匿名查询验证部分结果拒绝。 | `ldap-before-unpaged-fixture-fix.json` |
| 首轮仓库转移得到 422 | 目标部门已存在同名测试仓库；先通过真实 API 改名再转移，保留同名跨部门用例。 | `manager-initial-results.json` |
| AI 接入无法加密令牌 | 隔离启动器使用了错误的主密钥变量名；修正 `AI_REVIEW_MASTER_KEY_FILE` 并只在缺失时创建持久测试密钥。 | `manager-before-review-key-fix-results.json` |
| 审批个人审计误报失败 | 原断言误要求登录名，实际正确记录中文显示名和账号 ID；改为严格匹配当前会话实际身份。 | `manager-before-review-key-fix-results.json` |
| 转入组织合法读取得到 400 | 测试遗漏原有接口必填组织查询参数；补齐参数后完整 120 项重新执行通过。 | `manager-before-transfer-query-fix-results.json` |
| 首次登录属性测试读到其他来源账号 | 差分环境已有故意宽泛过滤器，统一测试密码允许来源回退；改为独立随机密码，精确锁定测试来源后两版各两种属性上下文全部通过。 | `ldap-attribute-first-results.json` |

增强版 Gitea 现有认证源编辑命令另外存在“数据已变化或相关合并尚未完成”错误，官方版没有相同问题。认证差分使用新建独立来源，未修改增强版源码。此参考环境缺陷与 Manager 结果分开记录。

宿主 macOS Python/ldap3 的显式任意地址绑定出现连接错误；相同目录通过实际 Linux 运行时的完整协议测试与集成测试均通过。保留 `ldap-unittest.log`，没有把宿主失败掩盖成通过，也未为非目标宿主修改生产认证逻辑。

## 重跑与验收边界

运行方式见 [隔离环境说明](../deployment/access-test/README.md)，字段对照见 [LDAP 兼容说明](access-ldap-compatibility.md)。接口分类见 [管理路由权限矩阵](access-route-matrix.md)。各脚本只新建随机测试仓库和配置，失败证据不覆盖为伪造成功；环境清理保留原生卷和报告。

本报告的 Manager 工程环境挂载当前源码和前端构建。因此，最终交付镜像、安装、重复安装、升级、回退和无源码挂载检查须以对应最终包报告为准。AI 仓库权限和业务样本导出通过不代表已验证实际模型审查质量；审批生产强制启用仍保留现有发布限制。

公司真实目录未接入，本报告不宣称公司 AD 验收通过。至少两个部门、每部门十项真实任务的人工基线对比尚未完成；人工精力减少至少 80% 的目标为**未验证**。
