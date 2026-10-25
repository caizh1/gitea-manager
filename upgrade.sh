#!/usr/bin/env bash
# 离线一键升级入口：所有 Python 依赖由交付镜像提供，秘密不回显。
set -Eeuo pipefail
umask 077
PACKAGE_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET=""
CHECK_ONLY=0
DESKTOP_TEST=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --container) TARGET="$2"; shift 2 ;;
    --check) CHECK_ONLY=1; shift ;;
    --desktop-test) DESKTOP_TEST=1; shift ;;
    --help) echo '用法：bash upgrade.sh [--container 已有后端容器] [--check]'; exit 0 ;;
    *) echo "不支持的参数：$1" >&2; exit 1 ;;
  esac
done
command -v docker >/dev/null || { echo '请先准备 Docker。' >&2; exit 1; }
docker compose version >/dev/null
if [ "$DESKTOP_TEST" -ne 1 ]; then
  [ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] || { echo '正式升级仅支持 Linux x86_64；原服务未改变。' >&2; exit 1; }
fi
if [ -z "$TARGET" ]; then
  CANDIDATES=()
  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    database="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$candidate" | sed -n 's/^DATABASE_URL=//p')"
    case "$database" in *gitea-manager.db*) CANDIDATES+=("$candidate") ;; esac
  done < <(docker ps --filter label=com.docker.compose.service=backend --format '{{.Names}}')
  [ "${#CANDIDATES[@]}" -gt 0 ] || { echo '没有识别到现有 Manager，请使用 --container 指定后端容器。' >&2; exit 1; }
  if [ "${#CANDIDATES[@]}" -eq 1 ]; then TARGET="${CANDIDATES[0]}"
  else
    [ -t 0 ] || { echo '发现多个 Manager，请使用 --container 指定升级目标。' >&2; exit 1; }
    echo '请选择要升级的 Manager：'
    select candidate in "${CANDIDATES[@]}"; do [ -n "$candidate" ] && { TARGET="$candidate"; break; }; done
  fi
fi
if [ "$DESKTOP_TEST" -eq 1 ]; then
  [ "$(docker inspect --format '{{index .Config.Labels "io.gitea-manager.upgrade-test"}}' "$TARGET")" = true ] || { echo '桌面验收仅允许显式标记的隔离测试实例。' >&2; exit 1; }
fi
PROJECT="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$TARGET")"
DEPLOYMENT="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$TARGET")"
CONFIG_FILES="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$TARGET")"
[ -n "$PROJECT" ] && [ -d "$DEPLOYMENT" ] && [ -n "$CONFIG_FILES" ] || { echo '现有 Compose 项目路径不可读取，未修改部署。' >&2; exit 1; }
STARTED_AT="$(date +%s)"
[ -f "$PACKAGE_DIR/gitea-manager.tar.gz" ] && [ -f "$PACKAGE_DIR/gitea-manager.tar.gz.sha256" ] || { echo '请把安装包和校验文件放在 upgrade.sh 同一目录。' >&2; exit 1; }
EXPECTED="$(awk 'NR==1 {print $1}' "$PACKAGE_DIR/gitea-manager.tar.gz.sha256")"
if command -v sha256sum >/dev/null; then ACTUAL="$(sha256sum "$PACKAGE_DIR/gitea-manager.tar.gz" | awk '{print $1}')"
else ACTUAL="$(shasum -a 256 "$PACKAGE_DIR/gitea-manager.tar.gz" | awk '{print $1}')"; fi
[ "$EXPECTED" = "$ACTUAL" ] && [ "${#ACTUAL}" -eq 64 ] || { echo '安装包校验失败，原服务未改变。' >&2; exit 1; }
ROOT="$DEPLOYMENT/.gitea-manager-upgrades"
mkdir -p "$ROOT"
LOCK="$ROOT/执行锁"
mkdir "$LOCK" 2>/dev/null || { echo '已有升级正在执行；若前次被强制中断，请先查看升级记录，勿直接删除执行锁。' >&2; exit 1; }
printf '%s\n' "$$" > "$LOCK/进程"
RELEASE="$ROOT/releases/$ACTUAL"
STATE="$ROOT/records/$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$RELEASE" "$STATE"
RECOVER=0
CONTROLLER_IMAGE=gitea-review-backend:offline
runtime() {
  docker run --rm --platform linux/amd64 --network none \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$RELEASE:/release:ro" -v "$STATE:/state" -v "$DEPLOYMENT:/deployment" \
    -e "GM_TARGET_CONTAINER=$TARGET" -e "GM_RELEASE_HOST=$RELEASE" \
    -e "GM_STATE_HOST=$STATE" -e "GM_DEPLOYMENT_HOST=$DEPLOYMENT" \
    -e "GM_CONTROLLER_IMAGE=$CONTROLLER_IMAGE" \
    -e "GM_UPGRADE_STARTED_AT=$STARTED_AT" \
    "$CONTROLLER_IMAGE" python /release/scripts/upgrade_runtime.py "$1"
}
cleanup() {
  code=$?
  trap - EXIT INT TERM
  if [ "$code" -ne 0 ] && [ "$RECOVER" -eq 1 ]; then
    echo '升级未通过，正在恢复原服务。'
    if runtime rollback; then
      if [ -f "$STATE/回退Compose.json" ]; then
        if docker compose -p "$PROJECT" --project-directory "$DEPLOYMENT" -f "$STATE/回退Compose.json" up --no-start --no-build --pull never --no-deps "${OLD_ALL_SERVICES[@]}" > "$STATE/回退创建记录.txt" 2>&1 &&
           docker compose -p "$PROJECT" --project-directory "$DEPLOYMENT" -f "$STATE/回退Compose.json" up -d --no-build --pull never --no-deps "${OLD_SERVICES[@]}" > "$STATE/回退启动记录.txt" 2>&1 && runtime rollback_health; then RECOVER=0
        else echo '原容器恢复未完成，请保留现场并查看私有升级记录。' >&2; fi
      else RECOVER=0; fi
    else echo '自动回退未完成，请保留现场并查看私有升级记录。' >&2; fi
  fi
  if [ "$code" -eq 0 ] || [ "$RECOVER" -eq 0 ]; then rm -f "$LOCK/进程"; rmdir "$LOCK"; fi
  echo "升级记录：$STATE"
  exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
tar -tzf "$PACKAGE_DIR/gitea-manager.tar.gz" > "$STATE/package-members.txt"
if grep -Fxq 'offline/运行镜像.tar' "$STATE/package-members.txt"; then
  echo '校验通过，正在导入离线运行镜像。'
  tar -xzf "$PACKAGE_DIR/gitea-manager.tar.gz" -C "$RELEASE" offline/运行镜像.tar scripts/upgrade_runtime.py
  docker load -i "$RELEASE/offline/运行镜像.tar" > "$STATE/镜像导入记录.txt" 2>&1
else
  echo '校验通过，复用已安装镜像；后续必须通过固定镜像身份核对。'
  tar -xzf "$PACKAGE_DIR/gitea-manager.tar.gz" -C "$RELEASE" scripts/upgrade_runtime.py
fi
CONTROLLER_IMAGE="$(docker image inspect --format '{{.Id}}' gitea-review-backend:offline)"
docker run --rm --platform linux/amd64 --network none \
  -v "$PACKAGE_DIR:/package:ro" -v "$RELEASE:/release" \
  "$CONTROLLER_IMAGE" python /release/scripts/upgrade_runtime.py extract
COMPOSE=(docker compose -p "$PROJECT" --project-directory "$DEPLOYMENT")
IFS=',' read -r -a ORIGINAL_FILES <<< "$CONFIG_FILES"
for file in "${ORIGINAL_FILES[@]}"; do COMPOSE+=(-f "$file"); done
"${COMPOSE[@]}" config --format json > "$STATE/原Compose.json" 2> "$STATE/配置检查记录.txt"
OLD_SERVICES=()
OLD_ALL_SERVICES=()
while IFS= read -r service; do
  case "$service" in backend|frontend|review-worker|review-executor|review-progress|review-feedback|approval-view|svn-migration-worker) OLD_ALL_SERVICES+=("$service") ;; esac
done < <(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Label "com.docker.compose.service"}}')
while IFS= read -r service; do
  case "$service" in backend|frontend|review-worker|review-executor|review-progress|review-feedback|approval-view|svn-migration-worker) OLD_SERVICES+=("$service") ;; esac
done < <(docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Label "com.docker.compose.service"}}')
runtime preflight
if [ "$CHECK_ONLY" -eq 1 ]; then echo '升级前置检查通过，原服务未停止。'; exit 0; fi
RECOVER=1
runtime freeze
runtime prepare
docker compose -p "$PROJECT" --project-directory "$DEPLOYMENT" -f "$STATE/新Compose.json" config --quiet > "$STATE/新配置检查记录.txt" 2>&1
runtime stop
runtime backup
runtime activate
docker compose -p "$PROJECT" --project-directory "$DEPLOYMENT" -f "$STATE/新Compose.json" run --rm --no-deps -T --user 0:0 --cap-add CHOWN --cap-add FOWNER --entrypoint python review-executor -c "import os; os.chown('/app/review-native',10001,10001); os.chmod('/app/review-native',0o700)" > "$STATE/native-volume-init.log" 2>&1
echo '正在启动新版本，业务写入将在校验通过后恢复。'
docker compose -p "$PROJECT" --project-directory "$DEPLOYMENT" -f "$STATE/新Compose.json" up -d --no-build --pull never --no-deps \
  backend frontend review-worker review-executor review-progress review-feedback approval-view svn-migration-worker > "$STATE/新服务启动记录.txt" 2>&1
runtime verify
runtime commit
RECOVER=0
