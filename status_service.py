#!/usr/bin/env python3
"""Docker runtime, status page, and one-time Tunnel Token configuration."""

from __future__ import annotations

import html
import json
import os
import platform
import queue
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import parse_qs, urlsplit


APP_DIRECTORY = Path(os.environ.get("APP_DIR") or Path(__file__).resolve().parent)
WSPORT = 8081
STATUS_DEFAULT_PORT = 3000
FALLBACK_TOKEN_RUNTIME_SECONDS = 600
FALLBACK_TERMINATION_GRACE_SECONDS = 10
PROCESS_POLL_INTERVAL_SECONDS = 0.5
LOGIN_SESSION_LIFETIME_SECONDS = 300
HTTP_REQUEST_TIMEOUT_SECONDS = 15
MAX_FORM_BODY_BYTES = 8 * 1024
MAX_PERSISTED_TOKEN_CONFIGURATION_BYTES = 64 * 1024
MAX_TOKEN_CHARACTERS = 16 * 1024
PERSISTED_TOKEN_CONFIGURATION_FILE_NAME = ".tunnel-tokens.json"
PERSISTED_TOKEN_CONFIGURATION_VERSION = 1
PERSISTED_TOKEN_CONFIGURATION_FILE_MODE = 0o600

# Keep the existing fallback-token behavior without putting it in logs or HTML.
DEFAULT_E = "e"
DEFAULT_Y = "y"
DEFAULT_TOKEN = (
    "JhIjoiZDZkMzEzZjA2MzI1OGJjODllNzc4YmVlMDQ5YTZmOTEiLCJ0IjoiYmYwYzQy"
    "ZWEtNGIyYy00ZTFhLWEyNDgtZWRiODgyNjM1YjA4IiwicyI6Ik5HTXpPRFE1TnpndE5H"
    "TmtPUzAwT1dObUxXSmpNV1F0T0RabU5EUXhNREkzTTJVMSJ9"
)
CLOUDFLARE_TOKEN_ENVIRONMENT_NAMES = ("envToken", "ENV_TOKEN", "token", "TOKEN")


class TokenConfigurationError(ValueError):
    """Raised when a Token pair is invalid."""


class PersistentTokenConfigurationError(RuntimeError):
    """Raised when a saved Token configuration cannot be used safely."""


@dataclass(frozen=True)
class TunnelConfiguration:
    cloudflare_token: str
    x_tunnel_token: str


@dataclass(frozen=True)
class StartupConfiguration:
    configuration: Optional[TunnelConfiguration]
    using_fallback_token: bool
    web_configuration_allowed: bool
    source: str
    persisted_configuration_error: bool = False


def _normalise_token(value: object) -> str:
    if not isinstance(value, str):
        raise TokenConfigurationError("Token 配置无效。")

    token = value.strip()
    if not token or len(token) > MAX_TOKEN_CHARACTERS:
        raise TokenConfigurationError("Token 配置无效。")
    if any(ord(character) < 32 or ord(character) == 127 for character in token):
        raise TokenConfigurationError("Token 配置无效。")
    return token


def make_tunnel_configuration(
    cloudflare_token: object, x_tunnel_token: object
) -> TunnelConfiguration:
    return TunnelConfiguration(
        cloudflare_token=_normalise_token(cloudflare_token),
        x_tunnel_token=_normalise_token(x_tunnel_token),
    )


def get_persisted_token_configuration_path(
    app_directory: Optional[Path] = None,
) -> Path:
    directory = Path(app_directory) if app_directory is not None else APP_DIRECTORY
    return directory / PERSISTED_TOKEN_CONFIGURATION_FILE_NAME


def _validate_secure_file_metadata(file_info: os.stat_result) -> None:
    if not stat.S_ISREG(file_info.st_mode):
        raise PersistentTokenConfigurationError("Token 配置文件不安全。")

    if os.name == "posix" and stat.S_IMODE(file_info.st_mode) != PERSISTED_TOKEN_CONFIGURATION_FILE_MODE:
        raise PersistentTokenConfigurationError("Token 配置文件权限不安全。")


def _get_existing_configuration_file_info(path: Path) -> Optional[os.stat_result]:
    try:
        file_info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PersistentTokenConfigurationError("无法安全读取 Token 配置文件。") from error

    if stat.S_ISLNK(file_info.st_mode):
        raise PersistentTokenConfigurationError("Token 配置文件不安全。")
    _validate_secure_file_metadata(file_info)
    return file_info


def load_persisted_token_configuration(
    app_directory: Optional[Path] = None,
) -> Optional[TunnelConfiguration]:
    """Load a strict, private JSON configuration without following symlinks."""

    configuration_path = get_persisted_token_configuration_path(app_directory)
    expected_info = _get_existing_configuration_file_info(configuration_path)
    if expected_info is None:
        return None

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor: Optional[int] = None
    try:
        file_descriptor = os.open(os.fspath(configuration_path), flags)
        actual_info = os.fstat(file_descriptor)
        _validate_secure_file_metadata(actual_info)
        if (
            actual_info.st_dev != expected_info.st_dev
            or actual_info.st_ino != expected_info.st_ino
        ):
            raise PersistentTokenConfigurationError("Token 配置文件在读取时发生变化。")

        with os.fdopen(file_descriptor, "rb", closefd=True) as configuration_file:
            file_descriptor = None
            raw_configuration = configuration_file.read(
                MAX_PERSISTED_TOKEN_CONFIGURATION_BYTES + 1
            )
    except PersistentTokenConfigurationError:
        raise
    except OSError as error:
        raise PersistentTokenConfigurationError("无法安全读取 Token 配置文件。") from error
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass

    if len(raw_configuration) > MAX_PERSISTED_TOKEN_CONFIGURATION_BYTES:
        raise PersistentTokenConfigurationError("Token 配置文件过大。")

    try:
        parsed_configuration = json.loads(raw_configuration.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PersistentTokenConfigurationError("Token 配置文件格式无效。") from error

    expected_keys = {"version", "cloudflare_token", "x_tunnel_token"}
    if not isinstance(parsed_configuration, dict) or set(parsed_configuration) != expected_keys:
        raise PersistentTokenConfigurationError("Token 配置文件格式无效。")
    if type(parsed_configuration["version"]) is not int or (
        parsed_configuration["version"] != PERSISTED_TOKEN_CONFIGURATION_VERSION
    ):
        raise PersistentTokenConfigurationError("Token 配置文件版本无效。")

    try:
        return make_tunnel_configuration(
            parsed_configuration["cloudflare_token"],
            parsed_configuration["x_tunnel_token"],
        )
    except TokenConfigurationError as error:
        raise PersistentTokenConfigurationError("Token 配置文件格式无效。") from error


def save_persisted_token_configuration(
    configuration: TunnelConfiguration, app_directory: Optional[Path] = None
) -> Path:
    """Atomically save a private Token file in the program directory."""

    try:
        safe_configuration = make_tunnel_configuration(
            configuration.cloudflare_token, configuration.x_tunnel_token
        )
    except (AttributeError, TokenConfigurationError) as error:
        raise PersistentTokenConfigurationError("Token 配置无效。") from error

    configuration_path = get_persisted_token_configuration_path(app_directory)
    if not configuration_path.parent.is_dir():
        raise PersistentTokenConfigurationError("无法保存 Token 配置文件。")

    # An unsafe old file is not silently repaired or replaced by a web request.
    _get_existing_configuration_file_info(configuration_path)

    temporary_path: Optional[Path] = None
    file_descriptor: Optional[int] = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".tunnel-tokens.", suffix=".tmp", dir=os.fspath(configuration_path.parent)
        )
        temporary_path = Path(temporary_name)
        if os.name == "posix":
            os.fchmod(file_descriptor, PERSISTED_TOKEN_CONFIGURATION_FILE_MODE)
            _validate_secure_file_metadata(os.fstat(file_descriptor))

        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n", closefd=True) as output_file:
            file_descriptor = None
            json.dump(
                {
                    "version": PERSISTED_TOKEN_CONFIGURATION_VERSION,
                    "cloudflare_token": safe_configuration.cloudflare_token,
                    "x_tunnel_token": safe_configuration.x_tunnel_token,
                },
                output_file,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            output_file.write("\n")
            output_file.flush()
            os.fsync(output_file.fileno())

        os.replace(temporary_path, configuration_path)
        temporary_path = None
        return configuration_path
    except PersistentTokenConfigurationError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise PersistentTokenConfigurationError("无法保存 Token 配置文件。") from error
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _read_environment_token(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    return value.strip() if isinstance(value, str) else ""


def get_environment_cloudflare_token(environment: Mapping[str, str]) -> str:
    for name in CLOUDFLARE_TOKEN_ENVIRONMENT_NAMES:
        token = _read_environment_token(environment, name)
        if token:
            return token
    return ""


def resolve_startup_configuration(
    app_directory: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> StartupConfiguration:
    """Resolve environment, saved, and fallback Tokens in precedence order."""

    active_environment = os.environ if environment is None else environment
    cloudflare_token = get_environment_cloudflare_token(active_environment)
    x_tunnel_token = _read_environment_token(active_environment, "TOKEN")

    # Keep historical Docker semantics: TOKEN is both the lowest-priority
    # Cloudflared alias and the x-tunnel Token. Any existing Token env locks web login.
    if cloudflare_token or x_tunnel_token:
        using_fallback_token = not bool(cloudflare_token)
        effective_cloudflare_token = cloudflare_token or (
            DEFAULT_E + DEFAULT_Y + DEFAULT_TOKEN
        )
        return StartupConfiguration(
            configuration=TunnelConfiguration(effective_cloudflare_token, x_tunnel_token),
            using_fallback_token=using_fallback_token,
            web_configuration_allowed=False,
            source="environment",
        )

    try:
        saved_configuration = load_persisted_token_configuration(app_directory)
    except PersistentTokenConfigurationError:
        # Fail closed: a corrupt or unsafe saved file never falls back to the default Token.
        return StartupConfiguration(
            configuration=None,
            using_fallback_token=False,
            web_configuration_allowed=False,
            source="invalid-saved-configuration",
            persisted_configuration_error=True,
        )

    if saved_configuration is not None:
        return StartupConfiguration(
            configuration=saved_configuration,
            using_fallback_token=False,
            web_configuration_allowed=False,
            source="saved-configuration",
        )

    return StartupConfiguration(
        configuration=TunnelConfiguration(DEFAULT_E + DEFAULT_Y + DEFAULT_TOKEN, ""),
        using_fallback_token=True,
        web_configuration_allowed=True,
        source="fallback",
    )


class WebTokenConfiguration:
    """One-use web login and configuration state shared by HTTP worker threads."""

    def __init__(self, web_configuration_allowed: bool) -> None:
        self._lock = threading.Lock()
        self._web_configuration_allowed = web_configuration_allowed
        self._session_identifier: Optional[str] = None
        self._session_expires_at = 0.0
        self._configuration: Optional[TunnelConfiguration] = None

    def allows_login(self) -> bool:
        with self._lock:
            return self._web_configuration_allowed

    def begin_login(self, account: str, password: str) -> Optional[str]:
        if not account or not password:
            return None

        with self._lock:
            if not self._web_configuration_allowed:
                return None
            self._session_identifier = secrets.token_urlsafe(32)
            self._session_expires_at = time.monotonic() + LOGIN_SESSION_LIFETIME_SECONDS
            return self._session_identifier

    def has_valid_login_session(self, session_identifier: Optional[str]) -> bool:
        if not session_identifier:
            return False

        with self._lock:
            if (
                not self._web_configuration_allowed
                or self._session_identifier is None
                or time.monotonic() >= self._session_expires_at
            ):
                return False
            return secrets.compare_digest(session_identifier, self._session_identifier)

    def save_configuration(
        self,
        session_identifier: Optional[str],
        cloudflare_token: str,
        x_tunnel_token: str,
        persist_configuration: Optional[Callable[[TunnelConfiguration], None]] = None,
    ) -> Optional[TunnelConfiguration]:
        configuration = make_tunnel_configuration(cloudflare_token, x_tunnel_token)

        with self._lock:
            if (
                not self._web_configuration_allowed
                or self._session_identifier is None
                or time.monotonic() >= self._session_expires_at
                or not session_identifier
                or not secrets.compare_digest(session_identifier, self._session_identifier)
            ):
                return None

            # Persist before changing state so a failed write can be retried with the
            # still-valid session and never activates a partial configuration.
            if persist_configuration is not None:
                persist_configuration(configuration)

            self._configuration = configuration
            self._web_configuration_allowed = False
            self._session_identifier = None
            self._session_expires_at = 0.0
            return configuration

    def get_configuration(self) -> Optional[TunnelConfiguration]:
        with self._lock:
            return self._configuration


class RuntimeStatus:
    """Thread-safe status-page clock and non-sensitive operational notices."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._started_at_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._notice = ""

    def start_new_cycle(self) -> None:
        with self._lock:
            self._started_at = time.time()
            self._started_at_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._notice = ""

    def set_notice(self, notice: str) -> None:
        with self._lock:
            self._notice = notice

    def snapshot(self) -> tuple[int, str, str]:
        with self._lock:
            elapsed_seconds = max(0, int(time.time() - self._started_at))
            return elapsed_seconds, self._started_at_text, self._notice


def _render_login_panel(web_configuration: WebTokenConfiguration) -> str:
    # Keep the login form visible in every mode. The server-side state check is
    # authoritative and returns a login error once env/saved/web Tokens exist.
    return """
    <section class="login-box">
        <h3>登录</h3>
        <form method="post" action="/login" autocomplete="off">
            <label for="account">账号</label>
            <input id="account" name="account" type="text" required>
            <label for="password">密码</label>
            <input id="password" name="password" type="password" required>
            <button type="submit">登录</button>
        </form>
    </section>
"""


def render_status_page(
    web_configuration: WebTokenConfiguration,
    runtime_status: RuntimeStatus,
    message: str = "",
) -> str:
    elapsed_seconds, started_at_text, notice = runtime_status.snapshot()
    days, remainder = divmod(elapsed_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    safe_message = html.escape(message)
    safe_notice = html.escape(notice)
    safe_start_time = html.escape(started_at_text)
    message_html = (
        f'<p class="error" role="alert">{safe_message}</p>' if safe_message else ""
    )
    notice_html = f'<p class="hint">{safe_notice}</p>' if safe_notice else ""

    # Preserve the original simple status content and styling; JavaScript updates
    # only the elapsed clock so a login/configuration form is not cleared by refresh.
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>服务运行状态</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; text-align: center; margin-top: 10vh; background-color: #f4f4f9; color: #333; }}
        .box {{ background: white; padding: 30px 50px; border-radius: 12px; display: inline-block; box-shadow: 0 8px 16px rgba(0,0,0,0.1); }}
        .time {{ font-size: 28px; color: #007bff; font-weight: bold; margin: 15px 0; letter-spacing: 1px; }}
        .footer {{ margin-top: 20px; color: #888; font-size: 13px; }}
        .login-box {{ max-width: 360px; margin: 22px auto; padding: 22px 28px; text-align: left; background: white; border-radius: 12px; box-shadow: 0 8px 16px rgba(0,0,0,0.1); }}
        .login-box h3 {{ margin-top: 0; text-align: center; }}
        label {{ display: block; margin: 12px 0 5px; }}
        input {{ box-sizing: border-box; width: 100%; padding: 9px; border: 1px solid #bbb; border-radius: 5px; }}
        button {{ width: 100%; margin-top: 18px; padding: 10px; color: white; background: #007bff; border: 0; border-radius: 5px; cursor: pointer; }}
        .checkbox-label {{ display: flex; align-items: center; gap: 8px; margin-top: 14px; }}
        .checkbox-label input {{ width: auto; margin: 0; }}
        .hint {{ color: #666; font-size: 13px; line-height: 1.5; }}
        .error {{ color: #b00020; text-align: center; }}
    </style>
</head>
<body>
    <div class="box">
        <h2> 服务器运行状态正常</h2>
        <div id="uptime" class="time">{days}天 {hours}小时 {minutes}分钟 {seconds}秒</div>
        <div class="footer">本次服务周期启动时间：{safe_start_time} (北京时间)</div>
    </div>
    {message_html}
    {notice_html}
    {_render_login_panel(web_configuration)}
    <script>
        (() => {{
            const startedAt = Date.now() - {elapsed_seconds * 1000};
            const clock = document.getElementById('uptime');
            const updateClock = () => {{
                const elapsed = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
                const days = Math.floor(elapsed / 86400);
                const hours = Math.floor((elapsed % 86400) / 3600);
                const minutes = Math.floor((elapsed % 3600) / 60);
                const seconds = elapsed % 60;
                clock.textContent = `${{days}}天 ${{hours}}小时 ${{minutes}}分钟 ${{seconds}}秒`;
            }};
            window.setInterval(updateClock, 1000);
        }})();
    </script>
</body>
</html>
"""


def render_configuration_page(message: str = "") -> str:
    safe_message = html.escape(message)
    message_html = (
        f'<p class="error" role="alert">{safe_message}</p>' if safe_message else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>配置 Tunnel Token</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; text-align: center; margin-top: 10vh; background-color: #f4f4f9; color: #333; }}
        .box {{ max-width: 420px; margin: auto; padding: 28px 34px; text-align: left; background: white; border-radius: 12px; box-shadow: 0 8px 16px rgba(0,0,0,0.1); }}
        h2 {{ text-align: center; margin-top: 0; }}
        label {{ display: block; margin: 14px 0 5px; }}
        input {{ box-sizing: border-box; width: 100%; padding: 9px; border: 1px solid #bbb; border-radius: 5px; }}
        button {{ width: 100%; margin-top: 18px; padding: 10px; color: white; background: #007bff; border: 0; border-radius: 5px; cursor: pointer; }}
        .checkbox-label {{ display: flex; align-items: center; gap: 8px; margin-top: 14px; }}
        .checkbox-label input {{ width: auto; margin: 0; }}
        .hint {{ color: #666; font-size: 13px; line-height: 1.5; }}
        .error {{ color: #b00020; }}
    </style>
</head>
<body>
    <main class="box">
        <h2>配置 Tunnel Token</h2>
        <p class="hint">两项都需要填写；不勾选时仅在当前进程中使用。</p>
        {message_html}
        <form method="post" action="/configure" autocomplete="off">
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
</html>
"""


class StatusHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class StatusRequestHandler(BaseHTTPRequestHandler):
    server: StatusHTTPServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(HTTP_REQUEST_TIMEOUT_SECONDS)

    def log_message(self, _format: str, *_arguments: object) -> None:
        # Form bodies may contain Tokens. Suppress default request logging entirely.
        return

    def _requested_path(self) -> Optional[str]:
        parsed_path = urlsplit(self.path)
        if parsed_path.query or parsed_path.fragment:
            return None
        return parsed_path.path

    def _send_html(
        self,
        response_status: int,
        page: str,
        headers: Optional[Mapping[str, str]] = None,
        include_body: bool = True,
    ) -> None:
        encoded_page = page.encode("utf-8")
        self.send_response(response_status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded_page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        if headers is not None:
            for name, value in headers.items():
                self.send_header(name, value)
        self.end_headers()
        if include_body:
            self.wfile.write(encoded_page)

    def _send_status_page(self, response_status: int = 200, message: str = "") -> None:
        self._send_html(
            response_status,
            render_status_page(
                self.server.web_configuration, self.server.runtime_status, message
            ),
        )

    def _send_redirect(self, location: str, session_identifier: Optional[str] = None) -> None:
        headers = {"Location": location}
        if session_identifier is not None:
            headers["Set-Cookie"] = (
                "tunnel_configuration_session="
                f"{session_identifier}; Max-Age={LOGIN_SESSION_LIFETIME_SECONDS}; "
                "HttpOnly; SameSite=Strict; Path=/"
            )
        self._send_html(303, "", headers)

    def _get_login_session_identifier(self) -> Optional[str]:
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        try:
            cookies = SimpleCookie()
            cookies.load(cookie_header)
        except CookieError:
            return None
        morsel = cookies.get("tunnel_configuration_session")
        if morsel is None or len(morsel.value) > 256:
            return None
        return morsel.value

    def _read_form(
        self,
        required_fields: tuple[str, ...],
        optional_fields: tuple[str, ...] = (),
    ) -> Optional[dict[str, str]]:
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            return None

        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        if content_length <= 0 or content_length > MAX_FORM_BODY_BYTES:
            return None

        raw_body = self.rfile.read(content_length)
        if len(raw_body) != content_length:
            return None
        try:
            parsed_form = parse_qs(
                raw_body.decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=len(required_fields) + len(optional_fields),
            )
        except (UnicodeDecodeError, ValueError):
            return None

        allowed_fields = set(required_fields) | set(optional_fields)
        if not set(required_fields).issubset(parsed_form) or not set(parsed_form).issubset(
            allowed_fields
        ):
            return None

        result: dict[str, str] = {}
        for name, values in parsed_form.items():
            if len(values) != 1:
                return None
            result[name] = values[0].strip()
        return result

    def do_HEAD(self) -> None:
        requested_path = self._requested_path()
        if requested_path in ("/", "/index.html"):
            page = render_status_page(
                self.server.web_configuration, self.server.runtime_status
            )
            self._send_html(200, page, include_body=False)
            return
        self._send_html(404, "", include_body=False)

    def do_GET(self) -> None:
        requested_path = self._requested_path()
        if requested_path in ("/", "/index.html"):
            self._send_status_page()
            return
        if requested_path == "/configure":
            if not self.server.web_configuration.has_valid_login_session(
                self._get_login_session_identifier()
            ):
                self._send_status_page(403, "登录会话无效或已过期。")
                return
            self._send_html(200, render_configuration_page())
            return
        self._send_html(404, "")

    def do_POST(self) -> None:
        requested_path = self._requested_path()
        if requested_path == "/login":
            self._handle_login()
            return
        if requested_path == "/configure":
            self._handle_configuration()
            return
        self._send_html(404, "")

    def _handle_login(self) -> None:
        form = self._read_form(("account", "password"))
        if form is None or not form["account"] or not form["password"]:
            self._send_status_page(400, "登录失败：账号和密码不能为空。")
            return

        session_identifier = self.server.web_configuration.begin_login(
            form["account"], form["password"]
        )
        if session_identifier is None:
            self._send_status_page(403, "登录失败：Token 已配置，不能再次登录。")
            return
        self._send_redirect("/configure", session_identifier)

    def _handle_configuration(self) -> None:
        session_identifier = self._get_login_session_identifier()
        if not self.server.web_configuration.has_valid_login_session(session_identifier):
            self._send_status_page(403, "登录会话无效或已过期。")
            return

        form = self._read_form(
            ("cloudflare_token", "x_tunnel_token"), ("persist_tokens",)
        )
        if form is None:
            self._send_html(400, render_configuration_page("配置提交无效。"))
            return
        if not form["cloudflare_token"] or not form["x_tunnel_token"]:
            self._send_html(400, render_configuration_page("两项 Token 都需要填写。"))
            return
        if "persist_tokens" in form and form["persist_tokens"] != "1":
            self._send_html(400, render_configuration_page("保存选项无效。"))
            return

        persist_tokens = form.get("persist_tokens") == "1"
        persist_callback: Optional[Callable[[TunnelConfiguration], None]] = None
        if persist_tokens:
            persist_callback = lambda configuration: save_persisted_token_configuration(
                configuration, self.server.app_directory
            )

        try:
            configuration = self.server.web_configuration.save_configuration(
                session_identifier,
                form["cloudflare_token"],
                form["x_tunnel_token"],
                persist_callback,
            )
        except TokenConfigurationError:
            self._send_html(400, render_configuration_page("Token 配置无效。"))
            return
        except PersistentTokenConfigurationError:
            self._send_html(
                500, render_configuration_page("无法保存 Token 文件，本次配置未生效。")
            )
            return

        if configuration is None:
            self._send_status_page(403, "登录会话无效或已过期。")
            return

        # The queue is intentionally unbounded; this short in-memory hand-off is
        # the only route from the HTTP worker to the process supervisor.
        self.server.configuration_queue.put_nowait(configuration)
        self._send_redirect("/")


def create_status_server(
    port: int,
    web_configuration: WebTokenConfiguration,
    configuration_queue: queue.Queue[TunnelConfiguration],
    runtime_status: RuntimeStatus,
    app_directory: Path,
) -> StatusHTTPServer:
    server = StatusHTTPServer(("0.0.0.0", port), StatusRequestHandler)
    server.web_configuration = web_configuration
    server.configuration_queue = configuration_queue
    server.runtime_status = runtime_status
    server.app_directory = app_directory
    return server


def _positive_integer_from_environment(
    environment: Mapping[str, str], name: str, default: int
) -> int:
    value = _read_environment_token(environment, name)
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def get_status_port(environment: Mapping[str, str]) -> int:
    configured_port = _read_environment_token(environment, "SERVER_PORT") or _read_environment_token(
        environment, "PORT"
    )
    try:
        port = int(configured_port)
    except ValueError:
        return STATUS_DEFAULT_PORT
    return port if 1 <= port <= 65535 else STATUS_DEFAULT_PORT


def get_ip_version(environment: Mapping[str, str]) -> str:
    return "6" if _read_environment_token(environment, "IPV") == "6" else "4"


def detect_download_architecture() -> Optional[str]:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("i386", "i686", "x86"):
        return "386"
    return None


class TunnelSupervisor:
    """Owns child processes and receives at most one web configuration hand-off."""

    def __init__(
        self,
        initial_configuration: TunnelConfiguration,
        using_fallback_token: bool,
        configuration_queue: queue.Queue[TunnelConfiguration],
        runtime_status: RuntimeStatus,
        stop_event: threading.Event,
        app_directory: Path,
        environment: Mapping[str, str],
    ) -> None:
        self._configuration = initial_configuration
        self._using_fallback_token = using_fallback_token
        self._configuration_queue = configuration_queue
        self._runtime_status = runtime_status
        self._stop_event = stop_event
        self._app_directory = app_directory
        self._ip_version = get_ip_version(environment)
        self._restart_delay_seconds = _positive_integer_from_environment(
            environment, "RESTART_DELAY_SECONDS", 60
        )
        self._download_retry_delay_seconds = _positive_integer_from_environment(
            environment, "DOWNLOAD_RETRY_DELAY_SECONDS", 120
        )
        self._x_tunnel_path = app_directory / "xxx"
        self._cloudflared_path = app_directory / "ccc"
        self._x_tunnel_process: Optional[subprocess.Popen[bytes]] = None
        self._cloudflared_process: Optional[subprocess.Popen[bytes]] = None

    def stop(self) -> None:
        self._stop_event.set()
        self._stop_tunnels()

    def _remove_runtime_binaries(self) -> None:
        for binary_path in (self._x_tunnel_path, self._cloudflared_path):
            try:
                binary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # The next download will fail/retry if a stale binary cannot be replaced.
                pass

    def _download_binary(self, url: str, destination: Path) -> bool:
        temporary_path: Optional[Path] = None
        file_descriptor: Optional[int] = None
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "tunnel-runtime"})
            with urllib.request.urlopen(request, timeout=60) as response:
                file_descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{destination.name}.download.",
                    suffix=".tmp",
                    dir=os.fspath(destination.parent),
                )
                temporary_path = Path(temporary_name)
                with os.fdopen(file_descriptor, "wb", closefd=True) as output_file:
                    file_descriptor = None
                    written_bytes = 0
                    while not self._stop_event.is_set():
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        output_file.write(chunk)
                        written_bytes += len(chunk)
                    output_file.flush()
                    os.fsync(output_file.fileno())

            if self._stop_event.is_set() or written_bytes <= 0:
                return False
            if os.name == "posix":
                os.chmod(temporary_path, 0o755)
            os.replace(temporary_path, destination)
            temporary_path = None
            return True
        except (OSError, urllib.error.URLError, ValueError):
            return False
        finally:
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def _download_runtime_binaries(self) -> bool:
        download_architecture = detect_download_architecture()
        if download_architecture is None:
            print("[-] 不支持的 CPU 架构。", flush=True)
            return False

        x_tunnel_url = (
            "https://www.baipiao.eu.org/xtunnel/"
            f"x-tunnel-linux-{download_architecture}"
        )
        cloudflared_url = (
            "https://github.com/cloudflare/cloudflared/releases/latest/download/"
            f"cloudflared-linux-{download_architecture}"
        )
        while not self._stop_event.is_set():
            print("[download] 正在下载运行时二进制。", flush=True)
            if self._download_binary(x_tunnel_url, self._x_tunnel_path) and self._download_binary(
                cloudflared_url, self._cloudflared_path
            ):
                return True

            self._remove_runtime_binaries()
            print(
                f"[-] 下载运行时二进制失败，{self._download_retry_delay_seconds} 秒后重试...",
                flush=True,
            )
            self._stop_event.wait(self._download_retry_delay_seconds)
        return False

    @staticmethod
    def _stop_process(process: Optional[subprocess.Popen[bytes]]) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=FALLBACK_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=FALLBACK_TERMINATION_GRACE_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except OSError:
            pass

    def _stop_cloudflared(self) -> None:
        self._stop_process(self._cloudflared_process)
        self._cloudflared_process = None

    def _stop_tunnels(self) -> None:
        self._stop_cloudflared()
        self._stop_process(self._x_tunnel_process)
        self._x_tunnel_process = None

    def _start_tunnels(self) -> bool:
        x_tunnel_command = [str(self._x_tunnel_path), "-l", f"ws://127.0.0.1:{WSPORT}"]
        if self._configuration.x_tunnel_token:
            x_tunnel_command.extend(("-token", self._configuration.x_tunnel_token))

        try:
            self._x_tunnel_process = subprocess.Popen(
                x_tunnel_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError:
            print("[-] 无法启动 x-tunnel。", flush=True)
            return False

        if self._stop_event.wait(1) or self._x_tunnel_process.poll() is not None:
            print("[-] x-tunnel 在 Cloudflare Tunnel 启动前退出。", flush=True)
            self._stop_tunnels()
            return False

        cloudflared_command = [
            str(self._cloudflared_path),
            "--edge-ip-version",
            self._ip_version,
            "--protocol",
            "http2",
            "--no-autoupdate",
            "tunnel",
            "run",
            "--token",
            self._configuration.cloudflare_token,
        ]
        try:
            self._cloudflared_process = subprocess.Popen(
                cloudflared_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError:
            print("[-] 无法启动 cloudflared。", flush=True)
            self._stop_tunnels()
            return False

        self._runtime_status.start_new_cycle()
        print(f"[x-tunnel] 启动在本地端口 {WSPORT} ...", flush=True)
        print("========================================", flush=True)
        print(f"当前本地服务端口: {WSPORT}", flush=True)
        print("当前状态页服务保持运行。", flush=True)
        print("========================================", flush=True)
        return True

    def _take_pending_configuration(self) -> Optional[TunnelConfiguration]:
        latest_configuration: Optional[TunnelConfiguration] = None
        while True:
            try:
                latest_configuration = self._configuration_queue.get_nowait()
            except queue.Empty:
                return latest_configuration

    def _wait_for_configuration(self) -> Optional[TunnelConfiguration]:
        while not self._stop_event.is_set():
            try:
                return self._configuration_queue.get(
                    timeout=PROCESS_POLL_INTERVAL_SECONDS
                )
            except queue.Empty:
                continue
        return None

    def _wait_for_fallback_configuration(self) -> Optional[TunnelConfiguration]:
        fallback_deadline = time.monotonic() + FALLBACK_TOKEN_RUNTIME_SECONDS
        fallback_timeout_reached = False

        while not self._stop_event.is_set():
            candidate_configuration = self._take_pending_configuration()
            if candidate_configuration is not None:
                return candidate_configuration

            if not fallback_timeout_reached and time.monotonic() >= fallback_deadline:
                self._stop_cloudflared()
                fallback_timeout_reached = True
                print(
                    "[fallback] 已达到 600 秒运行上限，正在停止 cloudflared；"
                    "保留本地 Web 服务和状态页。",
                    flush=True,
                )

            if not fallback_timeout_reached:
                if (
                    self._x_tunnel_process is None
                    or self._x_tunnel_process.poll() is not None
                    or self._cloudflared_process is None
                    or self._cloudflared_process.poll() is not None
                ):
                    self._stop_tunnels()
                    self._runtime_status.set_notice("隧道进程已退出；状态页仍可用。")
                    print("[-] 隧道进程已退出，等待网页配置。", flush=True)
                    return self._wait_for_configuration()
            elif self._x_tunnel_process is not None and self._x_tunnel_process.poll() is not None:
                self._x_tunnel_process = None
                self._runtime_status.set_notice("x-tunnel 已退出；状态页仍可用。")

            self._stop_event.wait(PROCESS_POLL_INTERVAL_SECONDS)
        return None

    def _wait_for_persistent_cycle(self) -> Optional[TunnelConfiguration]:
        while not self._stop_event.is_set():
            candidate_configuration = self._take_pending_configuration()
            if candidate_configuration is not None:
                return candidate_configuration
            if (
                self._x_tunnel_process is None
                or self._x_tunnel_process.poll() is not None
                or self._cloudflared_process is None
                or self._cloudflared_process.poll() is not None
            ):
                print("[-] 隧道进程已退出，本轮服务结束。", flush=True)
                return None
            self._stop_event.wait(PROCESS_POLL_INTERVAL_SECONDS)
        return None

    def run(self) -> None:
        try:
            while not self._stop_event.is_set():
                pending_configuration = self._take_pending_configuration()
                if pending_configuration is not None:
                    self._configuration = pending_configuration
                    self._using_fallback_token = False

                self._remove_runtime_binaries()
                if not self._download_runtime_binaries():
                    return

                pending_configuration = self._take_pending_configuration()
                if pending_configuration is not None:
                    self._configuration = pending_configuration
                    self._using_fallback_token = False

                if not self._start_tunnels():
                    if self._stop_event.is_set():
                        return
                    if self._using_fallback_token:
                        self._runtime_status.set_notice("隧道未启动；状态页仍可用。")
                        pending_configuration = self._wait_for_configuration()
                        if pending_configuration is None:
                            return
                        self._configuration = pending_configuration
                        self._using_fallback_token = False
                        continue
                    print(
                        f"[restart] {self._restart_delay_seconds} 秒后整体重启服务...",
                        flush=True,
                    )
                    self._stop_event.wait(self._restart_delay_seconds)
                    continue

                if self._using_fallback_token:
                    pending_configuration = self._wait_for_fallback_configuration()
                    self._stop_tunnels()
                    if pending_configuration is None:
                        return
                    self._configuration = pending_configuration
                    self._using_fallback_token = False
                    continue

                pending_configuration = self._wait_for_persistent_cycle()
                self._stop_tunnels()
                if self._stop_event.is_set():
                    return
                if pending_configuration is not None:
                    self._configuration = pending_configuration
                    self._using_fallback_token = False
                    continue

                self._remove_runtime_binaries()
                print(
                    f"[restart] {self._restart_delay_seconds} 秒后整体重启服务...",
                    flush=True,
                )
                self._stop_event.wait(self._restart_delay_seconds)
        finally:
            self._stop_tunnels()
            self._remove_runtime_binaries()


def run_application() -> int:
    environment = os.environ
    app_directory = Path(environment.get("APP_DIR") or APP_DIRECTORY)
    startup_configuration = resolve_startup_configuration(app_directory, environment)
    if startup_configuration.persisted_configuration_error:
        # Do not start a fallback tunnel or an idle status server after discovering
        # an unsafe local file. An operator must repair/remove it deliberately.
        print("[-] 已保存 Token 配置无效或不安全；隧道未启动。", flush=True)
        return 1

    runtime_status = RuntimeStatus()
    web_configuration = WebTokenConfiguration(
        startup_configuration.web_configuration_allowed
    )
    configuration_queue: queue.Queue[TunnelConfiguration] = queue.Queue()
    stop_event = threading.Event()

    try:
        status_server = create_status_server(
            get_status_port(environment),
            web_configuration,
            configuration_queue,
            runtime_status,
            app_directory,
        )
    except OSError:
        print("[-] 无法启动状态页服务。", flush=True)
        return 1

    def request_shutdown(_signal_number: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    status_thread = threading.Thread(
        target=status_server.serve_forever,
        kwargs={"poll_interval": PROCESS_POLL_INTERVAL_SECONDS},
        name="status-http-server",
        daemon=True,
    )
    status_thread.start()

    try:
        if startup_configuration.source == "fallback":
            print("[fallback] 未配置 Cloudflare Token 别名；启用 600 秒限时回退模式。", flush=True)
        elif startup_configuration.source == "saved-configuration":
            print("[saved] 已加载保存的 Token 配置，使用持续运行模式。", flush=True)

        if startup_configuration.configuration is None:
            return 1
        supervisor = TunnelSupervisor(
            startup_configuration.configuration,
            startup_configuration.using_fallback_token,
            configuration_queue,
            runtime_status,
            stop_event,
            app_directory,
            environment,
        )
        supervisor.run()
        return 0
    finally:
        stop_event.set()
        status_server.shutdown()
        status_server.server_close()
        status_thread.join(timeout=HTTP_REQUEST_TIMEOUT_SECONDS)


if __name__ == "__main__":
    sys.exit(run_application())
