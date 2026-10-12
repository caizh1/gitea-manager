#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ -f compose.managed.json ]; then
    echo '[提示] 当前实例由一键升级器管理，请将新版升级包与 upgrade.sh 放在同一目录后执行 bash upgrade.sh。'
    exit 0
fi

export SSH_KEY_PATH="${SSH_KEY_PATH:-$HOME/.ssh/id_rsa}"

umask 077
mkdir -p data/backups

# 空 SECRET_KEY 由后端在数据卷生成独立持久密钥；原有效配置保持不变。
# Compose 不再携带公开初始密码，首次部署生成后仅保存在本机私有文件。
if [ -z "${INIT_PASSWORD:-}" ] && ! grep -Eq '^INIT_PASSWORD=.+$' .env 2>/dev/null; then
    INITIAL_PASSWORD="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
    printf '\nINIT_PASSWORD=%s\n' "$INITIAL_PASSWORD" >> .env
    chmod 600 .env
fi
if [ "${SECRET_KEY:-}" = 'change-me-in-production' ] || grep -Eq '^SECRET_KEY=.*change-me-in-production' .env 2>/dev/null; then
    echo '[错误] 原配置仍含公开会话密钥，请删除 .env 中的 SECRET_KEY 默认值，后端将生成持久随机密钥。'
    exit 1
fi

if [ ! -f data/gitea-manager.db ]; then
    echo "[INFO] 首次部署，数据库将在启动后自动创建"
fi

echo "[INFO] 构建 Docker 镜像..."
docker compose build

echo "[INFO] 启动服务..."
docker compose up -d

echo "[INFO] 等待服务就绪..."
sleep 3

if docker compose ps | grep -q "gitea-manager-api.*Up"; then
    echo "[OK] gitea-manager 后端已启动 (端口 5000)"
else
    echo "[WARN] 后端可能未启动，请检查: docker compose logs backend"
fi

if docker compose ps | grep -q "gitea-manager-ui.*Up"; then
    echo "[OK] gitea-manager 前端已启动 (端口 5480)"
else
    echo "[WARN] 前端可能未启动，请检查: docker compose logs frontend"
fi

echo ""
echo "============================================"
echo "  Gitea Manager 已部署"
echo "  访问地址: http://<本机IP>:5480"
echo "  初始密码: 使用环境变量 INIT_PASSWORD；未指定时保存在当前目录 .env 中"
echo "  数据目录: $SCRIPT_DIR/data/"
echo "============================================"
