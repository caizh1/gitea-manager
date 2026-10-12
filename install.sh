#!/usr/bin/env bash
# Ubuntu 24.04 amd64 离线首次安装；入口与清单随专用安装包交付。
set -Eeuo pipefail
umask 077
cd "$(dirname "$0")"
ROOT="$PWD"
if command -v flock >/dev/null; then
  exec 9>.lifecycle.lock
  flock -n 9 || { echo "安装或卸载正在运行"; exit 1; }
fi
INSTALL_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
export INSTALL_RUN_ID
BOOTSTRAP_STAGE="初始化安装日志"
BOOTSTRAP_STARTED="$(date -u +%FT%TZ)"
mkdir -p "state/diagnostics/$INSTALL_RUN_ID/logs"
# Shell 引导阶段不依赖 Python；先限长和脱敏，再写入日志。
exec 3>&1 4>&2
BOOTSTRAP_FIFO="state/diagnostics/$INSTALL_RUN_ID/bootstrap.fifo"
mkfifo "$BOOTSTRAP_FIFO"
awk -v log_path="state/diagnostics/$INSTALL_RUN_ID/logs/bootstrap.log" '
BEGIN { limit=1048000; first=0; tail=""; hidden=0 }
{
  line=substr($0,1,65536)
  lower=tolower(line)
  if (lower ~ /-----begin .*private key/) hidden=1
  if (hidden || lower ~ /password|passwd|authorization|secret|token|api.key/) line="[可能包含凭据的引导输出已脱敏]"
  if (lower ~ /-----end .*private key/) hidden=0
  print line; fflush()
  if(first+length(line)<limit) {print line >> log_path; fflush(log_path); first+=length(line)+1}
  else {tail=tail line "\n"; if(length(tail)>limit) tail=substr(tail,length(tail)-limit+1); truncated=1}
}
END {if(truncated) {print "[日志超限，保留首尾]" >> log_path; printf "%s",tail >> log_path};close(log_path)}
' < "$BOOTSTRAP_FIFO" >&3 &
BOOTSTRAP_LOGGER=$!
exec > "$BOOTSTRAP_FIFO" 2>&1
stop_bootstrap_log() {
  if [[ -n "${BOOTSTRAP_LOGGER:-}" ]]; then
    exec 1>&3 2>&4
    wait "$BOOTSTRAP_LOGGER" || true
    unset BOOTSTRAP_LOGGER
    rm -f "$BOOTSTRAP_FIFO"
  fi
}
finish_bootstrap() {
  code="${1:-$?}"
  stop_bootstrap_log
  if [[ ! -f "state/diagnostics/diagnostics-$INSTALL_RUN_ID.tar.gz" ]]; then
    printf '失败步骤：%s\n错误编号：BOOTSTRAP_FAILED\n退出码：%s\n开始时间：%s\n结束时间：%s\n已确认事实：安装引导未完成。详细输出见 logs/bootstrap.log。\n尚未确定原因：以具体命令输出为准，不推测根因。\n请提供本目录诊断包，不需要手动收集日志。\n' "$BOOTSTRAP_STAGE" "$code" "$BOOTSTRAP_STARTED" "$(date -u +%FT%TZ)" > "state/diagnostics/$INSTALL_RUN_ID/summary.md"
    printf '{"状态":"失败","标题":"安装引导","步骤":"%s","退出码":%s}\n' "$BOOTSTRAP_STAGE" "$code" > "state/diagnostics/$INSTALL_RUN_ID/status.json"
    tar -czf "state/diagnostics/diagnostics-$INSTALL_RUN_ID.tar.gz" -C state/diagnostics "$INSTALL_RUN_ID"
    echo "诊断包：$ROOT/state/diagnostics/diagnostics-$INSTALL_RUN_ID.tar.gz"
  fi
}
trap finish_bootstrap EXIT

[[ -z "${DOCKER_HOST:-}" && -z "${DOCKER_CONTEXT:-}" ]] || { echo '请取消 DOCKER_HOST/DOCKER_CONTEXT，安装器只管理当前主机。' >&2; exit 1; }
[[ -f installer-manifest.json && -f SHA256SUMS ]] || { echo '请使用完整的一键安装包，不能直接从源码目录安装。' >&2; exit 1; }
BOOTSTRAP_STAGE='校验离线安装包完整性'
echo '正在校验离线安装包完整性。'
if command -v sha256sum >/dev/null; then sha256sum --check --quiet SHA256SUMS
else shasum -a 256 --check --quiet SHA256SUMS; fi
if [[ "${1:-}" != --desktop-test ]]; then
  BOOTSTRAP_STAGE="检查 Ubuntu 版本、架构与安装权限"
  [[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || { echo '仅支持 Ubuntu 24.04 amd64。' >&2; exit 1; }
  source /etc/os-release
  [[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 && "$EUID" == 0 ]] || { echo '请在 Ubuntu 24.04 amd64 上使用 sudo bash install.sh。' >&2; exit 1; }
  [[ -d /run/systemd/system ]] || { echo '需要正常启动的 systemd 主机；普通容器不属于裸机安装环境。' >&2; exit 1; }
  if ! command -v python3 >/dev/null || ! docker compose version >/dev/null 2>&1; then
    echo '正在从安装包配置离线依赖。'
    # 不替换已有 Docker CE、Snap 或其他容器运行时。
    if command -v docker >/dev/null && ! dpkg-query -W -f='${Status}' docker.io 2>/dev/null | grep -q 'install ok installed'; then
      echo '检测到非 Ubuntu docker.io 的现有 Docker，请先为它安装兼容的 Compose 和 Python 3；未替换现有运行时。' >&2
      exit 1
    fi
    [[ -f runtime/debs/Packages ]] || { echo '安装包缺少 Ubuntu 离线依赖。' >&2; exit 1; }
    # file 仓库副本位于 apt 可读取目录；不修改系统软件源，不访问网络。
    FRESH_DOCKER=0
    command -v docker >/dev/null || FRESH_DOCKER=1
    APT_DIR="$(mktemp -d /var/tmp/gitea-install-apt.XXXXXX)"
    trap 'BOOTSTRAP_EXIT=$?; rm -rf "$APT_DIR"; finish_bootstrap "$BOOTSTRAP_EXIT"' EXIT
    chmod 755 "$APT_DIR"
    cp -R runtime/debs "$APT_DIR/debs"
    chmod -R a+rX "$APT_DIR/debs"
    printf 'deb [trusted=yes] file:%s/debs ./\n' "$APT_DIR" > "$APT_DIR/offline.list"
    mkdir -p "$APT_DIR/lists/partial"
    APT_OPTIONS=(-o "Dir::Etc::sourcelist=$APT_DIR/offline.list" -o Dir::Etc::sourceparts=- -o "Dir::State::lists=$APT_DIR/lists" -o Acquire::Languages=none)
    BOOTSTRAP_STAGE="配置离线依赖索引"
    timeout 300s apt-get "${APT_OPTIONS[@]}" update
    BOOTSTRAP_STAGE="安装离线 Docker、Compose 与 Python"
    DEBIAN_FRONTEND=noninteractive timeout 900s apt-get "${APT_OPTIONS[@]}" --no-install-recommends --no-remove -y install docker.io docker-compose-v2 python3
    if [[ "$FRESH_DOCKER" == 1 ]]; then
      python3 - <<'PYCONFIG'
import json
from pathlib import Path
p=Path('/etc/docker/daemon.json');p.parent.mkdir(parents=True,exist_ok=True)
c=json.loads(p.read_text()) if p.exists() else {}
c.setdefault('features',{})['containerd-snapshotter']=False
c['storage-driver']='overlay2'
p.write_text(json.dumps(c,indent=2)+'\n')
PYCONFIG
      BOOTSTRAP_STAGE="配置新 Docker 使用 overlay2"
      dockerd --validate --config-file=/etc/docker/daemon.json
      systemctl restart docker
    fi
    rm -rf "$APT_DIR"
    trap finish_bootstrap EXIT
  fi
  BOOTSTRAP_STAGE="启动 Docker 服务"
  timeout 90s systemctl enable --now docker
fi
stop_bootstrap_log
BOOTSTRAP_STAGE="运行 Python 安装器"
python3 -u "$ROOT/deployment/install.py" "$@"
