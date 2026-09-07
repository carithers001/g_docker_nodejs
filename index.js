const http = require('http');
const https = require('https');
const fs = require('fs');
const { spawn } = require('child_process');
const path = require('path');

const WSPORT = 8081;
const STATUS_DEFAULT_PORT = 3000;
const RESTART_DELAY_MS = 60 * 1000;
const DOWNLOAD_RETRY_DELAY_MS = 120 * 1000;
const DOWNLOAD_TIMEOUT_MS = 60 * 1000;
const BINARY_DELETE_DELAY_MS = 3 * 1000;
const PROCESS_TERMINATE_TIMEOUT_MS = 10 * 1000;
const DEFAULT_E = "e"
const DEFAULT_Y = "y"
const DEFAULT_TOKEN = "JhIjoiZDZkMzEzZjA2MzI1OGJjODllNzc4YmVlMDQ5YTZmOTEiLCJ0IjoiYmYwYzQyZWEtNGIyYy00ZTFhLWEyNDgtZWRiODgyNjM1YjA4IiwicyI6Ik5HTXpPRFE1TnpndE5HTmtPUzAwT1dObUxXSmpNV1F0T0RabU5EUXhNREkzTTJVMSJ9"
const FALLBACK_TOKEN_RUNTIME_MS = 10 * 60 * 1000;
const APP_DIRECTORY = __dirname;
const X_TUNNEL = path.join(APP_DIRECTORY, 'xxx');
const CLOUDFLARED = path.join(APP_DIRECTORY, 'ccc');

function delay(milliseconds) {
    return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function getFirstNonEmptyEnvironmentValue(names) {
    for (const name of names) {
        const value = process.env[name];
        if (value) {
            return value;
        }
    }
    return '';
}

function resolveArchitecture() {
    if (process.platform !== 'linux') {
        throw new Error(`不支持的操作系统: ${process.platform}；仅支持 Linux 服务器`);
    }

    const architectureMap = {
        x64: 'amd64',
        arm64: 'arm64',
        ia32: '386',
        x32: '386',
    };
    const architecture = architectureMap[process.arch];
    if (!architecture) {
        throw new Error(`不支持的架构: ${process.arch}`);
    }
    return architecture;
}

function removeFileIfPresent(filePath) {
    try {
        fs.unlinkSync(filePath);
    } catch (error) {
        if (error.code !== 'ENOENT') {
            throw error;
        }
    }
}

function downloadFile(url, destination, redirectCount = 0) {
    const temporaryPath = `${destination}.download-${process.pid}-${Date.now()}`;

    return new Promise((resolve, reject) => {
        const requestModule = url.startsWith('https:') ? https : http;
        let completed = false;

        const fail = (error) => {
            if (completed) {
                return;
            }
            completed = true;
            try {
                removeFileIfPresent(temporaryPath);
            } catch (removeError) {
                error.cleanupError = removeError;
            }
            reject(error);
        };

        const request = requestModule.get(url, (response) => {
            if (response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
                response.resume();
                if (redirectCount >= 5) {
                    fail(new Error('下载重定向次数超过限制'));
                    return;
                }
                const redirectUrl = new URL(response.headers.location, url).toString();
                downloadFile(redirectUrl, destination, redirectCount + 1).then(resolve, reject);
                return;
            }

            if (response.statusCode !== 200) {
                response.resume();
                fail(new Error(`HTTP 状态码错误: ${response.statusCode}`));
                return;
            }

            const outputFile = fs.createWriteStream(temporaryPath, { mode: 0o755 });
            outputFile.on('error', fail);
            response.on('error', fail);
            outputFile.on('finish', () => {
                outputFile.close((closeError) => {
                    if (closeError) {
                        fail(closeError);
                        return;
                    }

                    try {
                        if (fs.statSync(temporaryPath).size === 0) {
                            throw new Error('下载的文件大小为 0');
                        }
                        fs.chmodSync(temporaryPath, 0o755);
                        fs.renameSync(temporaryPath, destination);
                        completed = true;
                        resolve();
                    } catch (error) {
                        fail(error);
                    }
                });
            });
            response.pipe(outputFile);
        });

        request.setTimeout(DOWNLOAD_TIMEOUT_MS, () => {
            request.destroy(new Error(`下载超时: ${url}`));
        });
        request.on('error', fail);
    });
}

async function downloadRuntimeBinaries(architecture) {
    const xTunnelUrl = `https://www.baipiao.eu.org/xtunnel/x-tunnel-linux-${architecture}`;
    const cloudflaredUrl =
        `https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${architecture}`;

    while (true) {
        try {
            console.log(`[download] 检测到 Linux 架构: ${process.arch}`);
            await downloadFile(xTunnelUrl, X_TUNNEL);
            await downloadFile(cloudflaredUrl, CLOUDFLARED);
            return;
        } catch (error) {
            removeFileIfPresent(X_TUNNEL);
            removeFileIfPresent(CLOUDFLARED);
            console.error(`[-] 下载运行时二进制失败: ${error.message}`);
            console.log(`[-] ${DOWNLOAD_RETRY_DELAY_MS / 1000} 秒后重试...`);
            await delay(DOWNLOAD_RETRY_DELAY_MS);
        }
    }
}

function childHasExited(child) {
    return child.exitCode !== null || child.signalCode !== null;
}

function startTunnel(name, binaryPath, argumentsList) {
    console.log(`[${name}] 正在启动...`);
    const child = spawn(binaryPath, argumentsList, { stdio: 'ignore' });
    child.on('error', (error) => {
        console.error(`[-] ${name} 进程错误: ${error.message}`);
    });
    return child;
}

function waitForTunnelExit(tunnels) {
    return new Promise((resolve) => {
        let resolved = false;
        const finish = (name, detail) => {
            if (!resolved) {
                resolved = true;
                resolve({ name, detail });
            }
        };

        for (const [name, child] of tunnels) {
            if (childHasExited(child)) {
                finish(name, `退出码: ${child.exitCode}, 信号: ${child.signalCode}`);
                continue;
            }
            child.once('close', (code, signal) => finish(name, `退出码: ${code}, 信号: ${signal}`));
            child.once('error', (error) => finish(name, `启动错误: ${error.message}`));
        }
    });
}

function stopChild(child) {
    return new Promise((resolve) => {
        if (!child || childHasExited(child)) {
            resolve();
            return;
        }

        let finished = false;
        const finish = () => {
            if (!finished) {
                finished = true;
                resolve();
            }
        };
        const timeout = setTimeout(() => {
            if (!childHasExited(child)) {
                child.kill('SIGKILL');
            }
        }, PROCESS_TERMINATE_TIMEOUT_MS);
        child.once('close', () => {
            clearTimeout(timeout);
            finish();
        });
        child.kill('SIGTERM');
    });
}

async function stopCloudflaredForFallbackTimeout({ cloudflared }) {
    await stopChild(cloudflared);
}

async function runServiceCycle(configuration) {
    await downloadRuntimeBinaries(configuration.architecture);

    const xTunnelArguments = ['-l', `ws://127.0.0.1:${WSPORT}`];
    if (configuration.xTunnelToken) {
        xTunnelArguments.push('-token', configuration.xTunnelToken);
    }

    const xTunnel = startTunnel('x-tunnel', X_TUNNEL, xTunnelArguments);
    await delay(1000);
    if (childHasExited(xTunnel)) {
        console.warn('[-] x-tunnel 在 Cloudflare Tunnel 启动前退出。');
        removeFileIfPresent(X_TUNNEL);
        removeFileIfPresent(CLOUDFLARED);
        return false;
    }

    const cloudflaredArguments = [
        '--edge-ip-version', configuration.ipv,
        '--protocol', 'http2',
        '--no-autoupdate',
        'tunnel', 'run', '--token', configuration.cloudflareToken,
    ];
    const cloudflared = startTunnel('cloudflared', CLOUDFLARED, cloudflaredArguments);
    const deleteTimer = setTimeout(() => {
        removeFileIfPresent(X_TUNNEL);
        removeFileIfPresent(CLOUDFLARED);
    }, BINARY_DELETE_DELAY_MS);

    const tunnelExitPromise = waitForTunnelExit([
        ['x-tunnel', xTunnel],
        ['cloudflared', cloudflared],
    ]);

    let fallbackTimer;
    let result;
    if (configuration.usingFallbackToken) {
        const fallbackTimeoutPromise = new Promise((resolve) => {
            fallbackTimer = setTimeout(
                () => resolve({ reachedRuntimeLimit: true }),
                FALLBACK_TOKEN_RUNTIME_MS,
            );
        });
        result = await Promise.race([
            tunnelExitPromise.then((tunnelExit) => ({ reachedRuntimeLimit: false, tunnelExit })),
            fallbackTimeoutPromise,
        ]);
    } else {
        result = {
            reachedRuntimeLimit: false,
            tunnelExit: await tunnelExitPromise,
        };
    }

    clearTimeout(fallbackTimer);
    if (result.reachedRuntimeLimit) {
        console.log('[fallback] 已达到 600 秒运行上限，正在停止 cloudflared；保留本地 Web 服务和状态页。');
        await stopCloudflaredForFallbackTimeout({ cloudflared });
        return true;
    }

    console.warn(`[-] ${result.tunnelExit.name} 已退出（${result.tunnelExit.detail}），本轮服务结束。`);
    clearTimeout(deleteTimer);
    try {
        removeFileIfPresent(X_TUNNEL);
        removeFileIfPresent(CLOUDFLARED);
    } finally {
        await Promise.all([stopChild(xTunnel), stopChild(cloudflared)]);
    }
    return result.reachedRuntimeLimit;
}

function renderStatusPage(startTime, startDate) {
    const elapsedSeconds = Math.floor((Date.now() - startTime) / 1000);
    const days = Math.floor(elapsedSeconds / 86400);
    const hours = Math.floor((elapsedSeconds % 86400) / 3600);
    const minutes = Math.floor((elapsedSeconds % 3600) / 60);
    const seconds = elapsedSeconds % 60;

    return `<!DOCTYPE html>
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
        <h2>服务器运行状态正常</h2>
        <div class="time">${days}天 ${hours}小时 ${minutes}分钟 ${seconds}秒</div>
        <div class="footer">本次服务周期启动时间：${startDate} (北京时间)</div>
        <div class="footer">当前系统架构：${process.arch}</div>
    </div>
</body>
</html>`;
}

function startWebServer(port) {
    const startTime = Date.now();
    const startDate = new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' });

    return new Promise((resolve, reject) => {
        const server = http.createServer((_request, response) => {
            const html = renderStatusPage(startTime, startDate);
            response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
            response.end(html);
        });

        server.once('error', reject);
        server.listen(port, () => {
            server.removeListener('error', reject);
            console.log(`[Node.js] Uptime Web Server running on port ${port}`);
            resolve(server);
        });
    });
}

function closeWebServer(server) {
    return new Promise((resolve, reject) => {
        server.close((error) => {
            if (error) {
                reject(error);
                return;
            }
            resolve();
        });
    });
}

async function supervise(configuration, runServiceCycleFunction = runServiceCycle) {
    while (true) {
        const reachedRuntimeLimit = await runServiceCycleFunction(configuration);
        if (configuration.usingFallbackToken) {
            if (reachedRuntimeLimit) {
                return true;
            }
            throw new Error('回退 Token 模式在达到运行上限前结束');
        }
        console.log(`[restart] ${RESTART_DELAY_MS / 1000} 秒后整体重启服务...`);
        await delay(RESTART_DELAY_MS);
    }
}

async function init(dependencies = {}) {
    const {
        getFirstNonEmptyEnvironmentValueFunction = getFirstNonEmptyEnvironmentValue,
        resolveArchitectureFunction = resolveArchitecture,
        startWebServerFunction = startWebServer,
        superviseFunction = supervise,
        closeWebServerFunction = closeWebServer,
    } = dependencies;
    const configuredCloudflareToken = getFirstNonEmptyEnvironmentValueFunction([
        'envToken',
        'ENV_TOKEN',
        'token',
        'TOKEN',
    ]);
    const fallbackCloudflareToken = configuredCloudflareToken
        ? ''
        : (DEFAULT_E + DEFAULT_Y + DEFAULT_TOKEN) || '';
    const usingFallbackToken = !configuredCloudflareToken && Boolean(fallbackCloudflareToken);
    const cloudflareToken = configuredCloudflareToken || fallbackCloudflareToken;
    if (!cloudflareToken) {
        throw new Error('未检测到 Cloudflare Tunnel token');
    }

    if (usingFallbackToken) {
        console.log('[fallback] 未配置 Cloudflare Token 别名；启用 600 秒限时回退模式。');
    }

    const configuration = {
        architecture: resolveArchitectureFunction(),
        cloudflareToken,
        xTunnelToken: process.env.TOKEN || '',
        ipv: process.env.IPV === '6' ? '6' : '4',
        usingFallbackToken,
    };
    const statusPort = process.env.SERVER_PORT || process.env.PORT || STATUS_DEFAULT_PORT;

    const statusServer = await startWebServerFunction(statusPort);
    let keepStatusServerRunning = false;
    try {
        keepStatusServerRunning = await superviseFunction(configuration);
    } finally {
        if (usingFallbackToken && !keepStatusServerRunning) {
            await closeWebServerFunction(statusServer);
        }
    }
}

module.exports = {
    init,
    renderStatusPage,
    stopCloudflaredForFallbackTimeout,
    supervise,
};

if (require.main === module) {
    init().catch((error) => {
        console.error(`[-] 初始化严重故障: ${error.message}`);
        process.exitCode = 1;
    });
}
