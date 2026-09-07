#!/bin/bash
set -u

APP_DIRECTORY=/app
WSPORT=8081
X_TUNNEL="$APP_DIRECTORY/xxx"
CLOUDFLARED="$APP_DIRECTORY/ccc"
RESTART_DELAY_SECONDS="${RESTART_DELAY_SECONDS:-5}"
DOWNLOAD_RETRY_DELAY_SECONDS="${DOWNLOAD_RETRY_DELAY_SECONDS:-10}"
FALLBACK_TOKEN_RUNTIME_SECONDS=600
FALLBACK_TERMINATION_GRACE_SECONDS=10
X_TUNNEL_PID=""
CLOUDFLARED_PID=""
STATUS_UPDATER_PID=""
HTTP_SERVER_PID=""
FALLBACK_TIMER_PID=""
USING_FALLBACK_CLOUDFLARE_TOKEN=0
DEFAULT_E="e"
DEFAULT_Y="y"
DEFAULT_TOKEN="JhIjoiZDZkMzEzZjA2MzI1OGJjODllNzc4YmVlMDQ5YTZmOTEiLCJ0IjoiYmYwYzQyZWEtNGIyYy00ZTFhLWEyNDgtZWRiODgyNjM1YjA4IiwicyI6Ik5HTXpPRFE1TnpndE5HTmtPUzAwT1dObUxXSmpNV1F0T0RabU5EUXhNREkzTTJVMSJ9"

if [ "${IPV:-4}" = "6" ]; then
    IPV="6"
else
    IPV="4"
fi

# 与 JS 分支一致：Cloudflare token 支持多个环境变量别名，按此顺序取第一个非空值。
CLOUDFLARE_TOKEN="${envToken:-${ENV_TOKEN:-${token:-${TOKEN:-}}}}"
X_TUNNEL_TOKEN="${TOKEN:-}"
if [ -z "$CLOUDFLARE_TOKEN" ]; then
    CLOUDFLARE_TOKEN="${DEFAULT_E}${DEFAULT_Y}${DEFAULT_TOKEN}"
    if [ -n "$CLOUDFLARE_TOKEN" ]; then
        USING_FALLBACK_CLOUDFLARE_TOKEN=1
        echo "[fallback] 未配置 Cloudflare Token 别名；启用 600 秒限时回退模式。"
    else
        echo "[-] 致命错误: 未检测到 Cloudflare Tunnel token！"
        exit 1
    fi
fi

UPTIME_PORT="${SERVER_PORT:-${PORT:-3000}}"

case "$(uname -m)" in
    x86_64 | amd64)
        DOWNLOAD_ARCH="amd64"
        ;;
    aarch64 | arm64)
        DOWNLOAD_ARCH="arm64"
        ;;
    i386 | i686 | x86)
        DOWNLOAD_ARCH="386"
        ;;
    *)
        echo "[-] 不支持的 CPU 架构: $(uname -m)"
        exit 1
        ;;
esac

X_TUNNEL_URL="https://www.baipiao.eu.org/xtunnel/x-tunnel-linux-${DOWNLOAD_ARCH}"
CLOUDFLARED_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${DOWNLOAD_ARCH}"

download_binary() {
    local url="$1"
    local destination="$2"
    local temporary_file="${destination}.download.$$"

    rm -f "$temporary_file"
    if ! curl --fail --location --silent --show-error --connect-timeout 10 --max-time 60 \
        "$url" -o "$temporary_file"; then
        rm -f "$temporary_file"
        return 1
    fi

    if [ ! -s "$temporary_file" ]; then
        echo "[-] 下载 ${destination##*/} 失败: 文件为空"
        rm -f "$temporary_file"
        return 1
    fi

    chmod 755 "$temporary_file"
    mv -f "$temporary_file" "$destination"
}

download_runtime_binaries() {
    while true; do
        echo "[download] 检测到 Linux 架构: $(uname -m)"
        if download_binary "$X_TUNNEL_URL" "$X_TUNNEL" && \
            download_binary "$CLOUDFLARED_URL" "$CLOUDFLARED"; then
            return 0
        fi

        rm -f "$X_TUNNEL" "$CLOUDFLARED"
        echo "[-] 下载运行时二进制失败，${DOWNLOAD_RETRY_DELAY_SECONDS} 秒后重试..."
        sleep "$DOWNLOAD_RETRY_DELAY_SECONDS"
    done
}

write_status_page() {
    local now elapsed days hours minutes seconds
    now=$(date +%s)
    elapsed=$((now - START_TIME))
    days=$((elapsed / 86400))
    hours=$(((elapsed % 86400) / 3600))
    minutes=$(((elapsed % 3600) / 60))
    seconds=$((elapsed % 60))

    cat <<EOF > /tmp/www/index.html
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="5">
    <title>服务运行状态</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; text-align: center; margin-top: 10vh; background-color: #f4f4f9; color: #333; }
        .box { background: white; padding: 30px 50px; border-radius: 12px; display: inline-block; box-shadow: 0 8px 16px rgba(0,0,0,0.1); }
        .time { font-size: 28px; color: #007bff; font-weight: bold; margin: 15px 0; letter-spacing: 1px; }
        .footer { margin-top: 20px; color: #888; font-size: 13px; }
    </style>
</head>
<body>
    <div class="box">
        <h2> 服务器运行状态正常</h2>
        <div class="time">${days}天 ${hours}小时 ${minutes}分钟 ${seconds}秒</div>
        <div class="footer">本次服务周期启动时间：${START_DATE} (北京时间)</div>
    </div>
</body>
</html>
EOF
}

terminate_process() {
    local process_id="$1"
    if [ -n "$process_id" ] && kill -0 "$process_id" 2>/dev/null; then
        kill "$process_id" 2>/dev/null || true
    fi
}

force_terminate_process() {
    local process_id="$1"
    if [ -n "$process_id" ] && kill -0 "$process_id" 2>/dev/null; then
        kill -KILL "$process_id" 2>/dev/null || true
    fi
}

stop_cloudflared_for_fallback_timeout() {
    local elapsed_seconds=0

    terminate_process "$CLOUDFLARED_PID"

    while kill -0 "$CLOUDFLARED_PID" 2>/dev/null; do
        if [ "$elapsed_seconds" -ge "$FALLBACK_TERMINATION_GRACE_SECONDS" ]; then
            force_terminate_process "$CLOUDFLARED_PID"
            break
        fi

        sleep 1
        elapsed_seconds=$((elapsed_seconds + 1))
    done
}

start_fallback_timer() {
    if [ "$USING_FALLBACK_CLOUDFLARE_TOKEN" -ne 1 ]; then
        return
    fi

    (
        sleep "$FALLBACK_TOKEN_RUNTIME_SECONDS"
    ) &
    FALLBACK_TIMER_PID=$!
}

cleanup_service_cycle() {
    terminate_process "$FALLBACK_TIMER_PID"
    terminate_process "$STATUS_UPDATER_PID"
    terminate_process "$HTTP_SERVER_PID"
    terminate_process "$X_TUNNEL_PID"
    terminate_process "$CLOUDFLARED_PID"

    wait "$FALLBACK_TIMER_PID" 2>/dev/null || true
    wait "$STATUS_UPDATER_PID" 2>/dev/null || true
    wait "$HTTP_SERVER_PID" 2>/dev/null || true
    wait "$X_TUNNEL_PID" 2>/dev/null || true
    wait "$CLOUDFLARED_PID" 2>/dev/null || true

    rm -f "$X_TUNNEL" "$CLOUDFLARED"
    X_TUNNEL_PID=""
    CLOUDFLARED_PID=""
    STATUS_UPDATER_PID=""
    HTTP_SERVER_PID=""
    FALLBACK_TIMER_PID=""
}

run_service_cycle() {
    download_runtime_binaries

    START_TIME=$(date +%s)
    START_DATE=$(date "+%Y-%m-%d %H:%M:%S")
    mkdir -p /tmp/www

    echo "[x-tunnel] 启动在本地端口 $WSPORT ..."
    if [ -n "$X_TUNNEL_TOKEN" ]; then
        "$X_TUNNEL" -l "ws://127.0.0.1:$WSPORT" -token "$X_TUNNEL_TOKEN" &
    else
        "$X_TUNNEL" -l "ws://127.0.0.1:$WSPORT" &
    fi
    X_TUNNEL_PID=$!

    sleep 1
    if ! kill -0 "$X_TUNNEL_PID" 2>/dev/null; then
        echo "[-] x-tunnel 在 Cloudflare Tunnel 启动前退出"
        cleanup_service_cycle
        return 1
    fi

    "$CLOUDFLARED" \
        --edge-ip-version "$IPV" \
        --protocol http2 \
        --no-autoupdate \
        tunnel run --token "$CLOUDFLARE_TOKEN" &
    CLOUDFLARED_PID=$!
    start_fallback_timer

    echo "========================================"
    echo "当前本地服务端口: $WSPORT"
    echo "当前状态页端口: $UPTIME_PORT"
    echo "========================================"

    (
        sleep 3
        rm -f "$X_TUNNEL" "$CLOUDFLARED"
        while true; do
            write_status_page
            sleep 60
        done
    ) &
    STATUS_UPDATER_PID=$!

    busybox httpd -f -p "$UPTIME_PORT" -h /tmp/www &
    HTTP_SERVER_PID=$!

    # 回退 Token 模式将限时器一并等待；普通模式仍只等待两个隧道。
    local completed_pid=""
    if [ "$USING_FALLBACK_CLOUDFLARE_TOKEN" -eq 1 ]; then
        wait -n -p completed_pid "$X_TUNNEL_PID" "$CLOUDFLARED_PID" "$FALLBACK_TIMER_PID"
    else
        wait -n "$X_TUNNEL_PID" "$CLOUDFLARED_PID"
    fi
    local tunnel_exit_code=$?

    if [ "$USING_FALLBACK_CLOUDFLARE_TOKEN" -eq 1 ] && [ "$completed_pid" = "$FALLBACK_TIMER_PID" ]; then
        echo "[fallback] 已达到 600 秒运行上限，正在停止 cloudflared；保留本地 Web 服务和状态页。"
        FALLBACK_TIMER_PID=""
        stop_cloudflared_for_fallback_timeout
        wait "$CLOUDFLARED_PID" 2>/dev/null || true
        CLOUDFLARED_PID=""

        # Do not run cleanup here: it would stop x-tunnel, the status updater,
        # and busybox httpd. Keep the local status page alive after fallback expiry.
        wait "$HTTP_SERVER_PID"
        local status_server_exit_code=$?
        echo "[-] 状态页服务已退出。"
        cleanup_service_cycle
        return "$status_server_exit_code"
    fi

    echo "[-] 隧道进程已退出，本轮服务结束。"
    cleanup_service_cycle
    return "$tunnel_exit_code"
}

trap 'cleanup_service_cycle; exit 0' INT TERM

if [ "$USING_FALLBACK_CLOUDFLARE_TOKEN" -eq 1 ]; then
    run_service_cycle
    exit $?
fi

while true; do
    run_service_cycle
    echo "[restart] ${RESTART_DELAY_SECONDS} 秒后整体重启服务..."
    sleep "$RESTART_DELAY_SECONDS"
done
