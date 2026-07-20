FROM debian:bookworm-slim

# 安装依赖
RUN apt-get update && apt-get install -y \
    curl \
    screen \
    lsof \
	ca-certificates \
    busybox \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 根据架构下载二进制（构建时决定）
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
      amd64) \
        curl -L https://www.baipiao.eu.org/xtunnel/x-tunnel-linux-amd64 -o x-tunnel-linux && \
        curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared-linux \
        ;; \
      386) \
        curl -L https://www.baipiao.eu.org/xtunnel/x-tunnel-linux-386 -o x-tunnel-linux && \
        curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-386 -o cloudflared-linux \
        ;; \
      arm64) \
        curl -L https://www.baipiao.eu.org/xtunnel/x-tunnel-linux-arm64 -o x-tunnel-linux && \
        curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o cloudflared-linux \
        ;; \
      *) echo "不支持的架构: ${TARGETARCH}" && exit 1 ;; \
    esac && \
    chmod +x x-tunnel-linux cloudflared-linux

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# 环境变量（默认值）
ENV TOKEN=""
ENV IPV="4"

ENTRYPOINT ["/app/entrypoint.sh"]
