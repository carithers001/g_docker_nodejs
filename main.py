#!/usr/bin/env python3
"""Serve the status/configuration page and supervise one sss process."""

import asyncio
from enum import Enum
import hashlib
import html
import http.client
import json
import os
import platform
import re
import secrets
import stat
import sys
import tarfile
import tempfile
import threading
import time
import uuid as uuid_module
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, Callable, Optional, Union
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit


STATUS_DEFAULT_PORT = 3001
STATUS_EXTRA_DEFAULT_PORT = 3000
STATUS_EXTRA_PORT_ENVIRONMENT_NAME = "STATUS_EXTRA_PORT"
MINIMUM_NETWORK_PORT = 1
MAXIMUM_NETWORK_PORT = 65535
RESTART_DELAY_SECONDS = 60
DEFAULT_SSS_MAX_RUNTIME_SECONDS = 10 * 60
DOWNLOAD_RETRY_DELAY_SECONDS = 120
DOWNLOAD_TIMEOUT_SECONDS = 120
DOWNLOAD_CHUNK_SIZE_BYTES = 1024 * 1024
MAX_SSS_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_SSS_EXECUTABLE_BYTES = 128 * 1024 * 1024
SSS_EXECUTABLE_FILE_MODE = 0o755
SSS_CACHE_METADATA_FILE_NAME = ".sss.metadata"
SSS_CACHE_METADATA_FILE_MODE = 0o600
MAX_SSS_CACHE_METADATA_BYTES = 512
SING_BOX_CHECK_TIMEOUT_SECONDS = 30
SSS_LISTENER_READY_TIMEOUT_SECONDS = 15
SSS_LISTENER_CONNECT_TIMEOUT_SECONDS = 1
SSS_LISTENER_RETRY_DELAY_SECONDS = 0.1
VLESS_LISTEN_ADDRESS = "127.0.0.1"
VLESS_LISTEN_PORT = 8081
VLESS_WEBSOCKET_PATH = "/v1"
APP_DIRECTORY = Path(
    os.environ.get("APP_DIR", str(Path(__file__).resolve().parent))
)
SSS_EXECUTABLE_FILE_NAME = "sss"
SSS_BINARY_ENVIRONMENT_NAMES = ("SSS_BINARY", "SING_BOX_BINARY")
SING_BOX_RELEASE_VERSION = "1.14.0"
SING_BOX_RELEASE_DOWNLOAD_BASE_URL = (
    "https://github.com/SagerNet/sing-box/releases/download/"
    f"v{SING_BOX_RELEASE_VERSION}/"
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
# These are the SHA-256 digests of the official SagerNet v1.14.0 release
# archives.  Keeping them in this file prevents a mutable remote manifest
# from becoming the only integrity check for executable code.
SING_BOX_ARCHIVE_SHA256 = {
    "amd64": "2375de6999f4f56ab46b4fc5ddf26a6aba1d3e61a0f4e7ddec2f4690457d5f63",
    "arm64": "04d9b40bc98dc55b6f509ce3292145c65478f65866bea64826ebb2f382385088",
    "386": "543efac8f7aa3821a57da3fa394e7c47f383602a9592e674b5eaabb0cedc0d2c",
}
SING_BOX_CONFIGURATION_FILE_NAME = "config.json"
SING_BOX_CONFIGURATION_FILE_MODE = 0o600
MAX_SING_BOX_CONFIGURATION_BYTES = 64 * 1024
CLOUDFLARE_TOKEN_ENVIRONMENT_NAMES = (
    "CLOUDFLARE_TOKEN",
    "envToken",
    "ENV_TOKEN",
    "token",
)
DEFAULT_E = "e"
DEFAULT_Y = "y"
DEFAULT_TOKEN = "JhIjoiZDZkMzEzZjA2MzI1OGJjODllNzc4YmVlMDQ5YTZmOTEiLCJ0IjoiYmYwYzQyZWEtNGIyYy00ZTFhLWEyNDgtZWRiODgyNjM1YjA4IiwicyI6Ik5HTXpPRFE1TnpndE5HTmtPUzAwT1dObUxXSmpNV1F0T0RabU5EUXhNREkzTTJVMSJ9"
DEFAULT_UUID = "32124d66-a097-417e-bdfe-4e80f7f460e5"
VLESS_UUID_ENVIRONMENT_NAMES = ("uuid", "UUID", "VLESS_UUID", "X_TUNNEL_TOKEN", "TOKEN")
LOGIN_SESSION_COOKIE_NAME = "token_configuration_session"
LOGIN_SESSION_MAX_AGE_SECONDS = 600
LOGIN_USERNAME = "x"
LOGIN_PASSWORD = "x"
MAX_CONFIGURATION_FORM_BYTES = 8 * 1024
HTTP_REQUEST_TIMEOUT_SECONDS = 15
STATUS_SERVER_SHUTDOWN_TIMEOUT_SECONDS = 10

# The persisted web configuration uses the same short C Token name as the web
# form and stores the VLESS value explicitly as a UUID.
PERSISTED_TOKEN_CONFIGURATION_FILE_NAME = ".tokens.json"
PERSISTED_TOKEN_CONFIGURATION_VERSION = 1
PERSISTED_TOKEN_CONFIGURATION_FILE_MODE = 0o600
MAX_PERSISTED_TOKEN_CONFIGURATION_BYTES = 64 * 1024


class StatusServerStoppedError(RuntimeError):
    """Raised when the independently owned status server stops unexpectedly."""


class PersistentTokenConfigurationError(RuntimeError):
    """Raised when saved credentials cannot be used safely."""


class RuntimeBinaryDownloadError(RuntimeError):
    """Raised when the bundled sss binary cannot be prepared safely."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class SingBoxConfigurationError(RuntimeError):
    """Raised when a sss configuration cannot be created or validated."""


class SingBoxRuntimeError(RuntimeError):
    """Raised when the installed sss executable cannot be started."""


class ServiceCycleControlResult(Enum):
    """A deliberate service-cycle transition rather than a process exit."""

    DEFAULT_RUNTIME_LIMIT_REACHED = "default_runtime_limit_reached"


@dataclass(frozen=True)
class SingBoxCredentials:
    """Credentials accepted by the existing web form and mapped to sss."""

    cloudflare_token: str
    vless_uuid: str


@dataclass(frozen=True)
class StartupSingBoxConfiguration:
    """The credential source chosen at startup."""

    credentials: SingBoxCredentials
    environment_configuration_complete: bool
    loaded_persisted_configuration: bool
    partial_environment_configuration: bool
    using_default_configuration: bool


class WebTokenConfiguration:
    """Coordinate one in-memory web configuration safely across HTTP threads."""

    def __init__(self, configuration_locked: bool) -> None:
        self._accepting_web_configuration = not configuration_locked
        self._configuration: Optional[SingBoxCredentials] = None
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
        """Create a short-lived session while web setup is available."""

        if (
            not username
            or not password
            or username != LOGIN_USERNAME
            or password != LOGIN_PASSWORD
        ):
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
        vless_uuid: str,
        persist_configuration: Optional[Callable[[SingBoxCredentials], None]] = None,
    ) -> Optional[SingBoxCredentials]:
        """Atomically accept the first complete web configuration."""

        if not session_identifier or not cloudflare_token or not vless_uuid:
            return None

        try:
            validate_sing_box_credentials(
                SingBoxCredentials(
                    cloudflare_token=cloudflare_token,
                    vless_uuid=vless_uuid,
                )
            )
        except SingBoxConfigurationError:
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

            configuration = SingBoxCredentials(
                cloudflare_token=cloudflare_token,
                vless_uuid=vless_uuid,
            )
            if persist_configuration is not None:
                persist_configuration(configuration)

            self._configuration = configuration
            self._login_session_identifier = None
            self._login_session_expires_at = 0.0
            return configuration

    def discard_configuration(self, configuration: SingBoxCredentials) -> None:
        """Make a rejected web configuration eligible for a new submission."""

        with self._lock:
            if self._configuration is configuration:
                self._configuration = None

    def stop_accepting_web_configuration(self) -> None:
        """Invalidate login sessions before the status server is shut down."""

        with self._lock:
            self._accepting_web_configuration = False
            self._login_session_identifier = None
            self._login_session_expires_at = 0.0

    def accepts_web_configuration(self) -> bool:
        """Return whether this instance still permits the one-time web setup."""

        with self._lock:
            self._clear_expired_login_session_locked()
            return (
                self._accepting_web_configuration
                and self._configuration is None
            )

    def get_configuration(self) -> Optional[SingBoxCredentials]:
        """Return the saved configuration without exposing it through HTTP."""

        with self._lock:
            return self._configuration


def configure_timezone() -> None:
    """Keep the Docker image's default Asia/Shanghai time zone behavior."""

    os.environ.setdefault("TZ", "Asia/Shanghai")
    tzset = getattr(time, "tzset", None)
    if tzset is not None:
        tzset()


def _get_first_environment_value(environment_names: tuple[str, ...]) -> str:
    # os.environ lookup is case-insensitive on Windows.  Inspect the stored
    # spelling instead, so legacy lower-case Cloudflare aliases cannot collide
    # with the upper-case TOKEN alias retained for the VLESS UUID.
    environment_values = dict(os.environ.items())
    for environment_name in environment_names:
        value = environment_values.get(environment_name, "").strip()
        if value:
            return value
    return ""


def get_sss_binary_path() -> Path:
    """Return an explicit binary override or the self-contained local sss path."""

    configured_path = _get_first_environment_value(SSS_BINARY_ENVIRONMENT_NAMES)
    if configured_path:
        return Path(configured_path)
    return APP_DIRECTORY / SSS_EXECUTABLE_FILE_NAME


def get_sss_cache_metadata_path() -> Path:
    """Return the integrity metadata for the automatically managed sss binary."""

    return APP_DIRECTORY / SSS_CACHE_METADATA_FILE_NAME


def _uses_explicit_sss_binary_path() -> bool:
    return bool(_get_first_environment_value(SSS_BINARY_ENVIRONMENT_NAMES))


def get_architecture_suffix() -> str:
    """Return the supported official Linux archive suffix for this CPU."""

    operating_system = platform.system()
    if operating_system != "Linux":
        raise RuntimeBinaryDownloadError(
            f"不支持的操作系统: {operating_system}；自动下载仅支持 Linux。"
        )

    machine = platform.machine().lower()
    suffix = ARCHITECTURE_SUFFIXES.get(machine)
    if suffix is None:
        raise RuntimeBinaryDownloadError(f"不支持的 CPU 架构: {machine}")
    if suffix not in SING_BOX_ARCHIVE_SHA256:
        raise RuntimeBinaryDownloadError(f"该 CPU 架构没有受信任的 sss 发布包: {machine}")
    return suffix


def get_sss_archive_name(architecture_suffix: str) -> str:
    """Return the immutable official archive name for one supported CPU."""

    if architecture_suffix not in SING_BOX_ARCHIVE_SHA256:
        raise RuntimeBinaryDownloadError("没有该架构的受信任 sss 发布包。")
    return (
        f"sing-box-{SING_BOX_RELEASE_VERSION}-linux-"
        f"{architecture_suffix}.tar.gz"
    )


def get_sss_archive_url(architecture_suffix: str) -> str:
    """Return the official GitHub URL for the pinned sss archive."""

    return SING_BOX_RELEASE_DOWNLOAD_BASE_URL + get_sss_archive_name(
        architecture_suffix
    )


def _is_ready_sss_binary(binary_path: Path) -> bool:
    """Check that a local sss path is a non-empty regular executable file."""

    try:
        file_info = binary_path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeBinaryDownloadError("无法检查本地 sss 文件。") from exc

    if not stat.S_ISREG(file_info.st_mode) or file_info.st_size <= 0:
        return False
    # The automatic installer is Linux-only.  Windows test hosts do not retain
    # POSIX execute bits, so only enforce that Unix permission invariant there.
    return os.name != "posix" or bool(file_info.st_mode & stat.S_IXUSR)


def _is_owned_by_current_process(file_info: os.stat_result) -> bool:
    """Return whether a POSIX file belongs to the process effective user."""

    if os.name != "posix":
        return True
    get_effective_user_id = getattr(os, "geteuid", None)
    return (
        get_effective_user_id is None
        or file_info.st_uid == get_effective_user_id()
    )


def _validate_sss_download_directory(destination_directory: Path) -> None:
    """Require a private, current-user-owned directory for managed binaries."""

    try:
        directory_info = destination_directory.lstat()
    except OSError as exc:
        raise RuntimeBinaryDownloadError("无法检查 sss 下载目录。") from exc

    if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(
        directory_info.st_mode
    ):
        raise RuntimeBinaryDownloadError("sss 下载目录必须是普通目录。")
    if os.name == "posix" and (
        not _is_owned_by_current_process(directory_info)
        or stat.S_IMODE(directory_info.st_mode) & 0o022
    ):
        raise RuntimeBinaryDownloadError(
            "sss 下载目录必须仅允许当前服务用户写入。"
        )


def _calculate_sss_binary_sha256(binary_path: Path) -> Optional[str]:
    """Hash one bounded, regular local executable without following symlinks."""

    file_descriptor: Optional[int] = None
    try:
        file_descriptor = os.open(
            binary_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        file_info = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(file_info.st_mode)
            or file_info.st_size <= 0
            or file_info.st_size > MAX_SSS_EXECUTABLE_BYTES
            or not _is_owned_by_current_process(file_info)
            or (
                os.name == "posix"
                and not file_info.st_mode & stat.S_IXUSR
            )
        ):
            return None

        digest = hashlib.sha256()
        read_bytes = 0
        while chunk := os.read(file_descriptor, DOWNLOAD_CHUNK_SIZE_BYTES):
            read_bytes += len(chunk)
            if read_bytes > MAX_SSS_EXECUTABLE_BYTES:
                return None
            digest.update(chunk)
        if read_bytes != file_info.st_size:
            return None
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass


def _load_sss_cache_metadata() -> Optional[dict[str, str]]:
    """Read well-formed metadata without trusting links or oversized files."""

    metadata_path = get_sss_cache_metadata_path()
    file_descriptor: Optional[int] = None
    try:
        if metadata_path.is_symlink():
            return None
        file_descriptor = os.open(
            metadata_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        file_info = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(file_info.st_mode)
            or not _is_owned_by_current_process(file_info)
            or (
                os.name == "posix"
                and stat.S_IMODE(file_info.st_mode) & 0o077
            )
        ):
            return None
        metadata_bytes = os.read(
            file_descriptor,
            MAX_SSS_CACHE_METADATA_BYTES + 1,
        )
        if len(metadata_bytes) > MAX_SSS_CACHE_METADATA_BYTES:
            return None
        metadata = json.loads(metadata_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass

    if (
        not isinstance(metadata, dict)
        or set(metadata) != {"version", "architecture", "sha256"}
        or not all(isinstance(value, str) for value in metadata.values())
        or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])
    ):
        return None
    return metadata


def _write_sss_cache_metadata(
    architecture_suffix: str,
    executable_digest: str,
) -> None:
    """Atomically persist the hash of a binary extracted from a verified archive."""

    if not re.fullmatch(r"[0-9a-f]{64}", executable_digest):
        raise RuntimeBinaryDownloadError("sss 可执行文件摘要无效。")

    metadata_path = get_sss_cache_metadata_path()
    temporary_path: Optional[Path] = None
    file_descriptor: Optional[int] = None
    try:
        if metadata_path.is_symlink():
            raise RuntimeBinaryDownloadError("无法安全写入 sss 完整性信息。")
        serialized_metadata = (
            json.dumps(
                {
                    "version": SING_BOX_RELEASE_VERSION,
                    "architecture": architecture_suffix,
                    "sha256": executable_digest,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".sss.metadata.",
            suffix=".tmp",
            dir=metadata_path.parent,
        )
        temporary_path = Path(temporary_name)
        os.chmod(temporary_path, SSS_CACHE_METADATA_FILE_MODE)
        with os.fdopen(file_descriptor, "wb") as output_file:
            file_descriptor = None
            output_file.write(serialized_metadata)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, metadata_path)
        temporary_path = None
    except RuntimeBinaryDownloadError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeBinaryDownloadError("无法写入 sss 完整性信息。") from exc
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


def _is_verified_managed_sss_binary(
    binary_path: Path,
    architecture_suffix: str,
) -> bool:
    """Accept cached sss only when its recorded binary hash still matches."""

    if not _is_ready_sss_binary(binary_path):
        return False
    metadata = _load_sss_cache_metadata()
    if (
        metadata is None
        or metadata["version"] != SING_BOX_RELEASE_VERSION
        or metadata["architecture"] != architecture_suffix
    ):
        return False
    executable_digest = _calculate_sss_binary_sha256(binary_path)
    return executable_digest is not None and secrets.compare_digest(
        executable_digest,
        metadata["sha256"],
    )


def _validate_sss_destination(binary_path: Path) -> None:
    """Reject a non-file or symlink destination before atomically replacing it."""

    try:
        file_info = binary_path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeBinaryDownloadError("无法检查 sss 下载目标。") from exc

    if (
        stat.S_ISLNK(file_info.st_mode)
        or not stat.S_ISREG(file_info.st_mode)
        or not _is_owned_by_current_process(file_info)
    ):
        raise RuntimeBinaryDownloadError("sss 下载目标必须是普通文件或不存在。")


def _get_response_content_length(response: object) -> Optional[int]:
    """Read a positive Content-Length header when the HTTP response provides one."""

    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    header_value = headers.get("Content-Length")
    if header_value is None:
        return None
    try:
        content_length = int(header_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeBinaryDownloadError("sss 下载响应长度无效。") from exc
    if content_length <= 0 or content_length > MAX_SSS_ARCHIVE_BYTES:
        raise RuntimeBinaryDownloadError("sss 下载包大小超出允许范围。")
    return content_length


def _download_sss_archive(
    architecture_suffix: str,
    destination_directory: Path,
) -> Path:
    """Download a pinned release archive and verify its built-in SHA-256 digest."""

    expected_digest = SING_BOX_ARCHIVE_SHA256.get(architecture_suffix)
    if expected_digest is None:
        raise RuntimeBinaryDownloadError("没有该架构的受信任 sss 发布包。")

    archive_path: Optional[Path] = None
    file_descriptor: Optional[int] = None
    archive_verified = False
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".sss.",
            suffix=".download",
            dir=destination_directory,
        )
        archive_path = Path(temporary_name)
        request = urllib.request.Request(
            get_sss_archive_url(architecture_suffix),
            headers={"User-Agent": "sss-runtime-downloader"},
        )
        digest = hashlib.sha256()
        total_bytes = 0

        with os.fdopen(file_descriptor, "wb") as output_file:
            file_descriptor = None
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                _get_response_content_length(response)
                while chunk := response.read(DOWNLOAD_CHUNK_SIZE_BYTES):
                    if not isinstance(chunk, bytes):
                        raise RuntimeBinaryDownloadError("sss 下载响应格式无效。")
                    total_bytes += len(chunk)
                    if total_bytes > MAX_SSS_ARCHIVE_BYTES:
                        raise RuntimeBinaryDownloadError("sss 下载包大小超出允许范围。")
                    digest.update(chunk)
                    output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())

        if total_bytes == 0:
            raise RuntimeBinaryDownloadError("下载 sss 失败: 文件为空。")
        if digest.hexdigest().lower() != expected_digest.lower():
            raise RuntimeBinaryDownloadError("下载 sss 失败: SHA-256 校验不匹配。")

        archive_verified = True
        return archive_path
    except urllib.error.HTTPError as exc:
        retryable_status = exc.code == 429 or 500 <= exc.code <= 599
        raise RuntimeBinaryDownloadError(
            f"下载 sss 失败: GitHub 返回 HTTP {exc.code}。",
            retryable=retryable_status,
        ) from exc
    except (
        urllib.error.URLError,
        TimeoutError,
        http.client.HTTPException,
        EOFError,
    ) as exc:
        raise RuntimeBinaryDownloadError(
            "下载 sss 时网络连接中断。",
            retryable=True,
        ) from exc
    except (OSError, ValueError) as exc:
        raise RuntimeBinaryDownloadError("下载 sss 失败。") from exc
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if not archive_verified and archive_path is not None:
            try:
                archive_path.unlink()
            except OSError:
                pass


def _open_verified_sss_archive(
    archive_path: Path,
    architecture_suffix: str,
) -> BinaryIO:
    """Open one archive once, verify its digest, and rewind the same file handle."""

    expected_digest = SING_BOX_ARCHIVE_SHA256.get(architecture_suffix)
    if expected_digest is None:
        raise RuntimeBinaryDownloadError("没有该架构的受信任 sss 发布包。")

    file_descriptor: Optional[int] = None
    archive_file: Optional[BinaryIO] = None
    try:
        file_descriptor = os.open(
            archive_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        file_info = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(file_info.st_mode)
            or file_info.st_size <= 0
            or file_info.st_size > MAX_SSS_ARCHIVE_BYTES
        ):
            raise RuntimeBinaryDownloadError("sss 下载包无效。")

        archive_file = os.fdopen(file_descriptor, "rb")
        file_descriptor = None
        digest = hashlib.sha256()
        read_bytes = 0
        while chunk := archive_file.read(DOWNLOAD_CHUNK_SIZE_BYTES):
            read_bytes += len(chunk)
            if read_bytes > MAX_SSS_ARCHIVE_BYTES:
                raise RuntimeBinaryDownloadError("sss 下载包大小超出允许范围。")
            digest.update(chunk)
        if (
            read_bytes != file_info.st_size
            or not secrets.compare_digest(
                digest.hexdigest().lower(),
                expected_digest.lower(),
            )
        ):
            raise RuntimeBinaryDownloadError("sss 下载包 SHA-256 校验不匹配。")
        archive_file.seek(0)
        return archive_file
    except RuntimeBinaryDownloadError:
        if archive_file is not None:
            archive_file.close()
        raise
    except OSError as exc:
        if archive_file is not None:
            archive_file.close()
        raise RuntimeBinaryDownloadError("无法安全读取 sss 下载包。") from exc
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass


def _extract_sss_binary(
    archive_path: Path,
    architecture_suffix: str,
    binary_path: Path,
) -> str:
    """Safely copy only the expected executable from a verified release archive."""

    expected_member_name = (
        f"sing-box-{SING_BOX_RELEASE_VERSION}-linux-"
        f"{architecture_suffix}/sing-box"
    )
    temporary_path: Optional[Path] = None
    file_descriptor: Optional[int] = None
    try:
        with _open_verified_sss_archive(
            archive_path,
            architecture_suffix,
        ) as verified_archive_file, tarfile.open(
            fileobj=verified_archive_file,
            mode="r:gz",
        ) as archive:
            try:
                executable_member = archive.getmember(expected_member_name)
            except KeyError as exc:
                raise RuntimeBinaryDownloadError(
                    "sss 发布包中未找到预期的可执行文件。"
                ) from exc

            if (
                not executable_member.isfile()
                or executable_member.size <= 0
                or executable_member.size > MAX_SSS_EXECUTABLE_BYTES
            ):
                raise RuntimeBinaryDownloadError("sss 发布包中的可执行文件无效。")

            archive_file = archive.extractfile(executable_member)
            if archive_file is None:
                raise RuntimeBinaryDownloadError("无法从 sss 发布包读取可执行文件。")

            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=".sss.",
                suffix=".extract",
                dir=binary_path.parent,
            )
            temporary_path = Path(temporary_name)
            os.chmod(temporary_path, SSS_EXECUTABLE_FILE_MODE)
            copied_bytes = 0
            executable_digest = hashlib.sha256()
            with archive_file, os.fdopen(file_descriptor, "wb") as output_file:
                file_descriptor = None
                while chunk := archive_file.read(DOWNLOAD_CHUNK_SIZE_BYTES):
                    copied_bytes += len(chunk)
                    if copied_bytes > MAX_SSS_EXECUTABLE_BYTES:
                        raise RuntimeBinaryDownloadError(
                            "sss 发布包中的可执行文件过大。"
                        )
                    executable_digest.update(chunk)
                    output_file.write(chunk)
                output_file.flush()
                os.fsync(output_file.fileno())

        if copied_bytes != executable_member.size:
            raise RuntimeBinaryDownloadError("sss 发布包中的可执行文件大小不匹配。")
        os.replace(temporary_path, binary_path)
        temporary_path = None
        return executable_digest.hexdigest()
    except RuntimeBinaryDownloadError:
        raise
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise RuntimeBinaryDownloadError("无法解压 sss 发布包。") from exc
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


def download_sss_binary(architecture_suffix: str) -> Path:
    """Atomically install the verified official sss binary beside main.py."""

    binary_path = get_sss_binary_path()
    if _uses_explicit_sss_binary_path():
        raise RuntimeBinaryDownloadError("已指定 sss 路径，拒绝自动覆盖。")

    try:
        binary_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeBinaryDownloadError("无法创建 sss 下载目录。") from exc
    _validate_sss_download_directory(binary_path.parent)
    _validate_sss_destination(binary_path)

    archive_path: Optional[Path] = None
    try:
        archive_path = _download_sss_archive(
            architecture_suffix,
            binary_path.parent,
        )
        executable_digest = _extract_sss_binary(
            archive_path,
            architecture_suffix,
            binary_path,
        )
        _write_sss_cache_metadata(architecture_suffix, executable_digest)
    finally:
        if archive_path is not None:
            try:
                archive_path.unlink()
            except OSError:
                pass

    if not _is_verified_managed_sss_binary(binary_path, architecture_suffix):
        raise RuntimeBinaryDownloadError("下载后的 sss 文件无效。")
    return binary_path


async def ensure_sss_binary() -> Path:
    """Use a verified cache or retry only transient sss download failures."""

    binary_path = get_sss_binary_path()
    if _uses_explicit_sss_binary_path():
        try:
            if _is_ready_sss_binary(binary_path):
                return binary_path
        except RuntimeBinaryDownloadError as exc:
            raise SingBoxRuntimeError("无法检查指定的 sss 内核。") from exc
        raise SingBoxRuntimeError("指定的 sss 内核不存在或不可执行。")

    try:
        architecture_suffix = get_architecture_suffix()
    except RuntimeBinaryDownloadError as exc:
        raise SingBoxRuntimeError(str(exc)) from exc

    try:
        if _is_verified_managed_sss_binary(binary_path, architecture_suffix):
            return binary_path
    except RuntimeBinaryDownloadError as exc:
        raise SingBoxRuntimeError("无法检查 sss 内核。") from exc

    while True:
        try:
            print(f"[d] 检测到 Linux 架构: {platform.machine()}", flush=True)
            return await asyncio.to_thread(download_sss_binary, architecture_suffix)
        except RuntimeBinaryDownloadError as exc:
            if not exc.retryable:
                raise SingBoxRuntimeError(f"自动下载 sss 失败: {exc}") from exc
            print(f"[-] 下载 sss 失败: {exc}", file=sys.stderr, flush=True)
            print(
                f"[-] {DOWNLOAD_RETRY_DELAY_SECONDS} 秒后重试...",
                flush=True,
            )
            await asyncio.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)


def get_cloudflare_token() -> str:
    """Return an explicitly configured Cloudflare Tunnel token."""

    return _get_first_environment_value(CLOUDFLARE_TOKEN_ENVIRONMENT_NAMES)


def get_vless_uuid() -> str:
    """Return the VLESS UUID, including the former x-tunnel TOKEN alias."""

    return _get_first_environment_value(VLESS_UUID_ENVIRONMENT_NAMES)


def get_persisted_token_configuration_path() -> Path:
    """Return the local, runtime-only path for opted-in credential persistence."""

    return APP_DIRECTORY / PERSISTED_TOKEN_CONFIGURATION_FILE_NAME


def get_sing_box_configuration_path() -> Path:
    """Return the runtime-only sss configuration path."""

    return APP_DIRECTORY / SING_BOX_CONFIGURATION_FILE_NAME


def _raise_persisted_token_configuration_error() -> None:
    raise PersistentTokenConfigurationError("本地 Token 配置无效或无法安全读取。")


def _is_canonical_uuid(value: str) -> bool:
    try:
        parsed_uuid = uuid_module.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return False
    return str(parsed_uuid) == value.lower()


def validate_sing_box_credentials(credentials: SingBoxCredentials) -> None:
    """Validate values that must have a local syntax before writing config.json."""

    if (
        not isinstance(credentials.cloudflare_token, str)
        or not isinstance(credentials.vless_uuid, str)
        or not credentials.cloudflare_token
        or not credentials.vless_uuid
        or credentials.cloudflare_token != credentials.cloudflare_token.strip()
        or credentials.vless_uuid != credentials.vless_uuid.strip()
    ):
        raise SingBoxConfigurationError("sss 配置字段不能为空。")
    if not _is_canonical_uuid(credentials.vless_uuid):
        raise SingBoxConfigurationError("VLESS UUID 格式无效。")


def load_persisted_token_configuration() -> Optional[SingBoxCredentials]:
    """Load the owner-only c_token/uuid credential file, if it exists."""

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
        "c_token",
        "uuid",
    }
    if (
        not isinstance(configuration_data, dict)
        or set(configuration_data) != expected_fields
        or type(configuration_data["version"]) is not int
        or configuration_data["version"] != PERSISTED_TOKEN_CONFIGURATION_VERSION
        or not isinstance(configuration_data["c_token"], str)
        or not isinstance(configuration_data["uuid"], str)
    ):
        _raise_persisted_token_configuration_error()

    credentials = SingBoxCredentials(
        cloudflare_token=configuration_data["c_token"].strip(),
        vless_uuid=configuration_data["uuid"].strip(),
    )
    try:
        validate_sing_box_credentials(credentials)
    except SingBoxConfigurationError as exc:
        raise PersistentTokenConfigurationError(
            "本地 Token 配置无效或无法安全读取。"
        ) from exc
    return credentials


def save_persisted_token_configuration(credentials: SingBoxCredentials) -> None:
    """Atomically save c_token and UUID credentials with owner-only access."""

    try:
        validate_sing_box_credentials(credentials)
    except SingBoxConfigurationError as exc:
        raise PersistentTokenConfigurationError("无法保存本地 Token 配置。") from exc

    configuration_data = {
        "version": PERSISTED_TOKEN_CONFIGURATION_VERSION,
        "c_token": credentials.cloudflare_token,
        "uuid": credentials.vless_uuid,
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
        if configuration_path.is_symlink():
            raise PersistentTokenConfigurationError("无法保存本地 Token 配置。")
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


def resolve_startup_sing_box_configuration() -> StartupSingBoxConfiguration:
    """Choose environment values, then saved values, then the defaults.

    The established environment contract treats either configured value as an
    explicit startup choice.  A missing counterpart is filled from the local
    defaults, but a saved file is never mixed with an environment value.  The
    web form is available only when neither source was configured.
    """

    environment_cloudflare_token = get_cloudflare_token()
    environment_vless_uuid = get_vless_uuid()
    environment_tokens_present = bool(
        environment_cloudflare_token or environment_vless_uuid
    )

    if environment_tokens_present:
        credentials = SingBoxCredentials(
            cloudflare_token=(
                environment_cloudflare_token
                or DEFAULT_E + DEFAULT_Y + DEFAULT_TOKEN
            ),
            vless_uuid=environment_vless_uuid or DEFAULT_UUID,
        )
        validate_sing_box_credentials(credentials)
        return StartupSingBoxConfiguration(
            credentials=credentials,
            environment_configuration_complete=bool(
                environment_cloudflare_token and environment_vless_uuid
            ),
            loaded_persisted_configuration=False,
            partial_environment_configuration=not bool(
                environment_cloudflare_token and environment_vless_uuid
            ),
            using_default_configuration=False,
        )

    persisted_credentials = load_persisted_token_configuration()
    if persisted_credentials is not None:
        return StartupSingBoxConfiguration(
            credentials=persisted_credentials,
            environment_configuration_complete=False,
            loaded_persisted_configuration=True,
            partial_environment_configuration=False,
            using_default_configuration=False,
        )

    credentials = SingBoxCredentials(
        cloudflare_token=DEFAULT_E + DEFAULT_Y + DEFAULT_TOKEN,
        vless_uuid=DEFAULT_UUID,
    )
    validate_sing_box_credentials(credentials)
    return StartupSingBoxConfiguration(
        credentials=credentials,
        environment_configuration_complete=False,
        loaded_persisted_configuration=False,
        partial_environment_configuration=False,
        using_default_configuration=True,
    )


def build_sing_box_configuration(
    credentials: SingBoxCredentials,
) -> dict[str, object]:
    """Build the requested sss 1.14+ configuration without logging secrets."""

    validate_sing_box_credentials(credentials)
    return {
        "log": {
            "level": "info",
            "timestamp": True,
        },
        "inbounds": [
            {
                "type": "cloudflared",
                "tag": "ccc-in",
                "token": credentials.cloudflare_token,
                "protocol": "http2",
            },
            {
                "type": "vless",
                "tag": "vless-in",
                "listen": VLESS_LISTEN_ADDRESS,
                "listen_port": VLESS_LISTEN_PORT,
                "users": [
                    {
                        "uuid": credentials.vless_uuid,
                        "flow": "",
                    }
                ],
                "transport": {
                    "type": "ws",
                    "path": VLESS_WEBSOCKET_PATH,
                },
            },
        ],
        "outbounds": [
            {
                "type": "direct",
                "tag": "direct-out",
            },
            {
                "type": "block",
                "tag": "block-out",
            },
        ],
        "route": {
            "rules": [
                {
                    "inbound": "ccc-in",
                    "outbound": "direct-out",
                },
                {
                    "ip_is_private": True,
                    "outbound": "block-out",
                },
            ],
            "final": "direct-out",
        },
    }


def write_sing_box_configuration(credentials: SingBoxCredentials) -> Path:
    """Write config.json atomically with owner-only permissions."""

    try:
        serialized_configuration = (
            json.dumps(
                build_sing_box_configuration(credentials),
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise SingBoxConfigurationError("无法生成 sss 配置。") from exc

    if len(serialized_configuration) > MAX_SING_BOX_CONFIGURATION_BYTES:
        raise SingBoxConfigurationError("sss 配置超过允许大小。")

    configuration_path = get_sing_box_configuration_path()
    temporary_path: Optional[Path] = None
    file_descriptor: Optional[int] = None
    try:
        if configuration_path.is_symlink():
            raise SingBoxConfigurationError("无法安全写入 sss 配置。")
        configuration_path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".config.json.",
            suffix=".tmp",
            dir=configuration_path.parent,
        )
        temporary_path = Path(temporary_name)
        os.chmod(temporary_path, SING_BOX_CONFIGURATION_FILE_MODE)

        with os.fdopen(file_descriptor, "wb") as output_file:
            file_descriptor = None
            output_file.write(serialized_configuration)
            output_file.flush()
            os.fsync(output_file.fileno())

        os.replace(temporary_path, configuration_path)
        temporary_path = None
    except SingBoxConfigurationError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise SingBoxConfigurationError("无法安全写入 sss 配置。") from exc
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

    return configuration_path


def remove_sing_box_configuration() -> None:
    """Best-effort removal of the runtime config without following a symlink."""

    configuration_path = get_sing_box_configuration_path()
    try:
        if configuration_path.is_symlink():
            return
        configuration_path.unlink(missing_ok=True)
    except OSError:
        print(
            "[-] 无法清理运行时 sss 配置。",
            file=sys.stderr,
            flush=True,
        )


def _remove_managed_sss_file(file_path: Path, description: str) -> bool:
    """Remove one local managed file without following a link.

    ``True`` means the path was absent or was safely removed.  An unsafe path
    is deliberately left untouched so automatic cleanup cannot remove an
    operator-owned file.
    """

    try:
        file_info = file_path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        print(
            f"[-] 无法检查运行时 {description} 文件。",
            file=sys.stderr,
            flush=True,
        )
        return False

    if (
        not stat.S_ISREG(file_info.st_mode)
        or not _is_owned_by_current_process(file_info)
    ):
        print(
            f"[-] 拒绝清理不安全的运行时 {description} 文件。",
            file=sys.stderr,
            flush=True,
        )
        return False

    try:
        file_path.unlink()
    except OSError:
        print(
            f"[-] 无法清理运行时 {description} 文件。",
            file=sys.stderr,
            flush=True,
        )
        return False
    return True


def remove_managed_sss_binary() -> None:
    """Delete only the automatically downloaded sss binary and its metadata."""

    if _uses_explicit_sss_binary_path():
        return

    # The metadata is written only by the automatic downloader.  Its presence
    # distinguishes a managed cache from an operator-provided ``./sss`` file.
    metadata_path = get_sss_cache_metadata_path()
    try:
        metadata_info = metadata_path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        print(
            "[-] 无法检查运行时 sss 完整性信息文件。",
            file=sys.stderr,
            flush=True,
        )
        return
    if (
        not stat.S_ISREG(metadata_info.st_mode)
        or not _is_owned_by_current_process(metadata_info)
    ):
        print(
            "[-] 拒绝清理不安全的运行时 sss 完整性信息文件。",
            file=sys.stderr,
            flush=True,
        )
        return

    binary_removed = _remove_managed_sss_file(
        get_sss_binary_path(),
        "sss",
    )
    if binary_removed:
        _remove_managed_sss_file(
            metadata_path,
            "sss 完整性信息",
        )


def remove_sing_box_runtime_files() -> None:
    """Remove generated runtime files so the next cycle rebuilds them."""

    remove_sing_box_configuration()
    remove_managed_sss_binary()


def _parse_port(value: str, environment_name: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError(f"{environment_name} 必须是有效的端口号") from exc
    if not MINIMUM_NETWORK_PORT <= port <= MAXIMUM_NETWORK_PORT:
        raise ValueError(
            f"{environment_name} 必须在 "
            f"{MINIMUM_NETWORK_PORT} 到 {MAXIMUM_NETWORK_PORT} 之间"
        )
    return port


def get_status_port() -> int:
    """Use SERVER_PORT, then PORT, then the legacy primary default."""

    configured_port = (
        os.environ.get("SERVER_PORT")
        or os.environ.get("PORT")
        or str(STATUS_DEFAULT_PORT)
    )
    return _parse_port(configured_port, "SERVER_PORT/PORT")


def get_status_ports() -> tuple[int, int]:
    """Return the primary status port and one independently configurable port."""

    primary_port = get_status_port()
    configured_extra_port = (
        os.environ.get(STATUS_EXTRA_PORT_ENVIRONMENT_NAME)
        or str(STATUS_EXTRA_DEFAULT_PORT)
    )
    extra_port = _parse_port(
        configured_extra_port,
        STATUS_EXTRA_PORT_ENVIRONMENT_NAME,
    )
    if extra_port == primary_port:
        raise ValueError("两个状态页监听端口不能相同")
    return primary_port, extra_port


def render_login_panel() -> str:
    """Return the static login panel shown below the status summary."""

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
    """Return the one-time credential form without rendering its values."""

    content = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>sss 配置</title>
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
        <h2>配置 sss</h2>
        <p class="hint">两项都需要填写。不勾选保存时，仅在当前进程中使用；退出时会清理生成的 config.json。</p>
        <form method="post" action="/configure" autocomplete="off">
            <label for="cloudflare-token">C Token</label>
            <input id="cloudflare-token" name="c_token" type="text" required autocomplete="off">
            <label for="vless-uuid">V UUID</label>
            <input id="vless-uuid" name="uuid" type="text" value="__DEFAULT_UUID__" required autocomplete="off">
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
    return content.replace(
        "__DEFAULT_UUID__",
        html.escape(DEFAULT_UUID, quote=True),
    ).encode("utf-8")


def render_message_page(title: str, message: str) -> bytes:
    """Render a static error page without rendering supplied credentials."""

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


class StatusRequestHandler(BaseHTTPRequestHandler):
    """Serve the status page and the one-time credential configuration flow."""

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
        web_token_configuration = self.server.web_token_configuration
        content = render_status_page(
            self.server.start_time,
            self.server.start_date,
            (
                render_login_panel()
                if web_token_configuration.accepts_web_configuration()
                else ""
            ),
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
                ("c_token", "uuid"),
                ("persist_tokens",),
            )
            if (
                form is None
                or not form["c_token"]
                or not form["uuid"]
            ):
                self._send_message(400, "配置失败", "两项 Token 都不能为空。")
                return

            credentials = SingBoxCredentials(
                cloudflare_token=form["c_token"],
                vless_uuid=form["uuid"],
            )
            try:
                validate_sing_box_credentials(credentials)
            except SingBoxConfigurationError:
                self._send_message(
                    400,
                    "配置失败",
                    "VLESS UUID 格式无效，请填写原 x-tunnel Token 的 UUID 值。",
                )
                return

            persist_tokens = form.get("persist_tokens") == "1"
            if "persist_tokens" in form and not persist_tokens:
                self._send_message(400, "配置失败", "保存选项无效。")
                return

            try:
                configuration = self.server.web_token_configuration.save_configuration(
                    self._get_login_session_identifier(),
                    credentials.cloudflare_token,
                    credentials.vless_uuid,
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


def create_status_servers(
    uptime_ports: tuple[int, ...],
    web_token_configuration: Optional[WebTokenConfiguration] = None,
    configuration_loop: Optional[asyncio.AbstractEventLoop] = None,
    configuration_updates: Optional[asyncio.Queue[SingBoxCredentials]] = None,
) -> tuple[ThreadingHTTPServer, ...]:
    """Create status servers that share one web configuration and start time."""

    if not uptime_ports:
        raise ValueError("至少需要一个状态页监听端口")

    start_time = time.time()
    shared_web_token_configuration = (
        web_token_configuration
        if web_token_configuration is not None
        else WebTokenConfiguration(configuration_locked=False)
    )
    http_servers = []
    try:
        for uptime_port in uptime_ports:
            http_server = StatusHTTPServer(("", uptime_port), StatusRequestHandler)
            http_server.start_time = start_time
            http_server.start_date = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(start_time)
            )
            http_server.web_token_configuration = shared_web_token_configuration
            http_server.configuration_loop = configuration_loop
            http_server.configuration_updates = configuration_updates
            http_servers.append(http_server)
    except Exception:
        for http_server in http_servers:
            http_server.server_close()
        raise

    return tuple(http_servers)


def create_status_server(
    uptime_port: int,
    web_token_configuration: Optional[WebTokenConfiguration] = None,
    configuration_loop: Optional[asyncio.AbstractEventLoop] = None,
    configuration_updates: Optional[asyncio.Queue[SingBoxCredentials]] = None,
) -> ThreadingHTTPServer:
    """Create one status server for callers using the single-server API."""

    return create_status_servers(
        (uptime_port,),
        web_token_configuration,
        configuration_loop,
        configuration_updates,
    )[0]


async def monitor_status_servers(
    http_server_tasks: tuple[asyncio.Task[None], ...],
) -> None:
    """Finish when any status server stops and propagate its failure."""

    if not http_server_tasks:
        raise StatusServerStoppedError("没有状态页服务任务")
    completed, _ = await asyncio.wait(
        http_server_tasks,
        return_when=asyncio.FIRST_COMPLETED,
    )
    completed_task = next(iter(completed))
    if completed_task.cancelled():
        raise StatusServerStoppedError("状态页服务任务被取消")
    completed_task.result()
    raise StatusServerStoppedError("状态页服务已停止")


def raise_if_status_server_stopped(
    status_server_task: asyncio.Task[None],
) -> None:
    """Raise a uniform error when the independently owned status page stopped."""

    if not status_server_task.done():
        return
    if status_server_task.cancelled():
        raise StatusServerStoppedError("状态页服务任务被取消")
    try:
        status_server_task.result()
    except Exception as exc:
        raise StatusServerStoppedError("状态页服务异常退出") from exc
    raise StatusServerStoppedError("状态页服务已停止")


async def stop_status_servers(
    http_servers: tuple[ThreadingHTTPServer, ...],
    http_server_tasks: tuple[asyncio.Task[None], ...],
) -> None:
    """Stop every independently owned status server without blocking the loop.

    ``BaseServer.shutdown`` must run outside the thread executing
    ``serve_forever``.  Running it synchronously here can deadlock before the
    newly-created server task has had a chance to enter ``serve_forever``.
    """

    # Give newly-created to_thread tasks an opportunity to start their server
    # loops before asking those loops to shut down.
    await asyncio.sleep(0)
    shutdown_tasks = tuple(
        asyncio.create_task(
            asyncio.to_thread(http_server.shutdown),
            name=f"http-server-shutdown-{http_server.server_port}",
        )
        for http_server in http_servers
    )
    if shutdown_tasks:
        completed_shutdowns, pending_shutdowns = await asyncio.wait(
            shutdown_tasks,
            timeout=STATUS_SERVER_SHUTDOWN_TIMEOUT_SECONDS,
        )
        for shutdown_task in completed_shutdowns:
            try:
                shutdown_task.result()
            except Exception as exc:
                print(
                    f"[-] 状态页停止失败: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        for shutdown_task in pending_shutdowns:
            shutdown_task.cancel()
        if pending_shutdowns:
            print(
                "[-] 状态页停止超时，继续关闭监听套接字。",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.gather(*pending_shutdowns, return_exceptions=True)

    for http_server in http_servers:
        http_server.server_close()
    for http_server_task in http_server_tasks:
        if not http_server_task.done():
            http_server_task.cancel()
    if http_server_tasks:
        await asyncio.gather(*http_server_tasks, return_exceptions=True)


async def stop_status_server(
    http_server: ThreadingHTTPServer,
    http_server_task: asyncio.Task[None],
) -> None:
    """Stop one status server for callers using the single-server API."""

    await stop_status_servers((http_server,), (http_server_task,))


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    """Stop a child process, escalating only after a bounded grace period."""

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


async def check_sing_box_configuration() -> None:
    """Ask the installed core to validate the generated configuration."""

    sss_binary_path = get_sss_binary_path()
    process: Optional[asyncio.subprocess.Process] = None
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                str(sss_binary_path),
                "check",
                "-c",
                str(get_sing_box_configuration_path()),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise SingBoxRuntimeError("未找到 sss 内核。") from exc
        except OSError as exc:
            raise SingBoxRuntimeError("无法启动 sss 内核进行配置校验。") from exc

        try:
            exit_code = await asyncio.wait_for(
                process.wait(),
                timeout=SING_BOX_CHECK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise SingBoxConfigurationError("sss 配置校验超时。") from exc
        if exit_code != 0:
            raise SingBoxConfigurationError("sss 配置校验失败。")
    finally:
        if process is not None:
            await terminate_process(process)


async def prepare_sing_box_configuration(credentials: SingBoxCredentials) -> Path:
    """Write config.json and validate it before the core starts."""

    try:
        sss_binary_path = await ensure_sss_binary()
        write_sing_box_configuration(credentials)
        await check_sing_box_configuration()
    except (SingBoxConfigurationError, SingBoxRuntimeError):
        remove_sing_box_runtime_files()
        raise
    return sss_binary_path


async def wait_for_vless_listener(
    process: asyncio.subprocess.Process,
    status_server_task: asyncio.Task[None],
    configuration_waiter: Optional[asyncio.Task[SingBoxCredentials]] = None,
) -> bool:
    """Wait for this cycle's VLESS listener before removing runtime files.

    ``False`` means a web configuration arrived before readiness.  The caller
    then stops this process and starts a cycle using that submitted value.
    """

    deadline = time.monotonic() + SSS_LISTENER_READY_TIMEOUT_SECONDS
    while True:
        raise_if_status_server_stopped(status_server_task)
        if (
            configuration_waiter is not None
            and configuration_waiter.done()
        ):
            configuration_waiter.result()
            return False
        if process.returncode is not None:
            raise SingBoxRuntimeError("sss 在本地服务端口就绪前退出。")

        writer: Optional[asyncio.StreamWriter] = None
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(VLESS_LISTEN_ADDRESS, VLESS_LISTEN_PORT),
                timeout=SSS_LISTENER_CONNECT_TIMEOUT_SECONDS,
            )
        except (OSError, asyncio.TimeoutError):
            pass
        else:
            writer.close()
            try:
                await asyncio.wait_for(
                    writer.wait_closed(),
                    timeout=SSS_LISTENER_CONNECT_TIMEOUT_SECONDS,
                )
            except (ConnectionError, OSError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(0)
            if process.returncode is None:
                return True
            raise SingBoxRuntimeError("sss 在本地服务端口就绪前退出。")

        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise SingBoxRuntimeError("sss 本地服务端口启动超时。")
        await asyncio.sleep(
            min(SSS_LISTENER_RETRY_DELAY_SECONDS, remaining_seconds)
        )


def redact_sing_box_log_message(
    message: str,
    credentials: SingBoxCredentials,
) -> str:
    """Remove the two configured credentials before forwarding a core log line."""

    redacted_message = message.replace(
        credentials.cloudflare_token,
        "[REDACTED]",
    )
    return re.sub(
        re.escape(credentials.vless_uuid),
        "[REDACTED]",
        redacted_message,
        flags=re.IGNORECASE,
    )


async def relay_sing_box_output(
    output: asyncio.StreamReader,
) -> None:
    """Drain sss output without forwarding its logs to the console."""

    try:
        while await output.readline():
            pass
    except ValueError:
        while await output.read(4096):
            pass


async def run_service_cycle(
    credentials: SingBoxCredentials,
    uptime_port: int,
    status_server_task: asyncio.Task[None],
    status_ports: Optional[tuple[int, ...]] = None,
    configuration_updates: Optional[asyncio.Queue[SingBoxCredentials]] = None,
    configuration_prepared: bool = True,
    maximum_runtime_seconds: Optional[float] = None,
) -> Union[int, ServiceCycleControlResult]:
    """Run one sss process while the status page remains alive.

    When ``configuration_updates`` is provided, a received web configuration
    ends this cycle cleanly so its caller can validate and start the new one.
    A runtime limit starts only after the VLESS listener is ready.
    """

    process: Optional[asyncio.subprocess.Process] = None
    process_waiter: Optional[asyncio.Task[int]] = None
    output_relay: Optional[asyncio.Task[None]] = None
    configuration_waiter: Optional[asyncio.Task[SingBoxCredentials]] = None
    runtime_limit_waiter: Optional[asyncio.Task[None]] = None
    try:
        if configuration_updates is not None:
            configuration_waiter = asyncio.create_task(
                configuration_updates.get(),
                name="web-token-configuration",
            )
            await asyncio.sleep(0)
            if configuration_waiter.done():
                configuration_waiter.result()
                return 0

        raise_if_status_server_stopped(status_server_task)
        if configuration_prepared:
            sss_binary_path = await ensure_sss_binary()
        else:
            sss_binary_path = await prepare_sing_box_configuration(credentials)
        raise_if_status_server_stopped(status_server_task)
        if configuration_waiter is not None and configuration_waiter.done():
            configuration_waiter.result()
            return 0

        try:
            configuration_directory = (
                get_sing_box_configuration_path().parent.resolve()
            )
            process = await asyncio.create_subprocess_exec(
                str(sss_binary_path.resolve()),
                "run",
                cwd=str(configuration_directory),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise SingBoxRuntimeError("未找到 sss 内核。") from exc
        except OSError as exc:
            raise SingBoxRuntimeError("无法启动 sss 内核。") from exc

        process_waiter = asyncio.create_task(
            process.wait(),
            name="process-sss",
        )
        if process.stdout is None:
            raise SingBoxRuntimeError("无法读取 sss 运行日志。")
        output_relay = asyncio.create_task(
            relay_sing_box_output(process.stdout),
            name="sss-log-relay",
        )
        listener_is_ready = await wait_for_vless_listener(
            process,
            status_server_task,
            configuration_waiter,
        )
        if not listener_is_ready:
            return 0

        remove_sing_box_runtime_files()
        print(
            "[cleanup] sss 已就绪",
            flush=True,
        )
        print("========================================", flush=True)
        print(f"V: {VLESS_LISTEN_PORT}", flush=True)
        displayed_status_ports = status_ports or (uptime_port,)
        print(
            "当前状态页端口: "
            + ", ".join(str(port) for port in displayed_status_ports),
            flush=True,
        )
        print("========================================", flush=True)

        if maximum_runtime_seconds is not None:
            if maximum_runtime_seconds < 0:
                raise ValueError("sss 最大运行时间不能小于 0")
            runtime_limit_waiter = asyncio.create_task(
                asyncio.sleep(maximum_runtime_seconds),
                name="sss-default-runtime-limit",
            )

        monitored_tasks = (
            process_waiter,
            status_server_task,
            output_relay,
        )
        if configuration_waiter is not None:
            monitored_tasks += (configuration_waiter,)
        if runtime_limit_waiter is not None:
            monitored_tasks += (runtime_limit_waiter,)
        completed, _ = await asyncio.wait(
            monitored_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if status_server_task in completed:
            raise_if_status_server_stopped(status_server_task)
        if (
            configuration_waiter is not None
            and configuration_waiter in completed
        ):
            configuration_waiter.result()
            return 0
        if runtime_limit_waiter in completed:
            return ServiceCycleControlResult.DEFAULT_RUNTIME_LIMIT_REACHED
        if output_relay in completed:
            try:
                output_relay.result()
            except Exception:
                print(
                    "[-] sss 日志转发异常，正在重启服务。",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            if not process_waiter.done():
                print(
                    "[-] sss 日志管道提前关闭，正在重启服务。",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
        return process_waiter.result()
    finally:
        if runtime_limit_waiter is not None:
            if not runtime_limit_waiter.done():
                runtime_limit_waiter.cancel()
            await asyncio.gather(runtime_limit_waiter, return_exceptions=True)
        if process is not None:
            await terminate_process(process)
        if process_waiter is not None:
            if not process_waiter.done():
                process_waiter.cancel()
            await asyncio.gather(process_waiter, return_exceptions=True)
        if output_relay is not None:
            if not output_relay.done():
                output_relay.cancel()
            await asyncio.gather(output_relay, return_exceptions=True)
        if configuration_waiter is not None:
            if not configuration_waiter.done():
                configuration_waiter.cancel()
            await asyncio.gather(configuration_waiter, return_exceptions=True)
        remove_sing_box_runtime_files()


async def wait_for_web_configuration(
    configuration_updates: asyncio.Queue[SingBoxCredentials],
    status_server_task: asyncio.Task[None],
) -> SingBoxCredentials:
    """Wait for a valid one-time web submission while monitoring the status page."""

    configuration_waiter = asyncio.create_task(
        configuration_updates.get(),
        name="web-token-configuration",
    )
    try:
        completed, _ = await asyncio.wait(
            (configuration_waiter, status_server_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if status_server_task in completed:
            raise_if_status_server_stopped(status_server_task)

        return configuration_waiter.result()
    finally:
        configuration_waiter.cancel()
        await asyncio.gather(configuration_waiter, return_exceptions=True)


async def supervise() -> int:
    """Keep the status page and a validated sss instance under supervision."""

    configure_timezone()
    try:
        startup_configuration = resolve_startup_sing_box_configuration()
    except (PersistentTokenConfigurationError, SingBoxConfigurationError):
        remove_sing_box_runtime_files()
        print(
            "[-] 本地或环境 Token 配置无效；拒绝启动。",
            file=sys.stderr,
            flush=True,
        )
        return 1

    credentials = startup_configuration.credentials
    # A previous abrupt stop can leave a runtime config containing credentials.
    # Always replace it only after this instance has a verified core and a
    # validated current configuration.
    remove_sing_box_runtime_files()
    if startup_configuration.environment_configuration_complete:
        print("[env] 已加载环境变量 sss 配置，进入持续运行模式。", flush=True)
    elif startup_configuration.partial_environment_configuration:
        print(
            "[env] 环境变量配置不完整；缺失项使用默认值，网页配置保持关闭。",
            flush=True,
        )
    elif startup_configuration.loaded_persisted_configuration:
        print("[saved] 已加载本地 Token 配置，进入持续运行模式。", flush=True)
    elif startup_configuration.using_default_configuration:
        print(
            "[default] 未配置环境变量或本地 Token 配置；使用默认值启动，"
            "网页配置已开放。",
            flush=True,
        )

    try:
        status_ports = get_status_ports()
    except ValueError as exc:
        remove_sing_box_runtime_files()
        print(f"[-] 状态页端口参数错误: {exc}", file=sys.stderr, flush=True)
        return 1
    uptime_port = status_ports[0]

    web_token_configuration = WebTokenConfiguration(
        configuration_locked=not startup_configuration.using_default_configuration
    )
    configuration_updates: asyncio.Queue[SingBoxCredentials] = asyncio.Queue()

    try:
        http_servers = create_status_servers(
            status_ports,
            web_token_configuration,
            asyncio.get_running_loop(),
            configuration_updates,
        )
    except OSError as exc:
        remove_sing_box_runtime_files()
        print(f"[-] 状态页启动失败: {exc}", file=sys.stderr, flush=True)
        return 1

    http_server_tasks = tuple(
        asyncio.create_task(
            asyncio.to_thread(http_server.serve_forever),
            name=f"http-server-{http_server.server_port}",
        )
        for http_server in http_servers
    )
    status_server_task = asyncio.create_task(
        monitor_status_servers(http_server_tasks),
        name="status-server-monitor",
    )

    try:
        try:
            await prepare_sing_box_configuration(credentials)
        except (SingBoxConfigurationError, SingBoxRuntimeError) as exc:
            print(f"[-] {exc}", file=sys.stderr, flush=True)
            return 1

        configuration_prepared = True
        using_default_configuration = startup_configuration.using_default_configuration
        default_runtime_limit_reached = False
        while using_default_configuration:
            if not default_runtime_limit_reached:
                try:
                    service_cycle_result = await run_service_cycle(
                        credentials,
                        uptime_port,
                        status_server_task,
                        status_ports=status_ports,
                        configuration_updates=configuration_updates,
                        configuration_prepared=configuration_prepared,
                        maximum_runtime_seconds=DEFAULT_SSS_MAX_RUNTIME_SECONDS,
                    )
                except SingBoxRuntimeError as exc:
                    print(f"[-] {exc}", file=sys.stderr, flush=True)
                    return 1
                except StatusServerStoppedError as exc:
                    print(f"[-] {exc}", file=sys.stderr, flush=True)
                    return 1
                configuration_prepared = False
                if (
                    service_cycle_result
                    is ServiceCycleControlResult.DEFAULT_RUNTIME_LIMIT_REACHED
                ):
                    default_runtime_limit_reached = True
                    print(
                        "[default] 默认 sss 已运行 "
                        f"{DEFAULT_SSS_MAX_RUNTIME_SECONDS // 60} 分钟且未完成网页配置；"
                        "已关闭并等待网页配置。",
                        flush=True,
                    )

            web_credentials = web_token_configuration.get_configuration()
            if web_credentials is None:
                if default_runtime_limit_reached:
                    try:
                        web_credentials = await wait_for_web_configuration(
                            configuration_updates,
                            status_server_task,
                        )
                    except StatusServerStoppedError as exc:
                        print(f"[-] {exc}", file=sys.stderr, flush=True)
                        return 1
                else:
                    print(
                        f"[restart] {RESTART_DELAY_SECONDS} 秒后重启默认 sss 服务...",
                        flush=True,
                    )
                    try:
                        web_credentials = await asyncio.wait_for(
                            wait_for_web_configuration(
                                configuration_updates,
                                status_server_task,
                            ),
                            timeout=RESTART_DELAY_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        web_credentials = web_token_configuration.get_configuration()
                        if web_credentials is None:
                            continue
                    except StatusServerStoppedError as exc:
                        print(f"[-] {exc}", file=sys.stderr, flush=True)
                        return 1

            try:
                await prepare_sing_box_configuration(web_credentials)
            except SingBoxRuntimeError as exc:
                print(f"[-] {exc}", file=sys.stderr, flush=True)
                return 1
            except SingBoxConfigurationError:
                web_token_configuration.discard_configuration(web_credentials)
                if default_runtime_limit_reached:
                    print(
                        "[-] 网页提交的 sss 配置校验失败；"
                        "默认 sss 保持关闭，请重新登录后填写。",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                print(
                    "[-] 网页提交的 sss 配置校验失败；已恢复默认配置，请重新登录后填写。",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    await prepare_sing_box_configuration(credentials)
                except (SingBoxConfigurationError, SingBoxRuntimeError) as exc:
                    print(f"[-] {exc}", file=sys.stderr, flush=True)
                    return 1
                configuration_prepared = True
                continue

            credentials = web_credentials
            configuration_prepared = True
            using_default_configuration = False
            print("[web] 已接收网页配置，正在使用新配置重新启动 sss。", flush=True)

        while True:
            try:
                await run_service_cycle(
                    credentials,
                    uptime_port,
                    status_server_task,
                    status_ports=status_ports,
                    configuration_prepared=configuration_prepared,
                )
            except SingBoxRuntimeError as exc:
                print(f"[-] {exc}", file=sys.stderr, flush=True)
                return 1
            except StatusServerStoppedError as exc:
                print(f"[-] {exc}", file=sys.stderr, flush=True)
                return 1
            configuration_prepared = False

            print(
                f"[restart] {RESTART_DELAY_SECONDS} 秒后重启 sss 服务...",
                flush=True,
            )
            await asyncio.sleep(RESTART_DELAY_SECONDS)
    finally:
        web_token_configuration.stop_accepting_web_configuration()
        if not status_server_task.done():
            status_server_task.cancel()
        await stop_status_servers(http_servers, http_server_tasks)
        await asyncio.gather(status_server_task, return_exceptions=True)
        remove_sing_box_runtime_files()


def main() -> int:
    try:
        return asyncio.run(supervise())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
