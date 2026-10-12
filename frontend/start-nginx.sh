#!/bin/sh
# 有效证书与私钥必须同时挂载；只提供一个文件时停止，不能静默降级。
set -eu
if [ -e /etc/nginx/tls/cert.pem ] || [ -e /etc/nginx/tls/key.pem ]; then
    if [ ! -s /etc/nginx/tls/cert.pem ] || [ ! -s /etc/nginx/tls/key.pem ]; then
        echo 'HTTPS 证书或私钥缺失，已停止启动' >&2
        exit 1
    fi
    cp /etc/nginx/manager-https.conf /etc/nginx/conf.d/default.conf
fi
exec nginx -g 'daemon off;'
