FROM node:22-bookworm-slim

ENV TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y \
    ca-certificates \
    tzdata \
    --no-install-recommends && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY index.js /app/index.js

# 环境变量（默认值）
ENV TOKEN=""
ENV IPV="4"
ENV APP_DIR="/app"

STOPSIGNAL SIGINT
ENTRYPOINT ["node", "/app/index.js"]
