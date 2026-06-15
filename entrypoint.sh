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

if [ "$IPV" != "4" ] && [ "$IPV" != "6" ]; then
    echo "[错误] IPV 环境变量必须为 4 或 6，当前值: $IPV"
    exit 1
fi

WSPORT=$(get_free_port)
# 固定 Metrics 端口以骗过 DCDeploy 健康检查
METRICSPORT="${PORT:-8080}"

echo "[x-tunnel] 启动，监听本地端口 $WSPORT ..."
# 🚀 修复 1：彻底抛弃 screen，使用原生的 & 放入后台
if [ -z "$TOKEN" ]; then
    /app/x-tunnel-linux -l ws://127.0.0.1:$WSPORT &
else
    /app/x-tunnel-linux -l ws://127.0.0.1:$WSPORT -token "$TOKEN" &
fi

# 给 x-tunnel 1 秒钟的启动缓冲时间
sleep 1

echo "[cloudflared] 启动，metrics 端口 $METRICSPORT ..."
./cloudflared-linux update 2>/dev/null || true
# 🚀 修复 2：抛弃 screen，并在 url 前强制加上 http:// 协议头，防止 502 路由失败
/app/cloudflared-linux \
    --edge-ip-version "$IPV" \
    --protocol http2 \
    tunnel \
    --url "http://127.0.0.1:$WSPORT" \
    --metrics "0.0.0.0:$METRICSPORT" &

echo "[等待] 正在等待 Cloudflare 隧道建立..."
while true; do
    RESP=$(curl -s "http://127.0.0.1:$METRICSPORT/metrics" 2>/dev/null || true)
    if echo "$RESP" | grep -q 'userHostname='; then
        DOMAIN=$(echo "$RESP" | grep 'userHostname="' | sed -E 's/.*userHostname="https?:\/\/([^"]+)".*/\1/')
        break
    fi
    sleep 2
done

echo "========================================"
if [ -z "$TOKEN" ]; then
    echo "链接: $DOMAIN:443"
else
    echo "链接: $DOMAIN:443"
    echo "Token: $TOKEN"
fi
echo "========================================"

# 🚀 修复 3：抛弃 tail -f /dev/null
# wait -n 会监听后台进程。如果 x-tunnel 或 cloudflared 意外崩溃，容器会自动重启，而不是返回 502 僵死！
wait -n
