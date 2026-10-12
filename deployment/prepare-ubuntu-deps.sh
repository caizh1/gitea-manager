#!/usr/bin/env bash
# 仅在联网准备机运行；使用 Ubuntu 签名仓库解析完整 amd64 离线依赖。
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p work/oneclick-build/debs
docker run --rm --platform linux/amd64 \
  -v "$PWD/work/oneclick-build/debs:/packages" ubuntu:24.04 bash -euc '
  apt-get update
  apt-get install -y --no-install-recommends dpkg-dev
  mkdir -p /packages/partial
  apt-get -o Dir::State::status=/dev/null -o Dir::Cache::archives=/packages \
    --download-only --no-install-recommends -y install docker.io docker-compose-v2 python3
  cd /packages
  dpkg-scanpackages . /dev/null > Packages
  echo "离线软件包、版本与架构：" > package-versions.txt
  for package in ./*.deb; do dpkg-deb -f "$package" Package Version Architecture >> package-versions.txt; done
  '
