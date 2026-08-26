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

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# 环境变量（默认值）
ENV TOKEN=""
ENV IPV="4"

ENTRYPOINT ["/app/entrypoint.sh"]
