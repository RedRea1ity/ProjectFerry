"""实例发现与扫描：语言文件 / Patchouli 读取、缺失 key 计算、模组状态构建。
"""

from __future__ import annotations

import io
import json
from copy import deepcopy
import re
import sys
import zipfile

from pathlib import Path
from typing import Any

from ferrycore.base import AI_MARKER, LOG, REVERT_MARKER
from ferrycore.models import Entry, LangFile, ModTranslationStatus, PatchouliFile, ScanResult
from ferrycore.json_components import apply_text_nodes, component_entries, translated_components
from ferrycore.text import _parse_lang_raw, load_json_document


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
        components = scan.reference_only_components.pop(modid, {})
        if not data and not components:
            continue
        scan.community.append(LangFile(modid, "zh_cn", "disabled-pack-promoted", dict(data), components=components))
        promoted += len(data) + len(components)
    scan.reference_only.clear()
    scan.reference_only_components.clear()
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
                    raw = zf.read(name)
                    data = _parse_lang_raw(raw, ext, f"{source}!{name}")
                    # 只有 Localizer 的导出才需要整棵 JSON 树；其它语言文件不重复解析一遍。
                    components = {}
                    if modid.lower() == "ftbquestlocalizer" and ext == "json":
                        components = {key: value for key, value in load_json_document(raw, f"{source}!{name}").items() if isinstance(value, (dict, list))}
                except (ValueError, OSError, zipfile.BadZipFile) as exc:
                    LOG.warning("Skipping language file %s!%s: %s", source, name, exc)
                    continue
                result.append(LangFile(modid, locale, f"{source}!{name}", data, is_ai, components))
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
            raw = file.read_bytes()
            data = _parse_lang_raw(raw, ext, str(file))
            components = {}
            if modid.lower() == "ftbquestlocalizer" and ext == "json":
                components = {key: value for key, value in load_json_document(raw, str(file)).items() if isinstance(value, (dict, list))}
        except (ValueError, OSError) as exc:
            print(f"[跳过] {exc}", file=sys.stderr)
            continue
        result.append(LangFile(modid, locale, str(file), data, is_ai, components))
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


LOCALIZER_MODIDS = {"ftbquestlocalizer"}
FTB_NAMESPACE = "ftbquestlocalizer"


def is_ftb_namespace(modid: str) -> bool:
    """FTB Localizer 的命名空间只有一种写法；这里统一归一化，避免各处比较不一致。"""
    return modid.lower().replace("-", "_") == FTB_NAMESPACE


def localizer_installed(mods_dir: Path) -> bool:
    """Is FTB Quest Localizer present? Prefer jar metadata over the file name."""
    jars = sorted(mods_dir.glob("*.jar"))
    if any(path.name.lower().startswith(("ftbquestlocalizer", "ftb-quest-localizer", "ftb_quest_localizer")) for path in jars):
        return True
    # 文件名被改过也认得出：读模组元数据里的 modId。只在没有导出文件时才走这条，
    # 避免每次扫描都拆一遍所有 jar。
    from ferrycore.literalbridge import read_mod_jar_info

    for path in jars:
        try:
            info = read_mod_jar_info(path)
        except (OSError, zipfile.BadZipFile, ValueError):
            continue
        if str(info.get("modid") or "").lower().replace("-", "_") in LOCALIZER_MODIDS:
            return True
    return False


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
        for file in files:
            if file.locale == "en_us":
                if file.modid.lower() == "ftbquestlocalizer" and file.components:
                    # jar 里的富文本不作为翻译来源，但它同一份文件里的普通字符串照常要翻。
                    file.components = {}
                result.english.append(file)
            else:
                result.community.append(file)
        result.patchouli_english.extend(x for x in patchouli if x.locale == "en_us")
        result.patchouli_community.extend(x for x in patchouli if x.locale == "zh_cn")
    kubejs = (instance or mods_dir.parent) / "kubejs" / "assets"
    localizer_lang = (instance or mods_dir.parent) / "kubejs" / "assets" / "ftbquestlocalizer" / "lang" / "en_us.json"
    if localizer_lang.is_file():
        result.ftb_localizer_installed = True
        try:
            document = load_json_document(localizer_lang.read_bytes(), str(localizer_lang))
            result.ftb_localizer = {key: value for key, value in document.items() if isinstance(value, (dict, list))}
            result.ftb_localizer_source = str(localizer_lang)
        except (OSError, ValueError) as exc:
            LOG.warning("Skipping FTB Quest Localizer export %s: %s", localizer_lang, exc)
    if not result.ftb_localizer_installed:
        result.ftb_localizer_installed = localizer_installed(mods_dir)
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
            modid = lang.parent.parent.name
            components = {}
            if match.group(2).lower() == "json":
                try:
                    document = load_json_document(lang.read_bytes(), str(lang))
                    structured = {key: value for key, value in document.items() if isinstance(value, (dict, list))}
                except (OSError, ValueError) as exc:
                    LOG.warning("Skipping structured language JSON %s: %s", lang, exc)
                    structured = {}
                if structured and modid.lower() == "ftbquestlocalizer":
                    components = structured
                elif structured:
                    # 结构化值只在 FTB Localizer 的命名空间里有意义；别处出现时留个线索，
                    # 方便用户反馈「导出文件落在别的命名空间」。
                    LOG.info("Structured lang values under %s are left untouched (expected namespace: ftbquestlocalizer)", modid)
            file = LangFile(modid, match.group(1).lower(), str(lang), data, components=components)
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
                    if file.components:
                        result.reference_only_components.setdefault(file.modid, {}).update(file.components)
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


def missing_ftb_entries(scan: ScanResult, pack: Path, modids: set[str] | None = None) -> list[Entry]:
    """Return missing Localizer component text entries; scalar lang keys use normal flow."""
    if not scan.ftb_localizer:
        return []
    from ferrycore.packio import load_pack_ftb_components, load_pack_ftb_sources, load_uninstalled_ai

    modid = "ftbquestlocalizer"
    wanted = {item.lower().replace("-", "_") for item in modids} if modids else None
    if wanted is not None and modid not in wanted:
        return []
    reverted = _normalize_reverted(scan.reverted).get(modid, set())
    reverted_paths = {key for key in reverted if key in scan.ftb_localizer}
    uninstalled = load_uninstalled_ai(pack)
    if modid in {m.lower().replace("-", "_") for m in uninstalled}:
        return []
    source_entries = component_entries(modid, scan.ftb_localizer, scan.ftb_localizer_source)
    # 人工汉化给出的语言 key 整条让位：同一个 key 只可能由更高优先级的包整体生效，
    # 摆渡再写一份数组反而会盖掉人工译文。
    human_paths = {entry.rel_path for file in scan.community if file.modid.lower() == modid for entry in component_entries(modid, file.components, file.path)}
    existing = load_pack_ftb_components(pack)
    existing_text = {entry.key: entry.english for entry in component_entries(modid, existing, str(pack))}
    old_sources = load_pack_ftb_sources(pack)
    result: list[Entry] = []
    for entry in source_entries:
        if entry.key in reverted or entry.rel_path in reverted_paths or entry.rel_path in human_paths:
            continue
        prior = existing_text.get(entry.key)
        if prior is not None and prior != entry.english:
            # 已译条目：只有记录在案的英文原稿和当前导出不一致时才算过时，需要重译。
            old_english = old_sources.get(entry.rel_path, {}).get(entry.json_pointer)
            if old_english is None or old_english == entry.english:
                continue
        result.append(entry)
    return result


def translated_ftb_components(scan: ScanResult, entries: list[Entry], translations: dict[str, dict[str, str]], pack: Path) -> dict[str, Any]:
    """Merge translated component text into prior packaged structures, preserving partial progress."""
    from ferrycore.packio import load_pack_ftb_components, load_pack_ftb_sources, load_uninstalled_ai

    modid = "ftbquestlocalizer"
    uninstalled = load_uninstalled_ai(pack)
    if modid in {m.lower().replace("-", "_") for m in uninstalled}:
        return load_pack_ftb_components(pack)
    result = load_pack_ftb_components(pack)
    if not scan.ftb_localizer:
        return result
    old_sources = load_pack_ftb_sources(pack)
    effective_reverted = _normalize_reverted(scan.reverted).get(modid, set())
    reverted_paths = {key for key in effective_reverted if key in scan.ftb_localizer}
    human_paths = {entry.rel_path for file in scan.community if file.modid.lower() == modid for entry in component_entries(modid, file.components, file.path)}
    for path in human_paths:
        result.pop(path, None)
    translated_keys = {
        entry.rel_path for entry in entries
        if entry.key in translations.get(modid, {})
    }
    source_entries = component_entries(modid, scan.ftb_localizer, scan.ftb_localizer_source)
    # 导出结构可能增删文本节点（任务描述加一行就够了）。旧包里的结构不能当底稿，
    # 否则多出来的英文段会被丢掉；结构一变就以当前导出为底，只带回英文原稿没变的旧译文。
    current_text = {(entry.rel_path, entry.json_pointer): entry.english for entry in source_entries}
    prior_text = {(entry.rel_path, entry.json_pointer): entry.english for entry in component_entries(modid, result, str(pack))}
    for key in list(result):
        if key not in scan.ftb_localizer:
            continue
        prior_pointers = {pointer for (path, pointer) in prior_text if path == key}
        current_pointers = {pointer for (path, pointer) in current_text if path == key}
        if prior_pointers == current_pointers:
            continue
        carried = {
            pointer: prior_text[(key, pointer)]
            for pointer in prior_pointers & current_pointers
            if old_sources.get(key, {}).get(pointer) == current_text[(key, pointer)]
        }
        rebuilt = deepcopy(scan.ftb_localizer[key])
        result[key] = apply_text_nodes(rebuilt, carried) if carried else rebuilt
    required_keys = set(translated_keys) | reverted_paths
    required_keys.update(
        entry.rel_path for entry in source_entries if entry.key in effective_reverted
    )
    for key in required_keys:
        if key not in result and key in scan.ftb_localizer:
            result[key] = deepcopy(scan.ftb_localizer[key])
    # 导出内容改过的指针先退回新英文，随后按缺译条目重翻，避免一直显示旧译文。
    for entry in source_entries:
        old_english = old_sources.get(entry.rel_path, {}).get(entry.json_pointer)
        if entry.rel_path in result and old_english is not None and old_english != entry.english:
            result[entry.rel_path] = apply_text_nodes(result[entry.rel_path], {entry.json_pointer: entry.english})
    for entry in source_entries:
        if entry.key in effective_reverted and entry.rel_path in result:
            result[entry.rel_path] = apply_text_nodes(result[entry.rel_path], {entry.json_pointer: entry.english})
    updated = translated_components(scan.ftb_localizer, entries, translations, modid, result)
    result.update(updated)
    for path in reverted_paths:
        result[path] = deepcopy(scan.ftb_localizer[path])
    valid_paths = {entry.rel_path for entry in source_entries} - human_paths
    return {key: value for key, value in result.items() if key in valid_paths}


def ftb_source_map(scan: ScanResult, components: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Record which English text each emitted FTB pointer was translated from."""
    if not scan.ftb_localizer or not components:
        return {}
    mapping: dict[str, dict[str, str]] = {}
    for entry in component_entries("ftbquestlocalizer", scan.ftb_localizer, scan.ftb_localizer_source):
        if entry.rel_path in components:
            mapping.setdefault(entry.rel_path, {})[entry.json_pointer] = entry.english
    return mapping


def ftb_real_key(key: str) -> str:
    """`ftblocalizer:<lang key>#<pointer>` → `<lang key>`；不是合成 key 时返回空串。"""
    if not key.startswith("ftblocalizer:"):
        return ""
    return key[len("ftblocalizer:"):].split("#", 1)[0]


def _modid_keys(files: list[LangFile]) -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {}
    for file in files:
        keys.setdefault(file.modid, set()).update(file.data)
        if file.components:
            component_keys = {entry.key for entry in component_entries(file.modid, file.components, file.path)}
            keys[file.modid].update(component_keys)
    return keys


def build_mod_status(english: list[LangFile], community: list[LangFile], ai: list[LangFile], include_modids: set[str] | None = None, reverted: dict[str, set[str]] | None = None, community_sources: dict[str, set[str]] | None = None) -> list[ModTranslationStatus]:
    include = {x.lower().replace("-", "_") for x in (include_modids or set())}
    en_keys: dict[str, set[str]] = {}
    for file in english:
        if is_excluded_mod(file.modid) and file.modid.lower().replace("-", "_") not in include:
            continue
        en_keys.setdefault(file.modid, set()).update(key for key, value in file.data.items() if value.strip())
        if file.components:
            en_keys[file.modid].update(entry.key for entry in component_entries(file.modid, file.components, file.path))
    com_keys = _modid_keys(community)
    ai_keys = _modid_keys(ai)
    community_sources = community_sources or {}
    revert_keys = _normalize_reverted(reverted)
    result: list[ModTranslationStatus] = []
    for modid in sorted(en_keys):
        keys = en_keys[modid]
        total = len(keys)
        reverted_set = keys & revert_keys.get(modid, set())
        if modid == "ftbquestlocalizer":
            # 组件的还原标记记的是真实 lang key，要映射回合成 key 才算得上已还原。
            reverted_paths = set(revert_keys.get(modid, set()))
            reverted_set |= {key for key in keys if ftb_real_key(key) in reverted_paths}
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
    return [
        entry for entry in entries
        if entry.key not in ignored.get(entry.modid, []) and ftb_real_key(entry.key) not in ignored.get(entry.modid, [])
    ]


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
