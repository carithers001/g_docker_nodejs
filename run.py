#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import hashlib
import hmac
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OWNER = "carithers001"
REPO = "g_docker_nodejs"
ASSET_PREFIX = "main"

NETWORK_TIMEOUT_SECONDS = 30
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MiB

# Fallback status-page ports when the caller does not supply environment values.
# Change these two values together with any external port mapping.
STATUS_PRIMARY_PORT = 3001
STATUS_EXTRA_PORT = 3000


def open_url(url: str, accept: str):
    request = Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "g-docker-nodejs-release-runner/1.0",
        },
    )
    return urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS)


def main() -> int:
    if sys.implementation.name != "cpython":
        raise RuntimeError("此启动器只支持 CPython。")

    cache_tag = sys.implementation.cache_tag
    if not cache_tag:
        raise RuntimeError("无法识别当前 Python 的字节码标签。")

    # CPython 3.12 -> main.cpython-312.pyc
    # CPython 3.13 -> main.cpython-313.pyc
    # CPython 3.14 -> main.cpython-314.pyc
    asset_name = f"{ASSET_PREFIX}.{cache_tag}.pyc"

    api_url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
    with open_url(api_url, "application/vnd.github+json") as response:
        release = json.load(response)

    assets = release.get("assets", [])
    asset = next(
        (
            item
            for item in assets
            if isinstance(item, dict) and item.get("name") == asset_name
        ),
        None,
    )

    if asset is None:
        available = ", ".join(
            str(item.get("name", "?"))
            for item in assets
            if isinstance(item, dict)
        )
        raise RuntimeError(
            f"最新 Release 未提供 {asset_name}；可用文件：{available or '无'}"
        )

    download_url = asset.get("browser_download_url")
    algorithm, separator, expected_sha256 = str(asset.get("digest", "")).partition(":")

    if (
        not isinstance(download_url, str)
        or algorithm != "sha256"
        or separator != ":"
        or len(expected_sha256) != 64
    ):
        raise RuntimeError("下载地址或 SHA-256 digest 无效，拒绝执行。")

    # 就是运行本脚本时所在的目录
    workdir = Path.cwd().resolve()
    payload = workdir / asset_name
    staged_payload: Path | None = None

    try:
        digest = hashlib.sha256()
        total_bytes = 0

        with open_url(download_url, "application/octet-stream") as response:
            # Download and validate a new payload before unconditionally
            # replacing any existing Release payload in the working directory.
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{asset_name}.",
                suffix=".download",
                dir=workdir,
                delete=False,
            ) as output:
                staged_payload = Path(output.name)

                while chunk := response.read(64 * 1024):
                    total_bytes += len(chunk)

                    if total_bytes > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError("下载文件超过允许大小。")

                    digest.update(chunk)
                    output.write(chunk)

        if not hmac.compare_digest(
            digest.hexdigest(),
            expected_sha256.lower(),
        ):
            raise RuntimeError("SHA-256 校验失败，拒绝执行。")

        with staged_payload.open("rb") as file:
            if file.read(4) != importlib.util.MAGIC_NUMBER:
                raise RuntimeError("下载的 .pyc 与当前 Python 不兼容。")

        os.replace(staged_payload, payload)
        staged_payload = None

        print(f"运行 Release {release.get('tag_name', '?')}：{asset_name}")

        child_environment = os.environ.copy()
        # Keep main.py's established primary-port priority:
        # SERVER_PORT, then PORT, then this fallback.
        if not (
            child_environment.get("SERVER_PORT")
            or child_environment.get("PORT")
        ):
            child_environment["SERVER_PORT"] = str(STATUS_PRIMARY_PORT)
        if not child_environment.get("STATUS_EXTRA_PORT"):
            child_environment["STATUS_EXTRA_PORT"] = str(STATUS_EXTRA_PORT)
        result = subprocess.run(
            [sys.executable, str(payload)],
            cwd=str(workdir),
            env=child_environment,
            check=False,
        )
        return result.returncode

    finally:
        # Keep the replaced payload; only remove an unpromoted download.
        if staged_payload is not None:
            try:
                staged_payload.unlink()
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                print(
                    f"警告: 无法清理临时下载文件 {staged_payload.name}: "
                    f"{cleanup_error}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HTTPError, URLError, OSError, ValueError, RuntimeError) as error:
        print(f"执行失败：{error}", file=sys.stderr)
        raise SystemExit(1)
