#!/usr/bin/env bash
# 先核对本地输入镜像身份，避免构建器将私有离线镜像当作远程仓库拉取。
set -euo pipefail
cd "$(dirname "$0")/.."
BASE=gitea-review-executor:integration-default-off-20260921
EXPECTED=sha256:033c19a89452b753baa37659d7fd887c2e593c1c90486b56ede190e415b6593e
[[ "$(docker image inspect --format '{{.Id}}' "$BASE")" == "$EXPECTED" ]] || { echo '原执行器镜像身份变化，拒绝构建，请核对交付基线。' >&2; exit 1; }
docker build --platform linux/amd64 -f deployment/Dockerfile.executor -t gitea-review-executor:oneclick-20260921 deployment
[[ "$(docker image inspect --format '{{.Id}}' "$BASE")" == "$EXPECTED" ]] || { echo '构建期间原执行器标签发生变化，不可交付。' >&2; exit 1; }
