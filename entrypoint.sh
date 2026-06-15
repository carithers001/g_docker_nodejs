#!/bin/bash
set -e

get_free_port() {
    while true; do
        PORT=$((RANDOM + 1024))
        if ! lsof -i TCP:$PORT >/dev/null 2>&1; then
            echo $PORT
            return
        fi
    done
}

# 验证 IPV 参数
if [ "$IPV" != "4" ] && [ "$IPV" != "6" ]; then
    echo "[错误] IPV 环境变量必须为 4 或 6，当前值: $IPV"
    exit 1
fi

# WSPORT=$(get_free_port)
METRICSPORT=8080

echo "[x-tunnel] 启动，监听端口 $WSPORT ..."
if [ -z "$TOKEN" ]; then
    screen -dmUS x-tunnel /app/x-tunnel-linux -l ws://127.0.0.1:$WSPORT
else
    screen -dmUS x-tunnel /app/x-tunnel-linux -l ws://127.0.0.1:$WSPORT -token "$TOKEN"
fi

echo "[cloudflared] 启动，metrics 端口 $METRICSPORT ..."
./cloudflared-linux update 2>/dev/null || true
screen -dmUS argo /app/cloudflared-linux \
    --edge-ip-version "$IPV" \
    --protocol http2 \
    tunnel \
    --url "127.0.0.1:$WSPORT" \
    --metrics "0.0.0.0:$METRICSPORT"

echo "[等待] 正在等待 Cloudflare 隧道建立..."
while true; do
    RESP=$(curl -s "http://127.0.0.1:$METRICSPORT/metrics" 2>/dev/null || true)
    if echo "$RESP" | grep -q 'userHostname='; then
        DOMAIN=$(echo "$RESP" | grep 'userHostname="' | sed -E 's/.*userHostname="https?:\/\/([^"]+)".*/\1/')
        break
    fi
    sleep 1
done

echo "========================================"
if [ -z "$TOKEN" ]; then
    echo "链接: $DOMAIN:443"
else
    echo "链接: $DOMAIN:443"
    echo "Token: $TOKEN"
fi
echo "Metrics: http://0.0.0.0:$METRICSPORT/metrics"
echo "========================================"

# 保持容器前台运行
tail -f /dev/null
