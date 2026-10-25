# Gitea Manager

多 Gitea 实例统一管理面板，支持远程备份、恢复与定时调度。

已有内网部署升级：将离线包、SHA-256 文件和 `upgrade.sh` 放在同一目录，运行 **`bash upgrade.sh`**。首次 AI 接入在页面填写模型地址和 Key、选择仓库，点击“检查并启用”；后续升级保留配置。详见 [一键升级与首次 AI 接入](docs/一键升级与首次AI接入.md)。

## 功能特性

- **服务器管理** — 添加、编辑、移除 Gitea 实例，实时查看状态、版本、仓库数、用户数、磁盘用量
- **一键备份** — 通过 SSH 连接远程主机，使用 Docker 命令备份 Gitea 的仓库、数据库和配置
- **灵活恢复** — 将备份恢复到原服务器或其他服务器
- **定时调度** — Cron 式定时备份任务，自动执行并记录日志
- **系统设置** — 管理员密码修改、本机 IP 配置
- **SVN 迁移中心** — 保存多个 SVN 来源、跨来源规划、串行迁移、逐提交核验及中断恢复；只迁移内容和历史，业务账号与权限后续配置。见 [使用说明](docs/svn-migration.md)。
- **仓库策略中心** — 先选实例和组织，管理独立组织模板；原生 Gitea 1.26.4 中文完整规则页、稳定人员/团队身份、明确跨组织复制、旧模板归属、组织仓库批量预览下发、写后核验和执行审计；共享管理员，无原生继承、版本或自动同步
- **AI Review 实时进度** — PR 对话、提交与文件页自动显示审查阶段和真实分析计数；通过官方自定义入口接入已有 Gitea 容器，详见 [安装与使用](docs/AIReview进度使用与部署.md) 和 [本机验收](docs/AIReview进度验收报告.md)。

仓库策略功能见 [使用说明](docs/仓库策略使用说明.md)、[组织模板验收报告](docs/组织模板验收报告.md)、[独立本地部署](docs/本地部署说明.md) 和 [后续待办](docs/仓库策略待办清单.md)。本地部署不会占用宿主机 5000 端口，也不复用现有内网数据库或凭据。

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python Flask + SQLAlchemy + Gunicorn |
| 前端 | Vue 3 + Element Plus + Axios + Vite |
| 数据库 | SQLite |
| 部署 | Docker Compose (nginx + gunicorn) |
| 通信 | REST API + Paramiko (SSH) + Docker SDK |

## 快速开始

### 前置条件

- Docker & Docker Compose
- SSH 密钥（用于连接远程 Gitea 服务器，默认为 `~/.ssh/id_rsa`）

### 部署

**Ubuntu 24.04 amd64 首次离线安装：** 使用独立的 `manager-ubuntu24-amd64-installer.tar.gz`，解压后运行 `sudo bash install.sh`，输入访问地址和端口。脚本包含离线 Docker 依赖安装、随机凭据生成、数据库初始化及完整后台服务就绪检查。见 [一键安装说明](docs/oneclick-installation.md)。下方源码部署流程属于旧的基础启动方式；已有实例升级继续使用 `upgrade.sh`。

```bash
git clone https://github.com/caizh1/gitea-manager.git
cd gitea-manager
bash deploy.sh
```

部署后访问 `http://<本机IP>:5480`，使用默认密码登录（首次登录后请及时修改密码）。

### 分步说明

`deploy.sh` 自动完成以下步骤：

1. 创建 `data/backups` 备份目录
2. 构建后端与前端 Docker 镜像
3. 通过 Docker Compose 启动服务
4. 检查服务是否正常运行

后端 API 运行在 `:5000`（仅内网），前端 nginx 运行在 `:5480` 并代理 `/api` 到后端。

## 开发模式

### 后端

```bash
cd backend
pip install -r requirements.txt
python run.py
```

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端开发服务器通过 Vite 代理 `/api` 请求到 `localhost:5000`。

## 配置说明

通过环境变量配置（参见 `docker-compose.yml`）：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DATABASE_URL` | 数据库连接地址 | `sqlite:////app/data/gitea-manager.db` |
| `BACKUP_DIR` | 备份文件存储目录 | `/app/data/backups` |
| `SECRET_KEY` | Flask 密钥（请修改） | `change-me-in-production` |
| `INIT_PASSWORD` | 初始管理员密码（请修改） | `admin123` |
| `SSH_KEY_PATH` | SSH 私钥路径（容器内） | `/home/appuser/.ssh/id_rsa` |
| `RESTORE_LONG_JOB_TIMEOUT_SECONDS` | 远程恢复长步骤最长运行时间 | `7200` |
| `RESTORE_JOB_POLL_SECONDS` | 远端作业状态轮询间隔 | `5` |
| `RESTORE_SSH_RECONNECT_GRACE_SECONDS` | 轮询期间 SSH 连续不可用的容忍时间 | `300` |
| `RESTORE_DIAGNOSTIC_INTERVAL_SECONDS` | 长步骤诊断指标记录间隔 | `30` |
| `INTEGRATION_ENABLED` | 是否启用 AIOps Sentinel 外部只读集成，设为 `true` 才启用 | `false` |
| `INTEGRATION_SERVICE_TOKEN` | 外部集成服务令牌，数据库仅保存 SHA-256 | 空；仅启用集成时必填 |

**注意：** 生产环境部署前请务必修改 `SECRET_KEY` 和 `INIT_PASSWORD`。

远程恢复会在目标机 `/tmp/gitea-manager/restore-<任务ID>/` 下启动独立后台作业。数据库导入、解压、仓库与数据复制、权限修复以及 hooks/keys 生成不占用长时间 SSH 会话；Manager 只做短连接轮询。上传文件会校验大小和 SHA-256，备份中的 `app.ini` 会覆盖目标 `/data/gitea/conf/app.ini`。恢复失败时现场默认保留 7 天，成功并通过 API 与 Commit ID 验证后才清理。

## 项目结构

```
gitea-manager/
├── backend/                # Flask API 后端
│   ├── app.py              # 应用工厂
│   ├── config.py           # 配置
│   ├── models.py           # 数据模型
│   ├── auth.py             # 认证
│   ├── requirements.txt    # Python 依赖
│   ├── Dockerfile
│   ├── run.py              # 本地开发入口
│   ├── routes/             # API 路由
│   │   ├── auth_routes.py
│   │   ├── server_routes.py
│   │   ├── backup_routes.py
│   │   ├── restore_routes.py
│   │   ├── schedule_routes.py
│   │   └── settings_routes.py
│   ├── services/           # 业务逻辑
│   │   ├── gitea_service.py
│   │   ├── ssh_service.py
│   │   ├── docker_service.py
│   │   ├── scheduler_service.py
│   │   └── task_manager.py
│   └── wheels/             # 离线依赖包
├── frontend/               # Vue 3 前端
│   ├── src/
│   │   ├── views/          # 页面组件
│   │   ├── api/            # API 客户端
│   │   └── router/         # 路由配置
│   ├── nginx.conf          # 生产环境 nginx 配置
│   ├── Dockerfile
│   └── vite.config.js
├── data/                   # 运行时数据（不纳入版本控制）
├── docker-compose.yml
├── deploy.sh
└── README.md
```

## API 概览

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/login` | POST | 登录 |
| `/api/servers` | GET/POST | 服务器列表/添加 |
| `/api/servers/:id` | GET/PUT/DELETE | 服务器详情/编辑/删除 |
| `/api/servers/:id/check` | POST | 检查服务器状态 |
| `/api/backups` | GET/POST | 备份列表/创建备份 |
| `/api/restore` | POST | 执行恢复 |
| `/api/restore-tasks/:id/steps` | GET | 查询恢复步骤、指标和有限长度日志尾部 |
| `/api/schedules` | GET/POST | 定时任务列表/创建 |
| `/api/schedules/:id` | PUT/DELETE | 编辑/删除定时任务 |
| `/api/settings` | GET/PUT | 系统设置 |
| `/api/integrations/v1/health` | GET | 服务令牌认证的只读集成健康检查 |
| `/api/integrations/v1/events?after_seq=&limit=` | GET | 单调序号增量事件与补传游标 |
| `/api/integrations/v1/tasks/:type/:id` | GET | 不含凭据和敏感配置的任务详情 |

外部集成默认关闭，接口返回 HTTP 404；即使数据库保留旧令牌也不能访问。需要接入时，配置 `INTEGRATION_ENABLED=true` 和独立的 `INTEGRATION_SERVICE_TOKEN`，重新创建后端容器使配置生效。未显式启用的旧部署升级后也默认关闭；已接入 Sentinel 的部署需要补充启用开关。关闭不删除历史事件和令牌记录。

集成接口使用 `Authorization: Bearer <INTEGRATION_SERVICE_TOKEN>`。备份、恢复、镜像和定时任务的状态变化写入同库 outbox；接口不会返回 Gitea API token、SSH 密钥、密码、备份路径或未脱敏配置。轮换环境变量后重启服务会吊销旧服务令牌并记录审计。

## License

[MIT](LICENSE)
# AI Code Review

新增独立 Worker、OpenCode / New API HTTP 执行器和 PR 行内审查管理入口，默认全局关闭。使用前阅读 [技术与部署说明](docs/AIReview技术与部署说明.md) 和 [直接发布验收报告](docs/AIReview直接发布验收报告.md) 和 [历史模型质量记录](docs/AIReview验收报告.md)。模型质量未达到验收门槛时不得将工程链路通过等同于生产效果通过。

行内意见在本地校验后直接通过 REST API 发布，官方 Gitea 1.26.4 无需补丁。发布处理中恰好 Push 的位置竞争暂未纳入本轮保证；不可靠锚点仍降级为普通意见。

AI Review 支持开发者反馈和人工审查经验数据集，详见 [人工反馈与数据集](docs/AIReview人工反馈与数据集.md)。升级后需启动 `review-feedback`，并重新核对仓库 Webhook。

## 外接审批规则（开发验收候选）

Manager 新增“审批规则”入口，复用 Gitea 原生批准票，支持总人数与指定组逐项核对、影子组件和 PR 展示。正式强制接入仍被发布门禁阻断，APR-RACE-001（P2）未关闭，不能视为 GitLab 企业版完整等价实现。

本次内网测试按[内网测试包说明](docs/审批规则内网测试包说明.md)执行；多用户并发与双 24 小时在发包后继续。详见 [使用与接入说明](docs/审批规则使用与接入说明.md)、[开发验收报告](docs/审批规则开发验收报告.md)、[测试矩阵](docs/审批规则测试矩阵.md) 和 [缺陷与发布门禁](docs/审批规则缺陷与发布门禁.md)。
