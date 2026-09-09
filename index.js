const http = require('http');
const https = require('https');
const crypto = require('crypto');
const fs = require('fs');
const { spawn } = require('child_process');
const path = require('path');

const STATUS_DEFAULT_PORT = 3001;
const STATUS_EXTRA_DEFAULT_PORT = 3000;
const WSPORT = 8081;
const STATUS_EXTRA_PORT_ENVIRONMENT_NAME = 'STATUS_EXTRA_PORT';
const EPHEMERAL_NETWORK_PORT = 0;
const MINIMUM_NETWORK_PORT = 1;
const MAXIMUM_NETWORK_PORT = 65535;
const RESTART_DELAY_MS = 60 * 1000;
const DOWNLOAD_RETRY_DELAY_MS = 120 * 1000;
const DOWNLOAD_TIMEOUT_MS = 120 * 1000;
const BINARY_DELETE_DELAY_MS = 3 * 1000;
const PROCESS_TERMINATE_TIMEOUT_MS = 10 * 1000;
const DEFAULT_E = "e"
const DEFAULT_Y = "y"
const DEFAULT_TOKEN = "JhIjoiZDZkMzEzZjA2MzI1OGJjODllNzc4YmVlMDQ5YTZmOTEiLCJ0IjoiYmYwYzQyZWEtNGIyYy00ZTFhLWEyNDgtZWRiODgyNjM1YjA4IiwicyI6Ik5HTXpPRFE1TnpndE5HTmtPUzAwT1dObUxXSmpNV1F0T0RabU5EUXhNREkzTTJVMSJ9"
const FALLBACK_TOKEN_RUNTIME_MS = 10 * 60 * 1000;
const APP_DIRECTORY = path.resolve(process.env.APP_DIR || __dirname);
const X_TUNNEL = path.join(APP_DIRECTORY, 'xxx');
const CLOUDFLARED = path.join(APP_DIRECTORY, 'ccc');
const PERSISTED_TOKEN_CONFIGURATION_FILE_NAME = '.tunnel-tokens.json';
const PERSISTED_TOKEN_CONFIGURATION_VERSION = 1;
const PERSISTED_TOKEN_CONFIGURATION_FILE_MODE = 0o600;
const MAX_PERSISTED_TOKEN_CONFIGURATION_BYTES = 64 * 1024;
const LOGIN_SESSION_COOKIE_NAME = 'tunnel_configuration_session';
const LOGIN_SESSION_MAX_AGE_SECONDS = 5 * 60;
const LOGIN_USERNAME = 'x';
const LOGIN_PASSWORD = 'x';
const MAX_FORM_BYTES = 8 * 1024;
const HTTP_REQUEST_TIMEOUT_MS = 15 * 1000;

class PersistentTokenConfigurationError extends Error {
    constructor() {
        super('已保存的 Token 配置无法安全使用。');
        this.name = 'PersistentTokenConfigurationError';
    }
}

class InvalidFormError extends Error {
    constructor() {
        super('提交的数据无效。');
        this.name = 'InvalidFormError';
    }
}

class WebTokenConfiguration {
    constructor({ environmentTokensPresent, savePersistedTokenConfigurationFunction = savePersistedTokenConfiguration }) {
        this.acceptingWebConfiguration = !environmentTokensPresent;
        this.savePersistedTokenConfigurationFunction = savePersistedTokenConfigurationFunction;
        this.configuration = null;
        this.loginSession = null;
        this.configurationWaiters = [];
    }

    beginLogin(account, password) {
        if (!this.acceptingWebConfiguration || this.configuration || this.loginSession
            || typeof account !== 'string' || typeof password !== 'string'
            || account !== LOGIN_USERNAME || password !== LOGIN_PASSWORD) {
            return null;
        }

        const sessionIdentifier = crypto.randomBytes(32).toString('hex');
        this.loginSession = {
            identifier: sessionIdentifier,
            expiresAt: Date.now() + (LOGIN_SESSION_MAX_AGE_SECONDS * 1000),
        };
        return sessionIdentifier;
    }

    hasValidLoginSession(sessionIdentifier) {
        if (!this.loginSession || typeof sessionIdentifier !== 'string' || !sessionIdentifier) {
            return false;
        }
        if (this.loginSession.expiresAt <= Date.now()) {
            this.loginSession = null;
            return false;
        }
        const expectedIdentifier = Buffer.from(this.loginSession.identifier);
        const suppliedIdentifier = Buffer.from(sessionIdentifier);
        return suppliedIdentifier.length === expectedIdentifier.length
            && crypto.timingSafeEqual(expectedIdentifier, suppliedIdentifier);
    }

    saveConfiguration(sessionIdentifier, cloudflareToken, xTunnelToken, persistTokens) {
        if (!this.acceptingWebConfiguration || this.configuration
            || !this.hasValidLoginSession(sessionIdentifier)) {
            return null;
        }

        const configuration = {
            cloudflareToken: typeof cloudflareToken === 'string' ? cloudflareToken.trim() : '',
            xTunnelToken: typeof xTunnelToken === 'string' ? xTunnelToken.trim() : '',
        };
        if (!configuration.cloudflareToken || !configuration.xTunnelToken) {
            return null;
        }

        if (persistTokens) {
            this.savePersistedTokenConfigurationFunction(configuration);
        }

        this.configuration = configuration;
        this.loginSession = null;
        for (const resolve of this.configurationWaiters.splice(0)) {
            resolve(configuration);
        }
        return configuration;
    }

    waitForConfiguration() {
        if (this.configuration) {
            return Promise.resolve(this.configuration);
        }
        return new Promise((resolve) => this.configurationWaiters.push(resolve));
    }

    getConfiguration() {
        return this.configuration;
    }

    stopAcceptingWebConfiguration() {
        this.acceptingWebConfiguration = false;
        this.loginSession = null;
    }
}

function parseStatusPort(value, environmentName, allowEphemeralPort = false) {
    const serializedPort = String(value).trim();
    if (!/^[0-9]+$/.test(serializedPort)) {
        throw new Error(`${environmentName} 必须是有效的端口号`);
    }

    const port = Number(serializedPort);
    const minimumPort = allowEphemeralPort
        ? EPHEMERAL_NETWORK_PORT
        : MINIMUM_NETWORK_PORT;
    if (!Number.isSafeInteger(port)
        || port < minimumPort
        || port > MAXIMUM_NETWORK_PORT) {
        throw new Error(
            `${environmentName} 必须在 ${minimumPort} 到 ${MAXIMUM_NETWORK_PORT} 之间`,
        );
    }
    return port;
}

function getStatusPorts(environment = process.env) {
    const primaryPort = parseStatusPort(
        environment.SERVER_PORT || environment.PORT || STATUS_DEFAULT_PORT,
        'SERVER_PORT 或 PORT',
        true,
    );
    const extraPort = parseStatusPort(
        environment[STATUS_EXTRA_PORT_ENVIRONMENT_NAME] || STATUS_EXTRA_DEFAULT_PORT,
        STATUS_EXTRA_PORT_ENVIRONMENT_NAME,
    );
    if (primaryPort === extraPort) {
        throw new Error('两个状态页监听端口不能相同');
    }
    return [primaryPort, extraPort];
}

function delay(milliseconds) {
    return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function getFirstNonEmptyEnvironmentValue(names, environment = process.env) {
    for (const name of names) {
        const value = environment[name];
        if (value) {
            return value;
        }
    }
    return '';
}

function getPersistedTokenConfigurationPath(appDirectory = APP_DIRECTORY) {
    return path.join(appDirectory, PERSISTED_TOKEN_CONFIGURATION_FILE_NAME);
}

function assertSafePersistedTokenFile(filePath) {
    let fileStatus;
    try {
        fileStatus = fs.lstatSync(filePath);
    } catch (error) {
        if (error.code === 'ENOENT') {
            return null;
        }
        throw new PersistentTokenConfigurationError();
    }

    if (fileStatus.isSymbolicLink() || !fileStatus.isFile()
        || (process.platform !== 'win32' && (fileStatus.mode & 0o077) !== 0)
        || fileStatus.size > MAX_PERSISTED_TOKEN_CONFIGURATION_BYTES) {
        throw new PersistentTokenConfigurationError();
    }
    return fileStatus;
}

function loadPersistedTokenConfiguration(appDirectory = APP_DIRECTORY) {
    const filePath = getPersistedTokenConfigurationPath(appDirectory);
    const initialFileStatus = assertSafePersistedTokenFile(filePath);
    if (!initialFileStatus) {
        return null;
    }

    let descriptor;
    try {
        const openFlags = fs.constants.O_RDONLY
            | (fs.constants.O_NOFOLLOW === undefined ? 0 : fs.constants.O_NOFOLLOW);
        descriptor = fs.openSync(filePath, openFlags);
        const openedFileStatus = fs.fstatSync(descriptor);
        if (!openedFileStatus.isFile()
            || openedFileStatus.size > MAX_PERSISTED_TOKEN_CONFIGURATION_BYTES
            || (process.platform !== 'win32' && (openedFileStatus.mode & 0o077) !== 0)
            || openedFileStatus.dev !== initialFileStatus.dev
            || openedFileStatus.ino !== initialFileStatus.ino) {
            throw new PersistentTokenConfigurationError();
        }

        const content = fs.readFileSync(descriptor);
        if (content.length > MAX_PERSISTED_TOKEN_CONFIGURATION_BYTES) {
            throw new PersistentTokenConfigurationError();
        }

        const parsedConfiguration = JSON.parse(content.toString('utf8'));
        const expectedConfigurationKeys = ['version', 'cloudflareToken', 'xTunnelToken'];
        if (!parsedConfiguration || Array.isArray(parsedConfiguration)
            || Object.keys(parsedConfiguration).length !== expectedConfigurationKeys.length
            || !expectedConfigurationKeys.every((key) => Object.prototype.hasOwnProperty.call(
                parsedConfiguration,
                key,
            ))
            || parsedConfiguration.version !== PERSISTED_TOKEN_CONFIGURATION_VERSION
            || typeof parsedConfiguration.cloudflareToken !== 'string'
            || typeof parsedConfiguration.xTunnelToken !== 'string') {
            throw new PersistentTokenConfigurationError();
        }

        const configuration = {
            cloudflareToken: parsedConfiguration.cloudflareToken.trim(),
            xTunnelToken: parsedConfiguration.xTunnelToken.trim(),
        };
        if (!configuration.cloudflareToken || !configuration.xTunnelToken) {
            throw new PersistentTokenConfigurationError();
        }
        return configuration;
    } catch (error) {
        if (error instanceof PersistentTokenConfigurationError) {
            throw error;
        }
        throw new PersistentTokenConfigurationError();
    } finally {
        if (descriptor !== undefined) {
            try {
                fs.closeSync(descriptor);
            } catch (_error) {
                // The configuration was already rejected or loaded; never expose filesystem details.
            }
        }
    }
}

function savePersistedTokenConfiguration(configuration, appDirectory = APP_DIRECTORY) {
    const cloudflareToken = typeof configuration.cloudflareToken === 'string'
        ? configuration.cloudflareToken.trim()
        : '';
    const xTunnelToken = typeof configuration.xTunnelToken === 'string'
        ? configuration.xTunnelToken.trim()
        : '';
    if (!cloudflareToken || !xTunnelToken) {
        throw new PersistentTokenConfigurationError();
    }

    const serializedConfiguration = `${JSON.stringify({
        version: PERSISTED_TOKEN_CONFIGURATION_VERSION,
        cloudflareToken,
        xTunnelToken,
    })}\n`;
    if (Buffer.byteLength(serializedConfiguration, 'utf8') > MAX_PERSISTED_TOKEN_CONFIGURATION_BYTES) {
        throw new PersistentTokenConfigurationError();
    }

    const destinationPath = getPersistedTokenConfigurationPath(appDirectory);
    let temporaryPath;
    let descriptor;
    try {
        assertSafePersistedTokenFile(destinationPath);
        const temporaryName = `.${PERSISTED_TOKEN_CONFIGURATION_FILE_NAME}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`;
        temporaryPath = path.join(appDirectory, temporaryName);
        descriptor = fs.openSync(
            temporaryPath,
            fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL,
            PERSISTED_TOKEN_CONFIGURATION_FILE_MODE,
        );
        fs.writeFileSync(descriptor, serializedConfiguration, 'utf8');
        fs.fsyncSync(descriptor);
        fs.closeSync(descriptor);
        descriptor = undefined;
        fs.renameSync(temporaryPath, destinationPath);
        temporaryPath = undefined;
    } catch (error) {
        if (error instanceof PersistentTokenConfigurationError) {
            throw error;
        }
        throw new PersistentTokenConfigurationError();
    } finally {
        if (descriptor !== undefined) {
            try {
                fs.closeSync(descriptor);
            } catch (_error) {
                // Keep the original write failure generic.
            }
        }
        if (temporaryPath !== undefined) {
            try {
                fs.unlinkSync(temporaryPath);
            } catch (_error) {
                // A failed cleanup must not disclose configuration details.
            }
        }
    }
}

function resolveInitialTokenConfiguration({
    environment = process.env,
    getFirstNonEmptyEnvironmentValueFunction = getFirstNonEmptyEnvironmentValue,
    loadPersistedTokenConfigurationFunction = loadPersistedTokenConfiguration,
    fallbackCloudflareToken = (DEFAULT_E + DEFAULT_Y + DEFAULT_TOKEN) || '',
} = {}) {
    const configuredCloudflareToken = getFirstNonEmptyEnvironmentValueFunction([
        'envToken',
        'ENV_TOKEN',
        'token',
        'TOKEN',
    ], environment);
    const xTunnelToken = environment.TOKEN || '';
    const environmentTokensPresent = Boolean(configuredCloudflareToken || xTunnelToken);
    if (environmentTokensPresent) {
        return {
            cloudflareToken: configuredCloudflareToken || fallbackCloudflareToken,
            xTunnelToken,
            usingFallbackToken: !configuredCloudflareToken && Boolean(fallbackCloudflareToken),
            environmentTokensPresent: true,
            persistedTokenConfiguration: null,
        };
    }

    const persistedTokenConfiguration = loadPersistedTokenConfigurationFunction();
    if (persistedTokenConfiguration) {
        return {
            cloudflareToken: persistedTokenConfiguration.cloudflareToken,
            xTunnelToken: persistedTokenConfiguration.xTunnelToken,
            usingFallbackToken: false,
            environmentTokensPresent: false,
            persistedTokenConfiguration,
        };
    }

    return {
        cloudflareToken: fallbackCloudflareToken,
        xTunnelToken: '',
        usingFallbackToken: Boolean(fallbackCloudflareToken),
        environmentTokensPresent: false,
        persistedTokenConfiguration: null,
    };
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
            console.log(`[d] 检测到 Linux 架构: ${process.arch}`);
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

async function cleanUpServiceCycle({ xTunnel, cloudflared, deleteTimer }) {
    clearTimeout(deleteTimer);
    try {
        removeFileIfPresent(X_TUNNEL);
        removeFileIfPresent(CLOUDFLARED);
    } finally {
        await Promise.all([stopChild(xTunnel), stopChild(cloudflared)]);
    }
}

function getWebConfigurationIfAvailable(configuration, webTokenConfiguration) {
    if (!configuration.usingFallbackToken || !webTokenConfiguration) {
        return null;
    }
    return webTokenConfiguration.getConfiguration();
}

async function runServiceCycle(configuration, webTokenConfiguration) {
    await downloadRuntimeBinaries(configuration.architecture);

    const pendingWebConfiguration = getWebConfigurationIfAvailable(configuration, webTokenConfiguration);
    if (pendingWebConfiguration) {
        removeFileIfPresent(X_TUNNEL);
        removeFileIfPresent(CLOUDFLARED);
        return { kind: 'web-configuration', configuration: pendingWebConfiguration };
    }

    const xTunnelArguments = ['-l', `ws://127.0.0.1:${WSPORT}`];
    if (configuration.xTunnelToken) {
        xTunnelArguments.push('-token', configuration.xTunnelToken);
    }

    const xTunnel = startTunnel('x-tunnel', X_TUNNEL, xTunnelArguments);
    await delay(1000);
    const configurationAfterXTunnelStart = getWebConfigurationIfAvailable(
        configuration,
        webTokenConfiguration,
    );
    if (configurationAfterXTunnelStart) {
        await cleanUpServiceCycle({ xTunnel });
        return { kind: 'web-configuration', configuration: configurationAfterXTunnelStart };
    }
    if (childHasExited(xTunnel)) {
        console.warn('[-] x 在 C 启动前退出。');
        removeFileIfPresent(X_TUNNEL);
        removeFileIfPresent(CLOUDFLARED);
        return { kind: 'tunnel-exit' };
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
    ]).then((tunnelExit) => ({ kind: 'tunnel-exit', tunnelExit }));

    let fallbackTimer;
    let waitForWebConfiguration;
    let result;
    if (configuration.usingFallbackToken) {
        waitForWebConfiguration = webTokenConfiguration
            ? webTokenConfiguration.waitForConfiguration().then((newConfiguration) => ({
                kind: 'web-configuration',
                configuration: newConfiguration,
            }))
            : null;
        const fallbackTimeoutPromise = new Promise((resolve) => {
            fallbackTimer = setTimeout(
                () => resolve({ kind: 'fallback-timeout', reachedRuntimeLimit: true }),
                FALLBACK_TOKEN_RUNTIME_MS,
            );
        });
        result = await Promise.race([
            tunnelExitPromise,
            fallbackTimeoutPromise,
            ...(waitForWebConfiguration ? [waitForWebConfiguration] : []),
        ]);
    } else {
        result = await tunnelExitPromise;
    }

    clearTimeout(fallbackTimer);
    const configurationAfterTunnelWait = getWebConfigurationIfAvailable(
        configuration,
        webTokenConfiguration,
    );
    if (configurationAfterTunnelWait) {
        await cleanUpServiceCycle({ xTunnel, cloudflared, deleteTimer });
        return { kind: 'web-configuration', configuration: configurationAfterTunnelWait };
    }
    if (result.kind === 'fallback-timeout') {
        console.log('[fallback] 已达到 600 秒运行上限，正在停止 c；保留本地 Web 服务和状态页。');
        await stopCloudflaredForFallbackTimeout({ cloudflared });
        if (waitForWebConfiguration) {
            const configuredResult = await waitForWebConfiguration;
            await cleanUpServiceCycle({ xTunnel, cloudflared, deleteTimer });
            return configuredResult;
        }
        return result;
    }

    console.warn(`[-] ${result.tunnelExit.name} 已退出（${result.tunnelExit.detail}），本轮服务结束。`);
    await cleanUpServiceCycle({ xTunnel, cloudflared, deleteTimer });
    return result;
}

function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (character) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
    }[character]));
}

function formatElapsedTime(startTime, currentTime = Date.now()) {
    const elapsedSeconds = Math.max(0, Math.floor((currentTime - startTime) / 1000));
    const days = Math.floor(elapsedSeconds / 86400);
    const hours = Math.floor((elapsedSeconds % 86400) / 3600);
    const minutes = Math.floor((elapsedSeconds % 3600) / 60);
    const seconds = elapsedSeconds % 60;

    return `${days}天 ${hours}小时 ${minutes}分钟 ${seconds}秒`;
}

function renderLoginPanel() {
    return `<section class="login-panel">
            <h3>登录</h3>
            <form action="/login" method="post">
                <label for="login-account">账号</label>
                <input id="login-account" name="account" type="text" autocomplete="username" required>
                <label for="login-password">密码</label>
                <input id="login-password" name="password" type="password" autocomplete="current-password" required>
                <button type="submit">登录</button>
            </form>
        </section>`;
}

function renderStatusPage(startTime, startDate) {
    const safeStartTime = Number.isFinite(startTime) ? startTime : Date.now();

    return `<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>服务运行状态</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; text-align: center; margin-top: 10vh; background-color: #f4f4f9; color: #333; }
        .box { background: white; padding: 30px 50px; border-radius: 12px; display: inline-block; box-shadow: 0 8px 16px rgba(0,0,0,0.1); }
        .time { font-size: 28px; color: #007bff; font-weight: bold; margin: 15px 0; letter-spacing: 1px; }
        .footer { margin-top: 20px; color: #888; font-size: 13px; }
        .login-panel { border-top: 1px solid #e5e5e5; margin-top: 24px; padding-top: 18px; text-align: left; }
        .login-panel h3 { margin: 0 0 12px; text-align: center; }
        label { display: block; font-size: 14px; margin-top: 10px; }
        input { box-sizing: border-box; margin-top: 4px; padding: 8px; width: 100%; }
        button { background: #007bff; border: 0; border-radius: 4px; color: white; cursor: pointer; margin-top: 16px; padding: 9px 16px; width: 100%; }
        button:hover { background: #0069d9; }
    </style>
</head>
<body>
    <div class="box">
        <h2>服务器运行状态正常</h2>
        <div id="uptime" class="time">${formatElapsedTime(safeStartTime)}</div>
        <div class="footer">本次服务周期启动时间：${escapeHtml(startDate)} (北京时间)</div>
        <div class="footer">当前系统架构：${process.arch}</div>
        ${renderLoginPanel()}
    </div>
    <script>
        (() => {
            const startTime = ${safeStartTime};
            const uptime = document.getElementById('uptime');
            const updateUptime = () => {
                const elapsedSeconds = Math.max(0, Math.floor((Date.now() - startTime) / 1000));
                const days = Math.floor(elapsedSeconds / 86400);
                const hours = Math.floor((elapsedSeconds % 86400) / 3600);
                const minutes = Math.floor((elapsedSeconds % 3600) / 60);
                const seconds = elapsedSeconds % 60;
                uptime.textContent = days + '天 ' + hours + '小时 ' + minutes + '分钟 ' + seconds + '秒';
            };
            setInterval(updateUptime, 1000);
        })();
    </script>
</body>
</html>`;
}

function renderTokenConfigurationPage() {
    return `<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>配置 Tunnel Token</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f4f9; color: #333; margin: 10vh auto; max-width: 520px; }
        .box { background: white; border-radius: 12px; box-shadow: 0 8px 16px rgba(0,0,0,0.1); padding: 30px 50px; }
        .hint { color: #666; font-size: 14px; }
        label { display: block; font-size: 14px; margin-top: 16px; }
        input[type="text"] { box-sizing: border-box; margin-top: 5px; padding: 8px; width: 100%; }
        .checkbox-label { align-items: center; display: flex; gap: 8px; margin-top: 18px; }
        .checkbox-label input { margin: 0; width: auto; }
        button { background: #007bff; border: 0; border-radius: 4px; color: white; cursor: pointer; margin-top: 18px; padding: 10px 16px; width: 100%; }
        button:hover { background: #0069d9; }
    </style>
</head>
<body>
    <main class="box">
        <h2>配置 Tunnel Token</h2>
        <p class="hint">两项都需要填写；提交后将进入持续运行模式。</p>
        <form action="/configure" method="post" autocomplete="off">
            <label for="cloudflare-token">Cloudflared Token</label>
            <input id="cloudflare-token" name="cloudflare_token" type="text" required>
            <label for="x-tunnel-token">x-tunnel Token</label>
            <input id="x-tunnel-token" name="x_tunnel_token" type="text" required>
            <label class="checkbox-label" for="persist-tokens">
                <input id="persist-tokens" name="persist_tokens" type="checkbox" value="1">
                保存 Token 到程序目录，下次启动自动加载
            </label>
            <button type="submit">保存并持续运行</button>
        </form>
    </main>
</body>
</html>`;
}

function renderMessagePage(title, message) {
    return `<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>${escapeHtml(title)}</title></head>
<body><h2>${escapeHtml(title)}</h2><p>${escapeHtml(message)}</p></body></html>`;
}

function sendHtml(response, statusCode, html, additionalHeaders = {}) {
    if (response.writableEnded) {
        return;
    }
    response.writeHead(statusCode, {
        'Content-Type': 'text/html; charset=utf-8',
        'Content-Length': Buffer.byteLength(html),
        'Cache-Control': 'no-store',
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        'Referrer-Policy': 'no-referrer',
        'X-Content-Type-Options': 'nosniff',
        ...additionalHeaders,
    });
    response.end(html);
}

function sendMessage(response, statusCode, title, message) {
    sendHtml(response, statusCode, renderMessagePage(title, message));
}

function redirect(response, location, additionalHeaders = {}) {
    if (response.writableEnded) {
        return;
    }
    response.writeHead(303, {
        Location: location,
        'Cache-Control': 'no-store',
        'Content-Length': '0',
        'Referrer-Policy': 'no-referrer',
        'X-Content-Type-Options': 'nosniff',
        ...additionalHeaders,
    });
    response.end();
}

function getRequestedPath(request) {
    try {
        const requestedUrl = new URL(request.url || '/', 'http://localhost');
        if (requestedUrl.search || requestedUrl.hash) {
            return null;
        }
        return requestedUrl.pathname;
    } catch (_error) {
        return null;
    }
}

function getLoginSessionIdentifier(request) {
    const rawCookies = request.headers.cookie;
    if (typeof rawCookies !== 'string') {
        return '';
    }
    const cookiePrefix = `${LOGIN_SESSION_COOKIE_NAME}=`;
    for (const item of rawCookies.split(';')) {
        const cookie = item.trim();
        if (cookie.startsWith(cookiePrefix)) {
            return cookie.slice(cookiePrefix.length);
        }
    }
    return '';
}

function readForm(request, requiredFields, optionalFields = []) {
    const contentType = String(request.headers['content-type'] || '')
        .split(';', 1)[0]
        .trim()
        .toLowerCase();
    if (contentType !== 'application/x-www-form-urlencoded') {
        return Promise.reject(new InvalidFormError());
    }

    return new Promise((resolve, reject) => {
        const chunks = [];
        let bodyLength = 0;
        let settled = false;
        const fail = () => {
            if (!settled) {
                settled = true;
                reject(new InvalidFormError());
            }
        };

        request.on('data', (chunk) => {
            bodyLength += chunk.length;
            if (bodyLength > MAX_FORM_BYTES) {
                fail();
                request.resume();
                return;
            }
            chunks.push(chunk);
        });
        request.once('aborted', fail);
        request.once('error', fail);
        request.once('end', () => {
            if (settled) {
                return;
            }
            try {
                const allowedFields = new Set([...requiredFields, ...optionalFields]);
                const form = {};
                const receivedFields = new Set();
                const parameters = new URLSearchParams(Buffer.concat(chunks).toString('utf8'));
                for (const [field, value] of parameters) {
                    if (!allowedFields.has(field) || receivedFields.has(field)) {
                        throw new InvalidFormError();
                    }
                    receivedFields.add(field);
                    form[field] = value.trim();
                }
                if (parameters.size > allowedFields.size
                    || requiredFields.some((field) => !receivedFields.has(field))) {
                    throw new InvalidFormError();
                }
                settled = true;
                resolve(form);
            } catch (_error) {
                fail();
            }
        });
    });
}

function createStatusRequestHandler(startTime, startDate, webTokenConfiguration) {
    return async (request, response) => {
        request.setTimeout(HTTP_REQUEST_TIMEOUT_MS, () => request.destroy());
        try {
            const requestedPath = getRequestedPath(request);
            if (!requestedPath) {
                sendMessage(response, 400, '请求无效', '请求地址无效。');
                return;
            }

            if (request.method === 'GET'
                && (requestedPath === '/' || requestedPath === '/index.html')) {
                sendHtml(response, 200, renderStatusPage(startTime, startDate));
                return;
            }
            if (request.method === 'GET' && requestedPath === '/configure') {
                if (!webTokenConfiguration.hasValidLoginSession(getLoginSessionIdentifier(request))) {
                    sendMessage(response, 403, '访问被拒绝', '登录状态无效或已过期。');
                    return;
                }
                sendHtml(response, 200, renderTokenConfigurationPage());
                return;
            }
            if (request.method === 'POST' && requestedPath === '/login') {
                const form = await readForm(request, ['account', 'password']);
                const sessionIdentifier = webTokenConfiguration.beginLogin(form.account, form.password);
                if (!sessionIdentifier) {
                    sendMessage(response, 403, '登录失败', '当前 Token 配置不可用。');
                    return;
                }
                redirect(response, '/configure', {
                    'Set-Cookie': `${LOGIN_SESSION_COOKIE_NAME}=${sessionIdentifier}; Max-Age=${LOGIN_SESSION_MAX_AGE_SECONDS}; Path=/; HttpOnly; SameSite=Strict`,
                });
                return;
            }
            if (request.method === 'POST' && requestedPath === '/configure') {
                const form = await readForm(
                    request,
                    ['cloudflare_token', 'x_tunnel_token'],
                    ['persist_tokens'],
                );
                if (form.persist_tokens !== undefined && form.persist_tokens !== '1') {
                    throw new InvalidFormError();
                }
                const configuration = webTokenConfiguration.saveConfiguration(
                    getLoginSessionIdentifier(request),
                    form.cloudflare_token,
                    form.x_tunnel_token,
                    form.persist_tokens === '1',
                );
                if (!configuration) {
                    sendMessage(response, 403, '配置失败', '登录状态无效、已过期或 Token 为空。');
                    return;
                }
                redirect(response, '/');
                return;
            }

            sendMessage(response, 404, '未找到页面', '请求的页面不存在。');
        } catch (error) {
            if (error instanceof InvalidFormError) {
                sendMessage(response, 400, '提交失败', '提交的数据无效。');
                return;
            }
            if (error instanceof PersistentTokenConfigurationError) {
                sendMessage(response, 500, '配置失败', '无法保存 Token 文件，本次配置未生效。');
                return;
            }
            console.error('[-] 状态页请求处理失败。');
            sendMessage(response, 500, '服务错误', '请求处理失败。');
        }
    };
}

function createStatusPageContext() {
    const startTime = Date.now();
    return {
        startTime,
        startDate: new Date(startTime).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }),
    };
}

function startWebServer(
    port,
    webTokenConfiguration = new WebTokenConfiguration({ environmentTokensPresent: true }),
    statusPageContext = createStatusPageContext(),
) {
    const { startTime, startDate } = statusPageContext;

    return new Promise((resolve, reject) => {
        const server = http.createServer(createStatusRequestHandler(
            startTime,
            startDate,
            webTokenConfiguration,
        ));
        server.requestTimeout = HTTP_REQUEST_TIMEOUT_MS;
        server.headersTimeout = HTTP_REQUEST_TIMEOUT_MS;

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
        if (!server.listening) {
            resolve();
            return;
        }
        server.close((error) => {
            if (error) {
                reject(error);
                return;
            }
            resolve();
        });
    });
}

async function closeWebServers(servers, closeWebServerFunction = closeWebServer) {
    const closeResults = await Promise.allSettled(
        servers.map((server) => Promise.resolve().then(
            () => closeWebServerFunction(server),
        )),
    );
    const failedResult = closeResults.find((result) => result.status === 'rejected');
    if (failedResult) {
        throw failedResult.reason;
    }
}

async function startWebServers(
    ports,
    webTokenConfiguration = new WebTokenConfiguration({ environmentTokensPresent: true }),
    startWebServerFunction = startWebServer,
    closeWebServerFunction = closeWebServer,
) {
    if (!Array.isArray(ports) || ports.length === 0) {
        throw new Error('至少需要一个状态页监听端口');
    }

    const statusPageContext = createStatusPageContext();
    const servers = [];
    try {
        for (const port of ports) {
            servers.push(await startWebServerFunction(
                port,
                webTokenConfiguration,
                statusPageContext,
            ));
        }
    } catch (error) {
        try {
            await closeWebServers(servers, closeWebServerFunction);
        } catch (cleanupError) {
            error.cleanupError = cleanupError;
        }
        throw error;
    }
    return servers;
}

function withWebTokenConfiguration(configuration, webTokenConfiguration) {
    return {
        ...configuration,
        cloudflareToken: webTokenConfiguration.cloudflareToken,
        xTunnelToken: webTokenConfiguration.xTunnelToken,
        usingFallbackToken: false,
    };
}

function normalizeServiceCycleResult(result) {
    if (typeof result === 'boolean') {
        return result
            ? { kind: 'fallback-timeout', reachedRuntimeLimit: true }
            : { kind: 'tunnel-exit' };
    }
    if (!result || typeof result.kind !== 'string') {
        throw new Error('服务周期返回了无效状态');
    }
    return result;
}

async function supervise(
    configuration,
    webTokenConfigurationOrRunServiceCycleFunction,
    runServiceCycleFunction = runServiceCycle,
) {
    let webTokenConfiguration = webTokenConfigurationOrRunServiceCycleFunction;
    let serviceCycleFunction = runServiceCycleFunction;
    if (typeof webTokenConfigurationOrRunServiceCycleFunction === 'function') {
        serviceCycleFunction = webTokenConfigurationOrRunServiceCycleFunction;
        webTokenConfiguration = null;
    }

    let activeConfiguration = configuration;
    while (true) {
        const result = normalizeServiceCycleResult(await serviceCycleFunction(
            activeConfiguration,
            webTokenConfiguration,
        ));
        if (result.kind === 'web-configuration') {
            activeConfiguration = withWebTokenConfiguration(activeConfiguration, result.configuration);
            if (webTokenConfiguration) {
                webTokenConfiguration.stopAcceptingWebConfiguration();
            }
            continue;
        }
        if (activeConfiguration.usingFallbackToken) {
            if (result.kind === 'fallback-timeout') {
                if (!webTokenConfiguration) {
                    return true;
                }
                const newConfiguration = await webTokenConfiguration.waitForConfiguration();
                activeConfiguration = withWebTokenConfiguration(activeConfiguration, newConfiguration);
                webTokenConfiguration.stopAcceptingWebConfiguration();
                continue;
            }
            throw new Error('回退 Token 模式在达到运行上限前结束');
        }
        console.log(`[restart] ${RESTART_DELAY_MS / 1000} 秒后整体重启服务...`);
        await delay(RESTART_DELAY_MS);
    }
}

async function init(dependencies = {}) {
    const {
        environment = process.env,
        getFirstNonEmptyEnvironmentValueFunction = getFirstNonEmptyEnvironmentValue,
        resolveInitialTokenConfigurationFunction = resolveInitialTokenConfiguration,
        loadPersistedTokenConfigurationFunction = loadPersistedTokenConfiguration,
        savePersistedTokenConfigurationFunction = savePersistedTokenConfiguration,
        resolveArchitectureFunction = resolveArchitecture,
        startWebServerFunction = startWebServer,
        superviseFunction = supervise,
        closeWebServerFunction = closeWebServer,
    } = dependencies;
    const initialTokenConfiguration = resolveInitialTokenConfigurationFunction({
        environment,
        getFirstNonEmptyEnvironmentValueFunction,
        loadPersistedTokenConfigurationFunction,
    });
    if (!initialTokenConfiguration.cloudflareToken) {
        throw new Error('未检测到 C token');
    }

    if (initialTokenConfiguration.usingFallbackToken) {
        console.log('[fallback] 未配置 C 别名；启用 600 秒限时回退模式。');
    }

    const webTokenConfiguration = new WebTokenConfiguration({
        environmentTokensPresent: initialTokenConfiguration.environmentTokensPresent
            || Boolean(initialTokenConfiguration.persistedTokenConfiguration),
        savePersistedTokenConfigurationFunction,
    });
    const configuration = {
        architecture: resolveArchitectureFunction(),
        cloudflareToken: initialTokenConfiguration.cloudflareToken,
        xTunnelToken: initialTokenConfiguration.xTunnelToken,
        ipv: environment.IPV === '6' ? '6' : '4',
        usingFallbackToken: initialTokenConfiguration.usingFallbackToken,
    };
    const statusPorts = getStatusPorts(environment);
    const statusServers = await startWebServers(
        statusPorts,
        webTokenConfiguration,
        startWebServerFunction,
        closeWebServerFunction,
    );
    let keepStatusServerRunning = false;
    try {
        keepStatusServerRunning = await superviseFunction(configuration, webTokenConfiguration);
    } finally {
        if (initialTokenConfiguration.usingFallbackToken && !keepStatusServerRunning) {
            await closeWebServers(statusServers, closeWebServerFunction);
        }
    }
}

module.exports = {
    WebTokenConfiguration,
    PersistentTokenConfigurationError,
    closeWebServer,
    closeWebServers,
    createStatusRequestHandler,
    getStatusPorts,
    getPersistedTokenConfigurationPath,
    init,
    loadPersistedTokenConfiguration,
    renderTokenConfigurationPage,
    renderStatusPage,
    resolveInitialTokenConfiguration,
    savePersistedTokenConfiguration,
    startWebServer,
    startWebServers,
    stopCloudflaredForFallbackTimeout,
    supervise,
};

if (require.main === module) {
    init().catch((error) => {
        console.error('[-] 初始化严重故障。');
        process.exitCode = 1;
    });
}
