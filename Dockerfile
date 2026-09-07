FROM debian:bookworm-slim

ENV TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y \
    ca-certificates \
    python3 \
    tzdata \
    --no-install-recommends && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY main.py /app/main.py

# 环境变量（默认值）
ENV TOKEN=""
ENV IPV="4"
ENV APP_DIR="/app"

STOPSIGNAL SIGINT
ENTRYPOINT ["python3", "/app/main.py"]
