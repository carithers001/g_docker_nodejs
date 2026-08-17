#!/usr/bin/env python3
"""Python entry point equivalent to the existing Docker shell entry point."""

import asyncio
import os
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


WSPORT = 8081
HEALTH_PORT = 8082
APP_DIRECTORY = Path(os.environ.get("APP_DIR", Path(__file__).resolve().parent))
X_TUNNEL = APP_DIRECTORY / "x-tunnel-linux"
CLOUDFLARED = APP_DIRECTORY / "cloudflared-linux"


def configure_timezone() -> None:
    """Keep the Docker image's default Asia/Shanghai time zone behavior."""
    os.environ.setdefault("TZ", "Asia/Shanghai")
    tzset = getattr(time, "tzset", None)
    if tzset is not None:
        tzset()


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


def heartbeat_request() -> None:
    try:
        with urllib.request.urlopen("https://1.1.1.1", timeout=5):
            pass
    except Exception:
        # Keep the shell script's "curl ... || true" behavior.
        pass


async def heartbeat_loop() -> None:
    while True:
        await asyncio.to_thread(heartbeat_request)
        await asyncio.sleep(300)


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


async def run() -> int:
    configure_timezone()

    ipv = os.environ.get("IPV", "4")
    if ipv not in ("4", "6"):
        print(f"[-] IPV 参数错误 : {ipv}", flush=True)
        return 1

    env_token = os.environ.get("ENV_TOKEN", "")
    if not env_token:
        print("[-] 致命错误: 未检测到环境变量 ENV_TOKEN！请在云平台设置该变量。", flush=True)
        return 1

    try:
        uptime_port = int(os.environ.get("PORT", "8080"))
    except ValueError:
        print(f"[-] PORT 参数错误 : {os.environ.get('PORT', '')}", flush=True)
        return 1

    start_time = time.time()
    start_date = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))

    child_processes = []
    background_tasks = []
    http_server = None

    try:
        background_tasks.append(asyncio.create_task(heartbeat_loop(), name="heartbeat"))

        print(f"[x-tunnel] 启动在本地端口 {WSPORT} ...", flush=True)
        x_tunnel_args = [str(X_TUNNEL), "-l", f"ws://127.0.0.1:{WSPORT}"]
        token = os.environ.get("TOKEN", "")
        if token:
            x_tunnel_args.extend(["-token", token])
        x_tunnel_process = await asyncio.create_subprocess_exec(*x_tunnel_args)
        child_processes.append(x_tunnel_process)

        await asyncio.sleep(1)

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
            env_token,
        )
        child_processes.append(cloudflared_process)

        print("========================================", flush=True)
        print(f"当前健康检查端口: {HEALTH_PORT}", flush=True)
        print(f"当前本地服务端口: {WSPORT}", flush=True)
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
        first_completed = next(iter(completed))

        try:
            result = first_completed.result()
        except Exception as exc:
            print(f"[-] 后台任务异常退出: {exc}", file=sys.stderr, flush=True)
            return 1

        return result if isinstance(result, int) else 0
    except FileNotFoundError as exc:
        print(f"[-] 未找到可执行文件: {exc.filename}", file=sys.stderr, flush=True)
        return 1
    except OSError as exc:
        print(f"[-] 启动失败: {exc}", file=sys.stderr, flush=True)
        return 1
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


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
