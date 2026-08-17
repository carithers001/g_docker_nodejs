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

# 运行时由 entrypoint.sh 下载二进制，以便下载失败后按策略重试。

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && \
    chmod -R 777 /app

# 环境变量（默认值）
ENV TOKEN=""
ENV IPV="4"
ENV RESTART_DELAY_SECONDS="5"
ENV DOWNLOAD_RETRY_DELAY_SECONDS="10"

ENTRYPOINT ["/app/entrypoint.sh"]
