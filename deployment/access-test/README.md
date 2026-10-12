# 真实目录隔离验收

本目录用于组织权限功能的真实 LDAP 兼容验收。测试对象包含 OpenLDAP、Samba AD DC、官方 Gitea 1.26.4 和本机已交付增强版 Gitea。它不是公司 LDAP 验收结果。

## 运行

在仓库根目录执行，要求 Docker Compose、Python 3 和项目已有的 cryptography 依赖：

```sh
python3 scripts/access-env.py up
python3 scripts/access-env.py test
python3 scripts/access-env.py status
python3 scripts/access-env.py down
```

脚本生成独立项目名、未占用子网、随机本地端口和测试密码。端口仅绑定到 `127.0.0.1`。启动与清理前会检查项目和容器标记；不会停止或删除其他项目。`down` 保留测试卷和证据，下一次 `up` 可以继续使用。

配置、密钥、目录夹具和证据全部位于 `work/access-acceptance/`，不得提交或加入交付包。源代码目录通过只读挂载加载当前生产 LDAP 实现；测试没有替换 LDAP 响应的 Mock。

## 数据与证据

两种目录各建立 1,501 个固定用户；AD 中 `user0002` 为已禁用用户，因此可授权分页结果应为 1,500 人。OpenLDAP 的停用条件通过与 Gitea 相同的用户过滤器判断。`user0000` 和 `user0001` 属于部门甲测试群组。

两个 Gitea 实例各有部门甲和部门乙，共四个独立组织。每项对照创建独立认证源，并通过 Gitea 原生管理 API 把临时测试用户绑定到该来源，确保不会回退到别的来源。

完整矩阵覆盖三种连接协议、BindDN、匿名查询、simple auth、重复参数过滤器、群组、属性读取身份、分页、目录中断、账号禁用与到期、身份改名和同名重建，以及可信、过期、不可信、主机名不符证书的正常验证和 SkipVerify。测试会暂时重启隔离 OpenLDAP 并切换证书；不要同时在该实例上运行人工浏览器验收。

`test` 还执行 Gitea 首次网页登录属性对照。该用例使用独立随机密码，防止其他测试认证源中故意宽泛的过滤器造成跨来源回退；比较姓名、用户名和邮箱，并验证 BindDN 属性读取上下文。

Manager HTTP 与 Git 回归在前端构建和测试后端镜像就绪后按顺序执行：

```sh
python3 scripts/access-manager-env.py
python3 scripts/access-manager-acceptance.py
python3 scripts/access-hook-acceptance.py
python3 scripts/access-session-acceptance.py
python3 scripts/access-task-acceptance.py
```

Manager 测试入口、端口和临时密码保存在 `work/access-acceptance/manager/runtime.json`。HTTPS 用于 LDAP，另有绑定本机的 HTTP 入口用于本地应急管理员 UI 检查，HTTP 不伪造 HTTPS。只有测试 Manager 后端挂载 Docker socket，以验收现有 Hook 安装路径；目录探针和 Worker 都不获得该挂载。

以上 Manager 环境加载当前源码及前端构建，是工程回归环境。它不能作为最终离线包安装、升级或回退验收的替代。

验收文件：

- `ldap-results.json`：最近一次完整执行的逐项证据。
- `ldap-report.md`：中文结果摘要。
- `images.json`：实际容器镜像内容摘要。
- `ldap-initial-results.json`：首轮 AD 未排除系统对象时的结果，作为夹具修正证据保留。
- `ldap-revalidation-initial.json`：真实 AD 禁用账号复核缺陷的修复前证据。
- `ldap-attribute-results.json`：两版 Gitea 首次网页登录的属性对照。
- `manager-results.json`、`hook-results.json`、`session-results.json`、`task-results.json`：真实 HTTPS 业务隔离、Git/PR、会话及任务领取前撤权回归。

后台测试入口 `backend/tests/test_ldap_integration.py` 默认明确跳过。设置 `ACCESS_LDAP_INTEGRATION=1` 后才连接上述真实环境，不会把未执行标记为通过。

## 已有参考环境差异

现有增强版 Gitea 的认证源编辑命令返回“数据已变化或相关合并尚未完成”；对照用例采用新建独立来源，并保留原始失败记录。官方 Gitea 没有同一编辑问题。本测试不修改增强版源码或生产配置，也不将该参考环境问题计作 Manager 的认证结果。
