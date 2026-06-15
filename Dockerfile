# 使用轻量级的 Debian 作为基础镜像
FROM debian:bullseye-slim

# 设置环境变量，避免 apt 安装时产生交互提示
ENV DEBIAN_FRONTEND=noninteractive

# 安装依赖工具 (curl 和 ca-certificates 用于下载和验证 HTTPS)
RUN apt-get update && apt-get install -y curl ca-certificates && rm -rf /var/lib/apt/lists/*

# 下载 amd64 架构的核心文件 (如果你的宿主机是 ARM/Mac M1，请替换下面链接中的 amd64 为 arm64)
RUN curl -L https://www.baipiao.eu.org/xtunnel/x-tunnel-linux-amd64 -o /usr/local/bin/x-tunnel && \
    curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && \
    chmod +x /usr/local/bin/x-tunnel /usr/local/bin/cloudflared

# 将启动脚本复制进容器
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 设置容器启动时执行的程序
ENTRYPOINT ["/entrypoint.sh"]