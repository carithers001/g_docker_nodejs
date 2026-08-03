const http = require('http');
const fs = require('fs');
const { spawn, execSync } = require('child_process');

// 延时辅助函数 (替代 bash 中的 sleep)
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function init() {
    // ================= 1. 环境检查与预设 =================
    let IPV = process.env.IPV;
    if (IPV !== "4" && IPV !== "6") {
        IPV = "4";
        process.env.IPV = "4";
    }

    const envToken = process.env.envToken;
	if (!envToken) {
        console.error("[-] 致命错误: 未检测到环境变量 envToken！请在控制面板/环境变量中设置。");
        process.exit(1);
    }

    // ================= 2. 识别架构并下载依赖 =================
    // 将 Node.js 的架构名称映射为二进制文件所需的名称
    const archMap = {
        'x64': 'amd64',
        'arm64': 'arm64',
        'ia32': '386',
        'x32': '386'
    };
    const DL_ARCH = archMap[process.arch];
    
    if (!DL_ARCH) {
        console.error(`[-] 不支持的架构: ${process.arch}`);
        process.exit(1);
    }

    console.log(`[Init] 检测到架构 ${DL_ARCH}，正在准备二进制文件...`);

    // 使用 child_process 调用 curl 下载并赋予执行权限
    try {
        if (!fs.existsSync('./x-tunnel-linux')) {
            execSync(`curl -sL -o x-tunnel-linux "https://www.baipiao.eu.org/xtunnel/x-tunnel-linux-${DL_ARCH}"`);
            execSync('chmod +x ./x-tunnel-linux');
        }

        if (!fs.existsSync('./cloudflared-linux')) {
            execSync(`curl -sL -o cloudflared-linux "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${DL_ARCH}"`);
            execSync('chmod +x ./cloudflared-linux');
        }
    } catch (error) {
        console.error("[-] 下载二进制文件时出错:", error.message);
    }

    // ================= 3. 后台启动服务 =================
    const WSPORT = 8081;
    const HEALTH_PORT = 8082;
    const TOKEN = process.env.TOKEN;

    console.log(`[x-tunnel] 启动在本地端口 ${WSPORT} ...`);
    const xTunnelArgs = TOKEN 
        ? ['-l', `ws://127.0.0.1:${WSPORT}`, '-token', TOKEN] 
        : ['-l', `ws://127.0.0.1:${WSPORT}`];
    
    // spawn 开启后台独立进程 (对应 shell 中的 &)
    const xtunnel = spawn('./x-tunnel-linux', xTunnelArgs, { detached: true, stdio: 'ignore' });
    xtunnel.unref(); // 允许 Node 主进程不被该子进程阻塞

    await sleep(1000); // sleep 1

    console.log("[cloudflared] 检查更新并启动固定隧道...");
    try { execSync('./cloudflared-linux update 2>/dev/null || true'); } catch (e) {}

    const cfArgs = [
        '--edge-ip-version', IPV,
        '--protocol', 'http2',
        '--metrics', `0.0.0.0:${HEALTH_PORT}`,
        'tunnel', 'run', '--token', envToken
    ];
    const cloudflared = spawn('./cloudflared-linux', cfArgs, { detached: true, stdio: 'ignore' });
    cloudflared.unref();

    await sleep(3000); // sleep 3

    console.log("正在清理静态二进制文件以规避磁盘扫描...");
    if (fs.existsSync('./x-tunnel-linux')) fs.unlinkSync('./x-tunnel-linux');
    if (fs.existsSync('./cloudflared-linux')) fs.unlinkSync('./cloudflared-linux');

    // ================= 4. 启动 Node.js 主服务 =================
    console.log("========================================");
    console.log("准备就绪！接管主进程启动监控 Web...");
    console.log("========================================");
    
    startWebServer();
}

// 提取出来的 Web 服务器启动函数
function startWebServer() {
    const startTime = Date.now();
    const startDate = new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' });

    const server = http.createServer((req, res) => {
        const now = Date.now();
        const diff = Math.floor((now - startTime) / 1000);
        
        const days = Math.floor(diff / 86400);
        const hours = Math.floor((diff % 86400) / 3600);
        const mins = Math.floor((diff % 3600) / 60);
        const secs = diff % 60;

        const html = `<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="5">
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
        <h2>?? 节点运行状态正常</h2>
        <div class="time">${days}天 ${hours}小时 ${mins}分钟 ${secs}秒</div>
        <div class="footer">本次容器启动时间：${startDate} (北京时间)</div>
        <div class="footer">当前系统架构：${process.arch}</div>
    </div>
</body>
</html>`;

        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        res.end(html);
    });

    // 这里我添加了环境变量判断，为了防止面板分配的端口不是 3000 导致无法访问
    const PORT = process.env.SERVER_PORT || process.env.PORT || 3000;
    server.listen(PORT, () => {
        console.log(`[Node.js] Uptime Web Server running on port ${PORT}`);
    });
}

// 捕获未处理的异常以防止进程崩溃退出
process.on('uncaughtException', (err) => {
    console.error('发生未捕获的错误:', err);
});

// 执行初始化流程
init().catch(console.error);