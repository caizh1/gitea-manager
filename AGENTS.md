# Gitea Manager - AGENTS.md

本文档是给 AI coding agent 使用的项目入口说明，目标是让一个全新的 session 能快速理解当前代码库、部署约束和容易踩坑的地方。

**命名说明：** 文件应继续叫 `AGENTS.md`。这是 Codex/AI agent 常用的约定入口名，适合放工程上下文和协作规则；面向普通使用者的说明继续放在 `README.md`。

---

## 1. 项目概述

Gitea Manager 是一个 Web 管理面板，用于统一管理多台 Gitea 服务器的备份、恢复、定时调度、镜像同步、统计分析、提交消息门禁和全局告警。

**核心功能：** 服务器管理 · 一键备份 · 灵活恢复 · 定时调度 · 统计分析 · 镜像管理 · 提交消息门禁 · 全局告警 · 系统设置

**技术栈：**

| 层级 | 技术 |
|------|------|
| 前端 | Vue 3 + Element Plus + Axios + Vite |
| 后端 | Python Flask + SQLAlchemy + Gunicorn |
| 数据库 | SQLite, 默认 `data/gitea-manager.db`, 启动时自动创建和迁移 |
| 部署 | Docker Compose, frontend nginx + backend gunicorn |
| 外部通信 | REST API + Paramiko SSH + Docker SDK + Gitea REST API |

---

## 2. 架构与运行方式

```
浏览器 http://<IP>:5480
  |
  v
frontend nginx
  |-- /           -> Vue SPA, history mode
  |-- /api/*      -> proxy_pass backend:5000
  |-- /index.html -> SPA fallback
  |
  v
backend gunicorn
  |-- routes/     -> Flask Blueprint API
  |-- services/   -> 备份、恢复、调度、镜像、统计、提交门禁等业务逻辑
  |-- models.py   -> SQLAlchemy 模型
  |-- auth.py     -> Flask-Login
  |-- app.py      -> create_app(), 迁移, 蓝图注册, scheduler 启动
```

| 服务 | 容器内端口 | 宿主机端口 |
|------|------------|------------|
| frontend nginx | 80 | 5480 |
| backend gunicorn | 5000 | 5000 |

后端容器挂载 `data/`、SSH key 和 Docker socket。Docker socket 用于操作本机 Gitea/PostgreSQL 容器；远程服务器通过 SSH 执行 Docker 命令和传输文件。

---

## 3. 数据模型速览

所有 ORM 模型集中在 `backend/models.py`，启动时 `app.py` 会执行 `db.create_all()` 和一组幂等 SQL 迁移，兼容旧 SQLite 数据库。

| 模型 | 作用 |
|------|------|
| `GiteaServer` | Gitea 服务器连接信息、角色、容器名、API token、状态和资源信息 |
| `Backup` | 备份记录，关联源服务器，保存文件名、路径、状态、失败原因、源 token 快照 |
| `RestoreTask` | 恢复任务，关联备份和目标服务器，包含 `progress_*` 实时进度字段 |
| `ScheduledTask` | 定时任务，源服务器 + 多个目标服务器 JSON，包含 `progress_*`、`current_backup_id`、`current_restore_task_id` 和恢复序号 |
| `ScheduleLog` | 每次调度执行日志，含 `backup_id`、`backup_filename`、`backup_error`、`restore_results` |
| `Setting` / `User` | `Setting` 存管理员密码和 `host_ip`；`User` 是 Flask-Login 虚拟用户，固定 id=1 |
| `RepoStatistics` / `CommitStatistics` / `AuthorStatistics` | 仓库、提交趋势、作者贡献统计 |
| `MirrorConfig` / `MirrorRepoStatus` | Gitea 镜像配置和单仓库同步状态 |
| `RestoreVerification` / `BackupRepoCommit` | 备份时采集 commit 校验集，恢复后验证目标仓库 commit ID |
| `RestoreStepLog` | 记录恢复步骤状态、耗时、退出码、诊断指标和有限长度日志尾部 |
| `CommitMessageRule` / `CommitGateAssignment` | 提交消息规则和仓库应用状态 |
| `Alert` | 全局告警，记录备份/恢复/镜像等失败事件 |

重要关系：

- `Backup.source_server_id -> GiteaServer.id`
- `RestoreTask.backup_id -> Backup.id`, `RestoreTask.target_server_id -> GiteaServer.id`
- `ScheduledTask.source_server_id -> GiteaServer.id`, `target_ids` 用 JSON 保存多个目标服务器 id
- `ScheduleLog.schedule_task_id -> ScheduledTask.id`
- `MirrorConfig` 同时关联源服务器和目标服务器
- `CommitGateAssignment` 以 `(server_id, repo_name)` 保证一个仓库只有一条门禁应用记录

---

## 4. 后端结构

### 4.1 入口与配置

| 文件 | 用途 |
|------|------|
| `backend/app.py` | 应用工厂 `create_app()`；初始化 Flask、SQLAlchemy、CORS、认证；执行数据库迁移；注册蓝图；检查 `host_ip`；启动 scheduler |
| `backend/run.py` | 本地 Flask dev server 入口，监听 `:5000` |
| `backend/config.py` | 环境变量配置：`DATABASE_URL`、`BACKUP_DIR`、`SSH_KEY_PATH`、`SECRET_KEY`、`INIT_PASSWORD` |
| `backend/auth.py` | Flask-Login 集成和密码校验 |
| `backend/models.py` | 全部 SQLAlchemy 模型和 `get_setting()` / `set_setting()` |
| `backend/Dockerfile` | 基于私有 Python 镜像，使用 `backend/wheels/` 离线安装依赖，Gunicorn 默认 2 workers |

### 4.2 API 蓝图

所有业务 API 前缀为 `/api`，除登录/session 外需要登录。

| 文件 | 蓝图 | 主要端点 |
|------|------|----------|
| `auth_routes.py` | `auth_bp` | `GET /session`, `POST /login`, `POST /logout` |
| `server_routes.py` | `server_bp` | 服务器 CRUD、连接测试、刷新信息、删除影响检查、详情 |
| `backup_routes.py` | `backup_bp` | `GET/POST /backups`, 删除备份, 下载备份 |
| `restore_routes.py` | `restore_bp` | 恢复任务列表、创建恢复、步骤详情、恢复验证、备份 commit 列表 |
| `schedule_routes.py` | `schedule_bp` | 定时任务 CRUD、手动执行、执行日志和 `steps` |
| `settings_routes.py` | `settings_bp` | 读取/更新 `host_ip` |
| `alert_routes.py` | `alert_bp` | 告警列表、摘要、清除告警 |
| `dashboard_routes.py` | `dashboard_bp` | 仪表盘最近活动 |
| `mirror_routes.py` | `mirror_bp` | 镜像配置 CRUD、初始化、同步、单仓库同步、状态 |
| `statistics_routes.py` | `statistics_bp` | 概览、提交趋势、仓库排行、作者排行、作者详情和刷新 |
| `commit_gate_routes.py` | `commit_gate_bp` | 提交规则 CRUD、仓库列表、应用/移除门禁、规则测试 |

### 4.3 服务层

| 文件 | 作用 |
|------|------|
| `gitea_service.py` | 核心备份/恢复/服务器检测逻辑；本地 Docker 和远程 SSH 两套路径 |
| `task_manager.py` | 普通备份/恢复的异步线程入口 |
| `schedule_runner.py` | 定时任务共享 runner；手动执行和后台调度共用；包含数据库原子 claim |
| `schedule_progress.py` | 定时任务总进度写入和响应聚合，恢复阶段会读取当前 `RestoreTask.progress_*` |
| `scheduler_service.py` | 后台调度线程，每 60 秒扫描启用任务，到点后先 claim 再执行 |
| `restore_progress.py` | 恢复任务进度写入 helper |
| `restore_step_service.py` | 恢复步骤日志、指标和有限长度输出尾部持久化 |
| `remote_job_service.py` | 远端后台作业启动、状态轮询、断线重连、超时终止和日志读取 |
| `commit_service.py` | 备份采集仓库 commit 集，恢复后做 commit ID 验证 |
| `commit_gate_service.py` | 规则测试、仓库查询、安装/移除 Gitea `pre-receive.d/gitea-manager-commit-msg` hook |
| `mirror_service.py` | 镜像配置、创建镜像、同步和状态更新 |
| `statistics_service.py` | 仓库/提交/作者统计采集和查询 |
| `alert_service.py` | 失败事件告警记录和清除 |
| `ssh_service.py` | Paramiko 封装：远程命令、SFTP 上传/下载、连接测试 |
| `docker_service.py` | 本地 Docker SDK 封装：容器执行、文件复制 |

---

## 5. 前端结构

### 5.1 入口

| 文件 | 用途 |
|------|------|
| `frontend/src/main.js` | Vue 应用启动，注册 Element Plus、图标和 Router |
| `frontend/src/App.vue` | 根布局：认证状态、侧边栏、顶栏面包屑、全局 `AlertBell`、未配置 `host_ip` 时阻断写操作入口 |
| `frontend/src/router/index.js` | Vue Router 路由表 |
| `frontend/src/api/index.js` | Axios 实例，`baseURL=/api`，`withCredentials=true`，401 自动刷新 |
| `frontend/vite.config.js` | Vite dev server `:5173`，代理 `/api` 到 `localhost:5000` |
| `frontend/nginx.conf` | 生产 nginx，SPA fallback 和 `/api` 反代 |

`App.vue` 的导航细节：

- `/servers/:id` 高亮“服务器管理”。
- 所有 `/statistics...` 子路由都高亮“统计分析”。
- 统计分析深层面包屑为 `首页 / 统计分析 / 作者贡献排行 / 作者名`，并保留 `server_id` query。
- 顶栏包含全局告警铃铛和退出登录按钮。

### 5.2 页面组件

| 文件 | 路由 | 功能 |
|------|------|------|
| `Login.vue` | 未认证时显示 | 登录页 |
| `Dashboard.vue` | `/dashboard` | 统计卡片、服务器卡片、最近活动、快捷操作 |
| `Servers.vue` | `/servers` | 服务器列表、添加/编辑/删除、连接测试 |
| `ServerDetail.vue` | `/servers/:id` | 单服务器详情、资源信息、日志、备份/恢复历史 |
| `Backups.vue` | `/backups` | 备份列表、创建/下载/删除；failed 行显示错误原因并支持完整错误弹窗 |
| `Restore.vue` | `/restore` | 恢复表单、恢复历史、恢复进度条、验证入口 |
| `Schedule.vue` | `/schedules` | 定时任务列表、running 进度条、展开执行日志 `steps`、手动执行、编辑、删除、运行中轮询 |
| `Mirrors.vue` | `/mirrors` | 镜像配置、初始化、同步、仓库状态 |
| `CommitGates.vue` | `/commit-gates` | 提交消息规则、仓库门禁应用/移除、规则测试 |
| `Statistics.vue` | `/statistics` | 概览、提交趋势、仓库排行、作者排行、语言和代码/文档分布 |
| `AuthorList.vue` | `/statistics/authors` | 作者贡献排行列表，支持搜索、排序和跳转详情 |
| `AuthorDetail.vue` | `/statistics/authors/:name` | 作者详情、趋势图、仓库贡献明细 |
| `Settings.vue` | `/settings` | 系统设置，目前主要配置本机 IP |

---

## 6. 关键工作流

### 6.1 备份

```
POST /api/backups { source_server_id }
  -> 创建 Backup(status=running)
  -> task_manager.run_backup(backup_id)
  -> do_backup(backup_id)
```

要点：

- 备份文件名包含高精度时间后缀，避免本地 `data/backups` 产物覆盖。
- 容器内临时文件名包含 `backup.id`，形如 `/tmp/gitea-manager-backup-<id>-<filename>`，避免并发任务同名冲突。
- 本地服务器优先用 Docker SDK 执行和拷贝，失败时会尝试走远程 SSH 路径。
- 远程服务器通过 SSH 在目标机器执行 `docker exec gitea dump`、`docker cp`，再 SFTP 下载到本机。
- 失败详情写入 `Backup.error_msg`，备份管理页会直接展示简短原因，并可弹窗查看完整 stdout/stderr。
- 备份成功后会采集仓库 commit 校验集到 `BackupRepoCommit`，供恢复后验证。

### 6.2 恢复

```
POST /api/restore { backup_id, target_server_id }
  -> 创建 RestoreTask(status=running)
  -> task_manager.run_restore(task_id)
  -> do_restore(task_id)
```

要点：

- 只有 `Backup.status == success` 的备份允许恢复。
- 恢复过程会持续更新 `RestoreTask.progress_stage`、`progress_label`、`progress_percent`、`progress_detail`。
- 远程恢复先上传并校验 ZIP 大小/SHA-256、完成解压和内容检查，再停止目标 Gitea。
- 远端数据库导入、解压、仓库/数据复制、权限修复和 hooks/keys 使用 `/tmp/gitea-manager/restore-<task_id>/steps/<step>/` 后台作业；Manager 每 5 秒用短 SSH 连接轮询，默认允许 300 秒 SSH 重连并限制长步骤最多运行 7200 秒。
- `repos/` 恢复到 `/data/git/repositories/`，`data/` 恢复到 `/data/gitea/`；保留目标 `app.ini` 的连接配置，只将数据加密密钥切换到备份的源密钥。
- `restore_secrets.py` 支持普通配置、文件密钥与容器环境覆盖；有覆盖时保留实际端口、挂载和网络重建容器，并同步目标 Compose 的密钥项。原容器及 Compose 回退文件保留到验证结束；不能只改 `app.ini` 后宣称重启兼容。
- 新备份携带密钥快照，包括源实例外置文件密钥。历史备份若缺失外置密钥，需要重新备份源实例；不要求重装目标。备份和回退文件均按敏感数据保管，密钥不能进入日志。边界与验收见 `docs/restore-secret-compatibility.md`。
- 每个步骤写入 `RestoreStepLog`，长步骤每 30 秒更新进程、输出大小、源/目标大小、数据库状态和磁盘空间等有限诊断信息，不保存 Token、SQL 正文、app.ini 内容或密码。
- 远程恢复只有在同步源 API Token并通过 API 与 Commit ID 验证后才清理现场；失败现场由后续恢复任务清理超过 7 天的目录。
- 本地和远程恢复都会停止目标 Gitea、覆盖仓库/配置、重建或导入 PostgreSQL、修复权限、重新生成 hooks/keys、启动 Gitea。
- 恢复接近完成后执行 commit ID 验证，结果写入 `RestoreVerification`；失败时进度会进入 `verify_failed`。

### 6.3 定时调度

```
start_scheduler(app)
  -> 每 60 秒扫描 enabled ScheduledTask
  -> 当前 UTC hour/minute 匹配
  -> claim_schedule_task(task_id)
  -> run_schedule_task(task_id)
```

要点：

- Gunicorn 使用 2 workers，每个 worker 都可能启动 scheduler 线程，因此必须依赖数据库原子 claim 去重。
- `schedule_runner.claim_schedule_task()` 使用条件 `UPDATE` 抢占任务，要求任务不在 running 且超过 5 分钟冷却窗口。
- 手动“立即执行”和后台 scheduler 都必须 claim 成功才进入 runner；claim 失败时返回“任务正在运行或冷却中”。
- runner 会先备份源服务器，再按 `target_ids` 逐台恢复。
- 总进度写在 `ScheduledTask.progress_*`，恢复阶段会根据 `current_restore_task_id` 读取当前 `RestoreTask.progress_*` 并折算到整条任务进度。
- `/api/schedules/<id>/logs` 返回兼容旧字段，并新增 `steps` 数组；前端优先渲染 `steps`，每次执行拆成“备份”和每个“恢复”步骤。
- `Schedule.vue` 在存在 running 任务时每 2 秒刷新列表，同时刷新已展开行日志，避免日志缓存停留在旧结果。

### 6.4 提交消息门禁

提交门禁由 `commit_gate_service.py` 安装到 Gitea 裸仓库：

```
/data/git/repositories/<owner>/<repo>.git/hooks/pre-receive.d/gitea-manager-commit-msg
```

要点：

- 规则使用 `grep -E` 语法，默认规则类似 `[ID-123] type(scope): subject`。
- 门禁是服务器端 `pre-receive` hook 方案，主要拦截 Git push 新增的 commit。
- Gitea 网页编辑、PR 合并等路径是否触发，取决于 Gitea 实际 hook 调度链，不能简单等同于“本地 push 限制”或“PR 合并限制”。
- 排查门禁是否生效时，同时检查仓库实际路径大小写、`hooks/pre-receive` wrapper、`hooks/pre-receive.d/gitea-manager-commit-msg` 文件、执行权限和 owner。
- UI 中 `Hook installed` 只代表文件安装成功，不代表目标 Gitea 版本一定会执行 `pre-receive.d` 下的脚本。

### 6.5 认证与 host_ip

- 登录接口验证 `ManagerAccount.password_hash`，首次启动从 `Setting.admin_password` 迁移本地管理员；不存在旧密码时才用 `INIT_PASSWORD` 初始化。
- 默认管理员密码为 `admin123`，生产环境必须通过环境变量修改。
- 顶栏“账号与安全”对应 `/account/security`，本地管理员通过 `/api/access/password` 修改本人密码。与首次强制改密共用 `PasswordChangeForm.vue`；后端同时更新两处密码哈希、撤销旧会话、轮换当前会话与 CSRF，并以条件更新避免并发覆盖。
- `session.user.auth_source` 区分本地与目录账号，不按用户名或管理员角色推断；目录账号只显示公司目录改密说明。账号页面独立于本机 IP 和组织授权门禁。
- 侧栏主图标使用固定版本 `@lucide/vue`，图标与按钮留在正常文档流，折叠菜单禁用宽度过渡以避免短暂裁切。操作与验收范围见 `docs/account-security.md`。
- `app.py` 的 `before_request` 会阻止部分写操作，直到 `Settings.vue` 配置 `host_ip`。
- 读接口、登录、设置读取等不会被 `host_ip` 阻断。

### 6.6 告警、镜像、统计

- `alert_service.py` 记录备份、恢复、镜像等失败事件，`AlertBell.vue` 在全局顶栏显示摘要。
- `mirror_service.py` 通过 Gitea API 和远程命令管理镜像配置、初始化和同步。
- `statistics_service.py` 采集仓库、提交周期和作者贡献数据；作者详情路径较深，所以全局面包屑承担定位和跳转。

---

## 7. 离线环境与依赖约束

- 目标 Linux 环境可能完全离线，不能依赖 npm/pip 外网源。
- Docker 基础镜像来自私有仓库 `10.10.5.21:5001`，目标环境需要能访问该仓库。
- 后端依赖通过 `backend/wheels/` 离线安装，Dockerfile 已使用 `--no-index --find-links=/tmp/wheels/`。
- 前端容器不构建源码，只 COPY `frontend/dist/` 到 nginx。
- 打包部署时必须包含 `frontend/dist/` 和 `backend/wheels/`。
- `.gitignore` 通常会忽略 `frontend/dist/`、`data/backups/`、数据库文件、缓存和 `node_modules/`。

---

## 8. 常用命令

```bash
# 后端本地开发
cd backend
pip install -r requirements.txt
python run.py

# 前端本地开发
cd frontend
npm install
npm run dev

# 前端生产构建，离线部署前必须执行
cd frontend
npm run build

# Docker 部署
docker compose build
docker compose up -d
docker compose ps
docker compose logs backend
docker compose logs frontend
```

后端语法检查可用：

```bash
python -m py_compile backend/app.py backend/models.py backend/routes/*.py backend/services/*.py
```

Windows PowerShell 下通配符传给 Python 可能不会自动展开，必要时用 PowerShell 先枚举文件再调用。

---

## 9. 打包与部署

项目默认交付规则：每次完成代码或文档修改后，都必须重新构建前端并更新 `gitea-manager.tar.gz` 离线部署包；只有只读侦察、纯问答或未改动文件时才不需要打包。

代码变更后，如果要交付离线部署包，先构建前端：

```bash
cd frontend
npm run build
```

然后在项目目录外或项目目录内打包，包内直接平铺 `frontend/`、`backend/`、`data/` 等目录，不加外层目录：

```bash
tar czf gitea-manager.tar.gz \
  --exclude=.git \
  --exclude=__pycache__ \
  --exclude=node_modules \
  -C /path/to/gitea-manager \
  frontend backend data docker-compose.yml deploy.sh AGENTS.md README.md
```

Linux 端部署：

```bash
mkdir -p gitea-manager
cd gitea-manager
tar xzf ../gitea-manager.tar.gz
bash deploy.sh
```

---

## 10. UI 风格

整体是 Apple 毛玻璃风格：

- 大量半透明白色背景、`backdrop-filter: blur(...)`、细边框和柔和阴影。
- 登录页使用动态渐变和浮动光效。
- 侧边栏为浅色毛玻璃，active 项有蓝色强调。
- 内容区使用渐变底色、玻璃卡片和页面切换动画。
- 表格型业务页面应保持信息密度，避免做成营销落地页。
- 既有 Element Plus 组件优先复用，图标优先用 `@element-plus/icons-vue`。

---

## 11. 新 session 接手注意事项

- 本项目日常功能、界面与修复统一在 `master` 工作；开始前执行 `git worktree list`，使用唯一承载 `master` 的工作树，不因旧对话的工作目录或分支名称继续在旧修复分支开发。历史迁移分支保留为独立历史，不自动并入日常主线。
- 开始修改前先获取远端状态，核对本地 `master` 与 `origin/master`；存在分叉时先保留并合并双方提交。禁止从其他分支执行 `git push origin HEAD:master` 绕过本地主线，禁止用强推或重置解决分叉。
- 用户要求打包或发布时，先确认工作目录位于 `master`，本地和远端主线一致，目标功能提交均已包含，并以该提交构建；分支名称相同不能替代提交及文件核对。用户明确要求暂不打包时优先遵从，不更新安装包、镜像和发布记录。
- 先读当前代码事实，再按需更新文档；不要相信旧的“数量描述”，这类数字很容易过期。
- 工作区可能已有用户改动，绝不要随意 `git reset --hard` 或回滚不属于自己的修改。
- 后端迁移集中在 `app.py` 的 SQL 列表，新增 SQLite 字段时要保持幂等。
- Gunicorn 2 workers 是既定部署形态，定时任务必须通过数据库 claim 去重，不要靠单 worker 规避。
- 备份和恢复都可能跨本地 Docker 与远程 SSH 两种路径，修 bug 时要同时考虑两边。
- 定时任务运行状态的用户可见进度来自 `ScheduledTask.progress_*`，恢复子步骤进度来自 `RestoreTask.progress_*`。
- 备份失败原因应该在备份管理页可见，不要只写告警。
- 提交门禁的安装成功不等于运行链路一定生效，排查时进入 Gitea 容器检查 bare repo hooks。
- 只改文档时不需要跑前端构建或后端编译；改代码时按影响范围跑 `py_compile`、`npm run build`，若用户要求部署包则重新打 tar。

## 12. 仓库策略中心第一期

- 新模块：`services/policy_rules.py`（字段与匹配差异）、`policy_gitea.py`（权限及共享限速）、`policy_tasks.py`（持久化批次、进程锁、双仓库执行）、`database_startup.py`（初始化互斥与锁自检）、`routes/policy_routes.py`、`RepositoryPolicies.vue`。
- 模型：`PolicyTemplate`、`PolicyBatch`、`PolicyRepoResult`、`PolicyRequestSlot`；API 前缀 `/api/repository-policies`。写入要求现有登录与专用 CSRF。审计必须写“共享管理员”，不虚构个人身份。
- `content_hash` 只防止过期编辑/预览；不能称为模板版本。已提交批次使用不可变内容快照，删除模板不能删除历史。
- 每实例批次串行、每批最多两个仓库并发，SQLite 统一分配请求时隙。文件锁和数据库必须在同一个可靠本地持久卷；不支持多节点或不可靠共享文件系统，不能去掉启动自检来绕过环境问题。
- Gitea 1.26.4 分单元团队可能总体 `permission=none`，必须检查 `units_map`。审批名单需 PR 读取权限，其他名单需代码写入权限。人员/团队可能被 Gitea 静默过滤，必须回读后才成功。
- 只修改模板指定的同名规则；未管理规则必须保留；保留规则与模板规则范围重叠时阻断。规则覆盖范围使用确定性分析，不把现有分支枚举当作未来无冲突的证明。
- 超时与进程中断不能盲目重复写入；保存部分修改/未知结果，并要求新预览。现有备份调度仅复用其原子领取与进度模式，不将策略任务塞入备份恢复业务状态机。
- 本地测试使用 `compose.local.yml`、`scripts/local-env.py` 及真实验收脚本，凭据与仓库在 `work/`；部署包必须排除 `work/`、`.env`、SQLite、密钥、缓存和 `node_modules`，同时包含 `docs/`、`scripts/`、本地 Compose、前端 dist 和后端 wheels。
- 完整边界、操作与后置范围以 `docs/仓库策略使用说明.md`、`docs/仓库策略待办清单.md` 为准。禁止把模拟 1,000 仓库测试写成真实千仓库验收，也禁止把 API 成功当作 Git/PR 保护行为通过。


## 13. 一键升级与 AI 自动接入

- 升级入口 `upgrade.sh`；控制器 `scripts/upgrade_runtime.py`，卷助手 `scripts/upgrade_volume.py`。保留同一 Compose 项目、实际挂载、端口、数据库、原 Review 主密钥和账号；已有 Review 数据不得生成新密钥代替。
- `.gitea-manager-upgrades/` 与 `compose.managed.json` 仅在部署机保存，含秘密，不得打包或提交。升级验证期间必须保留共享维护标记，同时阻止 Web 请求、策略/备份调度、审查、进度、反馈与初始化后台写入；健康和历史核对后才能解除。
- 自动接入在 `backend/ai_review/setup_service.py`、`setup_transport.py`、`host_policy.py` 与 `AIReviewSetup.vue`。管理端凭据只用于初始化；Worker/执行器不能新增 Docker socket、SSH 或管理凭据挂载。
- 分批失败的自动检查位于 `backend/ai_review/failure_checks.py`，结果附在原诊断中；历史任务通过 `GET /api/ai-review/tasks/<job>/failure-checks` 只读补查。只使用当前任务、会话与冻结配置，不调用模型、不跨单元放行证据、不自动重试或覆盖首因；读取上限和权限边界见 `docs/review-failure-checks.md`。
- 新分批计划使用 `batch_references.py` 的本单元短编号协议；持久化证据与全局定位不重编号。仅对完整响应且回执已确认的引用错误允许一次独立纠正，首次记录保留并计入成本上限；历史计划、未知结果、截断和格式错误不能因此自动重放。边界及真实模型样本见 `docs/review-evidence-references.md`。
- 第三版分批流程由 `batch_graph.py` 收集固定版本语法关系、`batch_flow.py` 生成主审与关联并汇总分组反证；不是按相同标识符配对。新任务固定 `batch_flow_version=4` 并使用 `batch_locations.py` 的源码位置协议，已存在的 2/3 任务保留原流程与编号协议；不得替换运行中的旧计划。全部主审与关联完成后事务性创建反证组，候选逐项校验且保留来源。验收见 `docs/review-flow-optimization.md`。
- 版本 4 的任务在已有原生回执确认 `finish_reason=length` 时，可将关联按原函数关系、反证按候选拆分，最多两层；不能接受截断报告，不能用于未知回执或普通格式错误。保留原失败、重算可见成本计划，仍需满足调用授权。此机制不同于一次引用纠正，不改动历史版本任务。
- Gitea 1.26.4 的 Webhook 测试只发 Push 且受订阅过滤；临时订阅必须恢复并回读。机器人身份和不确定的令牌回执必须先核对，不能盲目重建。
- AI 完整交付使用当前 Linux amd64 构建镜像及 `scripts/review-package.py --export-images --source-image-tag <固定标签>`。不得用只含前后端的旧通用 tar 命令代替。打包器会拒绝并发打包及归档期间内容变化。
- 使用独立标记实例验收，不操作用户现有环境。工程链路、实际内网模型效果、真实宿主机验收和人工精力指标分别报告，依据 `docs/一键升级验收报告.md`，不能互相替代。

## 14. 外接审批规则（仅开发与隔离验收）

- 2026-09-13 用户最新调整：先完成审批功能、权限、原生规则兼容、安装回退及最终包复验，交付内网测试包；多用户并发、容量、并发接入边界和影子与隔离强制各 24 小时改为发包后继续，不阻塞内网测试包。已有失败原样保留，生产强制接入仍受未关闭缺陷和完整验收门禁限制。

- 新模块 `backend/approval/`、`routes/approval_routes.py`、`ApprovalRules.vue`；扩展表在 Manager，禁止改 Gitea 源码、结构、历史或现有原生规则。
- 复用原生批准/撤回；总人数去重、每组单独计票。只支持官方 1.26.4 Linux amd64、明确分支、组织团队稳定 ID、新代码重新批准。
- `APR-RACE-001`（最终检查后撤回/移组）是 P2 已知失败、暂缓修复、后续必修。原脚本和十个首次失败保持；禁止通过修改断言或开放生产开关掩盖。
- 生产 enable 接口始终拒绝。隔离强制同时校验进程指定运行标识与独立容器标签，不能接受浏览器自报 test=true。
- Hook 临时文件不得放入 pre-receive.d/update.d；不重建包装器。安装预览需摘要/权限核对、目录锁、原子替换、模板热重载和真实页面；回执丢失用原包 verify-installed，不重复换令牌。
- 页面卸载和门禁停用分开；先记录停用原因，再移除本组件 Hook，保留新增业务和其他模板内容。不能恢复整个 Gitea 数据库或旧页脚覆盖他人更新。
- `scripts/approval-integration.py` 只创建独立实例；`--packaged --image-tag offline` 验证交付镜像，不挂载源码。当前40项有限断言不能替代全矩阵、持续写流量、24小时、生产与人工精力验收。
- 精确状态、证据与后续缺口见 `docs/审批规则开发验收报告.md`，不得根据核心路径通过宣称全部生产无感接入完成。

## 15. SVN 迁移中心

- `backend/svn_migration/` 管理来源、计划、固定执行快照和内容核验；API 为 `/api/svn-migrations`，仅平台管理员可用。`SvnMigrations.vue` 提供工作台、来源与仓库和执行记录路由；预检详情与执行详情使用独立地址。每次预检写入 `SvnPreflight` 与仓库关联索引，旧记录不覆盖；两个页面共用同一预检编号，输入变化后不得沿用旧结果。
- `svn-migration-worker` 是独立串行服务，不接入 AI 执行器，不挂载 Docker socket 或宿主机 SSH 密钥。数据库、任务锁、`svn-migrations/` 及 `svn-migration.key` 在同一个 Manager 持久卷；缺失原密钥不能重新生成替代品。
- 本期不创建业务账号、不迁移 SVN 权限、不自动冻结源端。正式执行需记录完整读取与停写确认；源变化、externals、目标冲突或无法确认的写入必须明确阻断或进入待确认。
- 只接管任务已记录的目标或带该任务唯一标记的写入回执；不能按同名接管其他群组或仓库。新页面明确选择已有群组时，使用稳定群组 ID、完整路径及授权摘要核对，保留成员和共享；旧自动群组模式遇到人工授权仍为新批次规划新私有群组。子群组仓库身份优先使用增强版 `full_path`，不能把内部兼容组织名当作对外路径。
- 复用原工具的历史转换，但保留逐提交内容/元数据核验、目标重新克隆和引用核对。重启先回收本任务标记的孤儿进程；暂停在仓库边界生效，取消保留现场。
- 迁移与备份、恢复和实例修改互斥；升级器必须识别并处理此 Worker，保留原密钥、数据库和检查点。离线包及首次安装包均需包含固定的 SVN Worker 镜像。
- 隔离验收入口 `scripts/svn-migration-acceptance.py`，原工具回归入口 `scripts/svn-original-regression.py`，证据在 `work/`。真实生产、大仓库与人工节省 80% 指标没有实测时必须标为未验证。使用边界见 `docs/svn-migration.md`。


## 16. 默认单次 AI Review

- 新任务使用流程版本 6，默认 `batch_splitting_enabled=false`，硬性限制新增与删除合计不超过 1200 行；所有新增和删除仍完整审查。完整 PR 和固定版本相关上下文一次主审，P0/P1 汇总后一次独立反证；历史意见生命周期核对继续保留。
- 行数超限、完整输入超 Token 额度、反证超额或输出被截断时明确停止，不能切回分批、删减证据或接受不完整报告。位置纠正仍最多一次，只允许修改位置。
- 分批实现保留供以后显式启用；当前界面不提供启用入口。旧任务继续其冻结的计划（含第五版净增 1000 行规则），配置开关只影响保存后创建的新任务。
