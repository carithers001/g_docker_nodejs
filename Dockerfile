FROM debian:bookworm-slim

# 运行器只使用 Python 标准库；状态页、网页登录和 Token 文件不依赖 busybox CGI。
ENV TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y \
    ca-certificates \
    python3 \
    tzdata \
    --no-install-recommends && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 运行时由 status_service.py 下载二进制，以便下载失败后按策略重试。

COPY entrypoint.sh status_service.py /app/
RUN chmod 755 /app /app/entrypoint.sh /app/status_service.py

# 环境变量（默认值）
ENV TOKEN=""
ENV IPV="4"
ENV RESTART_DELAY_SECONDS="60"
ENV DOWNLOAD_RETRY_DELAY_SECONDS="120"
ENV APP_DIR="/app"

ENTRYPOINT ["/app/entrypoint.sh"]
