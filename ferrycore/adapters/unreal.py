"""Unreal Engine 4 / 5（虚幻引擎 `.locres` 本地化资源包）适配器。

虚幻 4 与虚幻 5 是欧美及全球 3D 独立游戏与中大型单机游戏的第一大引擎。
所有使用 Unreal `FText` / `LOCTEXT` 系统的游戏，其多语言文本最终都会被编译为
`Content/Localization/<Target>/<Lang>/<Target>.locres` 二进制本地化资源文件。

本模块完全基于 Python 标准库实现 UE4 / UE5 `FTextLocalizationResource` (`.locres`) 的解析与无损重建：
- 支持 **Optimized v1 / v2 / v3**（含 16 字节 `LOCRES_MAGIC` GUID 头 + `int64` 字符串池偏移 + 引用计数 `ref_count`）
- 支持 **Legacy v0 (Compact)**（早期 UE4 命名空间键值直排结构）
- 回写中文时自动按 Unreal `FString` 规范切换为负数长度的 **UTF-16LE 宽字符串（`-(len + 1)`）**，
  在不破坏前置 CRC32 / CityHash64 命名空间哈希索引表的前提下，支持任意长度简体中文无损重建！
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .undo import UNDO_NAME, UndoLog
from .unity import is_unity_translatable

LOCRES_MAGIC = bytes.fromhex("0E147475674A03FC4A15909DC3377F1B")
UE_PAK_MAGIC = 0x5A6F12E1


def read_fstring(blob: bytes, pos: int) -> tuple[str, int]:
    """读取 Unreal Engine `FString`（正长度为 ANSI/UTF-8，负长度为 UTF-16LE 字符数）。"""
    if pos + 4 > len(blob):
        raise ValueError("FString 长度越界")
    length = struct.unpack_from("<i", blob, pos)[0]
    pos += 4
    if length == 0:
        return "", pos
    if length > 0:
        if length > 1_000_000 or pos + length > len(blob):
            raise ValueError("FString UTF-8 内容越界")
        raw = blob[pos : pos + length]
        pos += length
        if raw.endswith(b"\x00"):
            raw = raw[:-1]
        return raw.decode("utf-8", errors="replace"), pos
    # length < 0: UTF-16LE code units
    char_count = -length
    byte_len = char_count * 2
    if char_count > 1_000_000 or pos + byte_len > len(blob):
        raise ValueError("FString UTF-16LE 内容越界")
    raw = blob[pos : pos + byte_len]
    pos += byte_len
    if raw.endswith(b"\x00\x00"):
        raw = raw[:-2]
    return raw.decode("utf-16-le", errors="replace"), pos


def write_fstring(text: str) -> bytes:
    """将字符串按 Unreal Engine `FString` 规范序列化（纯 ASCII 用正长度，含中文/非 ASCII 用负长度 UTF-16LE）。"""
    if not text:
        return struct.pack("<i", 0)
    try:
        raw_ascii = text.encode("ascii")
        return struct.pack("<i", len(raw_ascii) + 1) + raw_ascii + b"\x00"
    except UnicodeEncodeError:
        raw_u16 = text.encode("utf-16-le") + b"\x00\x00"
        char_count = len(raw_u16) // 2
        return struct.pack("<i", -char_count) + raw_u16


@dataclass
class LocresStringItem:
    index: int
    text: str
    ref_count: int = 1


@dataclass
class LocresFile:
    version: int
    string_array_offset: int
    strings: list[LocresStringItem]


def parse_locres(blob: bytes) -> LocresFile:
    """解析 UE4 / UE5 `.locres` 文件中的本地化字符串表。"""
    if len(blob) >= 25 and blob[:16] == LOCRES_MAGIC:
        version = blob[16]
        if version not in (1, 2, 3):
            raise ValueError(f"不支持的 Unreal .locres 版本: {version}")
        str_off = struct.unpack_from("<q", blob, 17)[0]
        if str_off <= 0 or str_off + 4 > len(blob):
            raise ValueError(".locres 字符串表偏移越界")
        pos = str_off
        count = struct.unpack_from("<I", blob, pos)[0]
        pos += 4
        if count > 1_000_000:
            raise ValueError(".locres 字符串数量异常")
        items: list[LocresStringItem] = []
        for idx in range(count):
            text, pos = read_fstring(blob, pos)
            ref_count = 1
            if version >= 2:
                if pos + 4 > len(blob):
                    raise ValueError(".locres ref_count 越界")
                ref_count = struct.unpack_from("<i", blob, pos)[0]
                pos += 4
            items.append(LocresStringItem(index=idx, text=text, ref_count=ref_count))
        return LocresFile(version=version, string_array_offset=str_off, strings=items)

    # Legacy v0 (Compact)
    pos = 0
    if len(blob) < 4:
        raise ValueError(".locres 过短")
    ns_count = struct.unpack_from("<I", blob, pos)[0]
    pos += 4
    if ns_count > 100_000:
        raise ValueError("不是合法的 Legacy .locres")
    items = []
    idx = 0
    for _ in range(ns_count):
        _ns, pos = read_fstring(blob, pos)
        if pos + 4 > len(blob):
            raise ValueError("Legacy .locres key_count 越界")
        key_count = struct.unpack_from("<I", blob, pos)[0]
        pos += 4
        for _ in range(key_count):
            _key, pos = read_fstring(blob, pos)
            pos += 4  # source_string_hash (uint32)
            val, pos = read_fstring(blob, pos)
            items.append(LocresStringItem(index=idx, text=val, ref_count=1))
            idx += 1
    return LocresFile(version=0, string_array_offset=0, strings=items)


def rebuild_locres(blob: bytes, replacements: dict[str, str]) -> tuple[bytes, int]:
    """将 `{LOCRES:<idx> 或 原文: 译文}` 写回 `.locres`，返回 `(新字节流, 成功条数)`。"""
    loc = parse_locres(blob)
    applied = 0

    if loc.version >= 1:
        # 保留 [0 : string_array_offset] 头部与末尾哈希目录引用，并在原 string_array_offset 重写字符串表
        # 注意：标准 UE4/UE5 .locres 的 localized_string_array 位于文件头 (offset 25/29) 或文件尾部。
        # 我们先定位原 string_array 结束位置，以保留 string_array 之后的命名空间索引表（若 string_array 在前）。
        pos = loc.string_array_offset + 4
        for _ in range(len(loc.strings)):
            _, pos = read_fstring(blob, pos)
            if loc.version >= 2:
                pos += 4
        old_array_end = pos

        new_array = bytearray()
        new_array.extend(struct.pack("<I", len(loc.strings)))
        for item in loc.strings:
            tr = replacements.get(f"LOCRES:{item.index}") or replacements.get(item.text)
            if tr and tr.strip() and tr != item.text:
                final_text = tr
                applied += 1
            else:
                final_text = item.text
            new_array.extend(write_fstring(final_text))
            if loc.version >= 2:
                new_array.extend(struct.pack("<i", item.ref_count))

        if applied == 0:
            return blob, 0

        prefix = bytearray(blob[: loc.string_array_offset])
        suffix = blob[old_array_end:]
        if not suffix:
            # 字符串数组在文件末尾（UE4.14+ 标准布局）：头部的 string_array_offset 完全不变
            return bytes(prefix) + bytes(new_array), applied
        # 若字符串数组在中间，把新的字符串数组搬到文件末尾并更新 offset 17 的 int64 指针
        new_offset = len(prefix) + len(suffix)
        struct.pack_into("<q", prefix, 17, new_offset)
        return bytes(prefix) + suffix + bytes(new_array), applied

    # Legacy v0 重建
    pos = 0
    ns_count = struct.unpack_from("<I", blob, pos)[0]
    pos += 4
    out = bytearray(struct.pack("<I", ns_count))
    idx = 0
    for _ in range(ns_count):
        ns, pos = read_fstring(blob, pos)
        out.extend(write_fstring(ns))
        key_count = struct.unpack_from("<I", blob, pos)[0]
        pos += 4
        out.extend(struct.pack("<I", key_count))
        for _ in range(key_count):
            key, pos = read_fstring(blob, pos)
            src_hash = blob[pos : pos + 4]
            pos += 4
            val, pos = read_fstring(blob, pos)
            tr = replacements.get(f"LOCRES:{idx}") or replacements.get(val)
            if tr and tr.strip() and tr != val:
                val = tr
                applied += 1
            out.extend(write_fstring(key))
            out.extend(src_hash)
            out.extend(write_fstring(val))
            idx += 1
    return (bytes(out) if applied > 0 else blob), applied


class UnrealAdapter(Adapter):
    """Unreal Engine 4 / 5 `.locres` 本地化适配器。"""

    name = "unreal"
    priority = 26

    def _find_locres_files(self, root: Path) -> list[Path]:
        root = Path(root)
        if root.is_file() and root.suffix.lower() == ".locres":
            return [root]
        if not root.is_dir():
            return []
        return sorted(
            p for p in root.rglob("*.locres")
            if p.is_file() and ".ferry-backup" not in p.parts
        )

    def _has_pak_files(self, root: Path) -> list[Path]:
        root = Path(root)
        if not root.is_dir():
            return []
        return sorted(root.rglob("*.pak"))

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        locres_files = self._find_locres_files(root)
        paks = self._has_pak_files(root)
        if not locres_files:
            if paks and (root / "Engine").is_dir():
                return ProbeReport(
                    engine="Unreal Engine 4/5",
                    root=root,
                    readable=False,
                    writable=False,
                    container="pak",
                    notes=[
                        f"检测到 Unreal Engine 游戏（含 {len(paks)} 个 .pak 归档），但未发现解包出的 .locres 语言文件；请先用 UnrealPak / repak 提取 Content/Localization/*.locres。"
                    ],
                )
            return ProbeReport(engine=self.name, root=root, notes=["未找到 Unreal Engine .locres 语言资源文件"])

        entries = self.collect(root)
        if not entries:
            return ProbeReport(
                engine="Unreal Engine 4/5",
                root=root,
                readable=False,
                writable=False,
                notes=["找到 .locres 文件，但未解析出可翻译文本"],
            )

        return ProbeReport(
            engine="Unreal Engine 4/5 (.locres)",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container="locres",
            text_format=".locres (FString UTF-16LE)",
            unicode_engine=True,
            in_place=True,
            notes=[
                f"检出 {len(locres_files)} 个 Unreal .locres 本地化文件，共 {len(entries)} 条文本。",
                "回写采用 FString UTF-16LE 宽字符重建字符串池，保持前置 CRC32/CityHash64 键索引表完整。",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        entries: list[Entry] = []
        for path in self._find_locres_files(root):
            rel = path.relative_to(base).as_posix()
            try:
                loc = parse_locres(path.read_bytes())
            except Exception:
                continue
            for item in loc.strings:
                if is_unity_translatable(item.text):
                    entries.append(
                        Entry(
                            modid=root.name,
                            key=f"{rel}#LOCRES:{item.index}",
                            english=item.text,
                            source="game",
                            kind="locres",
                            rel_path=rel,
                            json_pointer=f"LOCRES:{item.index}",
                        )
                    )
        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        entries = self.collect(root)
        report = ApplyReport(in_place=True, out_path=base)
        if not entries:
            report.notes.append("未找到可回写的 .locres 条目")
            return report

        by_file: dict[str, list[Entry]] = {}
        for e in entries:
            by_file.setdefault(e.rel_path, []).append(e)

        pending: dict[str, bytes] = {}
        for rel, file_entries in by_file.items():
            full_path = base / rel
            try:
                raw = full_path.read_bytes()
            except OSError as exc:
                report.conflicts.append(f"{rel}: {exc}")
                continue
            loc_map: dict[str, str] = {}
            for e in file_entries:
                tr = translations.get(e.key) or translations.get(e.english)
                if tr and tr.strip():
                    loc_map[e.json_pointer] = tr
                    loc_map[e.english] = tr
                else:
                    report.missed += 1
            if not loc_map:
                continue
            try:
                rebuilt, count = rebuild_locres(raw, loc_map)
            except Exception as exc:
                report.conflicts.append(f"{rel}: {exc}")
                continue
            if count > 0:
                pending[rel] = rebuilt
                report.applied += count

        if not pending:
            report.notes.append("未命中任何译文，未修改 .locres")
            return report

        undo = UndoLog.open(base, engine=self.name)
        for rel, new_raw in pending.items():
            undo.will_modify(rel)
            (base / rel).write_bytes(new_raw)
            undo.did_write(rel)
        report.undo_path = undo.finish()
        report.notes.append(f"已原地重建 {len(pending)} 个 Unreal .locres 语言包（共 {report.applied} 条）。")
        return report
