#!/usr/bin/env bash
# 仅用于本安装包创建的实例；两次确认由卸载程序执行。
set -Eeuo pipefail
umask 077
SCRIPT_ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
if [[ $# -ne 0 ]]; then
  echo '用法：sudo bash uninstall.sh；脚本会自动发现实例，无需指定旧包目录。'; exit 1
fi
cd -- "$SCRIPT_ROOT"
if [[ $EUID -ne 0 ]]; then echo '请使用 sudo bash uninstall.sh'; exit 1; fi
if ! command -v python3 >/dev/null; then echo '缺少 Python 3，尚未删除任何数据。请先恢复系统 Python 3。'; exit 1; fi
if ! command -v flock >/dev/null; then echo '缺少 flock，无法安全锁定安装目录，尚未删除数据。'; exit 1; fi
exec 9>.lifecycle.lock
flock -n 9 || { echo '安装或卸载正在运行，尚未删除数据。'; exit 1; }
exec python3 -u "$SCRIPT_ROOT/deployment/uninstall.py"
