# Gitea Manager

多 Gitea 实例统一管理面板，支持远程备份、恢复与定时调度。

## 功能特性

- **服务器管理** — 添加、编辑、移除 Gitea 实例，实时查看状态、版本、仓库数、用户数、磁盘用量
- **一键备份** — 通过 SSH 连接远程主机，使用 Docker 命令备份 Gitea 的仓库、数据库和配置
- **灵活恢复** — 将备份恢复到原服务器或其他服务器
- **定时调度** — Cron 式定时备份任务，自动执行并记录日志
- **系统设置** — 管理员密码修改、本机 IP 配置

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

**注意：** 生产环境部署前请务必修改 `SECRET_KEY` 和 `INIT_PASSWORD`。

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
| `/api/schedules` | GET/POST | 定时任务列表/创建 |
| `/api/schedules/:id` | PUT/DELETE | 编辑/删除定时任务 |
| `/api/settings` | GET/PUT | 系统设置 |

## License

[MIT](LICENSE)
