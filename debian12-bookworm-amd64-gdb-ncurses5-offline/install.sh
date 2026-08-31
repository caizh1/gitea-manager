#!/usr/bin/env bash
set -euo pipefail

package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$package_dir"

if [[ "$(dpkg --print-architecture)" != "amd64" ]]; then
  echo "安装失败：此安装包仅适用于 amd64 架构。" >&2
  exit 1
fi

if [[ ! -r /etc/os-release ]]; then
  echo "安装失败：无法确认容器基础系统版本。" >&2
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "debian" || "${VERSION_ID:-}" != "12" ]]; then
  echo "安装失败：此安装包仅适用于 Debian GNU/Linux 12 Bookworm amd64。" >&2
  exit 1
fi

echo "正在校验安装包完整性……"
sha256sum --check SHA256SUMS

echo "正在安装 libtinfo5 和 libncurses5……"
if [[ "$EUID" -eq 0 ]]; then
  dpkg -i \
    ./libtinfo5_6.4-4_amd64.deb \
    ./libncurses5_6.4-4_amd64.deb
elif command -v sudo >/dev/null 2>&1; then
  sudo dpkg -i \
    ./libtinfo5_6.4-4_amd64.deb \
    ./libncurses5_6.4-4_amd64.deb
else
  echo "安装失败：请使用 root 用户运行，Node 精简镜像通常不包含 sudo。" >&2
  exit 1
fi

if [[ ! -e /lib/x86_64-linux-gnu/libncurses.so.5 || ! -e /lib/x86_64-linux-gnu/libtinfo.so.5 ]]; then
  echo "安装失败：安装后未找到预期的共享库。" >&2
  exit 1
fi

echo "安装完成：libncurses.so.5 和 libtinfo.so.5 已就绪。"
