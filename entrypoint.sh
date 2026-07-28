#!/bin/bash
set -e

if [ "$IPV" != "4" ] && [ "$IPV" != "6" ]; then
    echo "[-] IPV 参数错误 : $IPV"
    exit 1
fi

# 检查是否配置了 envToken 环境变量
if [ -z "$envToken" ]; then
    echo "[-] 致命错误: 未检测到环境变量 envToken！请在云平台设置该变量。"
    exit 1
fi

# 1. 设置给云平台健康检查用的对外端口 (通常平台会自动分配 PORT 变量，默认 8080)
UPTIME_PORT="${PORT:-8080}"

# 2. x-tunnel 本地内部使用的真实端口换为 8081，避免和健康检查端口冲突
WSPORT=8081
HEALTH_PORT=8082

# 心跳保活逻辑
(
    while true; do
        curl -s -m 5 https://1.1.1.1 > /dev/null 2>&1 || true
        sleep 300
    done
) &

echo "[x-tunnel] 启动在本地端口 $WSPORT ..."

# 启动 x-tunnel 进程
if [ -z "$TOKEN" ]; then
    /app/x-tunnel-linux -l ws://127.0.0.1:$WSPORT &
else
    /app/x-tunnel-linux -l ws://127.0.0.1:$WSPORT -token "$TOKEN" &
fi

sleep 1

echo "[cloudflared] 检查更新并启动固定隧道..."
./cloudflared-linux update 2>/dev/null || true

# 3. 启动 Cloudflare Tunnel，加入 --metrics 满足健康检查，并硬编码你的固定 Token
/app/cloudflared-linux \
    --edge-ip-version "$IPV" \
    --protocol http2 \
    --metrics "0.0.0.0:$HEALTH_PORT" \
    tunnel run --token "$ENV_TOKEN" &

echo "========================================"
echo "已连接到 Cloudflare Zero Trust (Token 硬编码模式)"
echo "当前健康检查端口: $HEALTH_PORT"
echo "当前本地服务端口: $WSPORT"
echo "========================================"

# ================= 运行状态监控网页 (Uptime Web Server) =================
START_TIME=$(date +%s)
START_DATE=$(date "+%Y-%m-%d %H:%M:%S")
mkdir -p /tmp/www

# 后台循环：每 5 秒更新一次 index.html
(
    while true; do
        NOW=$(date +%s)
        DIFF=$((NOW - START_TIME))
        DAYS=$((DIFF / 86400))
        HOURS=$(( (DIFF % 86400) / 3600 ))
        MINS=$(( (DIFF % 3600) / 60 ))
        SECS=$((DIFF % 60))
        
        # 写入 HTML 文件
        cat <<EOF > /tmp/www/index.html
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="5"> <!-- 每5秒自动刷新网页 -->
    <title>服务运行状态</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; text-align: center; margin-top: 10vh; background-color: #f4f4f9; color: #333; }
        .box { background: white; padding: 30px 50px; border-radius: 12px; display: inline-block; box-shadow: 0 8px 16px rgba(0,0,0,0.1); }
        .time { font-size: 28px; color: #007bff; font-weight: bold; margin: 15px 0; letter-spacing: 1px;}
        .footer { margin-top: 20px; color: #888; font-size: 13px; }
    </style>
</head>
<body>
    <div class="box">
        <h2>🚀 节点运行状态正常</h2>
        <div class="time">${DAYS}天 ${HOURS}小时 ${MINS}分钟 ${SECS}秒</div>
        <div class="footer">本次容器启动时间：$START_DATE (北京时间)</div>
    </div>
</body>
</html>
EOF
        sleep 60
    done
) &
# 启动简易 Web 服务器
busybox httpd -f -p $UPTIME_PORT -h /tmp/www &

# 4. 监听后台进程。任何一个后台进程 (x-tunnel 或 cloudflared) 崩溃，容器就会主动退出，触发云平台自动重启
wait -n
exit $?
