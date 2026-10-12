"""社区汉化：CFPA 整合包下载 / 解析、人工汉化包下载、参考语料。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from pathlib import Path
from typing import Any

from ferrycore.base import AI_MARKER, LOG, app_dir
from ferrycore.models import LangFile, ScanResult
from ferrycore.scan import LANG_FILE_RE, _modid_keys
from ferrycore.text import _parse_lang_raw
from ferrycore.update import download_release_asset


CFPA_PACK_URL = "https://github.com/CFPAOrg/Minecraft-Mod-Language-Package/releases/download/autobuild/Minecraft-Mod-Language-Modpack.zip"


CFPA_RELEASE_PAGE = "https://github.com/CFPAOrg/Minecraft-Mod-Language-Package/releases"


CFPA_PACK_TTL = 14 * 86400


def community_pack_path(cache_dir: Path | None = None) -> Path:
    return (Path(cache_dir) if cache_dir else app_dir() / "ferry_cache") / "CFPA_Modpack.zip"


def ensure_community_pack(cache_dir: Path | None = None, timeout: float = 120.0, max_age: float = CFPA_PACK_TTL, allow_fetch: bool = True, progress=None) -> Path | None:
    """拿到 CFPA 社区整合包（合并版资源包，含全部模组的 zh_cn）；过期才重新下载。"""
    path = community_pack_path(cache_dir)
    if path.is_file() and time.time() - path.stat().st_mtime < max_age:
        return path
    if not allow_fetch:
        return path if path.is_file() else None
    part = path.with_name(path.name + ".part")
    try:
        download_release_asset(CFPA_PACK_URL, part, timeout=timeout, progress=progress)
        part.replace(path)
        return path
    except (OSError, ValueError) as exc:
        LOG.warning("Community pack download failed: %s", exc)
        part.unlink(missing_ok=True)
        return path if path.is_file() else None


def community_pack_translations(pack_path: Path | None, modids) -> dict[str, dict[str, str]]:
    """从社区整合包里抽取指定模组的 zh_cn 内容（json / .lang 都认）。"""
    result: dict[str, dict[str, str]] = {}
    if not pack_path:
        return result
    wanted = {modid.lower(): modid for modid in modids}
    if not wanted:
        return result
    pattern = re.compile(r"assets/([^/]+)/lang/zh_cn[.](json|lang)", re.IGNORECASE)
    try:
        with zipfile.ZipFile(pack_path) as zf:
            for name in zf.namelist():
                match = pattern.fullmatch(name)
                if not match or match.group(1).lower() not in wanted:
                    continue
                try:
                    data = _parse_lang_raw(zf.read(name), match.group(2).lower(), f"{pack_path}!{name}")
                except ValueError:
                    continue
                if data:
                    result.setdefault(wanted[match.group(1).lower()], {}).update(data)
    except (zipfile.BadZipFile, OSError):
        return {}
    return result


def fetch_community_baseline(scan: ScanResult, config: dict[str, Any], cache_dir: Path, modids: set[str] | None = None) -> dict[str, dict[str, str]]:
    """Fetch optional community keys, excluding anything already present in the instance."""
    if not config.get("community_enabled", True):
        return {}
    current = _modid_keys(scan.community)
    ai = _modid_keys(scan.ai)
    english: dict[str, dict[str, str]] = {}
    for file in scan.english:
        if modids is None or file.modid in modids:
            english.setdefault(file.modid, {}).update(file.data)
    wanted = [modid for modid in english if re.fullmatch(r"[a-z0-9_.-]+", modid, re.IGNORECASE)]
    # 整合包只在已缓存时使用（下载由 GUI 扫描后/翻译前单独预热），测试与离线场景不触网。
    pack = ensure_community_pack(cache_dir, allow_fetch=False)
    raw_by_modid = community_pack_translations(pack, wanted)
    result: dict[str, dict[str, str]] = {}
    for modid, original in english.items():
        raw = raw_by_modid.get(modid, {})
        filtered = {key: value for key, value in raw.items() if original.get(key, "").strip() and value.strip() and key not in current.get(modid, set()) and key not in ai.get(modid, set()) and key not in scan.reverted.get(modid, set())}
        if filtered:
            result[modid] = filtered
    scan.community_baseline = result
    return result


def community_corpus(english: list[LangFile], community: list[LangFile],
                     reference_only: dict[str, dict[str, str]] | None = None) -> dict[str, dict[str, tuple[str, str]]]:
    """构建「英文原文 -> 人工译文」语料，供 AI 参考。

    reference_only 是"存在于 resourcepacks 目录但未启用"的包里的人工译文。
    它们不抵扣缺口（游戏里未生效），但**必须**进入语料——
    否则摆渡会把汉化组的心血当空气，重新用通用译法去译同一个词。

    实测：boh 的"后室 / 抱脸虫 / 无眼杰克"等专名术语全部来自这些未启用的包。
    没有它们，摆渡会把 backrooms 译成"后面的房间"。
    """
    english_values: dict[str, dict[str, str]] = {}
    for file in english:
        english_values.setdefault(file.modid, {}).update(file.data)
    corpus: dict[str, dict[str, tuple[str, str]]] = {}
    for file in community:
        for key, chinese in file.data.items():
            original = english_values.get(file.modid, {}).get(key)
            if original and chinese.strip():
                corpus.setdefault(file.modid, {})[key] = (original, chinese)
    for modid, data in (reference_only or {}).items():
        bucket = corpus.setdefault(modid, {})
        for key, chinese in data.items():
            original = english_values.get(modid, {}).get(key)
            # 已生效的人工汉化优先，不被参考语料覆盖。
            if original and chinese.strip() and key not in bucket:
                bucket[key] = (original, chinese)
    return corpus


def human_pack_destination(instance: Path, modid: str) -> Path:
    if not re.fullmatch(r"[a-z0-9_.-]+", modid, flags=re.IGNORECASE) or modid in {".", ".."}:
        raise ValueError("模组 ID 无效，无法生成资源包文件名。")
    return instance / "resourcepacks" / f"ProjectFerry_Human_{modid}.zip"


def download_human_pack(url: str, instance: Path, modid: str) -> Path:
    """Download a direct resource-pack ZIP and install it without changing the AI pack."""
    if urllib.parse.urlparse(url).scheme.lower() != "https":
        raise ValueError("请输入人工汉化资源包的 HTTPS ZIP 直链。")
    destination = human_pack_destination(instance, modid)
    if not (instance / "mods").is_dir():
        raise ValueError(f"实例的 mods 目录不存在：{instance}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ProjectFerry/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            if urllib.parse.urlparse(response.geturl()).scheme.lower() != "https":
                raise ValueError("下载链接跳转到了非 HTTPS 地址。")
            with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".zip.part", delete=False) as handle:
                temporary = Path(handle.name)
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 64 * 1024 * 1024:
                        raise ValueError("资源包超过 64 MB，请检查下载链接。")
                    handle.write(chunk)
        try:
            with zipfile.ZipFile(temporary) as zf:
                files = zf.infolist()
                if len(files) > 5000 or sum(info.file_size for info in files) > 256 * 1024 * 1024:
                    raise ValueError("资源包解压后体积过大。")
                if any(info.filename.startswith("/") or "\\" in info.filename or ".." in info.filename.split("/") for info in files):
                    raise ValueError("资源包中含有不安全的路径。")
                names = {info.filename for info in files}
                if AI_MARKER in names:
                    raise ValueError("下载内容是 AI 汉化包，无法作为人工汉化导入。")
                if "pack.mcmeta" not in names:
                    raise ValueError("压缩包根目录缺少 pack.mcmeta，请使用资源包 ZIP 直链。")
                try:
                    metadata = json.loads(zf.read("pack.mcmeta").decode("utf-8-sig"))
                    if not isinstance(metadata.get("pack"), dict):
                        raise ValueError("pack.mcmeta 缺少 pack 配置。")
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError("资源包的 pack.mcmeta 无效。") from exc
                matches = [info for info in files if (m := LANG_FILE_RE.fullmatch(info.filename)) and m.group(1).lower() == modid.lower() and m.group(2).lower() == "zh_cn"]
                if not matches or not any(any(value.strip() for value in _parse_lang_raw(zf.read(info), info.filename.rsplit(".", 1)[1].lower(), info.filename).values()) for info in matches):
                    raise ValueError(f"资源包里没有 {modid} 的有效 zh_cn 语言文件。")
                if zf.testzip() is not None:
                    raise ValueError("资源包 ZIP 校验失败。")
        except zipfile.BadZipFile as exc:
            raise ValueError("下载内容不是有效的 ZIP 资源包。") from exc
        os.replace(temporary, destination)
        temporary = None
        return destination
    except urllib.error.URLError as exc:
        raise RuntimeError(f"下载人工汉化包失败：{exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
