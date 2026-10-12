"""资源包读写：生成 / 卸载 / 还原 / 让位，锁文件与质量检查。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import zipfile

from pathlib import Path
from typing import Any

from ferrycore.base import AI_MARKER, COMMUNITY_NOTICE, LOCKS_FILE, LOG, REVERT_MARKER, SOURCES_MARKER
from ferrycore.engine import translate_entries
from ferrycore.models import Entry, LangFile, ScanResult
from ferrycore.scan import PATCHOULI_RE, _modid_keys, _normalize_reverted, english_map_for, load_pack_patchouli, load_revert_marker
from ferrycore.text import _parse_lang_raw, placeholder_tokens
from ferrycore.versions import clean_pack_format, read_pack_format, split_pack_format, uses_min_max_format


def _write_pack_file(output: Path, translations: dict[str, dict[str, str]], pack_format: int | float, reverted: dict[str, set[str]] | None = None, version_unconfirmed: bool = False, patchouli: dict[str, dict[str, Any]] | None = None, sources: dict[str, dict[str, str]] | None = None, yield_history: list[str] | None = None, pack_progress=None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    clean_reverted = {modid: sorted(keys) for modid, keys in _normalize_reverted(reverted).items() if keys}
    yield_history = yield_history or []
    number = clean_pack_format(pack_format)
    pack_meta: dict[str, Any] = {"description": "摆渡计划 · AI 临时汉化（低优先级，占位稿）"}
    if uses_min_max_format(float(pack_format)):
        # 1.21.9 / 25w31a 起改用 min_format + max_format，取值可为整数或 [major, minor]。
        major, minor = split_pack_format(pack_format)
        value: Any = [major, minor] if minor else major
        pack_meta["min_format"] = value
        pack_meta["max_format"] = value
    else:
        pack_meta["pack_format"] = number
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pack.mcmeta", json.dumps({"pack": pack_meta}, ensure_ascii=False, indent=2))
        zf.writestr(AI_MARKER, "摆渡计划 / ProjectFerry —— 人无语言则茫然无依，故有摆渡。\n本资源包仅补人工汉化未覆盖的 en_us key；可选模式读取人工译文作为参考语料，不覆盖已有人工译文。\n检测到人工/官方汉化时自动让位（摆渡到岸即离）。\n\n"
            + ("摆渡退场记录（这些都是摆渡主动撤回的，不是翻译失败）：\n" + "\n".join(f"  · {line}" for line in yield_history) + "\n" if yield_history else ""))
        if version_unconfirmed:
            zf.writestr("README_VERSION_UNCONFIRMED.txt", "版本未确认：pack_format 15 仅为 --yes 强制回退。请核对 Minecraft 版本，可能无法加载本资源包。\n")
        write_total = len(translations) + len(patchouli or {})
        write_done = 0

        def emit_pack_progress(name: str) -> None:
            # 写包进度回调：按命名空间上报，UI 显示「写入资源包 · n/N · ns」。
            if pack_progress:
                try:
                    pack_progress(write_done, write_total, name)
                except Exception as exc:
                    LOG.warning("pack_progress callback failed: %s", type(exc).__name__)

        for modid, data in sorted(translations.items()):
            emit_pack_progress(modid)
            write_done += 1
            if pack_format <= 3:
                if any("\n" in key or "\r" in key for key in data):
                    raise ValueError(f"{modid} 的语言 key 包含换行，无法写入旧版 .lang")
                # 3.11 的 f-string 表达式里不允许反斜杠，换行转义先在表达式外拼好。
                escaped_newline = chr(92) + "n"
                content = "\n".join(f"{key}={value.replace(chr(13), '').replace(chr(10), escaped_newline)}" for key, value in sorted(data.items())) + "\n"
                # 1.11（pack_format 3）起资源包内文件名要求全小写；1.6.1–1.10.2 的语言区域代码仍是 zh_CN。
                lang_name = "zh_cn.lang" if pack_format >= 3 else "zh_CN.lang"
                zf.writestr(f"assets/{modid}/lang/{lang_name}", content.encode("utf-8"))
            else:
                zf.writestr(f"assets/{modid}/lang/zh_cn.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        for path, data in sorted((patchouli or {}).items()):
            emit_pack_progress(path)
            write_done += 1
            if not PATCHOULI_RE.fullmatch(path) or "/zh_cn/" not in path.lower():
                raise ValueError(f"Invalid Patchouli output path: {path}")
            zf.writestr(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        if clean_reverted:
            zf.writestr(REVERT_MARKER, json.dumps(clean_reverted, ensure_ascii=False, indent=2) + "\n")
        sources = sources or {modid: {key: "ai" for key in data} for modid, data in translations.items()}
        sources = {modid: {key: source for key, source in entries.items() if key in translations.get(modid, {}) and source in {"ai", "community"}} for modid, entries in sources.items()}
        zf.writestr(SOURCES_MARKER, json.dumps(sources, ensure_ascii=False, indent=2) + "\n")
        if any("community" in entries.values() for entries in sources.values()):
            zf.writestr(COMMUNITY_NOTICE, "部分译文来源：CFPAOrg/Minecraft-Mod-Language-Package 社区汉化项目。原作者保留其版权；请遵守原项目许可证与署名要求。\nhttps://github.com/CFPAOrg/Minecraft-Mod-Language-Package\n")


def write_pack(output: Path, translations: dict[str, dict[str, str]], pack_format: int | float, reverted: dict[str, set[str]] | None = None, version_unconfirmed: bool = False, patchouli: dict[str, dict[str, Any]] | None = None, sources: dict[str, dict[str, str]] | None = None, yield_history: list[str] | None = None, pack_progress=None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=output.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
        _write_pack_file(temporary, translations, pack_format, reverted, version_unconfirmed, patchouli, sources, yield_history, pack_progress=pack_progress)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_pack_reverted(output: Path) -> dict[str, set[str]]:
    return load_revert_marker(output)


def load_pack_translations(output: Path) -> dict[str, dict[str, str]]:
    if not output.exists():
        return {}
    result: dict[str, dict[str, str]] = {}
    try:
        with zipfile.ZipFile(output) as zf:
            for name in zf.namelist():
                match = re.fullmatch(r"assets/([^/]+)/lang/zh_cn\.(json|lang)", name, re.IGNORECASE)
                if not match:
                    continue
                modid = match.group(1)
                try:
                    data = _parse_lang_raw(zf.read(name), match.group(2).lower(), f"{output}!{name}")
                    result.setdefault(modid, {}).update(data)
                except ValueError:
                    continue
    except (zipfile.BadZipFile, OSError):
        pass
    return result


def migrate_legacy_pack(pack: Path, pack_format: int) -> bool:
    """Replace an old ProjectFerry JSON language pack with a 1.6–1.12 .lang pack."""
    if pack_format > 3 or not pack.is_file():
        return False
    try:
        with zipfile.ZipFile(pack) as zf:
            names = set(zf.namelist())
            if AI_MARKER not in names or not any(re.fullmatch(r"assets/[^/]+/lang/zh_cn\.json", name, re.IGNORECASE) for name in names):
                return False
            version_unconfirmed = "README_VERSION_UNCONFIRMED.txt" in names
    except (OSError, zipfile.BadZipFile):
        return False
    translations = load_pack_translations(pack)
    sources = load_pack_sources(pack)
    reverted = load_pack_reverted(pack)
    pages = load_pack_patchouli(pack)
    backup = pack.with_name(pack.name + ".pre-lang-backup")
    fd, name = tempfile.mkstemp(dir=pack.parent, suffix=".zip")
    os.close(fd)
    temporary = Path(name)
    try:
        write_pack(temporary, translations, pack_format, reverted, version_unconfirmed, pages, sources)
        if not backup.exists():
            shutil.copy2(pack, backup)
        os.replace(temporary, pack)
    finally:
        temporary.unlink(missing_ok=True)
    LOG.info("Migrated legacy language pack to .lang: %s", pack)
    return True


def load_pack_sources(output: Path) -> dict[str, dict[str, str]]:
    if not output.is_file():
        return {}
    try:
        with zipfile.ZipFile(output) as zf:
            raw = json.loads(zf.read(SOURCES_MARKER).decode("utf-8-sig"))
            return {modid: {key: source for key, source in entries.items() if source in {"ai", "community"}} for modid, entries in raw.items() if isinstance(modid, str) and isinstance(entries, dict)}
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, AttributeError):
        return {}


def load_locks(path: Path = LOCKS_FILE) -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {modid: {key: value for key, value in data.items() if isinstance(key, str) and isinstance(value, str)} for modid, data in raw.items() if isinstance(data, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


def lock_translation(modid: str, key: str, value: str, path: Path = LOCKS_FILE) -> None:
    locks = load_locks(path)
    locks.setdefault(modid, {})[key] = value
    path.write_text(json.dumps(locks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def remove_pack_keys(pack: Path, keys: dict[str, set[str]], locks_path: Path = LOCKS_FILE) -> int:
    translations = load_pack_translations(pack)
    locks = load_locks(locks_path)
    sources = load_pack_sources(pack)
    removed = 0
    for modid, selected in keys.items():
        for key in selected:
            if key not in locks.get(modid, {}) and key in translations.get(modid, {}) and sources.get(modid, {}).get(key, "ai") == "ai":
                del translations[modid][key]
                removed += 1
        if not translations.get(modid):
            translations.pop(modid, None)
    if removed:
        pages = load_pack_patchouli(pack)
        if translations or pages:
            write_pack(pack, translations, read_pack_format(pack) or 15, load_pack_reverted(pack), patchouli=pages, sources=load_pack_sources(pack))
        else:
            pack.unlink()
    return removed


def quality_issues(scan: ScanResult, pack: Path, locks_path: Path = LOCKS_FILE) -> list[tuple[str, str, str, str, str, str]]:
    english = english_map_for(scan.english, {file.modid for file in scan.english})
    translations = load_pack_translations(pack)
    sources = load_pack_sources(pack)
    locks = load_locks(locks_path)
    result: list[tuple[str, str, str, str, str, str]] = []
    for modid, data in translations.items():
        for key, target in data.items():
            if key in locks.get(modid, {}) or sources.get(modid, {}).get(key, "ai") != "ai":
                continue
            source = english.get(modid, {}).get(key)
            if source is None:
                result.append((modid, key, "", target, "陈旧 key", "警告"))
                continue
            issue = ""
            severity = "错误"
            if not target.strip():
                issue = "空译文"
            elif source == target:
                issue = "译文等于原文"
            elif sorted(placeholder_tokens(source)) != sorted(placeholder_tokens(target)):
                issue = "占位符丢失或错乱"
            elif re.search(r"§(?![0-9a-fk-orx])", target, re.IGNORECASE):
                issue = "格式码错乱"
            elif not re.search(r"[\u3400-\u9fff]", target) and len(source) > 15:
                issue, severity = "疑似残留英文", "警告"
            elif len(target) > len(source) * 3 or len(target) * 5 < len(source):
                issue, severity = "长度异常", "警告"
            if issue:
                result.append((modid, key, source, target, issue, severity))
    return result


def merged_sources(pack: Path, translations: dict[str, dict[str, str]], new_sources: dict[str, dict[str, str]] | None = None) -> dict[str, dict[str, str]]:
    old = load_pack_sources(pack)
    new_sources = new_sources or {}
    return {modid: {key: new_sources.get(modid, {}).get(key, old.get(modid, {}).get(key, "ai")) for key in data} for modid, data in translations.items()}


def retranslate_pack_keys(scan: ScanResult, pack: Path, selected: dict[str, set[str]], settings: dict[str, Any]) -> tuple[int, list[str]]:
    locks = load_locks()
    human = _modid_keys(scan.community)
    sources = load_pack_sources(pack)
    original = english_map_for(scan.english, set(selected))
    entries = [Entry(modid, key, original[modid][key], "QC") for modid, keys in selected.items() for key in keys if key in original.get(modid, {}) and key not in locks.get(modid, {}) and key not in human.get(modid, set()) and sources.get(modid, {}).get(key, "ai") == "ai"]
    if not entries:
        return 0, []
    translated, errors, _ = translate_entries(entries, settings, bypass_cache=True)
    if translated:
        merged = merge_pack_translations(pack, translated, scan.community)
        write_pack(pack, merged, settings["pack_format"], load_pack_reverted(pack), patchouli=load_pack_patchouli(pack), sources=merged_sources(pack, merged))
    return sum(map(len, translated.values())), errors


def merge_pack_translations(output: Path, new_translations: dict[str, dict[str, str]], community: list[LangFile] | None = None) -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, str]] = {modid: dict(data) for modid, data in load_pack_translations(output).items()}
    for modid, data in new_translations.items():
        if data:
            merged.setdefault(modid, {}).update(data)
    human_keys = _modid_keys(community or [])
    reverted = load_pack_reverted(output)
    locks = load_locks()
    for modid, data in list(merged.items()):
        for key in human_keys.get(modid, set()) - reverted.get(modid, set()) - locks.get(modid, {}).keys():
            data.pop(key, None)
        for key, value in locks.get(modid, {}).items():
            if key in data:
                data[key] = value
        if not data:
            del merged[modid]
    return merged


class YieldReport:
    """一次「摆渡到岸即离」的结果记录：撤了多少条、撤了哪些、包还在不在。

    定义 __bool__ 让旧的 `assertTrue(yield_to_community(...))` 断言继续成立。
    """

    def __init__(self, keys: dict[str, int] | None = None, pages: int = 0, pack_removed: bool = False):
        self.keys: dict[str, int] = keys or {}
        self.pages = pages
        self.pack_removed = pack_removed

    def __bool__(self) -> bool:
        return bool(self.keys) or bool(self.pages)

    @property
    def key_count(self) -> int:
        return sum(self.keys.values())

    @property
    def modids(self) -> list[str]:
        return sorted(self.keys)

    def merge(self, other: "YieldReport") -> "YieldReport":
        """一次操作扫过多个实例时合并报告。"""
        merged: dict[str, int] = dict(self.keys)
        for modid, count in other.keys.items():
            merged[modid] = merged.get(modid, 0) + count
        return YieldReport(merged, self.pages + other.pages, self.pack_removed or other.pack_removed)

    def summary(self) -> str:
        """给状态栏用的一句人话，说清楚摆渡刚才撤了什么。"""
        parts = [f"撤回自己此前生成的 {self.key_count} 条译文"]
        if self.pages:
            parts.append(f"含 {self.pages} 页手册")
        detail = "、".join(parts)
        if self.pack_removed:
            return f"{detail}，摆渡包已空并删除"
        if self.modids:
            if len(self.modids) <= 3:
                detail += f"（{'、'.join(self.modids)}）"
            else:
                detail += f"（{'、'.join(self.modids[:3])} 等 {len(self.modids)} 个模组）"
        return detail


def load_yield_history(output: Path) -> list[str]:
    """读回摆渡包内已记录的退场历史（每次退场追加一行，最新在最后）。"""
    try:
        with zipfile.ZipFile(output) as zf:
            if AI_MARKER not in zf.namelist():
                return []
            notice = zf.read(AI_MARKER).decode("utf-8", "replace")
    except (OSError, zipfile.BadZipFile, KeyError):
        return []
    lines: list[str] = []
    in_section = False
    for line in notice.splitlines():
        stripped = line.strip()
        if stripped.startswith("摆渡退场记录"):
            in_section = True
            continue
        if in_section:
            if stripped.startswith("·"):
                lines.append(stripped.lstrip("· "))
            elif stripped:
                in_section = False
    return lines


def yield_to_community(scan: ScanResult, pack: Path) -> YieldReport:
    """Remove this tool's overrides when human translations appear for the same keys."""
    empty = YieldReport()
    if not pack.is_file():
        return empty
    try:
        with zipfile.ZipFile(pack) as zf:
            if AI_MARKER not in zf.namelist():
                return empty
    except (zipfile.BadZipFile, OSError):
        return empty
    translations = load_pack_translations(pack)
    original_sources = load_pack_sources(pack)
    patchouli = load_pack_patchouli(pack)
    human_pages = {f"assets/{file.modid}/patchouli_books/{file.book}/zh_cn/entries/{file.rel_path}" for file in scan.patchouli_community}
    removed_pages = human_pages & patchouli.keys()
    for page in removed_pages:
        del patchouli[page]
    reverted = load_pack_reverted(pack)
    community_keys = _modid_keys(scan.community)
    removed: dict[str, set[str]] = {}
    locks = load_locks()
    for modid, data in list(translations.items()):
        overlap = (set(data) & community_keys.get(modid, set())) - reverted.get(modid, set()) - locks.get(modid, {}).keys()
        if not overlap:
            continue
        removed[modid] = overlap
        for key in overlap:
            del data[key]
        if not data:
            del translations[modid]
        if modid in reverted:
            reverted[modid].difference_update(overlap)
            if not reverted[modid]:
                del reverted[modid]
    if not removed and not removed_pages:
        return empty
    pack_removed = False
    report = YieldReport(
        keys={modid: len(keys) for modid, keys in removed.items()},
        pages=len(removed_pages),
        pack_removed=pack_removed,
    )
    if translations or reverted or patchouli:
        # 退场历史累积：读出包里已有的记录，把这次追加进去再写回。
        history = load_yield_history(pack)
        stamp = time.strftime("%Y-%m-%d %H:%M")
        history.append(f"[{stamp}] {report.summary()}")
        write_pack(pack, translations, read_pack_format(pack) or 15, reverted, patchouli=patchouli, sources=original_sources, yield_history=history[-20:])
    else:
        pack.unlink()
        pack_removed = True
        report.pack_removed = True
    for file in scan.ai:
        if file.path.startswith(f"{pack}!"):
            for key in removed.get(file.modid, set()):
                file.data.pop(key, None)
    scan.ai = [file for file in scan.ai if file.data]
    scan.patchouli_ai = [file for file in scan.patchouli_ai if f"assets/{file.modid}/patchouli_books/{file.book}/zh_cn/entries/{file.rel_path}" not in removed_pages]
    for modid, keys in removed.items():
        if modid in scan.reverted:
            scan.reverted[modid].difference_update(keys)
    return YieldReport(
        keys={modid: len(keys) for modid, keys in removed.items()},
        pages=len(removed_pages),
        pack_removed=pack_removed,
    )


def uninstalled_ai_path(pack: Path) -> Path:
    return pack.with_name(pack.name + ".uninstalled.json")


def load_uninstalled_ai(pack: Path) -> dict[str, dict[str, str]]:
    path = uninstalled_ai_path(pack)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        modid: {key: value for key, value in entries.items() if isinstance(key, str) and isinstance(value, str)}
        for modid, entries in raw.items()
        if isinstance(modid, str) and isinstance(entries, dict)
    }


def _save_uninstalled_ai(pack: Path, saved: dict[str, dict[str, str]]) -> None:
    path = uninstalled_ai_path(pack)
    if not saved:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def remove_ai_translations(pack: Path, modids: set[str]) -> list[str]:
    """Uninstall AI translations, retaining them beside the pack for a later reload."""
    if not pack.is_file():
        return []
    try:
        with zipfile.ZipFile(pack) as zf:
            if AI_MARKER not in zf.namelist():
                return []
    except (OSError, zipfile.BadZipFile):
        return []
    translations = load_pack_translations(pack)
    sources = load_pack_sources(pack)
    reverted = load_pack_reverted(pack)
    saved = load_uninstalled_ai(pack)
    removed: list[str] = []
    for modid in modids:
        data = translations.get(modid, {})
        ai_data = {key: value for key, value in data.items() if key not in reverted.get(modid, set()) and sources.get(modid, {}).get(key, "ai") == "ai"}
        if not ai_data:
            continue
        saved.setdefault(modid, {}).update(ai_data)
        for key in ai_data:
            del data[key]
        if not data:
            del translations[modid]
        removed.append(modid)
    if removed:
        _save_uninstalled_ai(pack, saved)
        pages = load_pack_patchouli(pack)
        if translations or reverted or pages:
            write_pack(pack, translations, read_pack_format(pack) or 15, reverted, patchouli=pages, sources=sources)
        else:
            pack.unlink()
    return removed


def reload_ai_translations(pack: Path, modids: set[str], scan: ScanResult, pack_format: int) -> list[str]:
    """Restore saved AI keys that still exist in English and lack a human translation."""
    saved = load_uninstalled_ai(pack)
    human_keys = _modid_keys(scan.community)
    english = english_map_for(scan.english, modids)
    reverted = load_pack_reverted(pack)
    restored: dict[str, dict[str, str]] = {}
    for modid in modids:
        if modid not in saved or modid not in english:
            continue
        eligible = {
            key: value for key, value in saved[modid].items()
            if english[modid].get(key, "").strip()
            and key not in human_keys.get(modid, set())
            and key not in reverted.get(modid, set())
        }
        if eligible:
            restored[modid] = eligible
        del saved[modid]
    if restored:
        merged = merge_pack_translations(pack, restored, scan.community)
        write_pack(pack, merged, pack_format, reverted, patchouli=load_pack_patchouli(pack), sources=merged_sources(pack, merged))
    _save_uninstalled_ai(pack, saved)
    return sorted(restored)


def revert_mods(pack: Path, english_map: dict[str, dict[str, str]], pack_format: int) -> list[str]:
    """Write English values as zh_cn overrides so the given mods show English, and mark them reverted."""
    if not english_map:
        return []
    translations = load_pack_translations(pack)
    reverted = load_pack_reverted(pack)
    for modid, data in english_map.items():
        translations[modid] = {key: value for key, value in data.items()}
        reverted[modid] = set(data.keys())
    write_pack(pack, translations, pack_format, reverted, patchouli=load_pack_patchouli(pack), sources=load_pack_sources(pack))
    return list(english_map.keys())
