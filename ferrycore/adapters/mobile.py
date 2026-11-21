"""移动端平台适配器 —— Android (`.apk`) 与 iOS (`.ipa` / `.app`)。

移动端游戏（不论是原生开发，还是由 RPG Maker MV/MZ、TyranoScript、吉里吉里 2/KrkrZ、
Godot、Unity、Cocos2d-x 移植打包）在分发时都被封装进两类标准容器：

1. **Android `.apk` (`ApkAdapter`)**：
   - 本质是 ZIP 归档，内含 `AndroidManifest.xml`、`resources.arsc`、`classes.dex` 与 `assets/`。
   - 支持直接抽取与回写：
     * `assets/` 下的 RPG Maker JSON（`System.json` / `MapNNN.json` 等）与通用 JSON 词表；
     * `assets/` 下的 `.ks`（Tyrano / 吉里吉里移植版）、`.tscn`（Godot）、`.po`、`.csv`、`.txt`；
     * 明文 `res/values*/strings.xml`；
     * `classes.dex`（Dalvik Executable `dex\\n035\\0`）：纯标准库解析 `string_ids` MUTF-8 字符串池，
       支持等字节长度内原位覆写，并**自动重算 DEX 头部 SHA-1 签名（offset 12）与 Adler32 校验和（offset 8）**。
   - 重建 `.cn.apk` 时自动保持 `resources.arsc` 为 `ZIP_STORED`（不压缩，符合 Android 11+ PackageParser 要求），
     并自动剔除失效的 `META-INF/*.SF` / `*.RSA` / `*.DSA` 旧签名块以便重新签名安装。

2. **iOS `.ipa` / `Payload/*.app` (`IpaAdapter`)**：
   - 本质是含 `Payload/<App>.app/` 的 ZIP 归档。
   - 支持直接抽取与回写：
     * Apple 标准本地化文件 `*.lproj/*.strings` —— **同时支持明文 `"key" = "value";`（UTF-8 / UTF-16）
       与编译后二进制 `bplist00` 格式（基于标准库 `plistlib` 无损读写）**；
     * `Payload/*.app/` 内的游戏剧本与词表（`.json` / `.ks` / `.tscn` / `.po` / `.csv`）。
   - 重建 `.cn.ipa` 时严格保留 ZIP 条目的 `external_attr`（UNIX `0755` 可执行权限位），防止实机或模拟器闪退。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import plistlib
import re
import struct
import zipfile
import zlib
from pathlib import Path

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .container import unique_output
from .godot import apply_tscn_lines, parse_tscn_lines
from .i18n_table import _iter_json_i18n, apply_po_translations, parse_po_entries
from .kirikiri import apply_ks, parse_ks
from .rpgmaker import iter_texts as iter_rm_texts
from .rpgmaker import set_pointer
from .undo import UNDO_NAME, UndoLog
from .unity import is_unity_translatable

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
_STRINGS_LINE_RE = re.compile(
    r'^(?P<head>\s*"(?P<key>(?:\\.|[^"\\])*)"\s*=\s*")'
    r'(?P<val>(?:\\.|[^"\\])*)'
    r'(?P<tail>"\s*;\s*(?://.*|/\*.*\*/\s*)?)$'
)
_ANDROID_XML_STRING_RE = re.compile(
    r'(<string\b[^>]*\bname\s*=\s*"(?P<name>[^"]+)"[^>]*>)(?P<val>[^<]+)(</string>)'
)
_RM_JSON_NAMES = {
    "System.json", "Actors.json", "Classes.json", "Skills.json", "Items.json",
    "Weapons.json", "Armors.json", "Enemies.json", "Troops.json", "States.json",
    "CommonEvents.json", "MapInfos.json",
}
_RM_MAP_RE = re.compile(r"^Map\d{3}\.json$", re.IGNORECASE)
_META_INF_SIG_RE = re.compile(r"^META-INF/.*\.(?:SF|RSA|DSA|EC)$", re.IGNORECASE)


# ============================================================================
# Dalvik Executable (classes.dex) 字符串表解析与定长原位覆写（含 SHA-1 & Adler32 重算）
# ============================================================================


def _read_uleb128(buf: bytes | bytearray, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            break
        shift += 7
    return result, pos


def extract_dex_strings(dex_bytes: bytes) -> list[tuple[int, int, str]]:
    """从 Android `classes.dex` 抽取玩家可见文本。

    返回 `[(string_idx, data_offset, text), ...]`，其中 `data_offset` 指向 `string_data_item`
    在 DEX 内的绝对偏移（ULEB128 字符长度之后的正文起始位置由 `_patch_dex_strings` 处理）。
    """
    if len(dex_bytes) < 0x70 or not dex_bytes.startswith(b"dex\n"):
        return []
    string_ids_size, string_ids_off = struct.unpack_from("<II", dex_bytes, 0x38)
    if string_ids_size == 0 or string_ids_size > 500000:
        return []
    if string_ids_off + string_ids_size * 4 > len(dex_bytes):
        return []

    results: list[tuple[int, int, str]] = []
    for idx in range(string_ids_size):
        data_off = struct.unpack_from("<I", dex_bytes, string_ids_off + idx * 4)[0]
        if data_off <= 0 or data_off >= len(dex_bytes):
            continue
        try:
            _utf16_len, str_start = _read_uleb128(dex_bytes, data_off)
        except IndexError:
            continue
        nul_pos = dex_bytes.find(b"\x00", str_start, min(len(dex_bytes), str_start + 4096))
        if nul_pos < 0:
            continue
        raw_str = dex_bytes[str_start:nul_pos]
        if len(raw_str) < 3:
            continue
        try:
            text = raw_str.decode("utf-8")
        except UnicodeDecodeError:
            continue
        # DEX 里有大量 Java 类签名（如 Ljava/lang/String;），只取含 CJK 的游戏台词/UI 文本
        if not _CJK_RE.search(text):
            continue
        if text.startswith("L") and ";" in text and "/" in text:
            continue
        if is_unity_translatable(text):
            results.append((idx, data_off, text))
    return results


def patch_dex_strings(dex_bytes: bytes, replacements: dict[str, str]) -> tuple[bytes, int, list[str]]:
    """在 `classes.dex` 的 `string_data_item` 原位定长更新字符串，并重算 DEX SHA-1 与 Adler32。"""
    if len(dex_bytes) < 0x70 or not dex_bytes.startswith(b"dex\n"):
        return dex_bytes, 0, ["不是合法的 classes.dex"]

    buf = bytearray(dex_bytes)
    string_ids_size, string_ids_off = struct.unpack_from("<II", buf, 0x38)
    applied = 0
    conflicts: list[str] = []

    for idx in range(string_ids_size):
        data_off = struct.unpack_from("<I", buf, string_ids_off + idx * 4)[0]
        if data_off <= 0 or data_off >= len(buf):
            continue
        try:
            orig_u16_len, str_start = _read_uleb128(buf, data_off)
        except IndexError:
            continue
        uleb_bytes_len = str_start - data_off
        nul_pos = buf.find(b"\x00", str_start, min(len(buf), str_start + 4096))
        if nul_pos < 0:
            continue
        raw_str = bytes(buf[str_start:nul_pos])
        try:
            src_text = raw_str.decode("utf-8")
        except UnicodeDecodeError:
            continue

        tr = replacements.get(f"DEX:{idx}") or replacements.get(src_text)
        if not tr or not tr.strip() or tr == src_text:
            continue

        # 仅当 ULEB128 头占 1 字节（字符数 < 128）且新 UTF-8 字节数 <= 原字节预算时原位写回
        if uleb_bytes_len != 1 or len(tr) >= 128:
            conflicts.append(f"classes.dex#{idx}：长字符串不支持定长原位覆写")
            continue
        new_utf8 = tr.encode("utf-8")
        max_bytes = len(raw_str)
        if len(new_utf8) > max_bytes:
            conflicts.append(f"classes.dex#{idx}：译文 UTF-8 字节({len(new_utf8)})超出原空间({max_bytes})")
            continue

        # 若短于原字节，用全角或半角空格补齐，保证后续 string_data_item 偏移丝毫不差
        pad_needed = max_bytes - len(new_utf8)
        padded_tr = tr + (" " * pad_needed)
        padded_bytes = padded_tr.encode("utf-8")
        if len(padded_bytes) != max_bytes or len(padded_tr) >= 128:
            conflicts.append(f"classes.dex#{idx}：字节对齐失败")
            continue

        buf[data_off] = len(padded_tr)
        buf[str_start:nul_pos] = padded_bytes
        applied += 1

    if applied > 0:
        # Android DEX 规范：
        # 1. 先算 offset 32..end 的 SHA-1 写入 offset 12..32 (20 bytes)
        sha1_digest = hashlib.sha1(buf[32:]).digest()
        buf[12:32] = sha1_digest
        # 2. 再算 offset 12..end 的 Adler32 写入 offset 8..12 (uint32 LE)
        adler = zlib.adler32(buf[12:]) & 0xFFFFFFFF
        struct.pack_into("<I", buf, 8, adler)

    return bytes(buf), applied, conflicts


# ============================================================================
# Apple .strings（明文 UTF-8/UTF-16 与 bplist00 二进制属性列表）解析与回写
# ============================================================================


def parse_apple_strings(raw: bytes) -> list[tuple[str, str]]:
    """解析 iOS `*.lproj/*.strings`（自动识别 `bplist00`、XML plist 或明文 `"k" = "v";`）。"""
    if raw.startswith(b"bplist00") or raw.lstrip().startswith(b"<?xml"):
        try:
            data = plistlib.loads(raw)
            if isinstance(data, dict):
                return [
                    (str(k), str(v))
                    for k, v in data.items()
                    if isinstance(v, str) and is_unity_translatable(v)
                ]
        except Exception:
            return []

    text = _decode_apple_text(raw)
    if text is None:
        return []
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        m = _STRINGS_LINE_RE.match(line)
        if not m:
            continue
        key = m.group("key").replace('\\"', '"').replace("\\n", "\n")
        val = m.group("val").replace('\\"', '"').replace("\\n", "\n")
        if is_unity_translatable(val):
            out.append((key, val))
    return out


def apply_apple_strings(raw: bytes, mapping: dict[str, str]) -> tuple[bytes, int]:
    """将 `{key_or_src: translated}` 写回 iOS `.strings`，保持原有的二进制 plist 或文本编码。"""
    if raw.startswith(b"bplist00") or raw.lstrip().startswith(b"<?xml"):
        fmt = plistlib.FMT_BINARY if raw.startswith(b"bplist00") else plistlib.FMT_XML
        try:
            data = plistlib.loads(raw)
        except Exception:
            return raw, 0
        if not isinstance(data, dict):
            return raw, 0
        applied = 0
        for k, v in list(data.items()):
            if not isinstance(v, str):
                continue
            tr = mapping.get(str(k)) or mapping.get(v)
            if tr and tr.strip() and tr != v:
                data[k] = tr
                applied += 1
        if applied == 0:
            return raw, 0
        return plistlib.dumps(data, fmt=fmt), applied

    enc = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
    text = _decode_apple_text(raw)
    if text is None:
        return raw, 0
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r\n"))
    lines = text.splitlines()
    applied = 0
    for idx, line in enumerate(lines):
        m = _STRINGS_LINE_RE.match(line)
        if not m:
            continue
        key = m.group("key").replace('\\"', '"').replace("\\n", "\n")
        val = m.group("val").replace('\\"', '"').replace("\\n", "\n")
        tr = mapping.get(key) or mapping.get(val)
        if not tr or not tr.strip() or tr == val:
            continue
        escaped = tr.replace("\\", "\\\\").replace('"', '\\"').replace("\r\n", "\\n").replace("\n", "\\n")
        lines[idx] = f"{m.group('head')}{escaped}{m.group('tail')}"
        applied += 1
    if applied == 0:
        return raw, 0
    rebuilt = newline.join(lines) + (newline if trailing else "")
    return rebuilt.encode(enc), applied


def _decode_apple_text(raw: bytes) -> str | None:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            return None
    for enc in ("utf-8-sig", "utf-8", "cp932", "utf-16-le"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


# ============================================================================
# 通用包内文件抽取与回写（供 APK / IPA / VPK 复用）
# ============================================================================


def collect_entries_from_virtual_files(
    modid: str,
    files: dict[str, bytes],
) -> list[Entry]:
    """从 `{包内相对路径: 字节内容}` 抽取所有受支持格式的文本单元。"""
    entries: list[Entry] = []
    for rel, raw in sorted(files.items()):
        low = rel.lower()
        fname = Path(rel).name

        # 1. Android strings.xml
        if low.endswith(".xml") and ("res/values" in low or fname.lower() == "strings.xml"):
            try:
                xml_text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                continue
            for m in _ANDROID_XML_STRING_RE.finditer(xml_text):
                name_attr = m.group("name")
                val = m.group("val").strip()
                if is_unity_translatable(val):
                    entries.append(
                        Entry(
                            modid=modid,
                            key=f"{rel}#{name_attr}",
                            english=val,
                            source="game",
                            kind="android_xml",
                            rel_path=rel,
                            json_pointer=name_attr,
                        )
                    )
            continue

        # 2. Apple .strings
        if low.endswith(".strings"):
            for k, val in parse_apple_strings(raw):
                entries.append(
                    Entry(
                        modid=modid,
                        key=f"{rel}#{k}",
                        english=val,
                        source="game",
                        kind="apple_strings",
                        rel_path=rel,
                        json_pointer=k,
                    )
                )
            continue

        # 3. Android classes.dex
        if fname.lower() == "classes.dex":
            for s_idx, _off, val in extract_dex_strings(raw):
                entries.append(
                    Entry(
                        modid=modid,
                        key=f"{rel}#DEX:{s_idx}",
                        english=val,
                        source="game",
                        kind="dex",
                        rel_path=rel,
                        json_pointer=f"DEX:{s_idx}",
                    )
                )
            continue

        # 4. JSON（RPG Maker MV/MZ 或通用 i18n JSON）
        if low.endswith(".json") and not fname.lower().startswith(("package", "manifest", "tsconfig", "project")):
            try:
                payload = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError):
                continue
            if fname in _RM_JSON_NAMES or _RM_MAP_RE.match(fname):
                for ptr, text, kind in iter_rm_texts(fname, payload):
                    entries.append(
                        Entry(
                            modid=modid,
                            key=f"{rel}#{ptr}",
                            english=text,
                            source="game",
                            kind=f"rm_{kind}",
                            rel_path=rel,
                            json_pointer=ptr,
                        )
                    )
            else:
                for ptr, text in _iter_json_i18n(payload):
                    entries.append(
                        Entry(
                            modid=modid,
                            key=f"{rel}#{ptr}",
                            english=text,
                            source="game",
                            kind="json",
                            rel_path=rel,
                            json_pointer=ptr,
                        )
                    )
            continue

        # 5. KAG / Tyrano .ks
        if low.endswith(".ks"):
            text = _decode_apple_text(raw)
            if not text:
                continue
            for u in parse_ks(text):
                entries.append(
                    Entry(
                        modid=modid,
                        key=f"{rel}{u.locator}",
                        english=u.text,
                        source="game",
                        kind="ks",
                        rel_path=rel,
                        json_pointer=u.locator,
                    )
                )
            continue

        # 6. Godot .tscn
        if low.endswith(".tscn"):
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                continue
            for lineno, val in parse_tscn_lines(text):
                entries.append(
                    Entry(
                        modid=modid,
                        key=f"{rel}#{lineno}",
                        english=val,
                        source="game",
                        kind="tscn",
                        rel_path=rel,
                        json_pointer=str(lineno),
                    )
                )
            continue

        # 7. Gettext .po
        if low.endswith(".po"):
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                continue
            for lineno, msgid, _msgstr in parse_po_entries(text):
                entries.append(
                    Entry(
                        modid=modid,
                        key=f"{rel}:{lineno}",
                        english=msgid,
                        source="game",
                        kind="po",
                        rel_path=rel,
                        json_pointer=str(lineno),
                    )
                )
            continue

        # 8. CSV 词表
        if low.endswith(".csv"):
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                continue
            rows = list(csv.reader(io.StringIO(text)))
            for r_i, row in enumerate(rows):
                for c_i, cell in enumerate(row):
                    if c_i == 0 and re.fullmatch(r"[A-Za-z0-9_.:/-]+", cell.strip()):
                        continue
                    if is_unity_translatable(cell):
                        entries.append(
                            Entry(
                                modid=modid,
                                key=f"{rel}#{r_i}:{c_i}",
                                english=cell,
                                source="game",
                                kind="csv",
                                rel_path=rel,
                                json_pointer=f"{r_i}:{c_i}",
                            )
                        )
    return entries


def patch_virtual_files(
    files: dict[str, bytes],
    entries: list[Entry],
    translations: dict[str, str],
) -> tuple[dict[str, bytes], int, int, list[str]]:
    """根据 `entries` 和 `translations` 批量回写虚拟文件集，返回 `(modified_files, applied, missed, conflicts)`。"""
    by_file: dict[str, list[Entry]] = {}
    for e in entries:
        by_file.setdefault(e.rel_path, []).append(e)

    modified: dict[str, bytes] = {}
    total_applied = 0
    total_missed = 0
    conflicts: list[str] = []

    for rel, file_entries in by_file.items():
        raw = files.get(rel)
        if raw is None:
            continue
        kind = file_entries[0].kind

        if kind == "android_xml":
            text = raw.decode("utf-8-sig")
            name_map: dict[str, str] = {}
            for e in file_entries:
                tr = translations.get(e.key) or translations.get(e.english)
                if tr and tr.strip():
                    name_map[e.json_pointer] = tr
                else:
                    total_missed += 1

            def _repl_xml(m: re.Match) -> str:
                nonlocal total_applied
                nm = m.group("name")
                if nm in name_map:
                    total_applied += 1
                    escaped = (
                        name_map[nm]
                        .replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                    )
                    return f"{m.group(1)}{escaped}{m.group(4)}"
                return m.group(0)

            if name_map:
                new_xml = _ANDROID_XML_STRING_RE.sub(_repl_xml, text)
                modified[rel] = new_xml.encode("utf-8")

        elif kind == "apple_strings":
            str_map: dict[str, str] = {}
            for e in file_entries:
                tr = translations.get(e.key) or translations.get(e.english)
                if tr and tr.strip():
                    str_map[e.json_pointer] = tr
                    str_map[e.english] = tr
                else:
                    total_missed += 1
            if str_map:
                new_raw, count = apply_apple_strings(raw, str_map)
                if count > 0:
                    modified[rel] = new_raw
                    total_applied += count

        elif kind == "dex":
            dex_map: dict[str, str] = {}
            for e in file_entries:
                tr = translations.get(e.key) or translations.get(e.english)
                if tr and tr.strip():
                    dex_map[e.json_pointer] = tr
                    dex_map[e.english] = tr
                else:
                    total_missed += 1
            if dex_map:
                new_dex, count, dex_conflicts = patch_dex_strings(raw, dex_map)
                conflicts.extend(dex_conflicts)
                if count > 0:
                    modified[rel] = new_dex
                    total_applied += count

        elif kind == " json" or kind == "json" or kind.startswith("rm_"):
            try:
                payload = json.loads(raw.decode("utf-8-sig"))
            except Exception as exc:
                conflicts.append(f"{rel}: {exc}")
                continue
            changed = False
            for e in file_entries:
                tr = translations.get(e.key) or translations.get(e.english)
                if not tr or not tr.strip():
                    total_missed += 1
                    continue
                try:
                    set_pointer(payload, e.json_pointer, tr)
                    changed = True
                    total_applied += 1
                except Exception as exc:
                    conflicts.append(f"{e.key}: {exc}")
            if changed:
                sep = (",", ":") if kind.startswith("rm_") else None
                indent = None if kind.startswith("rm_") else 2
                modified[rel] = json.dumps(payload, ensure_ascii=False, indent=indent, separators=sep).encode("utf-8")

        elif kind == "ks":
            text = _decode_apple_text(raw) or ""
            ks_map: dict[str, str] = {}
            for e in file_entries:
                tr = translations.get(e.key) or translations.get(e.english)
                if tr and tr.strip():
                    ks_map[e.json_pointer] = tr
                    ks_map[e.english] = tr
                else:
                    total_missed += 1
            if ks_map:
                new_text, count, ks_conflicts = apply_ks(text, ks_map)
                conflicts.extend(f"{rel}{c}" for c in ks_conflicts)
                if count > 0:
                    modified[rel] = new_text.encode("utf-8")
                    total_applied += count

        elif kind == "tscn":
            text = raw.decode("utf-8-sig")
            line_map: dict[int, str] = {}
            for e in file_entries:
                tr = translations.get(e.key) or translations.get(e.english)
                if tr and tr.strip():
                    line_map[int(e.json_pointer)] = tr
                else:
                    total_missed += 1
            if line_map:
                new_text, count = apply_tscn_lines(text, line_map)
                if count > 0:
                    modified[rel] = new_text.encode("utf-8")
                    total_applied += count

        elif kind == "po":
            text = raw.decode("utf-8-sig")
            po_map: dict[str, str] = {}
            for e in file_entries:
                tr = translations.get(e.key) or translations.get(e.english)
                if tr and tr.strip():
                    po_map[e.english] = tr
                else:
                    total_missed += 1
            if po_map:
                new_text, count = apply_po_translations(text, po_map)
                if count > 0:
                    modified[rel] = new_text.encode("utf-8")
                    total_applied += count

        elif kind == "csv":
            text = raw.decode("utf-8-sig")
            rows = list(csv.reader(io.StringIO(text)))
            changed = False
            for e in file_entries:
                tr = translations.get(e.key) or translations.get(e.english)
                if not tr or not tr.strip():
                    total_missed += 1
                    continue
                r_s, c_s = e.json_pointer.split(":")
                r_i, c_i = int(r_s), int(c_s)
                if 0 <= r_i < len(rows) and 0 <= c_i < len(rows[r_i]):
                    rows[r_i][c_i] = tr
                    changed = True
                    total_applied += 1
            if changed:
                buf = io.StringIO()
                csv.writer(buf, lineterminator="\n").writerows(rows)
                modified[rel] = buf.getvalue().encode("utf-8")

    return modified, total_applied, total_missed, conflicts


# ============================================================================
# Android APK 适配器
# ============================================================================


def _is_candidate_zip_entry(rel: str) -> bool:
    low = rel.lower()
    fname = Path(low).name
    if fname == "classes.dex" or fname == "strings.xml":
        return True
    if low.endswith((".strings", ".ks", ".tscn", ".po", ".csv")):
        return True
    if low.endswith(".json") and ("assets/" in low or "data/" in low or "locale" in low or "i18n" in low or "payload/" in low):
        return True
    if low.endswith(".xml") and "res/values" in low:
        return True
    return False


class ApkAdapter(Adapter):
    """Android `.apk` 安装包与解包工程适配器。"""

    name = "apk"
    priority = 11

    def _find_apk(self, root: Path) -> Path | None:
        root = Path(root)
        if root.is_file() and root.suffix.lower() == ".apk" and ".cn" not in root.stem.lower():
            return root
        if root.is_dir():
            for p in sorted(root.glob("*.apk")):
                if p.is_file() and ".cn" not in p.stem.lower():
                    return p
        return None

    def _is_unpacked_apk(self, root: Path) -> bool:
        root = Path(root)
        return (
            root.is_dir()
            and (root / "AndroidManifest.xml").is_file()
            and (
                (root / "classes.dex").is_file()
                or (root / "resources.arsc").is_file()
                or (root / "assets").is_dir()
                or (root / "res").is_dir()
            )
        )

    def _load_files(self, root: Path) -> tuple[Path | None, dict[str, bytes]]:
        apk = self._find_apk(root)
        files: dict[str, bytes] = {}
        if apk is not None:
            try:
                with zipfile.ZipFile(apk, "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        if _is_candidate_zip_entry(info.filename):
                            files[info.filename] = zf.read(info.filename)
            except (OSError, zipfile.BadZipFile):
                return apk, {}
            return apk, files

        if self._is_unpacked_apk(root):
            for p in sorted(root.rglob("*")):
                if not p.is_file() or ".ferry-backup" in p.parts:
                    continue
                rel = p.relative_to(root).as_posix()
                if _is_candidate_zip_entry(rel):
                    try:
                        files[rel] = p.read_bytes()
                    except OSError:
                        continue
        return None, files

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        apk = self._find_apk(root)
        unpacked = self._is_unpacked_apk(root)
        if apk is None and not unpacked:
            return ProbeReport(engine=self.name, root=root, notes=["未找到 .apk 安装包或 AndroidManifest.xml"])

        _, files = self._load_files(root)
        entries = collect_entries_from_virtual_files(root.name, files)
        if not entries:
            return ProbeReport(
                engine="Android APK",
                root=root,
                readable=False,
                writable=False,
                container="apk" if apk else "folder",
                notes=["检测到 Android APK，但包内未检出明文 assets 脚本、strings.xml 或 classes.dex 中文/日文台词"],
            )

        kinds = sorted({e.kind for e in entries})
        return ProbeReport(
            engine="Android APK",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container="apk" if apk else "folder",
            text_format=", ".join(kinds),
            unicode_engine=True,
            in_place=apk is None,
            notes=[
                f"从 {'APK 归档 ' + apk.name if apk else 'APK 解包目录'} 抽出 {len(entries)} 条文本（通道：{', '.join(kinds)}）。",
                "回写时会保持 resources.arsc 为 ZIP_STORED 无压缩对齐，并自动清理旧 META-INF 签名块，重打包后用 apksigner / MT 管理器签名即可安装。",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        _, files = self._load_files(root)
        return collect_entries_from_virtual_files(root.name, files)

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        apk, files = self._load_files(root)
        entries = collect_entries_from_virtual_files(root.name, files)
        report = ApplyReport()
        if not entries:
            report.notes.append("APK 内没有可回写的文本单元")
            return report

        modified, applied, missed, conflicts = patch_virtual_files(files, entries, translations)
        report.applied = applied
        report.missed = missed
        report.conflicts.extend(conflicts)
        if not modified:
            report.notes.append("没有命中任何译文，未生成 APK")
            return report

        undo = UndoLog.open(base, engine=self.name)
        if apk is not None:
            default_out = apk.with_name(f"{apk.stem}.cn.apk")
            target_out = unique_output(Path(out) if out else default_out)
            if target_out.is_dir():
                target_out = unique_output(target_out / default_out.name)
            _rebuild_apk_zip(apk, target_out, modified)
            undo.did_copy(target_out)
            report.undo_path = undo.finish()
            report.out_path = target_out
            report.in_place = False
            report.notes.append(f"已生成汉化 APK 副本：{target_out.name}（安装前请执行 apksigner 重签名）。")
            return report

        # 解包目录原地回写
        for rel, new_raw in modified.items():
            undo.will_modify(rel)
            (root / rel).write_bytes(new_raw)
            undo.did_write(rel)
        report.undo_path = undo.finish()
        report.out_path = root
        report.in_place = True
        report.notes.append(f"已原地更新 APK 解包目录 {len(modified)} 个文件。")
        return report


def _rebuild_apk_zip(src_apk: Path, dst_apk: Path, replacements: dict[str, bytes]) -> None:
    """重建 `.apk`，保持 `resources.arsc` 为 `ZIP_STORED` 并剥除旧 V1 `META-INF/*.SF|RSA|DSA` 签名。"""
    dst_apk.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src_apk, "r") as zin, zipfile.ZipFile(dst_apk, "w") as zout:
        for info in zin.infolist():
            if _META_INF_SIG_RE.match(info.filename):
                continue
            data = replacements.get(info.filename, zin.read(info.filename))
            fresh = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
            fresh.external_attr = info.external_attr
            fresh.create_system = info.create_system
            if info.filename == "resources.arsc" or info.filename.endswith((".png", ".ogg", ".mp3", ".so")):
                fresh.compress_type = zipfile.ZIP_STORED
            else:
                fresh.compress_type = info.compress_type
            zout.writestr(fresh, data)


# ============================================================================
# iOS IPA 适配器
# ============================================================================


class IpaAdapter(Adapter):
    """iOS `.ipa` 归档与 `Payload/*.app` 应用包适配器。"""

    name = "ipa"
    priority = 12

    def _find_ipa(self, root: Path) -> Path | None:
        root = Path(root)
        if root.is_file() and root.suffix.lower() == ".ipa" and ".cn" not in root.stem.lower():
            return root
        if root.is_dir():
            for p in sorted(root.glob("*.ipa")):
                if p.is_file() and ".cn" not in p.stem.lower():
                    return p
        return None

    def _is_unpacked_ios(self, root: Path) -> bool:
        root = Path(root)
        if not root.is_dir():
            return False
        if (root / "Payload").is_dir() and any((root / "Payload").glob("*.app")):
            return True
        if any(root.glob("*.lproj/*.strings")):
            return True
        return False

    def _load_files(self, root: Path) -> tuple[Path | None, dict[str, bytes]]:
        ipa = self._find_ipa(root)
        files: dict[str, bytes] = {}
        if ipa is not None:
            try:
                with zipfile.ZipFile(ipa, "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        if _is_candidate_zip_entry(info.filename):
                            files[info.filename] = zf.read(info.filename)
            except (OSError, zipfile.BadZipFile):
                return ipa, {}
            return ipa, files

        if self._is_unpacked_ios(root):
            for p in sorted(root.rglob("*")):
                if not p.is_file() or ".ferry-backup" in p.parts:
                    continue
                rel = p.relative_to(root).as_posix()
                if _is_candidate_zip_entry(rel):
                    try:
                        files[rel] = p.read_bytes()
                    except OSError:
                        continue
        return None, files

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        ipa = self._find_ipa(root)
        unpacked = self._is_unpacked_ios(root)
        if ipa is None and not unpacked:
            return ProbeReport(engine=self.name, root=root, notes=["未找到 .ipa 归档或 Payload/*.app / *.lproj"])

        _, files = self._load_files(root)
        entries = collect_entries_from_virtual_files(root.name, files)
        if not entries:
            return ProbeReport(
                engine="iOS IPA",
                root=root,
                readable=False,
                writable=False,
                container="ipa" if ipa else "folder",
                notes=["检测到 iOS 应用包，但未在 *.lproj/*.strings 或包内资源中发现可翻译文本"],
            )

        kinds = sorted({e.kind for e in entries})
        return ProbeReport(
            engine="iOS IPA / App Bundle",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container="ipa" if ipa else "folder",
            text_format=", ".join(kinds),
            unicode_engine=True,
            in_place=ipa is None,
            notes=[
                f"从 {'iOS 归档 ' + ipa.name if ipa else 'iOS 应用目录'} 抽出 {len(entries)} 条文本（通道：{', '.join(kinds)}）。",
                "支持明文与 bplist00 二进制 .strings 无损回写，并完整保留 Payload/*.app 可执行文件 UNIX 权限位。",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        _, files = self._load_files(root)
        return collect_entries_from_virtual_files(root.name, files)

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        ipa, files = self._load_files(root)
        entries = collect_entries_from_virtual_files(root.name, files)
        report = ApplyReport()
        if not entries:
            report.notes.append("IPA 内没有可回写的文本单元")
            return report

        modified, applied, missed, conflicts = patch_virtual_files(files, entries, translations)
        report.applied = applied
        report.missed = missed
        report.conflicts.extend(conflicts)
        if not modified:
            report.notes.append("没有命中任何译文，未生成 IPA")
            return report

        undo = UndoLog.open(base, engine=self.name)
        if ipa is not None:
            default_out = ipa.with_name(f"{ipa.stem}.cn.ipa")
            target_out = unique_output(Path(out) if out else default_out)
            if target_out.is_dir():
                target_out = unique_output(target_out / default_out.name)
            _rebuild_ipa_zip(ipa, target_out, modified)
            undo.did_copy(target_out)
            report.undo_path = undo.finish()
            report.out_path = target_out
            report.in_place = False
            report.notes.append(f"已生成汉化 iOS IPA 副本：{target_out.name}（可通过 TrollStore / AltStore / Sideloadly 安装）。")
            return report

        for rel, new_raw in modified.items():
            undo.will_modify(rel)
            (root / rel).write_bytes(new_raw)
            undo.did_write(rel)
        report.undo_path = undo.finish()
        report.out_path = root
        report.in_place = True
        report.notes.append(f"已原地更新 iOS 应用目录 {len(modified)} 个文件。")
        return report


def _rebuild_ipa_zip(src_ipa: Path, dst_ipa: Path, replacements: dict[str, bytes]) -> None:
    """重建 `.ipa`，完整保留所有 UNIX `external_attr` 权限位。"""
    dst_ipa.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src_ipa, "r") as zin, zipfile.ZipFile(dst_ipa, "w") as zout:
        for info in zin.infolist():
            data = replacements.get(info.filename, zin.read(info.filename))
            fresh = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
            fresh.compress_type = info.compress_type
            fresh.external_attr = info.external_attr
            fresh.create_system = info.create_system
            zout.writestr(fresh, data)
