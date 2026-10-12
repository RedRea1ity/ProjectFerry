"""Minecraft 版本 ↔ 资源包格式：本地版本表、远程版本表、客户端 jar 探测、以及「这个实例该用哪个 pack_format」的仲裁。
"""

from __future__ import annotations

import collections
import json
import re
import time
import tomllib
import zipfile

from pathlib import Path
from typing import Any

from ferrycore.base import LOG, app_dir
from ferrycore.config import DEFAULT_CONFIG
from ferrycore.net import _fetch_json


# (版本元组, 资源包格式)，升序排列；取 <= 目标版本的最大一项。
# 数据来源：minecraft.wiki/w/Pack_format 的资源包格式历史（1.21.9 起为小数格式）。
PACK_FORMATS = [
    ((1, 6, 1), 1),
    ((1, 9), 2),
    ((1, 11), 3),
    ((1, 13), 4),
    ((1, 15), 5),
    ((1, 16, 2), 6),
    ((1, 17), 7),
    ((1, 18), 8),
    ((1, 19), 9),
    ((1, 19, 3), 12),
    ((1, 19, 4), 13),
    ((1, 20), 15),
    ((1, 20, 2), 18),
    ((1, 20, 3), 22),
    ((1, 20, 5), 32),
    ((1, 21), 34),
    ((1, 21, 2), 42),
    ((1, 21, 4), 46),
    ((1, 21, 5), 55),
    ((1, 21, 6), 63),
    ((1, 21, 7), 64),
    ((1, 21, 9), 69.0),
    ((1, 21, 11), 75.0),
    ((26, 1), 84.0),
    ((26, 2), 88.0),
    ((26, 3), 97.1),
    ((26, 4), 99.0),
]


# 资源包 1.21.9（25w31a）起 pack.mcmeta 改用 min_format / max_format，
# 参考minecraft.wiki/w/Pack_format。旧格式的最高值是 64（1.21.7–1.21.8）。
MIN_MAX_FORMAT_SINCE = 65.0


# 游戏已改用年份版本号（如 26.2）。把误写的 1.26.3 之类的 "1.NN.x" 归一为年号版本。
YEAR_VERSION_MIN_SECOND = 22


# 远程版本表：新 Minecraft 版本发布后，只需在仓库里更新 pack_formats.json，
# 旧安装会在本地表查不到（或表尾版本待定）时联网拉取，无需重新下载程序。
PACK_FORMATS_REMOTE_URLS = (
    "https://raw.githubusercontent.com/RedRea1ity/ProjectFerry/main/pack_formats.json",
    "https://cdn.jsdelivr.net/gh/RedRea1ity/ProjectFerry@main/pack_formats.json",
)


REMOTE_FORMAT_TTL = 24 * 3600


def version_key(text: Any) -> tuple[int, ...]:
    """把版本号压成可比较的整数元组："1.3.0" → (1,3,0)；读不出数字给 (0,)。

    这是全项目**唯一**的版本号比较口径 —— 自更新比较 Release tag、比较已装的
    LiteralBridge 版本都用它。
    """
    nums = [int(part) for part in re.findall(r"\d+", str(text or ""))]
    return tuple(nums) if nums else (0,)


def read_pack_format(output: Path) -> float | None:
    if not output.exists():
        return None
    try:
        with zipfile.ZipFile(output) as zf:
            if "pack.mcmeta" not in zf.namelist():
                return None
            data = json.loads(zf.read("pack.mcmeta").decode("utf-8-sig", "replace"))
            pack = data.get("pack", {})
            for key in ("pack_format", "min_format", "max_format"):
                value = pack.get(key)
                if isinstance(value, (list, tuple)) and len(value) >= 2:
                    return float(value[0]) + float(value[1]) / 10
                if isinstance(value, (int, float)):
                    return float(value)
            return None
    except (zipfile.BadZipFile, OSError, ValueError, TypeError):
        return None


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def normalize_version_tuple(vt: tuple[int, ...]) -> tuple[int, ...]:
    """游戏已用年份版本号（26.2）。兼容用户误写成的 1.26.3 形式。"""
    if len(vt) >= 2 and vt[0] == 1 and vt[1] >= YEAR_VERSION_MIN_SECOND:
        return (vt[1],) + tuple(vt[2:])
    return vt


def uses_min_max_format(pack_format: float) -> bool:
    return float(pack_format) >= MIN_MAX_FORMAT_SINCE


def clean_pack_format(value: Any) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else number


def pack_format_for_version(version: str) -> int | float:
    vt = normalize_version_tuple(_version_tuple(version))
    if not vt:
        raise ValueError(f"无法确认 {version} 的资源包格式，请手动指定 pack_format。")
    if vt < PACK_FORMATS[0][0]:
        # 资源包是 1.6.1 才引入的机制，更老的版本没有可写的东西。
        raise ValueError(f"{version} 没有资源包机制（1.6.1 起才支持），无法生成汉化包。")
    chosen = PACK_FORMATS[0][1]
    for ver, fmt in PACK_FORMATS:
        if ver <= vt:
            chosen = fmt
        else:
            break
    return clean_pack_format(chosen)


def _remote_format_cache_path(cache_dir: Path | None = None) -> Path:
    return (Path(cache_dir) if cache_dir else app_dir() / "ferry_cache") / "pack_formats.json"


def _parse_remote_formats(data: Any) -> dict[tuple[int, ...], int | float]:
    """解析 {"formats": {"26.3": 97.1}}；丢弃无法解析或离谱的条目。"""
    result: dict[tuple[int, ...], int | float] = {}
    entries = data.get("formats") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return result
    for version, value in entries.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= float(value) <= 9999:
            continue
        vt = normalize_version_tuple(_version_tuple(str(version)))
        if vt:
            result[vt] = clean_pack_format(value)
    return result


def pack_format_for_version_remote(version: str, cache_dir: Path | None = None, timeout: float = 8.0, online: bool = True) -> int | float | None:
    """从仓库的远程版本表查 pack_format；查不到或网络失败返回 None，调用方回退本地表。"""
    cache_file = _remote_format_cache_path(cache_dir)
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        cached = {}
    formats = _parse_remote_formats(cached if isinstance(cached, dict) else {})
    fresh = isinstance(cached, dict) and time.time() - float(cached.get("fetched_at") or 0) < REMOTE_FORMAT_TTL
    if formats and fresh:
        return formats.get(normalize_version_tuple(_version_tuple(version)))
    fetched: dict[tuple[int, ...], int | float] = {}
    if online:
        for url in PACK_FORMATS_REMOTE_URLS:
            try:
                fetched = _parse_remote_formats(_fetch_json(url, timeout))
            except (OSError, ValueError) as exc:
                LOG.debug("Remote pack format fetch failed: %s (%s)", url, exc)
                continue
            if fetched:
                break
    if fetched:
        formats = fetched
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {"fetched_at": time.time(), "formats": {".".join(map(str, key)): value for key, value in formats.items()}}
            cache_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            LOG.debug("Could not save remote pack format cache: %s", cache_file, exc_info=True)
    return formats.get(normalize_version_tuple(_version_tuple(version))) if formats else None


MC_VERSION_RE = re.compile(r"(?<![\d.])(1\.\d{1,2}(?:\.\d+)?|\d{2}\.\d{1,2}(?:\.\d+)?)(?![\d.])")


def client_jars(instance_path: Path) -> list[Path]:
    """Client jars that may contain the authoritative version.json."""
    candidates: list[Path] = []
    try:
        candidates.extend(sorted(instance_path.glob("*.jar")))
        candidates.extend(sorted(instance_path.glob("versions/*/*.jar")))
        candidates.extend(sorted((instance_path / ".minecraft" / "versions").glob("*/*.jar")))
    except OSError:
        LOG.debug("Could not enumerate client jars in %s", instance_path, exc_info=True)
    return candidates


def client_version_json(instance_path: Path) -> dict[str, Any] | None:
    """Read version.json from the client jar; it carries the exact pack_version."""
    for jar in client_jars(instance_path):
        try:
            with zipfile.ZipFile(jar) as zf:
                if "version.json" not in zf.namelist():
                    continue
                data = json.loads(zf.read("version.json").decode("utf-8-sig"))
                if isinstance(data, dict):
                    LOG.debug("Read version.json from %s", jar)
                    return data
        except (OSError, ValueError, zipfile.BadZipFile):
            LOG.debug("Could not read version.json from %s", jar, exc_info=True)
    return None


def client_pack_format(instance_path: Path) -> int | float | None:
    """Authoritative resource pack format from the client's pack_version (major.minor)."""
    data = client_version_json(instance_path)
    if not data:
        return None
    pack_version = data.get("pack_version")
    if not isinstance(pack_version, dict):
        return None
    try:
        major = int(pack_version["resource_major"])
        minor = int(pack_version.get("resource_minor") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= minor <= 9:
        LOG.warning("Unexpected resource_minor %s; using major only", minor)
        minor = 0
    return clean_pack_format(major + minor / 10)


def split_pack_format(pack_format: int | float) -> tuple[int, int]:
    number = float(pack_format)
    major = int(number)
    minor = int(round((number - major) * 10))
    if not 0 <= minor <= 9:
        major, minor = int(round(number)), 0
    return major, minor


def detect_minecraft_version(instance_path: Path) -> str | None:
    mmc = instance_path / "mmc-pack.json"
    if mmc.exists():
        try:
            data = json.loads(mmc.read_text(encoding="utf-8"))
            for comp in data.get("components", []):
                if comp.get("uid") == "net.minecraft":
                    version = MC_VERSION_RE.search(str(comp.get("version", "")))
                    if version:
                        LOG.debug("Minecraft version %s from %s", version.group(), mmc)
                        return version.group()
        except (OSError, ValueError):
            LOG.debug("Could not read %s", mmc, exc_info=True)

    data = client_version_json(instance_path)
    if data and data.get("id"):
        match = MC_VERSION_RE.search(str(data["id"]))
        if match:
            LOG.debug("Minecraft version %s from client version.json", match.group())
            return match.group()

    for root in (instance_path / "versions", instance_path / ".minecraft" / "versions"):
        if not root.is_dir():
            continue
        found = []
        for version_dir in sorted(root.iterdir()):
            if not version_dir.is_dir():
                continue
            descriptor = version_dir / (version_dir.name + ".json")
            jar = version_dir / (version_dir.name + ".jar")
            if not descriptor.is_file() and not jar.is_file():
                continue
            label = version_dir.name
            if descriptor.is_file():
                try:
                    data = json.loads(descriptor.read_text(encoding="utf-8-sig"))
                    label = str(data.get("id", ""))
                except (OSError, ValueError, AttributeError):
                    LOG.debug("Could not read version descriptor %s", descriptor, exc_info=True)
            match = MC_VERSION_RE.search(label)
            if match:
                found.append(match.group())
        if len(set(found)) == 1:
            LOG.debug("Minecraft version %s from %s", found[0], root)
            return found[0]
        if found:
            LOG.debug("Ambiguous versions in %s: %s", root, sorted(set(found)))

    # Launcher-specific files are not used until their real field format is confirmed.
    for launcher in ("hmclversion.cfg", "PCL.ini"):
        if (instance_path / launcher).is_file():
            LOG.debug("Skipping unverified launcher format: %s", instance_path / launcher)

    votes: collections.Counter[str] = collections.Counter()
    mods = instance_path / "mods"
    if mods.is_dir():
        for jar in sorted(mods.glob("*.jar"))[:50]:
            try:
                with zipfile.ZipFile(jar) as zf:
                    names = set(zf.namelist())
                    if "META-INF/mods.toml" in names:
                        data = tomllib.loads(zf.read("META-INF/mods.toml").decode("utf-8-sig"))
                        for dependencies in data.get("dependencies", {}).values():
                            for dep in dependencies:
                                if dep.get("modId", "").lower() == "minecraft":
                                    matches = set(MC_VERSION_RE.findall(str(dep.get("versionRange", ""))))
                                    if len(matches) == 1:
                                        votes[next(iter(matches))] += 1
                    if "mcmod.info" in names:
                        data = json.loads(zf.read("mcmod.info").decode("utf-8-sig"))
                        for mod in data if isinstance(data, list) else data.get("modList", []):
                            match = MC_VERSION_RE.search(str(mod.get("mcversion", "")))
                            if match:
                                votes[match.group()] += 1
                    if "fabric.mod.json" in names:
                        data = json.loads(zf.read("fabric.mod.json").decode("utf-8-sig"))
                        matches = set(MC_VERSION_RE.findall(str(data.get("depends", {}).get("minecraft", ""))))
                        if len(matches) == 1:
                            votes[next(iter(matches))] += 1
            except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
                LOG.debug("Could not read Minecraft metadata in %s", jar, exc_info=True)
    if votes:
        ranked = votes.most_common()
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            LOG.debug("Minecraft version %s from mod metadata votes %s", ranked[0][0], votes)
            return ranked[0][0]
        LOG.debug("Ambiguous mod metadata versions: %s", votes)

    match = MC_VERSION_RE.search(instance_path.name)
    if match:
        LOG.debug("Minecraft version %s from instance directory", match.group())
        return match.group()
    LOG.debug("No Minecraft version detected for %s", instance_path)
    return None


def detect_pack_format(instance_path: Path, cache_dir: Path | None = None, online: bool = True) -> int | float:
    # 优先读取客户端 version.json 的 pack_version，权威且含 minor，无需维护版本表。
    exact = client_pack_format(instance_path)
    if exact is not None:
        return exact
    version = detect_minecraft_version(instance_path)
    if version is None:
        raise ValueError("未检测到游戏版本；请在 GUI 选择版本或在命令行指定 --pack-format N。")
    local: int | float | None
    try:
        local = pack_format_for_version(version)
    except ValueError as exc:
        vt = normalize_version_tuple(_version_tuple(version))
        if vt and vt < PACK_FORMATS[0][0]:
            raise  # 1.6.1 之前没有资源包机制，联网也查不到，直接给出明确原因。
        local = None
        del exc
    # 表尾条目（26.4+）在正式版定版前随时可能变：联网核对远程版本表，失败再用本地表。
    vt = normalize_version_tuple(_version_tuple(version))
    live_zone = bool(vt) and vt >= PACK_FORMATS[-1][0]
    if local is not None and not live_zone:
        return local
    if online:
        remote = pack_format_for_version_remote(version, cache_dir=cache_dir)
        if remote is not None:
            return remote
    if local is not None:
        return local
    raise ValueError(f"无法确认 {version} 的资源包格式，请手动指定 pack_format 或检查网络后重试。")


def resolve_pack_format(config: dict[str, Any], instance: Path, overrides: dict[str, Any] | None = None) -> int | float:
    overrides = overrides or {}
    if overrides.get("pack_format"):
        return clean_pack_format(overrides["pack_format"])
    if config.get("pack_format"):
        return clean_pack_format(config["pack_format"])
    pack = Path(overrides.get("output_dir") or config.get("output_dir") or instance / "resourcepacks") / (overrides.get("pack_name") or config.get("pack_name") or DEFAULT_CONFIG["pack_name"])
    existing = read_pack_format(pack)
    try:
        detected = detect_pack_format(
            instance,
            cache_dir=Path(config.get("cache_dir") or app_dir() / "ferry_cache"),
            online=bool(config.get("online_pack_format", True)),
        )
    except ValueError:
        detected = None
    # 向上兼容：检测到的版本比旧包格式新时升级；反之尊重已有（可能是手动或更高版本）格式。
    if existing is not None and (detected is None or float(existing) >= float(detected)):
        LOG.debug("Reusing pack_format %s from %s (detected %s)", existing, pack, detected)
        return clean_pack_format(existing)
    if detected is not None:
        if existing is not None:
            LOG.info("Upgrading pack_format %s -> %s for %s", existing, detected, pack)
        return detected
    if existing is not None:
        return clean_pack_format(existing)
    raise ValueError("未检测到游戏版本；请在 GUI 选择版本或在命令行指定 --pack-format N。")
