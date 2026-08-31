# Ubuntu 20.04 amd64 离线兼容库安装包

本安装包用于解决旧版 `arm-none-eabi-gdb` 启动时提示以下错误的问题：

```text
libncurses.so.5: cannot open shared object file: No such file or directory
```

## 支持范围

- 操作系统：Ubuntu 20.04 LTS
- 主机架构：amd64（x86-64）
- 软件包版本：`6.2-0ubuntu2.1`
- 包含：`libncurses5`、`libtinfo5`

这里的 amd64 是运行 GDB 的 Linux 主机架构，与被调试目标是 ARM 无冲突。

## 安装方法

解压后进入目录，执行：

```bash
chmod +x install.sh
./install.sh
```

脚本会先校验系统版本、CPU 架构和两个安装包的 SHA-256，然后按照依赖顺序离线安装。

也可以手动安装：

```bash
sha256sum --check SHA256SUMS
sudo dpkg -i \
  ./libtinfo5_6.2-0ubuntu2.1_amd64.deb \
  ./libncurses5_6.2-0ubuntu2.1_amd64.deb
```

安装后检查 GDB 是否还有其他动态库缺失：

```bash
ldd /你的路径/arm-none-eabi-gdb | grep "not found" || true
/你的路径/arm-none-eabi-gdb --version
```

两个 `.deb` 均下载自 Ubuntu 官方 Focal Security 仓库，并通过仓库元数据中的文件大小和 SHA-256 校验。
