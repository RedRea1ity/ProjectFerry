"""程序自更新：查 GitHub 最新 Release、下载附件。
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request

from pathlib import Path
from typing import Any

from ferrycore.base import LOG
from ferrycore.net import _fetch_json
from ferrycore.versions import version_key


GITHUB_RELEASE_API = "https://api.github.com/repos/RedRea1ity/ProjectFerry/releases/latest"


GITHUB_RELEASE_PAGE = "https://github.com/RedRea1ity/ProjectFerry/releases/latest"


def latest_github_release(current_version: str, timeout: float = 8.0) -> dict[str, Any] | None:
    """查询 GitHub 最新 Release；只在比 current_version 新时返回信息，失败或无更新返回 None。"""
    try:
        data = _fetch_json(GITHUB_RELEASE_API, timeout)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    tag = str(data.get("tag_name") or "")
    remote, current = version_key(tag), version_key(current_version)
    if not remote or remote <= current:
        return None
    asset_url, asset_size = "", 0
    for asset in data.get("assets") or []:
        if isinstance(asset, dict) and str(asset.get("name", "")).lower().endswith(".exe"):
            asset_url = str(asset.get("browser_download_url") or "")
            asset_size = int(asset.get("size") or 0)
            break
    return {
        "version": tag.lstrip("v"),
        "notes": str(data.get("body") or "")[:800],
        "url": str(data.get("html_url") or GITHUB_RELEASE_PAGE),
        "asset_url": asset_url,
        "asset_size": asset_size,
    }


def download_release_asset(url: str, dest: Path, timeout: float = 60.0, progress=None, attempts: int = 1) -> Path:
    """流式下载 Release 附件；progress(done, total) 用于进度显示。

    attempts > 1 时对「连不上 / 传到一半被重置」重试。

    实测（2026-10-12 本机）：GitHub 的 Release 附件在国内网络下**约一半的尝试会失败**
    —— 连接无人应答，固定卡 21 秒后报 WinError 10060 / 10054。

    但**不追求把成功率刷上去**：单纯重试只是碰运气，堆到 5 次仍要赌，还让最坏耗时
    翻到 105 秒（用户干等）。这里只保留极轻的一次重试，失败就老实抛出去 ——
    调用方（LiteralBridge 那条线）会回落到「打开项目页让你自己下」，那是最可靠的路。
    重试整份重下（附件不大，66 KB），进度从 0 重报。
    """
    last: Exception | None = None
    for index in range(max(1, attempts)):
        if index:
            time.sleep(1.5 * index)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "ProjectFerry/1.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                with open(dest, "wb") as fh:
                    while True:
                        block = response.read(65536)
                        if not block:
                            break
                        fh.write(block)
                        done += len(block)
                        if progress:
                            progress(done, total)
            return dest
        except (OSError, ValueError) as exc:   # URLError / 连接重置 / IncompleteRead 都在内
            last = exc
            LOG.debug("下载失败（第 %s/%s 次）：%s", index + 1, max(1, attempts), url, exc_info=True)
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
    if isinstance(last, (OSError, ValueError)):
        raise last
    raise OSError(f"下载失败：{last!r}")
