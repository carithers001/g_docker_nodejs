#!/bin/bash
set -e

if [ "$IPV" != "4" ] && [ "$IPV" != "6" ]; then
    echo "[-] IPV 参数错误 : $IPV"
    exit 1
fi

# 1. 设置给云平台健康检查用的对外端口 (通常平台会自动分配 PORT 变量，默认 8080)
HEALTH_PORT="${PORT:-8080}"

# 2. x-tunnel 本地内部使用的真实端口换为 8081，避免和健康检查端口冲突
WSPORT=8081
UPTIME_PORT=8082 # 用于显示运行时间的本地端口

# 心跳保活逻辑
(
    while true; do
        curl -s -m 5 https://1.1.1.1 > /dev/null 2>&1 || true
        sleep 300
    done
) &

sleep 1

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

# ================= 指定时间定时重启逻辑 (每天 04:00) =================
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
