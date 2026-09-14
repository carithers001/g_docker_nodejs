#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import hashlib
import hmac
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OWNER = "carithers001"
REPO = "g_docker_nodejs"
ASSET_PREFIX = "main"

NETWORK_TIMEOUT_SECONDS = 30
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MiB


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

    # 避免覆盖或误删用户已有文件
    if payload.exists():
        raise RuntimeError(
            f"当前目录已存在 {payload.name}，为避免覆盖已停止。"
        )

    created_payload = False

    try:
        digest = hashlib.sha256()
        total_bytes = 0

        with open_url(download_url, "application/octet-stream") as response:
            # x：仅当文件不存在时创建
            with payload.open("xb") as output:
                created_payload = True

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

        with payload.open("rb") as file:
            if file.read(4) != importlib.util.MAGIC_NUMBER:
                raise RuntimeError("下载的 .pyc 与当前 Python 不兼容。")

        print(f"运行 Release {release.get('tag_name', '?')}：{payload.name}")

        result = subprocess.run(
            [sys.executable, str(payload)],
            cwd=str(workdir),
            check=False,
        )
        return result.returncode

    finally:
        # 无论运行成功、失败或异常，只删除本脚本本次创建的文件
        if created_payload:
            try:
                payload.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HTTPError, URLError, OSError, ValueError, RuntimeError) as error:
        print(f"执行失败：{error}", file=sys.stderr)
        raise SystemExit(1)