# Debian 12 Bookworm amd64 Node 20 镜像离线兼容库

本安装包用于 Debian GNU/Linux 12（Bookworm）amd64 的 Node 20 镜像，解决旧版 `arm-none-eabi-gdb` 启动时提示以下错误的问题：

```text
libncurses.so.5: cannot open shared object file: No such file or directory
```

## 支持范围

- 基础系统：Debian GNU/Linux 12（Bookworm）
- 主机架构：amd64（x86-64）
- 软件包版本：`6.4-4`
- 包含：`libncurses5`、`libtinfo5`
- 适用镜像示例：`node:20-bookworm`、`node:20-bookworm-slim`

不适用于 Alpine、Ubuntu、arm64 或其他 Debian 主版本镜像。

## 在容器内安装

解压后，以 root 用户执行：

```bash
chmod +x install.sh
./install.sh
```

脚本会先校验基础系统、CPU 架构及两个 `.deb` 的 SHA-256，然后按照依赖顺序离线安装。

## Dockerfile 示例

将解压后的目录放进构建上下文：

```dockerfile
FROM node:20-bookworm-slim

USER root
COPY debian12-bookworm-amd64-gdb-ncurses5-offline /tmp/gdb-ncurses5
RUN /tmp/gdb-ncurses5/install.sh
```

如果镜像最终需要使用非 root 用户，可在安装后恢复原来的 `USER`。

安装后检查 GDB 是否仍有其他动态库缺失：

```bash
ldd /你的路径/arm-none-eabi-gdb | grep "not found" || true
/你的路径/arm-none-eabi-gdb --version
```

两个 `.deb` 均来自 Debian 官方 Bookworm 仓库，并通过官方页面公布的文件大小和 SHA-256 校验。
