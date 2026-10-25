# 仅供隔离 SSH 恢复验收使用；生产恢复不依赖此网关镜像。
FROM docker:29-cli AS cli
FROM gitea/gitea:1.26.4
COPY --from=cli /usr/local/bin/docker /usr/local/bin/docker
