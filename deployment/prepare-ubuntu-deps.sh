#!/usr/bin/env bash
# 仅在联网准备机运行；使用 Ubuntu 签名仓库解析完整 amd64 离线依赖。
set -euo pipefail
cd "$(dirname "$0")/.."
UBUNTU_VERSION="${1:-24.04}"
case "$UBUNTU_VERSION" in
  20.04|24.04) ;;
  *) echo '支持准备 Ubuntu 20.04 或 24.04 的依赖。' >&2; exit 1 ;;
esac
DEFAULT_DEPS=work/oneclick-build/debs
if [[ "$UBUNTU_VERSION" == 20.04 ]]; then DEFAULT_DEPS=work/oneclick-ubuntu20/debs; fi
DEPS_DIR="${2:-$DEFAULT_DEPS}"
mkdir -p "$DEPS_DIR"
DEPS_DIR="$(cd "$DEPS_DIR" && pwd)"
if [[ -f "$DEPS_DIR/Packages" ]]; then
  EXISTING_VERSION=24.04
  if [[ -f "$DEPS_DIR/ubuntu-version" ]]; then EXISTING_VERSION="$(cat "$DEPS_DIR/ubuntu-version")"; fi
  [[ "$EXISTING_VERSION" == "$UBUNTU_VERSION" ]] || { echo '依赖目录属于其他 Ubuntu 版本，请使用独立目录。' >&2; exit 1; }
fi
docker run --rm --platform linux/amd64 \
  -v "$DEPS_DIR:/packages" "ubuntu:$UBUNTU_VERSION" bash -euc '
  apt-get update
  apt-get install -y --no-install-recommends dpkg-dev
  mkdir -p /packages/partial
  apt-get -o Dir::State::status=/dev/null -o Dir::Cache::archives=/packages \
    --download-only --no-install-recommends -y install docker.io docker-compose-v2 python3
  cd /packages
  dpkg-scanpackages . /dev/null > Packages
  . /etc/os-release
  printf "%s\n" "$VERSION_ID" > ubuntu-version
  echo "离线软件包、版本与架构：" > package-versions.txt
  for package in ./*.deb; do dpkg-deb -f "$package" Package Version Architecture >> package-versions.txt; done
  '
