#!/bin/sh
# 每个模拟主机拥有独立 Docker 引擎和 SSH 身份；不挂载外部 Docker socket。
set -eu
mkdir -p /root/.ssh /run/sshd /lab
chmod 700 /root/.ssh
cp /keys/authorized_keys /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
ssh-keygen -A
/usr/sbin/sshd -o PermitRootLogin=prohibit-password -o PasswordAuthentication=no -o MaxSessions=64 -o MaxStartups=64
exec /usr/local/bin/dockerd-entrypoint.sh dockerd --host=unix:///var/run/docker.sock --storage-driver=overlay2
