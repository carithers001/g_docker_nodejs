const http = require('http');

// 记录服务器启动时间
const startTime = Date.now();
const startDate = new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' });

const server = http.createServer((req, res) => {
    // 动态计算运行时长
    const now = Date.now();
    const diff = Math.floor((now - startTime) / 1000);
    
    const days = Math.floor(diff / 86400);
    const hours = Math.floor((diff % 86400) / 3600);
    const mins = Math.floor((diff % 3600) / 60);
    const secs = diff % 60;

    // 输出带有刷新代码的 HTML
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
        <h2>🚀 节点运行状态正常</h2>
        <div class="time">${days}天 ${hours}小时 ${mins}分钟 ${secs}秒</div>
        <div class="footer">本次容器启动时间：${startDate} (北京时间)</div>
        <div class="footer">当前系统架构：${process.arch}</div>
    </div>
</body>
</html>`;

    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(html);
});

// CodeRed 强制监听 3000
const PORT = 3000;
server.listen(PORT, () => {
    console.log(`[Node.js] Uptime Web Server running on port ${PORT}`);
});

