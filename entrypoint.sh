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
HEALTH_PORT="${PORT:-8080}"

# 2. x-tunnel 本地内部使用的真实端口换为 8081，避免和健康检查端口冲突
WSPORT=8081

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
    tunnel run --token "$envToken" &

echo "========================================"
echo "已连接到 Cloudflare Zero Trust (Token 硬编码模式)"
echo "当前健康检查端口: $HEALTH_PORT"
echo "当前本地服务端口: $WSPORT"
echo "========================================"

(
    # 1. 设置为你所在的时区（Asia/Shanghai 代表北京/香港时间）
    export TZ="Asia/Shanghai" 
    
    # 2. 设置你希望每天自动重启的时间 (24小时制，这里默认设为凌晨 04:00)
    RESTART_TIME="04:00"

    # 计算时间差并休眠
    NOW=$(date +%s)
    TARGET=$(date -d "$RESTART_TIME" +%s)

    # 如果容器启动时，今天的这个时间已经过了，就将目标时间设为明天的这个时间
    if [ $NOW -ge $TARGET ]; then
        TARGET=$(date -d "tomorrow $RESTART_TIME" +%s)
    fi

    # 计算距离目标时间还有多少秒
    SLEEP_SECONDS=$((TARGET - NOW))
    
    echo "[定时任务] 现在时间是 $(date "+%Y-%m-%d %H:%M:%S")"
    echo "[定时任务] 将在 $SLEEP_SECONDS 秒后 (即 $RESTART_TIME) 自动触发重启..."
    
    sleep $SLEEP_SECONDS
    
    echo "[定时任务] 到达设定时间，主动触发系统重启..."
    kill -TERM $$  # 杀死主进程触发重启
) &

# 4. 监听后台进程。任何一个后台进程 (x-tunnel 或 cloudflared) 崩溃，容器就会主动退出，触发云平台自动重启
wait -n
exit $?
