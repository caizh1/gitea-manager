#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

export SSH_KEY_PATH="${SSH_KEY_PATH:-$HOME/.ssh/id_rsa}"

mkdir -p data/backups

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
echo "  默认密码: admin123 (请尽快修改)"
echo "  数据目录: $SCRIPT_DIR/data/"
echo "============================================"
