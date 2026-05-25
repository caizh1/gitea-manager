# Gitea Manager — AGENTS.md

本文档供 AI Agent 在新会话中快速理解项目全貌，无需重新探索代码结构。

---

## 1. 项目概述

Gitea Manager 是一个 Web 管理面板，用于统一管理多台 Gitea 服务器的备份、恢复和定时调度。

**核心功能：** 服务器管理 · 一键备份 · 灵活恢复 · 定时调度 · 系统设置

**技术栈：**

| 层级 | 技术 |
|------|------|
| 前端 | Vue 3 + Element Plus + Axios + Vite |
| 后端 | Python Flask + SQLAlchemy + Gunicorn |
| 数据库 | SQLite（`data/gitea-manager.db`，自动创建） |
| 部署 | Docker Compose（nginx + python 容器） |
| 通信 | REST API + Paramiko (SSH) + Docker SDK |

---

## 2. 架构全景图

```
浏览器 (http://<IP>:5480)
  │
  ▼
┌──────────────────────────────────────┐
│  frontend (nginx :80 → 宿主机 :5480)  │
│  ├─ /           → SPA (Vue Router)    │
│  ├─ /api/*      → proxy_pass backend  │
│  └─ /index.html → 兜底 (history mode) │
└────────────┬─────────────────────────┘
             │ Docker network
             ▼
┌──────────────────────────────────────┐
│  backend (gunicorn :5000)            │
│  ├─ routes/           API 路由层      │
│  ├─ services/         业务逻辑层      │
│  ├─ models.py         数据模型        │
│  ├─ auth.py           认证 (Flask-Login)│
│  └─ config.py         配置 (环境变量)  │
│                                      │
│  外部交互:                            │
│  ├─ SQLite DB         本地数据持久化   │
│  ├─ SSH (Paramiko)    远程服务器操作   │
│  ├─ Docker SDK        本地容器操作     │
│  └─ Gitea REST API    获取服务器信息   │
└──────────────────────────────────────┘
```

**端口映射：**

| 服务 | 容器内端口 | 宿主机端口 |
|------|----------|-----------|
| 前端 nginx | 80 | 5480 |
| 后端 gunicorn | 5000 | 5000 |

---

## 3. 数据模型

```
┌─────────────────┐     ┌──────────────────┐
│   GiteaServer    │◄────│     Backup       │
│─────────────────│     │──────────────────│
│ id (PK)         │     │ id (PK)          │
│ name            │     │ source_server_id  │──FK→ GiteaServer
│ role (primary/  │     │ filename          │
│   backup)       │     │ file_path         │
│ host            │     │ file_size         │
│ ssh_port        │     │ status (running/  │
│ ssh_user        │     │   success/failed) │
│ gitea_container │     │ error_msg         │
│ pg_container    │     │ started_at        │
│ pg_dbname       │     │ completed_at      │
│ pg_user         │     └────────┬─────────┘
│ gitea_port      │              │
│ gitea_url       │              │ FK
│ api_token       │              ▼
│ status          │     ┌──────────────────┐
│ version         │     │   RestoreTask    │
│ repo_count      │     │──────────────────│
│ user_count      │     │ id (PK)          │
│ is_local        │     │ backup_id (FK)    │
│ disk_usage      │     │ target_server_id  │──FK→ GiteaServer
│ created_at      │     │ status           │
└────────┬────────┘     │ error_msg        │
         │              │ started_at       │
         │ FK           │ completed_at     │
         ▼              └──────────────────┘
┌─────────────────┐     ┌──────────────────┐
│ ScheduledTask   │     │   ScheduleLog    │
│─────────────────│     │──────────────────│
│ id (PK)         │     │ id (PK)          │
│ name            │     │ schedule_task_id  │──FK→ ScheduledTask
│ enabled         │     │ status           │
│ source_server_id │──FK │ log              │
│ target_ids (JSON)│    │ backup_status    │
│ schedule_hour   │     │ restore_results  │
│ schedule_minute │     │   (JSON)         │
│ last_run_at     │     │ started_at       │
│ last_status     │     │ completed_at     │
│ last_log        │     └──────────────────┘
│ created_at      │
└─────────────────┘

┌─────────────────┐
│    Setting      │     ┌──────────────────┐
│─────────────────│     │   User (虚拟)     │
│ key (PK)        │     │──────────────────│
│ value           │     │ id = 1 (硬编码)   │
└─────────────────┘     │ 密码存于 Setting   │
  admin_password        │ (admin_password)  │
  host_ip               └──────────────────┘
```

**表关系说明：**
- `GiteaServer` — 核心实体，存储 Gitea 服务器的连接信息和运行状态
- `Backup` → `GiteaServer`（多对一）：一个服务器可以有多个备份记录
- `RestoreTask` → `Backup` + `GiteaServer`：恢复任务同时关联备份文件和目标服务器
- `ScheduledTask` → `GiteaServer`（多对一）：定时任务指定一个源服务器和多个目标服务器（JSON）
- `ScheduleLog` → `ScheduledTask`（多对一）：每次调度执行的日志
- `Setting` — 键值存储，存管理员密码（bcrypt）和本机 IP
- `User` — 不存储到数据库，硬编码 id=1，通过 `Setting.admin_password` 验证

---

## 4. 文件清单

### 4.1 顶层文件

| 文件 | 用途 |
|------|------|
| `docker-compose.yml` | 定义 backend + frontend 两个服务，挂载 data 目录、SSH 密钥、Docker socket |
| `deploy.sh` | 一键部署脚本：创建目录 → docker compose build → up -d → 健康检查 → 打印访问信息 |
| `README.md` | 项目说明文档，面向使用者 |
| `AGENTS.md` | 本文件 |
| `.gitignore` | 排除 node_modules、__pycache__、*.db、data/backups/、dist/ |

### 4.2 后端（`backend/`）

#### 入口与配置

| 文件 | 用途 |
|------|------|
| `app.py` | **应用工厂 `create_app()`**。初始化 Flask、SQLAlchemy、CORS、认证；执行数据库迁移（`db.create_all()` + 原始 SQL ALTER）；种子化默认管理员密码；注册 6 个路由蓝图；添加 `before_request` 钩子（未配 host_ip 则拦截写操作）；启动后台调度器 |
| `run.py` | 本地开发入口，调用 `create_app()` 启动 Flask dev server (`:5000`, debug=True) |
| `config.py` | 读取环境变量提供配置：`DATABASE_URL`、`BACKUP_DIR`、`SSH_KEY_PATH`、`SECRET_KEY`、`INIT_PASSWORD` |
| `auth.py` | Flask-Login 集成。`init_auth(app)` 绑定 LoginManager，`Auth.login(password)` 验证 bcrypt 密码 |
| `models.py` | **所有 ORM 模型**。6 个 SQLAlchemy 模型类 + `db` 实例 + `get_setting()`/`set_setting()` 辅助函数 |
| `requirements.txt` | Python 依赖清单 |
| `Dockerfile` | 基于私有仓库 python:3.11-slim，通过 wheels/ 离线安装依赖，CMD gunicorn 2 workers |
| `.dockerignore` | 排除 __pycache__、*.pyc、.env |

#### 路由层（`backend/routes/`）

所有路由 Blueprint 前缀为 `/api`，均需登录（除登录接口外）。

| 文件 | 蓝图 | 主要端点 |
|------|------|---------|
| `routes/__init__.py` | — | 空文件，声明 package |
| `routes/auth_routes.py` | `auth_bp` | `GET /session`（检查登录）、`POST /login`、`POST /logout` |
| `routes/server_routes.py` | `server_bp` | CRUD 服务器（`GET/POST /servers`, `GET/PUT/DELETE /servers/<id>`）；`POST /servers/<id>/check`（测试连接）；`POST /servers/<id>/refresh`（刷新信息）；`GET /servers/<id>/detail`（详情含日志） |
| `routes/backup_routes.py` | `backup_bp` | `GET /backups`（列表）、`POST /backups`（创建备份 → 异步执行）、`DELETE /backups/<id>`、`GET /backups/<id>/download` |
| `routes/restore_routes.py` | `restore_bp` | `GET /restore-tasks`（列表）、`POST /restore`（创建恢复任务 → 异步执行） |
| `routes/schedule_routes.py` | `schedule_bp` | CRUD 定时任务、`POST /schedules/<id>/run`（手动触发，含 5 分钟冷却）、`GET /schedules/<id>/logs` |
| `routes/settings_routes.py` | `settings_bp` | `GET /settings`（获取 host_ip）、`POST /settings`（更新 host_ip） |

#### 业务逻辑层（`backend/services/`）

| 文件 | 核心类/函数 | 用途 |
|------|------------|------|
| `services/__init__.py` | — | 空文件，声明 package |
| `services/gitea_service.py` | `do_backup(backup_id)`, `do_restore(task_id)`, `fetch_server_info(server)`, `test_server_connection(server)`, `get_server_detail(server)` | **核心业务逻辑**（560行）。处理备份（本地 Docker 操作 / 远程 SSH+SFTP）、恢复、服务器状态检测 |
| `services/ssh_service.py` | `class SSHService` | Paramiko 封装：`exec()`（远程执行命令）、`get_file()`/`put_file()`（SFTP 传输）、`test_connection()` |
| `services/docker_service.py` | `local_exec()`, `local_cp_from()`, `local_cp_to()` | 本地 Docker SDK 操作：在容器内执行命令、从/向容器复制文件 |
| `services/scheduler_service.py` | `start_scheduler(app)` | 后台调度器。守护线程每 60 秒检查 `ScheduledTask`，匹配当前 UTC 时刻即触发执行 |
| `services/task_manager.py` | `class TaskManager` | 异步任务管理器。`run_backup()` / `run_restore()` 在独立线程中执行耗时操作，HTTP 请求立即返回 |

#### 离线依赖（`backend/wheels/`）

预下载的 Python wheel 文件，用于离线环境 pip install（`--no-index --find-links=/tmp/wheels/`）。

包含：Flask、Flask-CORS、Flask-Login、Flask-SQLAlchemy、SQLAlchemy、Paramiko、Cryptography、Requests、Gunicorn、Docker SDK、bcrypt、greenlet、cffi、pycparser、PyNaCl、urllib3、certifi、charset-normalizer、idna、MarkupSafe、Jinja2、Werkzeug、click、blinker、itsdangerous、packaging、typing_extensions 等。

### 4.3 前端（`frontend/`）

#### 入口与配置

| 文件 | 用途 |
|------|------|
| `index.html` | HTML 入口，标题"Gitea 管理系统" |
| `main.js` | Vue 应用启动：注册 Element Plus、全局图标、Vue Router |
| `App.vue` | **根组件**。认证状态管理、侧边栏+顶栏布局、host_ip 未配置时阻断提示 |
| `vite.config.js` | Vite 配置，端口 5173，代理 `/api` → `localhost:5000` |
| `router/index.js` | Vue Router 路由表（7 个页面，懒加载） |
| `api/index.js` | Axios 实例配置：`baseURL=/api`, `withCredentials=true`，401 自动刷新页面 |
| `package.json` | 依赖清单和脚本（`npm run dev` / `npm run build`） |
| `nginx.conf` | 生产环境 nginx 配置：SPA history 模式 + `/api` 代理到 `backend:5000`，超时 600s |
| `Dockerfile` | 基于 nginx，COPY dist/ + nginx.conf，**不在容器内构建** |
| `.dockerignore` | 排除源码和依赖文件，仅保留 dist/ |

#### 页面组件（`frontend/src/views/`）

| 文件 | 路由 | 功能描述 |
|------|------|---------|
| `Login.vue` | —（未认证时显示） | 6 色动态渐变背景 + 浮动光球 + 毛玻璃卡片，输入密码登录 |
| `Dashboard.vue` | `/dashboard` | 统计卡片（在线数、备份数、恢复成功率、调度数）+ 服务器卡片网格，支持快速备份/恢复 |
| `Servers.vue` | `/servers` | 服务器列表表格，添加/编辑/删除对话框，测试连接按钮 |
| `ServerDetail.vue` | `/servers/:id` | 单服务器详情：信息卡片、资源用量、容器日志、备份/恢复历史 |
| `Backups.vue` | `/backups` | 备份列表表格，创建备份对话框，下载/删除操作 |
| `Restore.vue` | `/restore` | 恢复执行表单 + 恢复历史表格，双重确认，轮询运行中的任务 |
| `Schedule.vue` | `/schedules` | 定时任务列表，展开行显示执行日志，创建/编辑/手动触发/删除，实时冷却倒计时 |
| `Settings.vue` | `/settings` | 系统设置表单，目前仅配置本机 IP |

---

## 5. 关键工作流

### 5.1 备份流程

```
POST /api/backups { source_server_id }
  │
  ├─► 创建 Backup 记录 (status=running)
  │
  └─► task_manager.run_backup(backup_id)  [新线程]
       │
       ├─ 本地服务器？
       │   ├─ docker exec <gitea_container> gitea dump -c ... → 生成 ZIP
       │   ├─ docker cp 从容器复制 ZIP 到宿主 data/backups/
       │   └─ 如果本地 Docker 失败 → 回退到远程模式
       │
       └─ 远程服务器？
           ├─ SSH.exec("docker exec gitea dump ...")
           ├─ SSH.exec("docker cp ...") → 复制到远程宿主
           └─ SSH.get_file() (SFTP) → 下载到本地 data/backups/
```

### 5.2 恢复流程

```
POST /api/restore { backup_id, target_server_id }
  │
  ├─► 创建 RestoreTask (status=running)
  │
  └─► task_manager.run_restore(task_id)
       │
       ├─ 本地目标？
       │   ├─ docker stop gitea
       │   ├─ 解压备份 ZIP
       │   ├─ docker cp 覆盖 repos/ app.ini
       │   ├─ docker exec pg dropdb + createdb
       │   ├─ docker exec psql 导入 SQL
       │   ├─ docker exec gitea regenerate hooks
       │   ├─ docker exec gitea regenerate keys
       │   └─ docker start gitea
       │
       └─ 远程目标？
           ├─ SSH.put_file() 上传 ZIP
           ├─ SSH.exec("docker stop gitea")
           ├─ SSH.exec("rm -rf ...") + unzip
           ├─ SSH.exec("docker cp ...") 拷贝文件
           ├─ SSH.exec("docker exec pg dropdb/createdb/psql")
           ├─ SSH.exec("docker exec gitea regenerate hooks/keys")
           └─ SSH.exec("docker start gitea")
```

### 5.3 定时调度

```
start_scheduler(app) → 守护线程循环（每 60s）
  │
  └─► 遍历所有 enabled=True 的 ScheduledTask
       │
       ├─ current_utc.hour == schedule_hour？
       ├─ current_utc.minute == schedule_minute？
       └─ last_run_at 超过 5 分钟？
           │
           └─► 执行备份 (source_server) → 遍历 target_ids → 逐个恢复
               ├─ 创建 ScheduleLog
               ├─ do_backup(source_server_id)
               ├─ for each target_id: do_restore()
               └─ 更新 ScheduleLog (status/log/backup_status/restore_results)
```

### 5.4 认证流程

```
用户输入密码 → POST /api/login
  │
  └─► Auth.login(password)
       ├─ Setting.query.get('admin_password')
       └─ check_password_hash(stored_hash, input)

首次启动时：
  db.create_all() → 检查 admin_password 是否存在
  → 不存在 → User.set_password(INIT_PASSWORD)
  → Setting(key='admin_password', value=bcrypt_hash)
```

---

## 6. 环境约束（重要！）

- **纯离线 Linux 目标环境**，无法访问外网（无 npm/pip 源）
- Docker 基础镜像来自私有仓库 `10.10.5.21:5001`（目标环境可访问）
- 后端 Python 依赖通过 `backend/wheels/` 离线安装（Dockerfile 已配置 `--no-index --find-links`）
- 前端通过预构建的 `frontend/dist/` 部署（Dockerfile 直接 COPY 到 nginx，不在容器内构建）
- **打包时必须包含 `frontend/dist/` 和 `backend/wheels/`**

---

## 7. 开发命令

```bash
# === 后端 ===
cd backend
pip install -r requirements.txt    # 注意：在线环境可直接安装，离线环境需预先下载 wheels
python run.py                      # 启动 Flask dev server, :5000

# === 前端 ===
cd frontend
npm install
npm run dev                        # Vite dev server, :5173, 代理 /api → localhost:5000
npm run build                      # 构建 dist/ （离线环境必须在开发机完成此步）

# === Docker ===
docker compose build               # 构建镜像
docker compose up -d               # 启动服务
docker compose ps                  # 检查运行状态
docker compose logs backend        # 查看后端日志
docker compose logs frontend       # 查看前端日志
```

---

## 8. UI 设计风格

Apple 毛玻璃风格。

**核心视觉特征：**
- 毛玻璃效果：`backdrop-filter: blur(24px)` + 半透明白色背景
- 渐变色系：蓝紫 `#667eea→#764ba2`、青绿 `#43e97b→#38f9d7`、粉橙 `#fa709a→#fee140`、紫粉 `#a18cd1→#fbc2eb`
- 登录页：6 色动态渐变背景 + 浮动光球
- 侧边栏：深色半透明 + 渐变 Logo + 发光 active 条
- 内容区：渐变底色 + 页面切换动画

---

## 9. 打包与部署

### 打包命令（在项目目录内执行）

```bash
tar czf gitea-manager.tar.gz \
  --exclude=.git \
  --exclude=__pycache__ \
  --exclude=node_modules \
  -C /path/to/gitea-manager \
  frontend backend data docker-compose.yml deploy.sh AGENTS.md README.md
```

解压后直接平铺为 `frontend/`、`backend/`、`data/` 等，没有外层目录包裹。

**排除说明：**
- `.git` — 版本控制历史
- `__pycache__` — Python 缓存
- `node_modules` — 前端依赖（Dockerfile 不使用，前端容器只用 dist/）

**包含说明（必须）：**
- `frontend/dist/` — **必须包含**，离线环境无法 `vite build`
- `backend/wheels/` — **必须包含**，离线环境无法 `pip install`

### Linux 端部署

```bash
mkdir -p gitea-manager && cd gitea-manager
tar xzf ../gitea-manager.tar.gz
bash deploy.sh
```

部署后访问 `http://<服务器IP>:5480`，默认密码 `admin123`（首次登录后请修改）。

---

## 10. 默认凭据与安全

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| 管理员密码 | `admin123` | 通过 `INIT_PASSWORD` 环境变量设置 |
| Flask Secret | `change-me-in-production` | 通过 `SECRET_KEY` 环境变量设置 |
| SSH 密钥 | `~/.ssh/id_rsa` | 通过 `SSH_KEY_PATH` 环境变量设置 |
| 数据库 | SQLite `data/gitea-manager.db` | 首次启动自动创建 |

**生产环境务必修改 `SECRET_KEY` 和 `INIT_PASSWORD`。**

---

## 11. 注意事项

- `app.py:30` 的 `db.create_all()` 使用了 `try/except OperationalError` 包裹，因此首次启动时数据库自动创建
- `app.py:33-45` 包含原始 SQL 迁移语句（ALTER TABLE 添加列等），这些需要在对已有数据库升级时执行（幂等设计）
- `routes/server_routes.py` 中创建服务器时会**立即**测试连接并获取信息，超时或失败不会回滚记录
- `routes/backup_routes.py` 的下载端点使用 `send_file` 直接返回备份文件
- `routes/restore_routes.py` 的恢复端点要求备份状态必须为 `success`
- `routes/schedule_routes.py` 的手动触发有 5 分钟冷却保护
- `services/task_manager.py` 使用守护线程执行异步任务，进程退出时可能中断
- `frontend/api/index.js` 的 401 拦截器会在认证失败时刷新页面
- 前端使用 `withCredentials: true` 发送 Cookie，需配合 Flask session 和 CORS
- Docker Compose 挂载了 Docker socket (`/var/run/docker.sock`)，用于操作宿主机和本地容器
