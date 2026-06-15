#!/bin/bash

# 定义端口
WS_PORT=8080
METRICS_PORT=8081

# 1. 启动本地 x-tunnel 服务 (丢入后台运行)
echo "正在启动后台 x-tunnel..."
if [ -z "$TOKEN" ]; then
    /usr/local/bin/x-tunnel -l ws://127.0.0.1:$WS_PORT &
else
    echo "检测到 Token: $TOKEN"
    /usr/local/bin/x-tunnel -l ws://127.0.0.1:$WS_PORT -token "$TOKEN" &
fi

# 2. 启动 Cloudflared 内网穿透 (丢入后台运行)
echo "正在启动 Cloudflare Argo Tunnel..."
/usr/local/bin/cloudflared tunnel --url 127.0.0.1:$WS_PORT --metrics 0.0.0.0:$METRICS_PORT &

# 3. 循环等待并抓取分配的临时域名
echo "正在获取 Cloudflare 分配的白嫖域名..."
while true; do
    RESP=$(curl -s "http://127.0.0.1:$METRICS_PORT/metrics")
    if echo "$RESP" | grep -q 'userHostname='; then
        DOMAIN=$(echo "$RESP" | grep 'userHostname="' | sed -E 's/.*userHostname="https?:\/\/([^"]+)".*/\1/')
        echo "======================================"
        echo "🚀 节点创建成功！"
        echo "🔗 节点地址: ${DOMAIN}:443"
        if [ -n "$TOKEN" ]; then
            echo "🔑 身份令牌 (Token): $TOKEN"
        fi
        echo "======================================"
        break
    else
        sleep 2
    fi
done

# 4. 关键：挂起主进程，防止 Docker 容器退出
# wait -n 会等待任何一个后台进程退出。如果有进程崩溃，容器也会随之停止，方便重启。
wait -n