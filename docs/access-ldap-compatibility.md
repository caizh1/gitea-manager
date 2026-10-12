# LDAP 与 Gitea 字段对照

Manager 使用已有公司目录认证，平台角色和组织范围仍由平台管理员手动授予。Gitea 的管理员过滤器、受限用户角色、群组团队映射和 SSH 公钥同步不参与 Manager 授权，也没有作为无效开关展示。

## 逐项迁移配置

以公司已经验证的 Gitea 认证源为准，复制其实际填写值。主机不包含 `ldap://` 或 `ldaps://` 前缀；端口独立填写。新配置默认 **LDAPS、636、校验证书**，页面占位示例不代表已保存配置。

| Gitea 字段或含义 | Manager 字段 | 填写与执行规则 |
|---|---|---|
| Authentication Name | 认证名称 | 仅标识认证来源，不赋予角色。 |
| Authentication Type | 认证类型 | 区分 LDAP via BindDN 与 LDAP simple auth。 |
| Active | 启用目录认证 | 停用后不能继续使用目录会话。 |
| Host、Port | 主机、端口 | 逐项复制公司已经验证的值。 |
| Security Protocol | 安全协议 | Unencrypted、LDAPS、StartTLS；失败不回退明文。 |
| Skip TLS Verify | 跳过 TLS 证书验证 | 按保存值执行；默认关闭。企业内部证书可以配置 CA。 |
| Bind DN、Bind Password | Bind DN、Bind Password | BindDN 模式的查询身份；DN 或密码为空时遵循 Gitea 的匿名查询语义。 |
| User Search Base | 用户搜索基准 | BindDN 必填；simple auth 可选。 |
| User DN | 用户 DN 模板 | simple auth 必填，例如 `uid=%s,ou=people,dc=example,dc=com`。先使用用户密码直接绑定，再读取属性。 |
| User Filter | 用户过滤器 | 支持 `%s`、重复参数 `%[1]s` 和字面百分号 `%%`。不使用 Python 百分号格式化替代 Go 语义。 |
| Username Attribute | 用户名属性 | 空值时使用本次登录名；不会将邮箱或姓名猜成稳定身份。 |
| First Name Attribute | 名字属性 | 例如 `givenName`；使用目录返回值。 |
| Surname Attribute | 姓氏属性 | 例如 `sn`；与名字组成显示姓名。 |
| Email Attribute | 邮箱属性 | 例如 `mail`。 |
| Fetch Attributes in Bind DN Context | 在 BindDN 上下文读取属性 | 开启时使用查询身份读取；关闭时使用已认证用户身份读取。 |
| Verify Group Membership in LDAP | 启用群组校验 | 登录条件，与组织授权、Gitea 团队映射无关。 |
| Group Search Base DN | 群组搜索基准 | 启用群组校验时填写。 |
| Group Filter | 群组过滤器 | 与 Gitea 相同，例如 `cn=department-a`；外层条件由认证实现组合。空过滤器遵循 Gitea 的不限制行为。 |
| Group Member Attribute | 群组成员属性 | 常见值 `member` 或 `memberUid`，必须与目录结构一致。 |
| User Attribute Listed in Group | 用户对应属性 | 完整 DN 使用 `dn`；其他值使用目录实际属性。 |
| Enable Page Size、Page Size | 启用分页、每页大小 | 目录用户查询使用分页；不改变单个用户密码认证流程。超过服务端限制的部分结果不会冒充完整列表。 |
| 企业 CA（Manager 扩展） | 企业 CA 证书 | 填写 PEM 证书；不要求修改目录结构或关闭验证。 |

过滤器重复引用示例：`(|(uid=%[1]s)(mail=%[1]s))`。一个参数不能误写成两个独立的 `%s`。与 Gitea 一致，登录名中的 LDAP 注入字符会被拒绝；simple auth 的直接 DN 模板额外拒绝 DN 分隔字符。

本次真实对照依据官方 Gitea 1.26.4 及本机现有增强版镜像，并连接相同 OpenLDAP、Samba AD。认证说明可参考 [Gitea 认证文档](https://docs.gitea.com/administration/authentication/)。不同 Gitea 版本的 UI 文案可能略有差异，迁移时应核对认证类型和实际保存值。

## 身份与授权

Manager 自动读取 AD 的 `objectGUID` 或 OpenLDAP 的 `entryUUID`。不需要新增自定义目录属性；读取不到可靠稳定身份时拒绝建立可授权账号。改名保留稳定身份，同名删除重建得到新身份，不继承旧授权。目录群组只限制登录，不会自动产生平台管理员或组织管理员角色。

用户可以先登录，再由平台管理员分配多个组织；也可以由平台管理员查询有效账号后按需授权。未授权用户只能看到“尚未分配管理权限”。无后台查询凭据的 simple auth 不提供全量目录查询，用户必须先登录。

用户密码不持久化。Bind Password 加密保存，查询配置只返回“已设置”；留空与清空按页面明确操作处理，不回显秘密。目录加密密钥独立于会话签名密钥，修改会话密钥不会导致目录凭据无法解密。

LDAP 登录和目录秘密设置必须走 HTTPS。会话默认闲置三十分钟、最长八小时。BindDN 会话五分钟重新核对登录条件；AD 同时检查禁用、计算出的锁定或密码到期状态及账号到期时间。simple auth 没有后台凭据时五分钟后重新登录。Manager 内停用账号或撤销组织授权在下一次请求生效，不等待目录检查周期。

## 测试与排错

先执行“测试连接”，再执行“测试用户登录”，按连接、用户定位、密码认证、属性和群组阶段查看脱敏结果。测试只证明当前配置和测试账号在当前网络下可用，不能代替公司账号生命周期、证书更换和组织授权验收。

隔离环境可通过 `python3 scripts/access-env.py up` 和 `python3 scripts/access-env.py test` 重建、重跑，详见 [真实目录验收环境](../deployment/access-test/README.md)。公司生产密码、测试目录数据、测试私钥和随机凭据均不进入发布包。

本次环境没有使用公司真实目录。Samba AD 的通过结果只作为工程对照证据；公司目录接入结论仍为**未验证**。
