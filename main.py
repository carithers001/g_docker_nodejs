#!/usr/bin/env python3
"""Python entry point equivalent to the existing Docker shell entry point."""
# nohup sh -c 'curl -fsSL https://raw.githubusercontent.com/carithers001/g_docker_nodejs/py/main.py | python3 -' >/dev/null 2>&1 &

import asyncio
import json
import os
import platform
import secrets
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit


WSPORT = 8081
STATUS_DEFAULT_PORT = 3000
RESTART_DELAY_SECONDS = 60
DOWNLOAD_RETRY_DELAY_SECONDS = 120
TUNNEL_STARTUP_CHECK_DELAY_SECONDS = 1
APP_DIRECTORY = Path(os.environ.get("APP_DIR", Path(__file__).resolve().parent))
X_TUNNEL = APP_DIRECTORY / "xxx"
CLOUDFLARED = APP_DIRECTORY / "ccc"
DOWNLOAD_TIMEOUT_SECONDS = 120
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
DEFAULT_E = "e"
DEFAULT_Y = "y"
DEFAULT_TOKEN = "JhIjoiZDZkMzEzZjA2MzI1OGJjODllNzc4YmVlMDQ5YTZmOTEiLCJ0IjoiYmYwYzQyZWEtNGIyYy00ZTFhLWEyNDgtZWRiODgyNjM1YjA4IiwicyI6Ik5HTXpPRFE1TnpndE5HTmtPUzAwT1dObUxXSmpNV1F0T0RabU5EUXhNREkzTTJVMSJ9"
FALLBACK_TOKEN_RUNTIME_SECONDS = 600
LOGIN_SESSION_COOKIE_NAME = "token_configuration_session"
LOGIN_SESSION_MAX_AGE_SECONDS = 600
MAX_CONFIGURATION_FORM_BYTES = 8 * 1024
HTTP_REQUEST_TIMEOUT_SECONDS = 15
PERSISTED_TOKEN_CONFIGURATION_FILE_NAME = ".tunnel-tokens.json"
PERSISTED_TOKEN_CONFIGURATION_VERSION = 1
PERSISTED_TOKEN_CONFIGURATION_FILE_MODE = 0o600
MAX_PERSISTED_TOKEN_CONFIGURATION_BYTES = 64 * 1024


class RuntimeBinaryDownloadError(RuntimeError):
    """Raised when a required runtime binary cannot be prepared safely."""


class StatusServerStoppedError(RuntimeError):
    """Raised when the independently owned status server stops unexpectedly."""


class PersistentTokenConfigurationError(RuntimeError):
    """Raised when saved token configuration cannot be used safely."""


@dataclass(frozen=True)
class TunnelConfiguration:
    """The two runtime-only tokens accepted from the status page."""

    cloudflare_token: str
    x_tunnel_token: str


@dataclass(frozen=True)
class StartupTunnelConfiguration:
    """The selected startup credentials and whether web setup must stay closed."""

    configuration: TunnelConfiguration
    using_fallback_token: bool
    environment_tokens_present: bool
    loaded_persisted_configuration: bool


class WebTokenConfiguration:
    """Coordinate one in-memory web configuration safely across HTTP threads."""

    def __init__(self, environment_tokens_present: bool) -> None:
        self._accepting_web_configuration = not environment_tokens_present
        self._configuration: Optional[TunnelConfiguration] = None
        self._login_session_identifier: Optional[str] = None
        self._login_session_expires_at = 0.0
        self._lock = threading.Lock()

    def _clear_expired_login_session_locked(self) -> None:
        if (
            self._login_session_identifier is not None
            and time.monotonic() >= self._login_session_expires_at
        ):
            self._login_session_identifier = None
            self._login_session_expires_at = 0.0

    def begin_login(self, username: str, password: str) -> Optional[str]:
        """Create a short-lived login session only while fallback mode is open."""

        if not username or not password:
            return None

        with self._lock:
            self._clear_expired_login_session_locked()
            if (
                not self._accepting_web_configuration
                or self._configuration is not None
            ):
                return None

            session_identifier = secrets.token_urlsafe(32)
            self._login_session_identifier = session_identifier
            self._login_session_expires_at = (
                time.monotonic() + LOGIN_SESSION_MAX_AGE_SECONDS
            )
            return session_identifier

    def has_valid_login_session(self, session_identifier: str) -> bool:
        """Return whether a request may view or submit the configuration form."""

        if not session_identifier:
            return False

        with self._lock:
            self._clear_expired_login_session_locked()
            return (
                self._accepting_web_configuration
                and self._configuration is None
                and self._login_session_identifier is not None
                and secrets.compare_digest(
                    self._login_session_identifier, session_identifier
                )
            )

    def save_configuration(
        self,
        session_identifier: str,
        cloudflare_token: str,
        x_tunnel_token: str,
        persist_configuration: Optional[Callable[[TunnelConfiguration], None]] = (
            None
        ),
    ) -> Optional[TunnelConfiguration]:
        """Atomically accept the first complete web configuration.

        When requested, persist the configuration before making it visible to the
        service loop.  A persistence failure deliberately leaves the valid login
        session in place so the operator can retry without changing runtime state.
        """

        if not session_identifier or not cloudflare_token or not x_tunnel_token:
            return None

        with self._lock:
            self._clear_expired_login_session_locked()
            if (
                not self._accepting_web_configuration
                or self._configuration is not None
                or self._login_session_identifier is None
                or not secrets.compare_digest(
                    self._login_session_identifier, session_identifier
                )
            ):
                return None

            configuration = TunnelConfiguration(
                cloudflare_token=cloudflare_token,
                x_tunnel_token=x_tunnel_token,
            )
            if persist_configuration is not None:
                persist_configuration(configuration)

            self._configuration = configuration
            self._login_session_identifier = None
            self._login_session_expires_at = 0.0
            return configuration

    def discard_configuration(self, configuration: TunnelConfiguration) -> None:
        """Roll back a configuration if it cannot be handed to asyncio safely."""

        with self._lock:
            if self._configuration is configuration:
                self._configuration = None

    def claim_configuration_or_close(self) -> Optional[TunnelConfiguration]:
        """Atomically take a saved configuration or reject later web submissions."""

        with self._lock:
            self._accepting_web_configuration = False
            self._login_session_identifier = None
            self._login_session_expires_at = 0.0
            return self._configuration

    def stop_accepting_web_configuration(self) -> None:
        """Invalidate login sessions before the status server is shut down."""

        with self._lock:
            self._accepting_web_configuration = False
            self._login_session_identifier = None
            self._login_session_expires_at = 0.0

    def get_configuration(self) -> Optional[TunnelConfiguration]:
        """Return the saved configuration without exposing it through HTTP."""

        with self._lock:
            return self._configuration


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


def get_x_tunnel_token() -> str:
    """Return the x-tunnel token using the existing environment contract."""

    return os.environ.get("TOKEN", "")


def get_persisted_token_configuration_path() -> Path:
    """Return the local, runtime-only path used for opted-in token persistence."""

    return APP_DIRECTORY / PERSISTED_TOKEN_CONFIGURATION_FILE_NAME


def _raise_persisted_token_configuration_error() -> None:
    """Raise a generic error without ever including a credential in its text."""

    raise PersistentTokenConfigurationError("本地 Token 配置无效或无法安全读取。")


def load_persisted_token_configuration() -> Optional[TunnelConfiguration]:
    """Load one complete, owner-only local configuration, if it exists.

    A file that is present but malformed, unsafe, or unreadable is deliberately
    rejected rather than treated as a missing file.  This prevents an existing
    persisted configuration from silently falling back to unauthenticated web
    setup.
    """

    configuration_path = get_persisted_token_configuration_path()
    try:
        if configuration_path.is_symlink():
            _raise_persisted_token_configuration_error()
    except PersistentTokenConfigurationError:
        raise
    except OSError as exc:
        raise PersistentTokenConfigurationError(
            "本地 Token 配置无效或无法安全读取。"
        ) from exc

    file_descriptor: Optional[int] = None
    try:
        open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(configuration_path, open_flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PersistentTokenConfigurationError(
            "本地 Token 配置无效或无法安全读取。"
        ) from exc

    try:
        file_info = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_info.st_mode):
            _raise_persisted_token_configuration_error()
        if (
            os.name == "posix"
            and stat.S_IMODE(file_info.st_mode) & 0o077
        ):
            _raise_persisted_token_configuration_error()
        if file_info.st_size > MAX_PERSISTED_TOKEN_CONFIGURATION_BYTES:
            _raise_persisted_token_configuration_error()

        with os.fdopen(file_descriptor, "rb") as input_file:
            file_descriptor = None
            serialized_configuration = input_file.read(
                MAX_PERSISTED_TOKEN_CONFIGURATION_BYTES + 1
            )
    except PersistentTokenConfigurationError:
        raise
    except OSError as exc:
        raise PersistentTokenConfigurationError(
            "本地 Token 配置无效或无法安全读取。"
        ) from exc
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass

    if len(serialized_configuration) > MAX_PERSISTED_TOKEN_CONFIGURATION_BYTES:
        _raise_persisted_token_configuration_error()

    try:
        configuration_data = json.loads(serialized_configuration.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PersistentTokenConfigurationError(
            "本地 Token 配置无效或无法安全读取。"
        ) from exc

    expected_fields = {
        "version",
        "cloudflare_token",
        "x_tunnel_token",
    }
    if (
        not isinstance(configuration_data, dict)
        or set(configuration_data) != expected_fields
        or type(configuration_data["version"]) is not int
        or configuration_data["version"] != PERSISTED_TOKEN_CONFIGURATION_VERSION
        or not isinstance(configuration_data["cloudflare_token"], str)
        or not isinstance(configuration_data["x_tunnel_token"], str)
    ):
        _raise_persisted_token_configuration_error()

    cloudflare_token = configuration_data["cloudflare_token"].strip()
    x_tunnel_token = configuration_data["x_tunnel_token"].strip()
    if not cloudflare_token or not x_tunnel_token:
        _raise_persisted_token_configuration_error()

    return TunnelConfiguration(
        cloudflare_token=cloudflare_token,
        x_tunnel_token=x_tunnel_token,
    )


def save_persisted_token_configuration(configuration: TunnelConfiguration) -> None:
    """Atomically save an opted-in token configuration with owner-only access."""

    if (
        not isinstance(configuration.cloudflare_token, str)
        or not isinstance(configuration.x_tunnel_token, str)
        or not configuration.cloudflare_token
        or not configuration.x_tunnel_token
        or configuration.cloudflare_token != configuration.cloudflare_token.strip()
        or configuration.x_tunnel_token != configuration.x_tunnel_token.strip()
    ):
        raise PersistentTokenConfigurationError("无法保存本地 Token 配置。")

    configuration_data = {
        "version": PERSISTED_TOKEN_CONFIGURATION_VERSION,
        "cloudflare_token": configuration.cloudflare_token,
        "x_tunnel_token": configuration.x_tunnel_token,
    }
    try:
        serialized_configuration = (
            json.dumps(
                configuration_data,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise PersistentTokenConfigurationError("无法保存本地 Token 配置。") from exc
    if len(serialized_configuration) > MAX_PERSISTED_TOKEN_CONFIGURATION_BYTES:
        raise PersistentTokenConfigurationError("无法保存本地 Token 配置。")

    configuration_path = get_persisted_token_configuration_path()
    temporary_path: Optional[Path] = None
    file_descriptor: Optional[int] = None
    try:
        configuration_path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".tunnel-tokens.",
            suffix=".tmp",
            dir=configuration_path.parent,
        )
        temporary_path = Path(temporary_name)
        os.chmod(temporary_path, PERSISTED_TOKEN_CONFIGURATION_FILE_MODE)

        with os.fdopen(file_descriptor, "wb") as output_file:
            file_descriptor = None
            output_file.write(serialized_configuration)
            output_file.flush()
            os.fsync(output_file.fileno())

        os.replace(temporary_path, configuration_path)
        temporary_path = None
    except PersistentTokenConfigurationError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise PersistentTokenConfigurationError("无法保存本地 Token 配置。") from exc
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def get_cloudflare_token_with_mode() -> tuple[str, bool]:
    """Return an external token first, otherwise a time-limited fallback token."""
    token = get_cloudflare_token()
    if token:
        return token, False

    fallback_token = DEFAULT_E + DEFAULT_Y + DEFAULT_TOKEN
    if fallback_token:
        return fallback_token, True
    return "", False


def resolve_startup_tunnel_configuration() -> StartupTunnelConfiguration:
    """Choose exactly one token source using environment-first precedence.

    A complete local configuration is considered only if none of the existing
    environment token inputs is set.  This prevents accidentally combining
    credentials from two sources during startup.
    """

    environment_cloudflare_token = get_cloudflare_token()
    x_tunnel_token = get_x_tunnel_token()
    environment_tokens_present = bool(
        environment_cloudflare_token or x_tunnel_token
    )

    if environment_tokens_present:
        cloudflare_token, using_fallback_token = get_cloudflare_token_with_mode()
        return StartupTunnelConfiguration(
            configuration=TunnelConfiguration(
                cloudflare_token=cloudflare_token,
                x_tunnel_token=x_tunnel_token,
            ),
            using_fallback_token=using_fallback_token,
            environment_tokens_present=True,
            loaded_persisted_configuration=False,
        )

    persisted_configuration = load_persisted_token_configuration()
    if persisted_configuration is not None:
        return StartupTunnelConfiguration(
            configuration=persisted_configuration,
            using_fallback_token=False,
            environment_tokens_present=False,
            loaded_persisted_configuration=True,
        )

    cloudflare_token, using_fallback_token = get_cloudflare_token_with_mode()
    return StartupTunnelConfiguration(
        configuration=TunnelConfiguration(
            cloudflare_token=cloudflare_token,
            x_tunnel_token=x_tunnel_token,
        ),
        using_fallback_token=using_fallback_token,
        environment_tokens_present=False,
        loaded_persisted_configuration=False,
    )


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


async def download_runtime_binaries(architecture_suffix: str) -> None:
    """Download both runtime binaries, retrying failures without closing the status page."""

    while True:
        try:
            print(f"[download] 检测到 Linux 架构: {platform.machine()}", flush=True)
            await asyncio.to_thread(
                download_binary,
                f"{X_TUNNEL_DOWNLOAD_BASE_URL}{architecture_suffix}",
                X_TUNNEL,
            )
            await asyncio.to_thread(
                download_binary,
                f"{CLOUDFLARED_DOWNLOAD_BASE_URL}{architecture_suffix}",
                CLOUDFLARED,
            )
            return
        except RuntimeBinaryDownloadError as exc:
            X_TUNNEL.unlink(missing_ok=True)
            CLOUDFLARED.unlink(missing_ok=True)
            print(f"[-] 下载运行时二进制失败: {exc}", file=sys.stderr, flush=True)
            print(
                f"[-] {DOWNLOAD_RETRY_DELAY_SECONDS} 秒后重试...",
                flush=True,
            )
            await asyncio.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)


def render_login_panel() -> str:
    """Return the static login panel shown below the unchanged status summary."""

    return """
    <section class="config-box">
        <h3>登录</h3>
        <form method="post" action="/login" autocomplete="off">
            <label for="username">账号</label>
            <input id="username" name="username" type="text" required autocomplete="off">
            <label for="password">密码</label>
            <input id="password" name="password" type="password" required autocomplete="off">
            <button type="submit">登录</button>
        </form>
    </section>
"""


def render_token_configuration_page() -> bytes:
    """Return the one-time token configuration form without rendering its values."""

    content = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Token 配置</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; text-align: center; margin-top: 10vh; background-color: #f4f4f9; color: #333; }
        .config-box { background: white; padding: 30px 50px; border-radius: 12px; display: inline-block; min-width: 280px; box-shadow: 0 8px 16px rgba(0,0,0,0.1); text-align: left; }
        label { display: block; margin-top: 14px; font-size: 14px; }
        input { box-sizing: border-box; width: 100%; margin-top: 6px; padding: 9px; border: 1px solid #c7c7c7; border-radius: 5px; }
        .checkbox-label { display: flex; align-items: center; gap: 8px; margin-top: 18px; line-height: 1.4; }
        .checkbox-label input { width: auto; margin: 0; padding: 0; }
        button { width: 100%; margin-top: 20px; padding: 10px; color: white; background: #007bff; border: 0; border-radius: 5px; cursor: pointer; }
        .hint { color: #666; font-size: 13px; line-height: 1.5; }
    </style>
</head>
<body>
    <section class="config-box">
        <h2>配置 Tunnel Token</h2>
        <p class="hint">两项都需要填写；不勾选保存时，仅在当前进程中使用。</p>
        <form method="post" action="/configure" autocomplete="off">
            <label for="cloudflare-token">Cloudflared Token</label>
            <input id="cloudflare-token" name="cloudflare_token" type="text" required autocomplete="off">
            <label for="x-tunnel-token">x-tunnel Token</label>
            <input id="x-tunnel-token" name="x_tunnel_token" type="text" required autocomplete="off">
            <label class="checkbox-label" for="persist-tokens">
                <input id="persist-tokens" name="persist_tokens" type="checkbox" value="1">
                保存 Token 到程序目录，下次启动自动加载
            </label>
            <button type="submit">保存并持续运行</button>
        </form>
    </section>
</body>
</html>
"""
    return content.encode("utf-8")


def render_message_page(title: str, message: str) -> bytes:
    """Render a static error page which never includes supplied credentials or tokens."""

    content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{title}</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; text-align: center; margin-top: 10vh; background-color: #f4f4f9; color: #333; }}
        .message {{ background: white; padding: 30px 50px; border-radius: 12px; display: inline-block; box-shadow: 0 8px 16px rgba(0,0,0,0.1); }}
        a {{ color: #007bff; }}
    </style>
</head>
<body>
    <section class="message">
        <h2>{title}</h2>
        <p>{message}</p>
        <a href="/">返回状态页</a>
    </section>
</body>
</html>
"""
    return content.encode("utf-8")


def render_status_page(
    start_time: float,
    start_date: str,
    login_panel: str = "",
) -> bytes:
    now = time.time()
    elapsed = int(now - start_time)
    days, remainder = divmod(elapsed, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>服务运行状态</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; text-align: center; margin-top: 10vh; background-color: #f4f4f9; color: #333; }}
        .box {{ background: white; padding: 30px 50px; border-radius: 12px; display: inline-block; box-shadow: 0 8px 16px rgba(0,0,0,0.1); }}
        .time {{ font-size: 28px; color: #007bff; font-weight: bold; margin: 15px 0; letter-spacing: 1px;}}
        .footer {{ margin-top: 20px; color: #888; font-size: 13px; }}
        .config-box {{ background: white; padding: 22px 30px; border-radius: 12px; display: block; width: min(360px, calc(100% - 40px)); margin: 22px auto; box-shadow: 0 8px 16px rgba(0,0,0,0.1); text-align: left; }}
        .config-box h3 {{ margin-top: 0; text-align: center; }}
        .config-box label {{ display: block; margin-top: 12px; font-size: 14px; }}
        .config-box input {{ box-sizing: border-box; width: 100%; margin-top: 6px; padding: 9px; border: 1px solid #c7c7c7; border-radius: 5px; }}
        .config-box button {{ width: 100%; margin-top: 18px; padding: 10px; color: white; background: #007bff; border: 0; border-radius: 5px; cursor: pointer; }}
    </style>
</head>
<body>
    <div class="box">
        <h2>服务器运行状态正常</h2>
        <div class="time" id="server-uptime">{days}天 {hours}小时 {minutes}分钟 {seconds}秒</div>
        <div class="footer">本次服务周期启动时间：{start_date} (北京时间)</div>
    </div>
{login_panel}
    <script>
        const initialElapsedSeconds = {elapsed};
        const statusPageLoadedAt = Date.now();
        const statusUptime = document.getElementById("server-uptime");
        function updateStatusUptime() {{
            let remainingSeconds = initialElapsedSeconds + Math.max(
                0,
                Math.floor((Date.now() - statusPageLoadedAt) / 1000)
            );
            const days = Math.floor(remainingSeconds / 86400);
            remainingSeconds %= 86400;
            const hours = Math.floor(remainingSeconds / 3600);
            remainingSeconds %= 3600;
            const minutes = Math.floor(remainingSeconds / 60);
            const seconds = remainingSeconds % 60;
            statusUptime.textContent = days + "天 " + hours + "小时 " + minutes + "分钟 " + seconds + "秒";
        }}
        window.setInterval(updateStatusUptime, 1000);
    </script>
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
    """Serve the status page and the one-time in-memory token configuration flow."""

    server_version = "StatusServer"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(HTTP_REQUEST_TIMEOUT_SECONDS)

    def log_message(self, format: str, *args: object) -> None:
        """Avoid recording request URLs or headers beside configuration secrets."""

    def _requested_path(self) -> Optional[str]:
        try:
            request_url = urlsplit(self.path)
        except ValueError:
            return None
        if request_url.query or request_url.fragment:
            return None
        return request_url.path

    def _send_html(
        self,
        status_code: int,
        content: bytes,
        include_body: bool = True,
        set_cookie: Optional[str] = None,
        location: Optional[str] = None,
    ) -> None:
        self.send_response(status_code)
        if location is not None:
            self.send_header("Location", location)
        if set_cookie is not None:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        if include_body:
            self.wfile.write(content)

    def _send_message(self, status_code: int, title: str, message: str) -> None:
        self._send_html(status_code, render_message_page(title, message))

    def _get_login_session_identifier(self) -> str:
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""

        session_cookie = cookies.get(LOGIN_SESSION_COOKIE_NAME)
        return session_cookie.value if session_cookie is not None else ""

    def _read_form(
        self,
        required_fields: tuple[str, ...],
        optional_fields: tuple[str, ...] = (),
    ) -> Optional[dict[str, str]]:
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != (
            "application/x-www-form-urlencoded"
        ):
            return None

        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        if not 0 < content_length <= MAX_CONFIGURATION_FORM_BYTES:
            return None

        try:
            body = self.rfile.read(content_length)
        except OSError:
            return None
        if len(body) != content_length:
            return None

        try:
            decoded_body = body.decode("utf-8", errors="strict")
            parsed_fields = parse_qs(
                decoded_body,
                keep_blank_values=True,
                strict_parsing=True,
                encoding="utf-8",
                errors="strict",
                max_num_fields=len(required_fields) + len(optional_fields),
            )
        except (UnicodeDecodeError, ValueError):
            return None

        allowed_fields = set(required_fields) | set(optional_fields)
        if (
            not set(required_fields).issubset(parsed_fields)
            or not set(parsed_fields).issubset(allowed_fields)
        ):
            return None
        if any(len(values) != 1 for values in parsed_fields.values()):
            return None
        return {field: values[0].strip() for field, values in parsed_fields.items()}

    def _send_status_page(self, include_body: bool) -> None:
        content = render_status_page(
            self.server.start_time,
            self.server.start_date,
            render_login_panel(),
        )
        self._send_html(200, content, include_body=include_body)

    def _send_login_redirect(self, session_identifier: str) -> None:
        session_cookie = (
            f"{LOGIN_SESSION_COOKIE_NAME}={session_identifier}; "
            f"Max-Age={LOGIN_SESSION_MAX_AGE_SECONDS}; Path=/; HttpOnly; SameSite=Strict"
        )
        self._send_html(
            303,
            b"",
            set_cookie=session_cookie,
            location="/configure",
        )

    def _send_configuration_redirect(self) -> None:
        expired_cookie = (
            f"{LOGIN_SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"
        )
        self._send_html(303, b"", set_cookie=expired_cookie, location="/")

    def do_GET(self) -> None:
        requested_path = self._requested_path()
        if requested_path in ("/", "/index.html"):
            self._send_status_page(include_body=True)
            return
        if requested_path == "/configure":
            configuration = self.server.web_token_configuration
            if configuration.has_valid_login_session(
                self._get_login_session_identifier()
            ):
                self._send_html(200, render_token_configuration_page())
            else:
                self._send_message(
                    403,
                    "配置不可用",
                    "登录会话无效或已失效，请返回状态页重新登录。",
                )
            return
        self._send_message(404, "未找到页面", "请求的页面不存在。")

    def do_HEAD(self) -> None:
        requested_path = self._requested_path()
        if requested_path in ("/", "/index.html"):
            self._send_status_page(include_body=False)
            return
        self._send_message(404, "未找到页面", "请求的页面不存在。")

    def do_POST(self) -> None:
        requested_path = self._requested_path()
        if requested_path == "/login":
            form = self._read_form(("username", "password"))
            if form is None or not form["username"] or not form["password"]:
                self._send_message(400, "登录失败", "账号和密码不能为空。")
                return

            session_identifier = self.server.web_token_configuration.begin_login(
                form["username"], form["password"]
            )
            if session_identifier is None:
                self._send_message(403, "登录失败", "账号或密码错误。")
                return

            self._send_login_redirect(session_identifier)
            return

        if requested_path == "/configure":
            form = self._read_form(
                ("cloudflare_token", "x_tunnel_token"),
                ("persist_tokens",),
            )
            if (
                form is None
                or not form["cloudflare_token"]
                or not form["x_tunnel_token"]
            ):
                self._send_message(400, "配置失败", "两项 Token 都不能为空。")
                return

            persist_tokens = form.get("persist_tokens") == "1"
            if "persist_tokens" in form and not persist_tokens:
                self._send_message(400, "配置失败", "保存选项无效。")
                return

            try:
                configuration = self.server.web_token_configuration.save_configuration(
                    self._get_login_session_identifier(),
                    form["cloudflare_token"],
                    form["x_tunnel_token"],
                    save_persisted_token_configuration if persist_tokens else None,
                )
            except PersistentTokenConfigurationError:
                self._send_message(
                    500,
                    "配置失败",
                    "无法保存本地 Token 配置，本次配置未生效。",
                )
                return
            if configuration is None:
                self._send_message(
                    403,
                    "配置失败",
                    "配置会话无效或当前实例无法再次配置。",
                )
                return

            try:
                self.server.configuration_loop.call_soon_threadsafe(
                    self.server.configuration_updates.put_nowait,
                    configuration,
                )
            except (AttributeError, RuntimeError):
                if persist_tokens:
                    self._send_message(
                        503,
                        "配置已保存",
                        "服务正在关闭，Token 已保存并将在下次启动时加载。",
                    )
                else:
                    self.server.web_token_configuration.discard_configuration(
                        configuration
                    )
                    self._send_message(
                        503,
                        "配置失败",
                        "服务正在关闭，未保存本次配置。",
                    )
                return

            self._send_configuration_redirect()
            return

        self._send_message(404, "未找到页面", "请求的页面不存在。")


class StatusHTTPServer(ThreadingHTTPServer):
    """Do not let a stalled HTTP client delay application shutdown."""

    daemon_threads = True


def create_status_server(
    uptime_port: int,
    web_token_configuration: Optional[WebTokenConfiguration] = None,
    configuration_loop: Optional[asyncio.AbstractEventLoop] = None,
    configuration_updates: Optional[asyncio.Queue[TunnelConfiguration]] = None,
) -> ThreadingHTTPServer:
    """Create one status server whose uptime spans tunnel service cycles."""

    start_time = time.time()
    http_server = StatusHTTPServer(("", uptime_port), StatusRequestHandler)
    http_server.start_time = start_time
    http_server.start_date = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))
    http_server.web_token_configuration = (
        web_token_configuration
        if web_token_configuration is not None
        else WebTokenConfiguration(environment_tokens_present=False)
    )
    http_server.configuration_loop = configuration_loop
    http_server.configuration_updates = configuration_updates
    return http_server


async def stop_status_server(
    http_server: ThreadingHTTPServer,
    http_server_task: asyncio.Task[None],
) -> None:
    """Stop the independently owned status server during application shutdown."""
    if not http_server_task.done():
        await asyncio.to_thread(http_server.shutdown)
    http_server.server_close()
    http_server_task.cancel()
    await asyncio.gather(http_server_task, return_exceptions=True)


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
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
    status_server_task: asyncio.Task[None],
    using_fallback_token: bool = False,
    x_tunnel_token: str = "",
    configuration_updates: Optional[asyncio.Queue[TunnelConfiguration]] = None,
) -> tuple[int, bool]:
    """Run tunnel processes while the status server remains independently alive.

    A fallback cycle also waits for a one-time web configuration. Its cleanup
    stops both child processes before supervise starts the persistent cycle
    with the submitted configuration.
    """

    child_processes = []
    background_tasks = []
    process_waiters = []
    fallback_timeout_task = None
    configuration_waiter = None
    x_tunnel_process = None
    cloudflared_process = None
    cloudflared_waiter = None
    fallback_runtime_limit_reached = False

    try:
        if using_fallback_token and configuration_updates is not None:
            configuration_waiter = asyncio.create_task(
                configuration_updates.get(),
                name="web-token-configuration",
            )

        await download_runtime_binaries(architecture_suffix)

        if (
            configuration_waiter is not None
            and configuration_waiter.done()
        ):
            configuration_waiter.result()
            return 0, fallback_runtime_limit_reached

        print(f"[x-tunnel] 启动在本地端口 {WSPORT} ...", flush=True)
        x_tunnel_args = [str(X_TUNNEL), "-l", f"ws://127.0.0.1:{WSPORT}"]
        if x_tunnel_token:
            x_tunnel_args.extend(["-token", x_tunnel_token])
        x_tunnel_process = await asyncio.create_subprocess_exec(*x_tunnel_args)
        child_processes.append(x_tunnel_process)

        if (
            configuration_waiter is not None
            and configuration_waiter.done()
        ):
            configuration_waiter.result()
            return 0, fallback_runtime_limit_reached

        await asyncio.sleep(TUNNEL_STARTUP_CHECK_DELAY_SECONDS)
        if (
            configuration_waiter is not None
            and configuration_waiter.done()
        ):
            configuration_waiter.result()
            return 0, fallback_runtime_limit_reached

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

        if (
            configuration_waiter is not None
            and configuration_waiter.done()
        ):
            configuration_waiter.result()
            return 0, fallback_runtime_limit_reached

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

        x_tunnel_waiter = asyncio.create_task(
            x_tunnel_process.wait(), name="process-x-tunnel"
        )
        cloudflared_waiter = asyncio.create_task(
            cloudflared_process.wait(), name="process-cloudflared"
        )
        process_waiters.extend((x_tunnel_waiter, cloudflared_waiter))
        monitored = [*background_tasks, *process_waiters, status_server_task]
        if configuration_waiter is not None:
            monitored.append(configuration_waiter)
        while True:
            completed, _ = await asyncio.wait(
                monitored, return_when=asyncio.FIRST_COMPLETED
            )

            if (
                configuration_waiter is not None
                and configuration_waiter in completed
            ):
                configuration_waiter.result()
                return 0, fallback_runtime_limit_reached

            if (
                fallback_timeout_task is not None
                and fallback_timeout_task in completed
            ):
                fallback_runtime_limit_reached = True
                print(
                    "[fallback] 已达到 600 秒运行上限，正在停止 cloudflared；"
                    "保留本地 Web 服务和状态页。",
                    flush=True,
                )

                monitored.remove(fallback_timeout_task)
                background_tasks.remove(fallback_timeout_task)
                if cloudflared_waiter in monitored:
                    monitored.remove(cloudflared_waiter)

                await terminate_process(cloudflared_process)
                await asyncio.gather(cloudflared_waiter, return_exceptions=True)
                child_processes.remove(cloudflared_process)
                fallback_timeout_task = None
                continue

            if status_server_task in completed:
                if status_server_task.cancelled():
                    raise StatusServerStoppedError("状态页服务任务被取消")

                try:
                    status_server_task.result()
                except Exception as exc:
                    raise StatusServerStoppedError("状态页服务异常退出") from exc
                raise StatusServerStoppedError("状态页服务已停止")

            first_completed = next(iter(completed))
            try:
                result = first_completed.result()
            except Exception as exc:
                print(f"[-] 后台任务异常退出: {exc}", file=sys.stderr, flush=True)
                return 1, fallback_runtime_limit_reached

            if (
                fallback_runtime_limit_reached
                and first_completed is x_tunnel_waiter
            ):
                print(
                    "[fallback] x-tunnel 已退出；状态页继续等待网页 Token 配置。",
                    flush=True,
                )
                monitored.remove(x_tunnel_waiter)
                process_waiters.remove(x_tunnel_waiter)
                if x_tunnel_process in child_processes:
                    child_processes.remove(x_tunnel_process)
                continue

            return (
                result if isinstance(result, int) else 0,
                fallback_runtime_limit_reached,
            )
    finally:
        if configuration_waiter is not None:
            configuration_waiter.cancel()
            await asyncio.gather(configuration_waiter, return_exceptions=True)

        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)

        await asyncio.gather(
            *(terminate_process(process) for process in child_processes),
            return_exceptions=True,
        )
        for process_waiter in process_waiters:
            process_waiter.cancel()
        if process_waiters:
            await asyncio.gather(*process_waiters, return_exceptions=True)


async def wait_for_web_configuration(
    configuration_updates: asyncio.Queue[TunnelConfiguration],
    status_server_task: asyncio.Task[None],
) -> None:
    """Keep an expired fallback status page configurable until it is stopped."""

    configuration_waiter = asyncio.create_task(
        configuration_updates.get(),
        name="web-token-configuration-idle",
    )
    try:
        completed, _ = await asyncio.wait(
            (configuration_waiter, status_server_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if configuration_waiter in completed:
            configuration_waiter.result()
            return

        if status_server_task.cancelled():
            raise StatusServerStoppedError("状态页服务任务被取消")
        try:
            status_server_task.result()
        except Exception as exc:
            raise StatusServerStoppedError("状态页服务异常退出") from exc
        raise StatusServerStoppedError("状态页服务已停止")
    finally:
        configuration_waiter.cancel()
        await asyncio.gather(configuration_waiter, return_exceptions=True)


async def supervise() -> int:
    """Restart complete service cycles after tunnel exits or recoverable failures."""
    configure_timezone()

    try:
        architecture_suffix = get_architecture_suffix()
    except RuntimeBinaryDownloadError as exc:
        print(f"[-] {exc}", file=sys.stderr, flush=True)
        return 1

    try:
        startup_configuration = resolve_startup_tunnel_configuration()
    except PersistentTokenConfigurationError:
        print(
            "[-] 本地 Token 配置无效或无法安全读取；拒绝启动。",
            file=sys.stderr,
            flush=True,
        )
        return 1

    cloudflare_token = startup_configuration.configuration.cloudflare_token
    x_tunnel_token = startup_configuration.configuration.x_tunnel_token
    using_fallback_token = startup_configuration.using_fallback_token

    if not cloudflare_token:
        print("[-] 致命错误: 未检测到 Cloudflare Tunnel token！", flush=True)
        return 1

    if startup_configuration.loaded_persisted_configuration:
        print("[saved] 已加载本地 Token 配置，进入持续运行模式。", flush=True)

    if using_fallback_token:
        print("[fallback] 未配置 Cloudflare Token 别名；启用 600 秒限时回退模式。", flush=True)

    try:
        uptime_port = get_status_port()
    except ValueError:
        configured_port = os.environ.get("SERVER_PORT") or os.environ.get("PORT") or ""
        print(f"[-] 状态页端口参数错误: {configured_port}", flush=True)
        return 1

    web_token_configuration = WebTokenConfiguration(
        environment_tokens_present=(
            startup_configuration.environment_tokens_present
            or startup_configuration.loaded_persisted_configuration
        )
    )
    configuration_updates: asyncio.Queue[TunnelConfiguration] = asyncio.Queue()

    try:
        http_server = create_status_server(
            uptime_port,
            web_token_configuration,
            asyncio.get_running_loop(),
            configuration_updates,
        )
    except OSError as exc:
        print(f"[-] 状态页启动失败: {exc}", file=sys.stderr, flush=True)
        return 1

    http_server_task = asyncio.create_task(
        asyncio.to_thread(http_server.serve_forever), name="http-server"
    )

    try:
        ipv = get_ipv()
        if using_fallback_token:
            web_configuration = None
            try:
                exit_code, reached_runtime_limit = await run_service_cycle(
                    ipv,
                    cloudflare_token,
                    uptime_port,
                    architecture_suffix,
                    http_server_task,
                    using_fallback_token=True,
                    x_tunnel_token=x_tunnel_token,
                    configuration_updates=configuration_updates,
                )
            except FileNotFoundError as exc:
                web_configuration = (
                    web_token_configuration.claim_configuration_or_close()
                )
                if web_configuration is None:
                    print(
                        f"[-] 未找到可执行文件: {exc.filename}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 1
                exit_code = 1
                reached_runtime_limit = False
            except OSError as exc:
                web_configuration = (
                    web_token_configuration.claim_configuration_or_close()
                )
                if web_configuration is None:
                    print(f"[-] 启动失败: {exc}", file=sys.stderr, flush=True)
                    return 1
                exit_code = 1
                reached_runtime_limit = False
            except StatusServerStoppedError as exc:
                print(f"[-] {exc}", file=sys.stderr, flush=True)
                return 1

            if web_configuration is None:
                web_configuration = web_token_configuration.get_configuration()
            if reached_runtime_limit and web_configuration is None:
                print(
                    "[fallback] Cloudflare 隧道已停止；状态页继续在本地端口运行。",
                    flush=True,
                )
                try:
                    await wait_for_web_configuration(
                        configuration_updates,
                        http_server_task,
                    )
                    web_configuration = web_token_configuration.get_configuration()
                except StatusServerStoppedError as exc:
                    print(f"[-] {exc}", file=sys.stderr, flush=True)
                    return 1

            if web_configuration is not None:
                cloudflare_token = web_configuration.cloudflare_token
                x_tunnel_token = web_configuration.x_tunnel_token
                using_fallback_token = False
                print(
                    "[web] 已接收网页 Token 配置，正在切换到持续运行模式。",
                    flush=True,
                )

            if using_fallback_token:
                web_configuration = (
                    web_token_configuration.claim_configuration_or_close()
                )
                if web_configuration is not None:
                    cloudflare_token = web_configuration.cloudflare_token
                    x_tunnel_token = web_configuration.x_tunnel_token
                    using_fallback_token = False
                    print(
                        "[web] 已接收网页 Token 配置，正在切换到持续运行模式。",
                        flush=True,
                    )
                else:
                    print(
                        "[-] 回退 Token 模式在达到运行上限前结束。",
                        file=sys.stderr,
                        flush=True,
                    )
                    return exit_code if exit_code != 0 else 1

        while True:
            try:
                await run_service_cycle(
                    ipv,
                    cloudflare_token,
                    uptime_port,
                    architecture_suffix,
                    http_server_task,
                    x_tunnel_token=x_tunnel_token,
                )
            except FileNotFoundError as exc:
                print(f"[-] 未找到可执行文件: {exc.filename}", file=sys.stderr, flush=True)
            except OSError as exc:
                print(f"[-] 启动失败: {exc}", file=sys.stderr, flush=True)
            except StatusServerStoppedError as exc:
                print(f"[-] {exc}", file=sys.stderr, flush=True)
                return 1

            print(f"[restart] {RESTART_DELAY_SECONDS} 秒后整体重启服务...", flush=True)
            await asyncio.sleep(RESTART_DELAY_SECONDS)
    finally:
        web_token_configuration.stop_accepting_web_configuration()
        await stop_status_server(http_server, http_server_task)

    # The loop above intentionally does not terminate while an explicit token is set.
    return 0


def main() -> int:
    try:
        return asyncio.run(supervise())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
