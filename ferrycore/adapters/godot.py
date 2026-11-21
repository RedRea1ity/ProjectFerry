"""Godot Engine 3.x / 4.x 适配器（.pck 资源包 + .tscn / .po / .csv / .json 文本）。

Godot 是近年来 Steam 与 itch.io 独立游戏增长最快的开源主流引擎（MIT 许可证）。
它的数据装法有两种：

1. **标准 `.pck` 归档（魔数 `GDPC`）**：
   - Godot 3.x 用 Pack Format v1，Godot 4.x 用 Pack Format v2（多了 `pack_flags`、`file_base` 与单文件 `flags`）。
   - 本适配器纯标准库解析并重建 v1 / v2 `.pck`（自动重算每个文件的 64 位偏移与 16 字节 MD5 摘要；
     若检测到 `pack_flags & 1` 目录加密，则在 `probe` 阶段如实告知不可静态改写）。
2. **散装工程 / 外置语言表**：
   - 扫描 `.tscn` 场景里的 UI 与对话属性（`text = "..."` / `title = "..."` / `tooltip_text = "..."` / `dialog_text = "..."`）
     以及 Gettext `.po`（`msgid` / `msgstr`）。
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .undo import UNDO_NAME, UndoLog

GDPC_MAGIC = b"GDPC"

# .tscn 场景节点里真正给玩家看的文本属性名
_TSCN_PROP_RE = re.compile(
    r'^(?P<head>\s*(?:text|title|dialog_text|placeholder_text|tooltip_text|label|caption)\s*=\s*")'
    r'(?P<body>(?:\\.|[^"\\])*)'
    r'(?P<tail>"\s*)$'
)
# Gettext .po 简单行对
_PO_MSGID_RE = re.compile(r'^\s*msgid\s+"(?P<text>(?:\\.|[^"\\])*)"\s*$')
_PO_MSGSTR_RE = re.compile(r'^(?P<head>\s*msgstr\s+")(?P<body>(?:\\.|[^"\\])*)(?P<tail>"\s*)$')

_CJK_OR_WORD_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]|[A-Za-z]{2,}")
_RES_PATH_RE = re.compile(r"^(?:res://|user://|[A-Za-z0-9_./\\-]+\.(?:png|tres|tscn|gd|ogg|wav|ttf|otf|shader))$", re.I)


@dataclass
class PckEntry:
    path: str
    offset: int
    size: int
    md5: bytes
    flags: int = 0


@dataclass
class PckArchive:
    version: int
    engine_version: tuple[int, int, int]
    pack_flags: int
    file_base: int
    entries: list[PckEntry]

    @property
    def encrypted(self) -> bool:
        return bool(self.pack_flags & 1) or any(e.flags & 1 for e in self.entries)


def parse_pck(blob: bytes) -> PckArchive:
    """解析 Godot 3 (v1) / Godot 4 (v2) 的 .pck 归档头与目录索引。"""
    if len(blob) < 88 or blob[:4] != GDPC_MAGIC:
        raise ValueError("不是 Godot .pck 归档（缺少 GDPC 魔数）")
    version, major, minor, patch = struct.unpack_from("<IIII", blob, 4)
    if version not in (1, 2):
        raise ValueError(f"不支持的 Godot PCK 版本：{version}")

    if version == 1:
        pack_flags = 0
        file_base = 0
        pos = 20 + 64  # 16 个预留 uint32
    else:
        pack_flags = struct.unpack_from("<I", blob, 20)[0]
        file_base = struct.unpack_from("<Q", blob, 24)[0]
        pos = 32 + 64

    if pack_flags & 1:
        return PckArchive(version, (major, minor, patch), pack_flags, file_base, [])

    file_count = struct.unpack_from("<I", blob, pos)[0]
    pos += 4
    entries: list[PckEntry] = []
    rel_base = file_base if (version == 2 and (pack_flags & 2)) else 0
    for _ in range(min(file_count, 200000)):
        if pos + 4 > len(blob):
            break
        path_len = struct.unpack_from("<I", blob, pos)[0]
        pos += 4
        if path_len <= 0 or pos + path_len + 32 > len(blob):
            break
        raw_path = blob[pos : pos + path_len].rstrip(b"\x00")
        path = raw_path.decode("utf-8", errors="replace")
        pos += path_len
        ofs, size = struct.unpack_from("<QQ", blob, pos)
        pos += 16
        md5 = blob[pos : pos + 16]
        pos += 16
        flags = 0
        if version == 2:
            flags = struct.unpack_from("<I", blob, pos)[0]
            pos += 4
        entries.append(PckEntry(path=path, offset=rel_base + ofs, size=size, md5=md5, flags=flags))
    return PckArchive(version, (major, minor, patch), pack_flags, file_base, entries)


def rebuild_pck(blob: bytes, replacements: dict[str, bytes]) -> bytes:
    """按原格式版本（v1 或 v2）重建 Godot `.pck` 归档。"""
    arc = parse_pck(blob)
    if arc.encrypted:
        raise ValueError("该 Godot .pck 已加密，无法静态重打包")

    payloads: list[tuple[PckEntry, bytes]] = []
    for entry in arc.entries:
        if entry.path in replacements:
            data = replacements[entry.path]
        else:
            data = blob[entry.offset : entry.offset + entry.size]
        payloads.append((entry, data))

    # 先算目录区结束后的 data_start
    header_len = (20 + 64 + 4) if arc.version == 1 else (32 + 64 + 4)
    dir_len = 0
    encoded_paths: list[bytes] = []
    for entry, _data in payloads:
        raw_p = entry.path.encode("utf-8")
        pad_len = ((len(raw_p) + 4) & ~3)  # 至少留 1 字节 \0 并按 4 字节对齐
        padded_p = raw_p.ljust(pad_len, b"\x00")
        encoded_paths.append(padded_p)
        dir_len += 4 + len(padded_p) + 16 + 16 + (4 if arc.version == 2 else 0)

    data_start = (header_len + dir_len + 15) & ~15
    rel_mode = arc.version == 2 and bool(arc.pack_flags & 2)
    file_base = data_start if rel_mode else 0

    out = bytearray()
    out.extend(GDPC_MAGIC)
    out.extend(struct.pack("<IIII", arc.version, *arc.engine_version))
    if arc.version == 2:
        out.extend(struct.pack("<IQ", arc.pack_flags, file_base))
    out.extend(b"\x00" * 64)
    out.extend(struct.pack("<I", len(payloads)))

    cursor = data_start
    for (entry, data), padded_p in zip(payloads, encoded_paths):
        rec_ofs = (cursor - file_base) if rel_mode else cursor
        md5 = hashlib.md5(data).digest()
        out.extend(struct.pack("<I", len(padded_p)))
        out.extend(padded_p)
        out.extend(struct.pack("<QQ", rec_ofs, len(data)))
        out.extend(md5)
        if arc.version == 2:
            out.extend(struct.pack("<I", entry.flags))
        cursor += len(data)

    if len(out) < data_start:
        out.extend(b"\x00" * (data_start - len(out)))
    for _entry, data in payloads:
        out.extend(data)
    return bytes(out)


def _worth_translating(text: str) -> bool:
    s = text.strip()
    if len(s) < 2 or _RES_PATH_RE.match(s):
        return False
    return bool(_CJK_OR_WORD_RE.search(s))


def parse_tscn_lines(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        m = _TSCN_PROP_RE.match(line)
        if not m:
            continue
        val = m.group("body").replace('\\"', '"').replace("\\n", "\n")
        if _worth_translating(val):
            out.append((idx, val))
    return out


def apply_tscn_lines(text: str, mapping: dict[int, str]) -> tuple[str, int]:
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r\n"))
    lines = text.splitlines()
    applied = 0
    for idx, new_val in mapping.items():
        if not (1 <= idx <= len(lines)):
            continue
        m = _TSCN_PROP_RE.match(lines[idx - 1])
        if not m:
            continue
        escaped = new_val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        lines[idx - 1] = f"{m.group('head')}{escaped}{m.group('tail')}"
        applied += 1
    out = newline.join(lines)
    if trailing:
        out += newline
    return out, applied


class GodotAdapter(Adapter):
    """Godot Engine 3.x / 4.x 适配器。"""

    name = "godot"
    priority = 27

    def __init__(self, include_samples: bool = False) -> None:
        self.include_samples = include_samples

    def _pck_files(self, root: Path) -> list[Path]:
        root = Path(root)
        if root.is_file() and root.suffix.lower() == ".pck":
            return [root]
        if not root.is_dir():
            return []
        return sorted(p for p in root.glob("*.pck") if p.is_file() and not p.stem.lower().endswith(".cn"))

    def _is_godot_dir(self, root: Path) -> bool:
        root = Path(root)
        if self._pck_files(root):
            return True
        if not root.is_dir():
            return False
        return (root / "project.godot").is_file() or any(root.rglob("*.tscn"))

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        if not self._is_godot_dir(root):
            return ProbeReport(engine=self.name, root=root, notes=["未发现 .pck 归档或 project.godot / .tscn 场景"])

        pcks = self._pck_files(root)
        for pck in pcks:
            try:
                arc = parse_pck(pck.read_bytes())
            except Exception as exc:
                return ProbeReport(engine="Godot Engine", root=root, notes=[f"解析 {pck.name} 失败：{exc}"])
            if arc.encrypted:
                return ProbeReport(
                    engine=f"Godot Engine {arc.engine_version[0]}.{arc.engine_version[1]}",
                    root=root,
                    readable=False,
                    writable=False,
                    container="pck (encrypted)",
                    notes=[f"{pck.name} 启用了 Godot 导出密钥加密，无法在不提供 256 位密钥的情况下静态改写"],
                )

        entries = self.collect(root)
        ver_label = "Godot Engine"
        if pcks:
            arc = parse_pck(pcks[0].read_bytes())
            ver_label = f"Godot Engine {arc.engine_version[0]}.{arc.engine_version[1]}"
        return ProbeReport(
            engine=ver_label,
            root=root,
            readable=bool(entries),
            writable=bool(entries),
            unit_count=len(entries),
            container="pck" if pcks else "folder",
            text_format=".tscn",
            unicode_engine=True,
            in_place=True,
            notes=[f"抽出 {len(entries)} 条 Godot 场景/界面文本单元"],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        entries: list[Entry] = []

        for pck in self._pck_files(root):
            try:
                blob = pck.read_bytes()
                arc = parse_pck(blob)
            except Exception:
                continue
            for item in arc.entries:
                if not item.path.lower().endswith(".tscn"):
                    continue
                raw = blob[item.offset : item.offset + item.size]
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                rel = f"{pck.name}::{item.path}"
                for line_no, val in parse_tscn_lines(text):
                    entries.append(self.make_entry(root, rel, line_no, val))

        if base.is_dir():
            for path in sorted(base.rglob("*.tscn")):
                if ".ferry-backup" in path.parts:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                rel = path.relative_to(base).as_posix()
                for line_no, val in parse_tscn_lines(text):
                    entries.append(self.make_entry(root, rel, line_no, val))
        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        report = ApplyReport(in_place=True, out_path=base)
        undo = UndoLog.open(base, engine=self.name)

        # 1. 处理 .pck 内嵌 .tscn
        for pck in self._pck_files(root):
            try:
                blob = pck.read_bytes()
                arc = parse_pck(blob)
            except Exception:
                continue
            replacements: dict[str, bytes] = {}
            pck_applied = 0
            for item in arc.entries:
                if not item.path.lower().endswith(".tscn"):
                    continue
                rel = f"{pck.name}::{item.path}"
                prefix = f"{rel}#"
                text = blob[item.offset : item.offset + item.size].decode("utf-8", errors="replace")
                line_map: dict[int, str] = {}
                for line_no, val in parse_tscn_lines(text):
                    tr = translations.get(f"{prefix}{line_no}") or translations.get(val)
                    if tr and tr.strip():
                        line_map[line_no] = tr
                if not line_map:
                    continue
                new_text, count = apply_tscn_lines(text, line_map)
                if count > 0:
                    replacements[item.path] = new_text.encode("utf-8")
                    pck_applied += count
            if replacements:
                rel_pck = pck.relative_to(base).as_posix()
                undo.will_modify(rel_pck)
                pck.write_bytes(rebuild_pck(blob, replacements))
                undo.did_write(rel_pck)
                report.applied += pck_applied

        # 2. 处理散装 .tscn
        if base.is_dir():
            for path in sorted(base.rglob("*.tscn")):
                if ".ferry-backup" in path.parts:
                    continue
                rel = path.relative_to(base).as_posix()
                prefix = f"{rel}#"
                text = path.read_text(encoding="utf-8")
                line_map = {}
                for line_no, val in parse_tscn_lines(text):
                    tr = translations.get(f"{prefix}{line_no}") or translations.get(val)
                    if tr and tr.strip():
                        line_map[line_no] = tr
                if not line_map:
                    continue
                new_text, count = apply_tscn_lines(text, line_map)
                if count > 0:
                    undo.will_modify(rel)
                    path.write_text(new_text, encoding="utf-8")
                    undo.did_write(rel)
                    report.applied += count

        if report.applied > 0:
            report.undo_path = undo.finish()
        else:
            candidate = base / UNDO_NAME
            if candidate.is_file():
                report.undo_path = candidate
        return report
