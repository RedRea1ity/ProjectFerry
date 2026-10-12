"""实例发现与扫描：语言文件 / Patchouli 读取、缺失 key 计算、模组状态构建。
"""

from __future__ import annotations

import io
import json
import re
import sys
import zipfile

from pathlib import Path
from typing import Any

from ferrycore.base import AI_MARKER, LOG, REVERT_MARKER
from ferrycore.models import Entry, LangFile, ModTranslationStatus, PatchouliFile, ScanResult
from ferrycore.text import _parse_lang_raw


# 默认排除纯前置、库、优化和辅助类模组，避免把没有面向玩家文本的依赖列出来。
# 这是“默认隐藏”而不是删除：命令行可用 --include-modid 强制纳入。
DEFAULT_EXCLUDED_MODIDS = {
    "literalbridge", "ferrybridge",
    "architectury", "cloth_config", "curios", "ftb_library", "geckolib", "kotlinforforge",
    "kurolib", "libipn", "playeranimator", "player_animation_lib",
    "yet_another_config_lib_v3", "memoryleakfix", "starlight", "ferritecore", "entityculling",
    "imblocker", "sodium", "embeddium", "modernfix", "smoothboot", "optifine",
}


DEFAULT_EXCLUDED_NAME_HINTS = (
    "memoryleak", "kurolib", "starlight", "architectury", "cloth", "geckolib", "kotlin",
    "ferritecore", "entityculling", "imblocker", "libipn", "player-animation", "modernfix",
)


def is_excluded_mod(modid: str, extra: set[str] | None = None) -> bool:
    normalized = modid.lower().replace("-", "_")
    excluded = DEFAULT_EXCLUDED_MODIDS | (extra or set())
    return normalized in excluded or any(hint in normalized for hint in DEFAULT_EXCLUDED_NAME_HINTS)


def enabled_resource_packs(instance: Path | None) -> set[str] | None:
    """读 options.txt 的 resourcePacks 列表，返回真正启用的包标识。

    返回 None 表示读不到（首次启动 / 文件不存在 / 格式异常），
    此时调用方应退回"全部视为启用"的旧行为，避免误判导致漏翻。
    """
    if instance is None:
        return None
    options = instance / "options.txt"
    try:
        text = options.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = re.search(r"^resourcePacks:\[(.*?)\]", text, re.MULTILINE | re.DOTALL)
    if not match:
        return None
    names = set()
    for raw in match.group(1).split(","):
        name = raw.strip().strip('"')
        if not name:
            continue
        # 条目形如 file/Foo.zip 或 vanilla / mod_resources
        names.add(name.replace("\\", "/").rsplit("/", 1)[-1].lower())
    return names


def is_resource_pack_enabled(pack: Path, resourcepacks_dir: Path, enabled: set[str]) -> bool:
    """判断某个资源包目录项是否在 options.txt 的启用列表里。

    匹配时忽略大小写与路径分隔符差异；用户手动删掉条目即视为不启用，
    这样玩家随时可以在游戏里开关任何包，摆渡下次运行就会跟上。
    """
    return pack.name.lower() in enabled


def _promote_disabled_packs(scan: ScanResult) -> int:
    """把未启用包里的译文提升为"人工汉化"（--trust-disabled-packs）。

    玩家主动选择推翻默认判断时使用：那些包里的译文从此抵扣缺口，
    摆渡不会重译。返回被提升的 key 数量，便于向用户报告。
    """
    promoted = 0
    for modid, data in list(scan.reference_only.items()):
        if not data:
            continue
        scan.community.append(LangFile(modid, "zh_cn", "disabled-pack-promoted", dict(data)))
        promoted += len(data)
    scan.reference_only.clear()
    if promoted:
        print(f"[--trust-disabled-packs] 已把未启用资源包里的 {promoted} 条译文算作人工汉化")
    return promoted


LANG_FILE_RE = re.compile(r"assets/([^/]+)/lang/(en_us|zh_cn)\.(json|lang)", re.IGNORECASE)


NESTED_JAR_RE = re.compile(r"META-INF/jars/[^/]+\.jar", re.IGNORECASE)


PATCHOULI_RE = re.compile(r"assets/([^/]+)/patchouli_books/([^/]+)/(en_us|zh_cn)/entries/(.+\.json)", re.IGNORECASE)


def patchouli_key(file: PatchouliFile, pointer: str) -> str:
    return f"patchouli:{file.book}/{file.rel_path}#{pointer}"


def patchouli_fields(data: dict[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    if isinstance(data.get("name"), str) and data["name"].strip():
        result.append(("/name", data["name"]))
    pages = data.get("pages")
    if isinstance(pages, list):
        for i, page in enumerate(pages):
            if isinstance(page, dict):
                for field_name in ("text", "title"):
                    if isinstance(page.get(field_name), str) and page[field_name].strip():
                        result.append((f"/pages/{i}/{field_name}", page[field_name]))
    return result


def missing_patchouli_entries(scan: ScanResult, modids: set[str] | None = None) -> list[Entry]:
    human = {(f.modid, f.book, f.rel_path) for f in scan.patchouli_community}
    ai = {(f.modid, f.book, f.rel_path) for f in scan.patchouli_ai}
    wanted = {name.lower().replace("-", "_") for name in modids} if modids else None
    result: list[Entry] = []
    for file in scan.patchouli_english:
        if wanted is not None and file.modid.lower().replace("-", "_") not in wanted:
            continue
        if (file.modid, file.book, file.rel_path) in human | ai:
            continue
        for pointer, text in patchouli_fields(file.data):
            result.append(Entry(file.modid, patchouli_key(file, pointer), text, file.path, "patchouli", file.book, file.rel_path, pointer))
    return result


def load_pack_patchouli(pack: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not pack.is_file():
        return result
    try:
        with zipfile.ZipFile(pack) as zf:
            for name in zf.namelist():
                match = PATCHOULI_RE.fullmatch(name)
                if match and match.group(3).lower() == "zh_cn":
                    data = json.loads(zf.read(name).decode("utf-8-sig"))
                    if isinstance(data, dict):
                        result[name] = data
    except (OSError, ValueError, zipfile.BadZipFile):
        LOG.warning("Could not load Patchouli entries in %s", pack, exc_info=True)
    return result


def translated_patchouli_files(scan: ScanResult, entries: list[Entry], translated: dict[str, dict[str, str]]) -> dict[str, dict[str, Any]]:
    by_file: dict[tuple[str, str, str], list[Entry]] = {}
    for entry in entries:
        if entry.kind == "patchouli":
            by_file.setdefault((entry.modid, entry.book, entry.rel_path), []).append(entry)
    result: dict[str, dict[str, Any]] = {}
    for file in scan.patchouli_english:
        group = by_file.get((file.modid, file.book, file.rel_path))
        if not group or len(group) != len(patchouli_fields(file.data)) or any(entry.key not in translated.get(file.modid, {}) for entry in group):
            continue
        data = json.loads(json.dumps(file.data, ensure_ascii=False))
        for entry in group:
            parts = entry.json_pointer.split("/")[1:]
            field = data
            for part in parts[:-1]:
                field = field[int(part)] if isinstance(field, list) else field[part]
            field[parts[-1]] = translated[file.modid][entry.key]
        result[f"assets/{file.modid}/patchouli_books/{file.book}/zh_cn/entries/{file.rel_path}"] = data
    return result


def language_files_in_zip(path: Path) -> list[LangFile]:
    with zipfile.ZipFile(path) as zf:
        return _language_files_in_archive(zf, str(path), depth=0)


def patchouli_files_in_zip(path: Path) -> list[PatchouliFile]:
    result: list[PatchouliFile] = []
    with zipfile.ZipFile(path) as zf:
        is_ai = AI_MARKER in zf.namelist()
        for name in zf.namelist():
            match = PATCHOULI_RE.fullmatch(name)
            if not match:
                continue
            try:
                data = json.loads(zf.read(name).decode("utf-8-sig"))
                if isinstance(data, dict):
                    result.append(PatchouliFile(match.group(1), match.group(2), match.group(4), data, f"{path}!{name}", match.group(3).lower(), is_ai))
            except (OSError, ValueError, zipfile.BadZipFile):
                LOG.warning("Skipping invalid Patchouli JSON %s!%s", path, name, exc_info=True)
    return result


def _language_files_in_archive(zf: zipfile.ZipFile, source: str, depth: int) -> list[LangFile]:
    result: list[LangFile] = []
    try:
        names = zf.namelist()
        is_ai = AI_MARKER in names
        for name in names:
            match = LANG_FILE_RE.fullmatch(name)
            if match:
                modid, locale, ext = match.group(1), match.group(2).lower(), match.group(3).lower()
                try:
                    data = _parse_lang_raw(zf.read(name), ext, f"{source}!{name}")
                except (ValueError, OSError, zipfile.BadZipFile) as exc:
                    LOG.warning("Skipping language file %s!%s: %s", source, name, exc)
                    continue
                result.append(LangFile(modid, locale, f"{source}!{name}", data, is_ai))
            elif NESTED_JAR_RE.fullmatch(name) and depth < 3:
                try:
                    with zipfile.ZipFile(io.BytesIO(zf.read(name))) as nested:
                        result.extend(_language_files_in_archive(nested, f"{source}!{name}", depth + 1))
                except (OSError, zipfile.BadZipFile, ValueError) as exc:
                    LOG.warning("Skipping invalid nested jar %s!%s: %s", source, name, exc)
    except (OSError, zipfile.BadZipFile) as exc:
        LOG.warning("Could not scan archive %s: %s", source, exc)
    return result


def language_files_in_directory(path: Path) -> list[LangFile]:
    result: list[LangFile] = []
    is_ai = (path / AI_MARKER).exists()
    files = sorted(list(path.rglob("*.json")) + list(path.rglob("*.lang")))
    for file in files:
        match = re.search(r"[\\/]assets[\\/]([^\\/]+)[\\/]lang[\\/](en_us|zh_cn)\.(json|lang)$", str(file), re.IGNORECASE)
        if not match:
            continue
        modid, locale, ext = match.group(1), match.group(2).lower(), match.group(3).lower()
        try:
            data = _parse_lang_raw(file.read_bytes(), ext, str(file))
        except ValueError as exc:
            print(f"[跳过] {exc}", file=sys.stderr)
            continue
        result.append(LangFile(modid, locale, str(file), data, is_ai))
    return result


def candidate_instance_roots() -> list[Path]:
    """Return existing common Minecraft instance roots without touching files."""
    home = Path.home()
    candidates = [
        home / "AppData/Roaming/.minecraft",
        home / "AppData/Roaming/PrismLauncher/instances",
        home / "AppData/Roaming/PrismLauncher",
        home / "AppData/Roaming/ATLauncher/instances",
        home / "AppData/Roaming/curseforge/minecraft/Instances",
        home / "AppData/Roaming/.minecraft/versions",
        Path("D:/Minecraft/versions"),
        Path("D:/游戏/.minecraft"),
        Path("D:/Minecraft/.minecraft"),
    ]
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate.is_dir() and candidate not in seen:
            result.append(candidate)
            seen.add(candidate)
    return result


def find_instance_dirs() -> list[Path]:
    """Find directories that look like MC instances by containing mods or resourcepacks."""
    roots = candidate_instance_roots()
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        probes = [root]
        if root.name.lower() in {"instances", "versions"}:
            try:
                probes.extend(x for x in root.iterdir() if x.is_dir())
            except OSError:
                pass
        for path in probes:
            if (path / "mods").is_dir() and path not in seen:
                found.append(path)
                seen.add(path)
    return found


def resolve_instance_path(value: str | None, interactive: bool = False) -> Path:
    if value:
        path = Path(value).expanduser()
        if not path.is_dir():
            raise FileNotFoundError(f"Minecraft 实例目录不存在: {path}")
        return path
    found = find_instance_dirs()
    if len(found) == 1:
        print(f"自动检测到实例: {found[0]}")
        return found[0]
    if interactive and found:
        print("检测到多个 Minecraft 实例:")
        for index, path in enumerate(found, 1):
            print(f"  {index}. {path}")
        answer = input("请输入编号，或直接粘贴实例路径: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(found):
            return found[int(answer) - 1]
        path = Path(answer).expanduser()
        if path.is_dir():
            return path
    if not found:
        raise FileNotFoundError("没有自动找到 Minecraft 实例，请使用 --instance 手动指定实例目录。")
    raise RuntimeError("检测到多个实例，请使用 --instance 指定，或在交互模式下运行。")


def scan_inputs(mods_dir: Path, resourcepacks_dir: Path | None, instance: Path | None = None) -> ScanResult:
    result = ScanResult()
    if not mods_dir.is_dir():
        raise FileNotFoundError(f"模组目录不存在: {mods_dir}")
    for jar in sorted(mods_dir.rglob("*.jar")):
        try:
            files = language_files_in_zip(jar)
            patchouli = patchouli_files_in_zip(jar)
        except (zipfile.BadZipFile, OSError) as exc:
            print(f"[跳过] 无法读取 {jar}: {exc}", file=sys.stderr)
            continue
        result.english.extend(x for x in files if x.locale == "en_us")
        result.community.extend(x for x in files if x.locale == "zh_cn")
        result.patchouli_english.extend(x for x in patchouli if x.locale == "en_us")
        result.patchouli_community.extend(x for x in patchouli if x.locale == "zh_cn")
    kubejs = (instance or mods_dir.parent) / "kubejs" / "assets"
    if kubejs.is_dir():
        for lang in sorted(kubejs.glob("*/lang/*")):
            match = re.fullmatch(r"(en_us|zh_cn)\.(json|lang)", lang.name, re.IGNORECASE)
            if not lang.is_file() or not match:
                continue
            try:
                data = _parse_lang_raw(lang.read_bytes(), match.group(2).lower(), str(lang))
            except (OSError, ValueError) as exc:
                LOG.warning("Skipping KubeJS language file %s: %s", lang, exc)
                continue
            file = LangFile(lang.parent.parent.name, match.group(1).lower(), str(lang), data)
            (result.english if file.locale == "en_us" else result.community).append(file)
    if resourcepacks_dir and resourcepacks_dir.is_dir():
        enabled = enabled_resource_packs(instance or resourcepacks_dir.parent)
        for pack in sorted(resourcepacks_dir.iterdir()):
            try:
                if pack.is_file() and pack.suffix.lower() in {".zip", ".jar"}:
                    files = language_files_in_zip(pack)
                    patchouli = patchouli_files_in_zip(pack)
                elif pack.is_dir():
                    files = language_files_in_directory(pack)
                    patchouli = []
                    for file in sorted(pack.rglob("*.json")):
                        match = PATCHOULI_RE.fullmatch(file.relative_to(pack).as_posix())
                        if match:
                            try:
                                data = json.loads(file.read_text(encoding="utf-8-sig"))
                                if isinstance(data, dict):
                                    patchouli.append(PatchouliFile(match.group(1), match.group(2), match.group(4), data, str(file), match.group(3).lower(), (pack / AI_MARKER).exists()))
                            except (OSError, ValueError):
                                LOG.warning("Skipping invalid Patchouli JSON %s", file, exc_info=True)
                else:
                    continue
            except (zipfile.BadZipFile, OSError) as exc:
                print(f"[跳过] 无法读取资源包 {pack}: {exc}", file=sys.stderr)
                continue
            # 只有真正在 options.txt 里启用的包才算「已生效的人工汉化」。
            # 未启用的包里同样可能有汉化组的成果（玩家为了测试而临时关掉很常见），
            # 那些译文**不属于**已生效汉化，不能拿来抵扣缺口 —— 否则摆渡会
            # 报「缺 0 / 已补全」而玩家实际看到英文。
            # 但它们也不该被忘掉：降级进 reference_only，喂给 AI 当术语参考，
            # 这正是「最尊重汉化组」的正确含义。
            # 用户可随时在 options.txt 里启用该包，或用 --trust-disabled-packs
            # 把它们重新算作人工汉化。
            is_enabled = enabled is None or is_resource_pack_enabled(pack, resourcepacks_dir, enabled)
            for file in files:
                if file.locale != "zh_cn":
                    continue
                if is_enabled:
                    (result.ai if file.is_ai_generated else result.community).append(file)
                else:
                    result.reference_only.setdefault(file.modid, {}).update(file.data)
            for modid, keys in load_revert_marker(pack).items():
                if keys:
                    result.reverted.setdefault(modid, set()).update(keys)
            result.patchouli_community.extend(x for x in patchouli if x.locale == "zh_cn" and not x.is_ai_generated)
            result.patchouli_ai.extend(x for x in patchouli if x.locale == "zh_cn" and x.is_ai_generated)
    return result


def load_revert_marker(pack: Path) -> dict[str, set[str]]:
    """Read the FERRY_REVERTED.json marker from a resource pack (zip/jar or folder)."""
    raw: str | None = None
    try:
        if pack.is_file() and pack.suffix.lower() in {".zip", ".jar"}:
            with zipfile.ZipFile(pack) as zf:
                if REVERT_MARKER in zf.namelist():
                    raw = zf.read(REVERT_MARKER).decode("utf-8-sig", "replace")
        elif pack.is_dir() and (pack / REVERT_MARKER).exists():
            raw = (pack / REVERT_MARKER).read_text(encoding="utf-8-sig", errors="replace")
    except (zipfile.BadZipFile, OSError, ValueError):
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, set[str]] = {}
    for modid, keys in data.items():
        if isinstance(keys, list):
            result[str(modid)] = {str(k) for k in keys}
    return result


def _modid_keys(files: list[LangFile]) -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {}
    for file in files:
        keys.setdefault(file.modid, set()).update(file.data)
    return keys


def build_mod_status(english: list[LangFile], community: list[LangFile], ai: list[LangFile], include_modids: set[str] | None = None, reverted: dict[str, set[str]] | None = None, community_sources: dict[str, set[str]] | None = None) -> list[ModTranslationStatus]:
    include = {x.lower().replace("-", "_") for x in (include_modids or set())}
    en_keys: dict[str, set[str]] = {}
    for file in english:
        if is_excluded_mod(file.modid) and file.modid.lower().replace("-", "_") not in include:
            continue
        en_keys.setdefault(file.modid, set()).update(key for key, value in file.data.items() if value.strip())
    com_keys = _modid_keys(community)
    ai_keys = _modid_keys(ai)
    community_sources = community_sources or {}
    revert_keys = _normalize_reverted(reverted)
    result: list[ModTranslationStatus] = []
    for modid in sorted(en_keys):
        keys = en_keys[modid]
        total = len(keys)
        reverted_set = keys & revert_keys.get(modid, set())
        community_set = (keys & (com_keys.get(modid, set()) | community_sources.get(modid, set()))) - reverted_set
        ai_set = (keys & ai_keys.get(modid, set())) - reverted_set - community_set
        uncovered = len(keys - community_set - ai_set - reverted_set)
        result.append(ModTranslationStatus(modid, total, len(community_set), len(ai_set), len(reverted_set), uncovered))
    return result


def missing_entries(english: list[LangFile], community: list[LangFile], ai: list[LangFile], include_modids: set[str] | None = None, fill_community: bool = False, modids: set[str] | None = None, reverted: dict[str, set[str]] | None = None, refine_community: bool = False) -> list[Entry]:
    include = {x.lower().replace("-", "_") for x in (include_modids or set())}
    modid_filter = {x.lower().replace("-", "_") for x in (modids or set())} if modids else None
    com_keys = _modid_keys(community)
    ai_keys = _modid_keys(ai)
    revert_keys = _normalize_reverted(reverted)
    result: list[Entry] = []
    seen: set[tuple[str, str]] = set()
    for file in english:
        normalized = file.modid.lower().replace("-", "_")
        if is_excluded_mod(file.modid) and normalized not in include:
            continue
        if modid_filter is not None and normalized not in modid_filter:
            continue
        com = com_keys.get(file.modid, set())
        ai = ai_keys.get(file.modid, set())
        reverted_set = revert_keys.get(file.modid, set())
        has_community = bool(com)
        for key, value in file.data.items():
            identity = (file.modid, key)
            if identity in seen or not value.strip():
                continue
            seen.add(identity)
            if key in reverted_set:
                continue
            if has_community and not fill_community and not refine_community:
                continue
            if key in com or key in ai:
                continue
            result.append(Entry(file.modid, key, value, file.path))
    return result


def skip_ignored_entries(entries: list[Entry], config: dict[str, Any]) -> list[Entry]:
    ignored = config.get("skip_keys") or {}
    if not isinstance(ignored, dict):
        return entries
    return [entry for entry in entries if entry.key not in ignored.get(entry.modid, [])]


def entries_with_baseline(scan: ScanResult, baseline: dict[str, dict[str, str]], include: set[str] | None = None, selected: set[str] | None = None, fill_community: bool = False, refine_community: bool = False) -> list[Entry]:
    files = scan.community + [LangFile(modid, "zh_cn", "community-baseline", data) for modid, data in baseline.items()]
    entries = missing_entries(scan.english, files, scan.ai, include, fill_community, selected, scan.reverted, refine_community)
    if not (fill_community or refine_community) and baseline:
        for entry in missing_entries(scan.english, files, scan.ai, include, True, set(baseline) & selected if selected else set(baseline), scan.reverted):
            if entry.modid in baseline and entry not in entries:
                entries.append(entry)
    return entries


def _normalize_reverted(reverted: dict[str, set[str]] | None) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for modid, keys in (reverted or {}).items():
        result[str(modid)] = {str(k) for k in (keys or set())}
    return result


def english_map_for(english: list[LangFile], modids: set[str]) -> dict[str, dict[str, str]]:
    """Collect the full en_us key set (id -> 原英文) for the given modids."""
    wanted = {x.lower().replace("-", "_") for x in (modids or set())}
    result: dict[str, dict[str, str]] = {}
    for file in english:
        if file.modid.lower().replace("-", "_") not in wanted:
            continue
        result.setdefault(file.modid, {}).update(file.data)
    return result
