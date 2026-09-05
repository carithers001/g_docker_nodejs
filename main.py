#!/usr/bin/env python3
"""Python entry point equivalent to the existing Docker shell entry point."""

import asyncio
import os
import platform
import sys
import tempfile
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


WSPORT = 8081
STATUS_DEFAULT_PORT = 3000
RESTART_DELAY_SECONDS = 60
DOWNLOAD_RETRY_DELAY_SECONDS = 120
APP_DIRECTORY = Path(os.environ.get("APP_DIR", Path(__file__).resolve().parent))
X_TUNNEL = APP_DIRECTORY / "x"
CLOUDFLARED = APP_DIRECTORY / "c"
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_CHUNK_SIZE_BYTES = 1024 * 1024
EXECUTABLE_FILE_MODE = 0o755
X_TUNNEL_DOWNLOAD_BASE_URL = "https://www.baipiao.eu.org/xtunnel/x-tunnel-linux-"
CLOUDFLARED_DOWNLOAD_BASE_URL = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-"
)
ARCHITECTURE_SUFFIXES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "i386": "386",
    "i686": "386",
    "x86": "386",
}
CLOUDFLARE_TOKEN_ENVIRONMENT_NAMES = ("envToken", "ENV_TOKEN", "token", "TOKEN")
FALLBACK_CLOUDFLARE_TOKEN_ENVIRONMENT_NAME = "CLOUDFLARE_FALLBACK_TOKEN"
FALLBACK_TOKEN_RUNTIME_SECONDS = 600


class RuntimeBinaryDownloadError(RuntimeError):
    """Raised when a required runtime binary cannot be prepared safely."""


def configure_timezone() -> None:
    """Keep the Docker image's default Asia/Shanghai time zone behavior."""
    os.environ.setdefault("TZ", "Asia/Shanghai")
    tzset = getattr(time, "tzset", None)
    if tzset is not None:
        tzset()


def get_architecture_suffix() -> str:
    """Return the original Docker image asset suffix for the current Linux CPU."""
    operating_system = platform.system()
    if operating_system != "Linux":
        raise RuntimeBinaryDownloadError(
            f"不支持的操作系统: {operating_system}；仅支持 Linux 服务器。"
        )

    machine = platform.machine().lower()
    suffix = ARCHITECTURE_SUFFIXES.get(machine)
    if suffix is None:
        raise RuntimeBinaryDownloadError(f"不支持的 CPU 架构: {machine}")
    return suffix


def get_cloudflare_token() -> str:
    """Return the first configured Cloudflare token using the JS branch's aliases."""
    for environment_name in CLOUDFLARE_TOKEN_ENVIRONMENT_NAMES:
        token = os.environ.get(environment_name, "")
        if token:
            return token
    return ""


def get_cloudflare_token_with_mode() -> tuple[str, bool]:
    """Return an external token first, otherwise a time-limited fallback token."""
    token = get_cloudflare_token()
    if token:
        return token, False

    fallback_token = os.environ.get(FALLBACK_CLOUDFLARE_TOKEN_ENVIRONMENT_NAME, "")
    if fallback_token:
        return fallback_token, True
    return "", False


def get_ipv() -> str:
    """Keep the JS branch behavior: only an explicit 6 selects IPv6."""
    return "6" if os.environ.get("IPV") == "6" else "4"


def get_status_port() -> int:
    """Use the JS branch's SERVER_PORT, PORT, then 3000 precedence."""
    configured_port = (
        os.environ.get("SERVER_PORT")
        or os.environ.get("PORT")
        or str(STATUS_DEFAULT_PORT)
    )
    return int(configured_port)


def download_binary(url: str, destination: Path) -> None:
    """Download one executable atomically and make it executable for all users."""
    temporary_path = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".download",
            dir=destination.parent,
        )
        temporary_path = Path(temporary_name)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "bms-runtime-downloader"},
        )

        with os.fdopen(file_descriptor, "wb") as output_file:
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                while chunk := response.read(DOWNLOAD_CHUNK_SIZE_BYTES):
                    output_file.write(chunk)

        if temporary_path.stat().st_size == 0:
            raise RuntimeBinaryDownloadError(f"下载 {destination.name} 失败: 文件为空")

        os.chmod(temporary_path, EXECUTABLE_FILE_MODE)
        os.replace(temporary_path, destination)
        temporary_path = None
    except RuntimeBinaryDownloadError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeBinaryDownloadError(f"下载 {destination.name} 失败: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def download_runtime_binaries(architecture_suffix: str) -> None:
    """Reproduce the Dockerfile's architecture-specific runtime binary downloads."""
    print(f"[download] 检测到 Linux 架构: {platform.machine()}", flush=True)
    try:
        download_binary(f"{X_TUNNEL_DOWNLOAD_BASE_URL}{architecture_suffix}", X_TUNNEL)
        download_binary(f"{CLOUDFLARED_DOWNLOAD_BASE_URL}{architecture_suffix}", CLOUDFLARED)
    except RuntimeBinaryDownloadError:
        X_TUNNEL.unlink(missing_ok=True)
        CLOUDFLARED.unlink(missing_ok=True)
        raise


def render_status_page(start_time: float, start_date: str) -> bytes:
    now = time.time()
    elapsed = int(now - start_time)
    days, remainder = divmod(elapsed, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="5"> <!-- 每5秒自动刷新网页 -->
    <title>服务运行状态</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; text-align: center; margin-top: 10vh; background-color: #f4f4f9; color: #333; }}
        .box {{ background: white; padding: 30px 50px; border-radius: 12px; display: inline-block; box-shadow: 0 8px 16px rgba(0,0,0,0.1); }}
        .time {{ font-size: 28px; color: #007bff; font-weight: bold; margin: 15px 0; letter-spacing: 1px;}}
        .footer {{ margin-top: 20px; color: #888; font-size: 13px; }}
    </style>
</head>
<body>
    <div class="box">
        <h2>🚀 节点运行状态正常</h2>
        <div class="time">{days}天 {hours}小时 {minutes}分钟 {seconds}秒</div>
        <div class="footer">本次容器启动时间：{start_date} (北京时间)</div>
    </div>
</body>
</html>
"""
    return content.encode("utf-8")


async def remove_runtime_binaries() -> None:
    await asyncio.sleep(3)

    # Deliberately retained from entrypoint.sh to preserve the existing behavior.
    X_TUNNEL.unlink(missing_ok=True)
    CLOUDFLARED.unlink(missing_ok=True)

    # Keep this monitored task alive, as the original background loop did.
    await asyncio.Event().wait()


class StatusRequestHandler(BaseHTTPRequestHandler):
    def _send_status_page(self, include_body: bool) -> None:
        requested_path = urlsplit(self.path).path
        if requested_path not in ("/", "/index.html"):
            self.send_error(404)
            return

        content = render_status_page(self.server.start_time, self.server.start_date)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if include_body:
            self.wfile.write(content)

    def do_GET(self) -> None:
        self._send_status_page(include_body=True)

    def do_HEAD(self) -> None:
        self._send_status_page(include_body=False)


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def run_service_cycle(
    ipv: str,
    cloudflare_token: str,
    uptime_port: int,
    architecture_suffix: str,
    using_fallback_token: bool = False,
) -> tuple[int, bool]:
    """Run one complete tunnel service cycle until a monitored task exits."""
    start_time = time.time()
    start_date = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))

    child_processes = []
    background_tasks = []
    fallback_timeout_task = None
    http_server = None

    try:
        await asyncio.to_thread(download_runtime_binaries, architecture_suffix)

        print(f"[x-tunnel] 启动在本地端口 {WSPORT} ...", flush=True)
        x_tunnel_args = [str(X_TUNNEL), "-l", f"ws://127.0.0.1:{WSPORT}"]
        token = os.environ.get("TOKEN", "")
        if token:
            x_tunnel_args.extend(["-token", token])
        x_tunnel_process = await asyncio.create_subprocess_exec(*x_tunnel_args)
        child_processes.append(x_tunnel_process)

        await asyncio.sleep(1)
        if x_tunnel_process.returncode is not None:
            print("[-] x-tunnel 在 Cloudflare Tunnel 启动前退出。", flush=True)
            return x_tunnel_process.returncode or 1, False

        cloudflared_process = await asyncio.create_subprocess_exec(
            str(CLOUDFLARED),
            "--edge-ip-version",
            ipv,
            "--protocol",
            "http2",
            "--no-autoupdate",
            "tunnel",
            "run",
            "--token",
            cloudflare_token,
        )
        child_processes.append(cloudflared_process)

        if using_fallback_token:
            fallback_timeout_task = asyncio.create_task(
                asyncio.sleep(FALLBACK_TOKEN_RUNTIME_SECONDS),
                name="fallback-token-timeout",
            )
            background_tasks.append(fallback_timeout_task)

        print("========================================", flush=True)
        print(f"当前本地服务端口: {WSPORT}", flush=True)
        print(f"当前状态页端口: {uptime_port}", flush=True)
        print("========================================", flush=True)

        background_tasks.append(
            asyncio.create_task(remove_runtime_binaries(), name="remove-runtime-binaries")
        )

        http_server = ThreadingHTTPServer(("", uptime_port), StatusRequestHandler)
        http_server.start_time = start_time
        http_server.start_date = start_date
        background_tasks.append(
            asyncio.create_task(asyncio.to_thread(http_server.serve_forever), name="http-server")
        )

        process_waiters = [
            asyncio.create_task(process.wait(), name=f"process-{index}")
            for index, process in enumerate(child_processes)
        ]
        monitored = [*background_tasks, *process_waiters]
        completed, _ = await asyncio.wait(monitored, return_when=asyncio.FIRST_COMPLETED)

        if fallback_timeout_task is not None and fallback_timeout_task in completed:
            print(
                "[fallback] 已达到 600 秒运行上限，正在停止 cloudflared 和 x-tunnel。",
                flush=True,
            )
            return 0, True

        first_completed = next(iter(completed))

        try:
            result = first_completed.result()
        except Exception as exc:
            print(f"[-] 后台任务异常退出: {exc}", file=sys.stderr, flush=True)
            return 1, False

        return result if isinstance(result, int) else 0, False
    finally:
        if http_server is not None:
            http_server.shutdown()
            http_server.server_close()

        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)

        await asyncio.gather(
            *(terminate_process(process) for process in child_processes),
            return_exceptions=True,
        )


async def supervise() -> int:
    """Restart complete service cycles after tunnel exits or recoverable failures."""
    configure_timezone()

    try:
        architecture_suffix = get_architecture_suffix()
    except RuntimeBinaryDownloadError as exc:
        print(f"[-] {exc}", file=sys.stderr, flush=True)
        return 1

    cloudflare_token, using_fallback_token = get_cloudflare_token_with_mode()
    if not cloudflare_token:
        print("[-] 致命错误: 未检测到 Cloudflare Tunnel token！", flush=True)
        return 1

    if using_fallback_token:
        print("[fallback] 未配置 Cloudflare Token 别名；启用 600 秒限时回退模式。", flush=True)

    try:
        uptime_port = get_status_port()
    except ValueError:
        configured_port = os.environ.get("SERVER_PORT") or os.environ.get("PORT") or ""
        print(f"[-] 状态页端口参数错误: {configured_port}", flush=True)
        return 1

    ipv = get_ipv()
    if using_fallback_token:
        try:
            exit_code, reached_runtime_limit = await run_service_cycle(
                ipv,
                cloudflare_token,
                uptime_port,
                architecture_suffix,
                using_fallback_token=True,
            )
        except RuntimeBinaryDownloadError as exc:
            print(f"[-] {exc}", file=sys.stderr, flush=True)
            return 1
        except FileNotFoundError as exc:
            print(f"[-] 未找到可执行文件: {exc.filename}", file=sys.stderr, flush=True)
            return 1
        except OSError as exc:
            print(f"[-] 启动失败: {exc}", file=sys.stderr, flush=True)
            return 1

        if reached_runtime_limit:
            return 0

        print("[-] 回退 Token 模式在达到运行上限前结束。", file=sys.stderr, flush=True)
        return exit_code if exit_code != 0 else 1

    while True:
        try:
            await run_service_cycle(
                ipv,
                cloudflare_token,
                uptime_port,
                architecture_suffix,
            )
        except RuntimeBinaryDownloadError as exc:
            print(f"[-] {exc}", file=sys.stderr, flush=True)
            print(
                f"[retry] {DOWNLOAD_RETRY_DELAY_SECONDS} 秒后重试下载...",
                flush=True,
            )
            await asyncio.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)
            continue
        except FileNotFoundError as exc:
            print(f"[-] 未找到可执行文件: {exc.filename}", file=sys.stderr, flush=True)
        except OSError as exc:
            print(f"[-] 启动失败: {exc}", file=sys.stderr, flush=True)

        print(f"[restart] {RESTART_DELAY_SECONDS} 秒后整体重启服务...", flush=True)
        await asyncio.sleep(RESTART_DELAY_SECONDS)


def main() -> int:
    try:
        return asyncio.run(supervise())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
