FROM debian:bookworm-slim

# 安装依赖
ENV TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    busybox \
    tzdata \
    --no-install-recommends && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 自动检测真实系统架构并下载对应文件
RUN ARCH=$(uname -m) && \
    case "${ARCH}" in \
        x86_64) \
            curl -L https://www.baipiao.eu.org/xtunnel/x-tunnel-linux-amd64 -o x-tunnel-linux && \
            curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared-linux \
            ;; \
        aarch64) \
            curl -L https://www.baipiao.eu.org/xtunnel/x-tunnel-linux-arm64 -o x-tunnel-linux && \
            curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o cloudflared-linux \
            ;; \
        i386 | i686) \
            curl -L https://www.baipiao.eu.org/xtunnel/x-tunnel-linux-386 -o x-tunnel-linux && \
            curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-386 -o cloudflared-linux \
            ;; \
        *) \
            echo "不支持的架构: ${ARCH}" && exit 1 \
            ;; \
    esac && \
    chmod +x x-tunnel-linux cloudflared-linux

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && \
    chmod -R 777 /app

# 环境变量（默认值）
ENV TOKEN=""
ENV IPV="4"

ENTRYPOINT ["/app/entrypoint.sh"]
