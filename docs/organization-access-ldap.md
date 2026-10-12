# 组织权限的目录认证与 HTTPS 部署

本功能只为现有 Manager 功能增加个人身份和组织权限。LDAP 验证账号密码；平台管理员在 Manager 中另行分配组织，目录群组不会自动授予平台管理员身份，也不会修改 Gitea 的成员、团队或原生权限。

## 配置与 Gitea 的对应关系

在“账号与权限”中选择公司已经验证的认证类型，按原 Gitea 认证源逐项填写。支持一个公司目录。

| Manager 字段 | Gitea 字段 | 填写与行为 |
|---|---|---|
| 认证名称、类型、启用 | Name、LDAP via BindDN / LDAP simple auth、Active | 两种模式分开执行；停用立即阻止后续目录会话 |
| 主机、端口 | Host、Port | 主机不含协议、路径、用户名和密码 |
| 安全协议 | Unencrypted、LDAPS、StartTLS | 明确选择，不在失败时自动降级 |
| 跳过证书验证 | Skip TLS Verify | 与 Gitea 相同；默认关闭 |
| 企业 CA | 企业证书链 | 可填写 PEM 格式 CA，适配内部证书 |
| Bind DN、Bind Password | Bind DN、Bind Password | 查询账号；任意一项为空时按 Gitea 使用匿名查询 |
| 用户搜索基准 | User Search Base | BindDN 必填；simple auth 可留空 |
| 用户 DN 模板 | User DN | simple auth 直接绑定模板 |
| 用户过滤器 | User Filter | 支持 `%s` 和重复引用 `%[1]s`；不支持任意格式化指令 |
| 用户名、名字、姓氏、邮箱属性 | 对应 Attribute 字段 | 用户名属性为空时使用输入登录名 |
| 在 BindDN 上下文读取属性 | Attributes in BindDN Context | 先用查询身份读取属性及群组，随后验证用户密码 |
| 群组校验、搜索基准 | Groups Enabled、Group Search Base | 只限制能否登录 |
| 群组过滤器 | Group Filter | 使用 Gitea 同样的内容，例如 `cn=department-a` |
| 群组成员属性、用户对应属性 | Group Member UID、User UID | 用户对应属性可用 `dn` |
| 分页及每页大小 | Search Page Size | 只用于目录用户查询；单用户认证不改变流程 |

例如用户过滤器 `(|(uid=%s)(mail=%[1]s))` 可以允许用户名或邮箱登录。重复第二个普通 `%s` 会被拒绝，应改用 `%[1]s`。过滤器和 DN 模板需要至少引用一次用户名。

Manager 不提供 Gitea 的管理员过滤器、受限用户映射、群组到团队映射和 SSH 公钥同步，因为本次只做手动组织授权。

目录必须返回 `objectGUID` 或 `entryUUID`。改名继续关联原身份，删除后同名重建不能继承权限。AD 无密码复核还读取禁用、计算后的锁定／密码过期以及账号到期状态，不仅检查用户名是否存在。无法读取必要状态时拒绝复核。

## 登录、授权和会话

1. 先用本地应急管理员登录；旧密码仍为默认值时必须先修改。
2. 配好 HTTPS，再保存目录设置，执行“测试连接”和“测试用户登录”。测试不创建账号、不分配权限。
3. 刷新实例中的已有组织。目录用户可先登录建立待授权身份，或由平台管理员按登录名查询并建立账号。
4. 将现有组织授予所需个人账号；一个账号可以管理多个组织。
5. 组织管理员登录后选择组织，现有业务页面只处理该组织资源。

LDAP 用户密码不保存。Bind Password 使用数据目录中的 `access.key` 独立加密；配置回读只说明是否已设置，不回显密码。密钥丢失时必须恢复原文件，不能用新文件替代。

连接与用户登录测试在失败时保留已经通过的阶段，并标出连接、查询绑定、用户查找、认证、属性或群组校验的失败位置。结果不包含目录原始错误或密码；测试账号认证失败不会退出当前平台管理员会话。

会话默认闲置三十分钟、最长八小时。目录账号每五分钟重新检查；simple auth 没有后台查询凭据时，五分钟后重新认证。Manager 内停用账号、撤销会话或撤销组织授权不等待目录同步。

## 一键首次安装启用 HTTPS

使用企业提供的证书链和与其匹配的未加密私钥：

```bash
sudo bash install.sh --host manager.example.internal --port 5480 \
  --tls-cert /opt/certificates/manager-cert.pem \
  --tls-key /opt/certificates/manager-key.pem \
  --tls-port 5443
```

访问 `https://manager.example.internal:5443`。证书的 SAN 必须覆盖实际访问域名或 IP，浏览器应信任签发 CA；不要使用浏览器忽略证书错误的方式验收。安装器校验证书和私钥配对，复制到私有 `state/tls/`，启动后验证实际证书、有效期和主机名。重复运行不替换已存文件，证书丢失或内容变化会要求明确核验，不重新生成。应按部署网络开放 HTTPS 端口并配置 DNS。

HTTP 端口继续服务安装健康检查与本地应急访问；LDAP 登录及其会话只能通过 HTTPS。未提供企业证书时可安装核心管理功能，目录配置的任何保存以及认证测试均要求先接入 HTTPS。

## 通用 Compose 部署启用 HTTPS

`bash deploy.sh` 为首次部署生成私有初始密码，并由后端生成数据卷中的 `.session-key`。已有数据库的管理员密码不会被环境变量重置。

在现有部署目录中使用附加配置：

```bash
export MANAGER_TLS_CERT=/opt/certificates/manager-cert.pem
export MANAGER_TLS_KEY=/opt/certificates/manager-key.pem
export MANAGER_TLS_PORT=5443
docker compose -f docker-compose.yml -f compose.tls.yml up -d --build
```

如果原实例还使用 AI Review 配置，保留原有 Compose 文件列表，并追加 `-f compose.tls.yml`。缺少证书或私钥时启动会失败，不自动创建目录占位或降级 HTTPS。

内置 nginx 固定转发自身连接的协议，忽略浏览器伪造的协议头。后端只信任明确配置的直接代理：内置 Compose 使用 `TRUSTED_PROXY_HOSTS=frontend`，容器重建后按 Docker 内部服务名解析；其他部署可指定直接代理的 CIDR `TRUSTED_PROXY_NETWORKS`。不要把不受控客户端网段加入可信范围。外层代理若转发到内置 frontend，应连接它的 HTTPS 端口。

## 升级与回退

一键升级保留同一项目、实际数据卷、有效管理员密码、原 Review 主密钥和企业 HTTPS 挂载。目录密钥 `access.key`、持久会话密钥 `.session-key` 随 Manager 卷一致性快照保存，并在升级前后比较指纹；存在目录密文时同时验证可以解密。

将升级包、校验文件和 `upgrade.sh` 放在同一目录。先选择实际后端容器完成只读预检，再执行升级：

```bash
bash upgrade.sh --container <现有后端容器名> --check
bash upgrade.sh --container <现有后端容器名>
```

升级期间保持维护状态；原数据卷、运行配置、旧镜像和独立密钥备份位于原部署目录的私有 `.gitea-manager-upgrades/`。在解除维护状态前发生故障会自动回退整套服务与数据，并执行健康检查。发生失败时保留终端给出的升级记录、原 `state/`、数据卷和密钥，不删除执行锁或重新生成密钥来跳过核验。

升级成功并重新开放写入后，直接换回旧镜像会遇到新数据和新权限结构，不能作为安全回退。此时需要先停写、备份升级后新增数据，再使用对应版本的完整数据与密钥快照恢复；本入口会拒绝自动覆盖已重新开放写入后的数据。

此前未启用 HTTPS 的实例，升级本身不会凭空生成企业证书。应在已生成的 `compose.managed.json` 上追加 `compose.tls.yml`，保持现有项目和文件列表，使用已交付镜像启动：

```bash
export MANAGER_TLS_CERT=/opt/certificates/manager-cert.pem
export MANAGER_TLS_KEY=/opt/certificates/manager-key.pem
export MANAGER_TLS_PORT=5443
docker compose -f compose.managed.json -f compose.tls.yml up -d --no-build --pull never
```

如果原部署使用显式 `-p` 项目名，命令必须继续提供同一个项目名；以原容器的 Compose 标签为准。企业 CA 信任与浏览器 HTTPS 验证完成后，再开启目录登录。

后台组织授权使用独立的 `access-authorization.token`。仅 backend、review-worker、review-progress、review-feedback 获得它；执行器没有该令牌。它不是 Review 主密钥或执行器令牌，丢失时应恢复原文件。

使用手工 AI Review Compose 时，需要在原秘密目录添加一次独立随机令牌并保留，之后仍用同一个文件：

```bash
umask 077
test -e "$AI_REVIEW_SECRET_DIR/access-authorization.token" || \
  openssl rand -hex 32 > "$AI_REVIEW_SECRET_DIR/access-authorization.token"
```

目录配置、密钥、安装状态和运行数据库不得进入发布包。公司目录与真实部门流程仍需在实际部署条件下验收；隔离 OpenLDAP／Samba AD 通过不等于公司目录或人工精力目标已经验收。
