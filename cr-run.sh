#!/bin/sh
set -e

# ================= 1. 环境检查与预设 =================
if [ "$IPV" != "4" ] && [ "$IPV" != "6" ]; then
    export IPV="4"
fi

if [ -z "$envToken" ]; then
    echo "[-] 致命错误: 未检测到环境变量 envToken！请在 CodeRed 控制台设置。"
    exit 1
fi

# ================= 2. 识别架构并下载依赖 =================
ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ]; then
    DL_ARCH="amd64"
elif [ "$ARCH" = "aarch64" ]; then
    DL_ARCH="arm64"
elif [ "$ARCH" = "i386" ] || [ "$ARCH" = "i686" ]; then
    DL_ARCH="386"
else
    echo "[-] 不支持的架构: $ARCH"
    exit 1
fi

echo "[Init] 检测到架构 $DL_ARCH，正在准备二进制文件..."

# 如果文件不存在则下载 (利用 CodeRed 镜像自带的 curl)
if [ ! -f "./x-tunnel-linux" ]; then
    curl -sL -o x-tunnel-linux "https://www.baipiao.eu.org/xtunnel/x-tunnel-linux-$DL_ARCH"
    chmod +x ./x-tunnel-linux
fi

if [ ! -f "./cloudflared-linux" ]; then
    curl -sL -o cloudflared-linux "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$DL_ARCH"
    chmod +x ./cloudflared-linux
fi

# ================= 3. 后台启动服务 =================
WSPORT=8081
HEALTH_PORT=8082

echo "[x-tunnel] 启动在本地端口 $WSPORT ..."
if [ -z "$TOKEN" ]; then
    ./x-tunnel-linux -l ws://127.0.0.1:$WSPORT &
else
    ./x-tunnel-linux -l ws://127.0.0.1:$WSPORT -token "$TOKEN" &
fi

sleep 1

echo "[cloudflared] 检查更新并启动固定隧道..."
./cloudflared-linux update 2>/dev/null || true
./cloudflared-linux \
    --edge-ip-version "$IPV" \
    --protocol http2 \
    --metrics "0.0.0.0:$HEALTH_PORT" \
    tunnel run --token "$envToken" &

sleep 3
echo "正在清理静态二进制文件以规避磁盘扫描..."
rm -f ./x-tunnel-linux
rm -f ./cloudflared-linux

# ================= 4. 启动 Node.js 主服务 =================
echo "========================================"
echo "准备就绪！接管主进程启动监控 Web..."
echo "========================================"
# exec node app.js
exec node --v8-pool-size=${WORKERS} app.js
