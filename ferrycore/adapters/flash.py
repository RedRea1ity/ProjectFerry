"""Adobe / Macromedia Flash 游戏适配器（`.swf` `FWS`/`CWS` + Flash Projector `.exe` 独立播放器）。

Flash 游戏（AS1/AS2 与 AS3 时代的海量网页/同人/独立游戏）以两种方式分发：
1. **标准 `.swf` 文件**：
   - `FWS`（未压缩）或 `CWS`（从第 8 字节起为标准 `zlib` 压缩流）。
2. **Flash Projector 独立 `.exe`（Macromedia / Adobe Flash Player 封装包）**：
   - 文件末尾最后 8 个字节固定为 `[0x56, 0x34, 0x12, 0xFA (魔数 0xFA123456)][uint32_le swf_size]`，
     紧挨在这 8 字节前面的就是完整的内嵌 `.swf`！

本模块纯标准库解析并重建 `.swf` 的两大字节码字符串常量池（**支持任意长度 UTF-8 中文，无定长截断**）：
- **ActionScript 1 / 2 (`DoAction` Tag 12 & `DoInitAction` Tag 59)**：
  直接解析并重建 `ActionConstantPool` (`0x88`) C 字符串常量表；
- **ActionScript 3 (`DoABC` Tag 82 & `DoABCDefine` Tag 72)**：
  直接解析 AVM2 `abcFile` 常量池（跳过 `int` / `uint` / `double` 变长表），重建 `string_info` (`u30` 长度前缀 UTF-8 表)。
  由于 AVM2 所有指令、类名、方法均通过 **1-based 下标**引用字符串池，修改字符串长度完全不影响任何字节码跳转偏移！
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from ferrygame.rawbin import EN_MENU_TERMS, JP_MENU_TERMS

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .container import unique_output
from .undo import UNDO_NAME, UndoLog
from .unity import is_unity_translatable

FLASH_PROJECTOR_FOOTER_MAGIC = 0xFA123456


def _is_flash_translatable(text: str) -> bool:
    s = text.strip()
    if not s or len(s) < 2 or len(s) > 2000:
        return False
    if s.startswith(("_root", "_parent", "_level", "flash.", "fl.", "mx.", "com.", "org.", "http://", "https://")):
        return False
    if s in ("onEnterFrame", "onRelease", "onPress", "gotoAndPlay", "gotoAndStop", "MovieClip", " Event"):
        return False
    if s in EN_MENU_TERMS or s in JP_MENU_TERMS:
        return True
    return is_unity_translatable(s)


# ============================================================================
# SWF 解压/重压缩与 Tag 读写
# ============================================================================


def unpack_swf(blob: bytes) -> tuple[bytes, int, bytes]:
    """解开 `FWS` 或 `CWS` `.swf`，返回 `(orig_magic, version, uncompressed_full_swf)`。"""
    if len(blob) < 12 or blob[:3] not in (b"FWS", b"CWS"):
        raise ValueError("不是合法的 Flash SWF 文件（缺少 FWS/CWS 头）")
    magic = blob[:3]
    version = blob[3]
    if magic == b"CWS":
        body = zlib.decompress(blob[8:])
    else:
        body = blob[8:]
    full = b"FWS" + bytes([version]) + struct.pack("<I", 8 + len(body)) + body
    return magic, version, full


def pack_swf(magic: bytes, version: int, uncompressed_full: bytes) -> bytes:
    """将修改后的未压缩 SWF 按原格式（`FWS` 或 `CWS`）重新封装并更新 `file_length`。"""
    body = uncompressed_full[8:]
    total_len = 8 + len(body)
    if magic == b"CWS":
        return b"CWS" + bytes([version]) + struct.pack("<I", total_len) + zlib.compress(body)
    return b"FWS" + bytes([version]) + struct.pack("<I", total_len) + body


def _swf_tag_stream_offset(uncompressed_swf: bytes) -> int:
    """跳过 8 字节文件头 + RECT 位域 + 4 字节帧率/帧数，返回第一个 SWF Tag 的字节偏移。"""
    nbits = uncompressed_swf[8] >> 3
    rect_bytes = (5 + nbits * 4 + 7) // 8
    return 8 + rect_bytes + 4


def _iter_swf_tags(uncompressed_swf: bytes):
    """遍历 SWF Tag，产出 `(tag_index, tag_type, tag_body_bytes)`。"""
    pos = _swf_tag_stream_offset(uncompressed_swf)
    n = len(uncompressed_swf)
    idx = 0
    while pos + 2 <= n:
        code_len = struct.unpack_from("<H", uncompressed_swf, pos)[0]
        pos += 2
        tag_type = code_len >> 6
        tag_len = code_len & 0x3F
        if tag_len == 0x3F:
            if pos + 4 > n:
                break
            tag_len = struct.unpack_from("<I", uncompressed_swf, pos)[0]
            pos += 4
        if pos + tag_len > n:
            break
        yield idx, tag_type, uncompressed_swf[pos : pos + tag_len]
        pos += tag_len
        idx += 1
        if tag_type == 0:  # End tag
            break


def _encode_swf_tag(tag_type: int, body: bytes) -> bytes:
    length = len(body)
    if length < 0x3F and tag_type not in (12, 59, 72, 82):
        return struct.pack("<H", (tag_type << 6) | length) + body
    return struct.pack("<HI", (tag_type << 6) | 0x3F, length) + body


# ============================================================================
# AS2 ActionConstantPool (0x88) & AS3 AVM2 DoABC string_info 解析与重建
# ============================================================================


def _extract_as2_pool(action_bytes: bytes) -> list[tuple[int, str]]:
    """在 AS1/AS2 ActionRecord 流首个 `ActionConstantPool` (`0x88`) 中抽取可翻译字符串。"""
    if len(action_bytes) < 5 or action_bytes[0] != 0x88:
        return []
    pool_len, count = struct.unpack_from("<HH", action_bytes, 1)
    if 3 + pool_len > len(action_bytes):
        return []
    pos = 5
    end = 3 + pool_len
    out: list[tuple[int, str]] = []
    for s_idx in range(count):
        nul = action_bytes.find(b"\x00", pos, end)
        if nul < 0:
            break
        raw_s = action_bytes[pos:nul]
        pos = nul + 1
        try:
            text = raw_s.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw_s.decode("cp932")
            except UnicodeDecodeError:
                continue
        if _is_flash_translatable(text):
            out.append((s_idx, text))
    return out


def _rebuild_as2_pool(action_bytes: bytes, replacements: dict[str, str], tag_idx: int) -> tuple[bytes, int]:
    if len(action_bytes) < 5 or action_bytes[0] != 0x88:
        return action_bytes, 0
    pool_len, count = struct.unpack_from("<HH", action_bytes, 1)
    if 3 + pool_len > len(action_bytes):
        return action_bytes, 0
    pos = 5
    end = 3 + pool_len
    new_pool_body = bytearray(struct.pack("<H", count))
    applied = 0

    for s_idx in range(count):
        nul = action_bytes.find(b"\x00", pos, end)
        if nul < 0:
            return action_bytes, 0
        raw_s = action_bytes[pos:nul]
        pos = nul + 1
        try:
            src_text = raw_s.decode("utf-8")
        except UnicodeDecodeError:
            src_text = raw_s.decode("cp932", errors="replace")

        tr = replacements.get(f"AS2:{tag_idx}:{s_idx}") or replacements.get(src_text)
        if tr and tr.strip() and tr != src_text:
            raw_s = tr.encode("utf-8")
            applied += 1
        new_pool_body.extend(raw_s + b"\x00")

    if applied == 0 or len(new_pool_body) > 65535:
        return action_bytes, 0
    rebuilt_pool = b"\x88" + struct.pack("<H", len(new_pool_body)) + bytes(new_pool_body)
    return rebuilt_pool + action_bytes[end:], applied


def _read_u30(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    for _ in range(5):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            break
        shift += 7
    return result, pos


def _write_u30(val: int) -> bytes:
    out = bytearray()
    while True:
        b = val & 0x7F
        val >>= 7
        if val > 0:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _locate_abc_string_pool(abc: bytes) -> tuple[int, int, list[bytes]]:
    """定位 AVM2 `abcFile` 中 `string_count` 的起止偏移与所有原始 UTF-8 字符串项。"""
    if len(abc) < 10:
        raise ValueError("abcFile 过短")
    minor, major = struct.unpack_from("<HH", abc, 0)
    if major != 46:
        raise ValueError(f"不支持的 AVM2 版本: {major}.{minor}")
    pos = 4
    int_count, pos = _read_u30(abc, pos)
    for _ in range(max(0, int_count - 1)):
        _, pos = _read_u30(abc, pos)
    uint_count, pos = _read_u30(abc, pos)
    for _ in range(max(0, uint_count - 1)):
        _, pos = _read_u30(abc, pos)
    double_count, pos = _read_u30(abc, pos)
    pos += max(0, double_count - 1) * 8

    str_start = pos
    str_count, pos = _read_u30(abc, pos)
    strings: list[bytes] = []
    for _ in range(max(0, str_count - 1)):
        s_len, pos = _read_u30(abc, pos)
        if pos + s_len > len(abc):
            raise ValueError("AVM2 string_info 越界")
        strings.append(abc[pos : pos + s_len])
        pos += s_len
    return str_start, pos, strings


def _extract_as3_abc(tag_type: int, tag_body: bytes) -> list[tuple[int, str]]:
    if tag_type == 82:  # DoABC: uint32 flags + C-string name + abcFile
        nul = tag_body.find(b"\x00", 4)
        if nul < 0:
            return []
        abc = tag_body[nul + 1 :]
    else:
        abc = tag_body
    try:
        _s_start, _s_end, raw_strings = _locate_abc_string_pool(abc)
    except Exception:
        return []
    out: list[tuple[int, str]] = []
    for idx, raw_s in enumerate(raw_strings, start=1):
        try:
            text = raw_s.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if _is_flash_translatable(text):
            out.append((idx, text))
    return out


def _rebuild_as3_abc(
    tag_type: int,
    tag_body: bytes,
    replacements: dict[str, str],
    tag_idx: int,
) -> tuple[bytes, int]:
    if tag_type == 82:
        nul = tag_body.find(b"\x00", 4)
        if nul < 0:
            return tag_body, 0
        prefix = tag_body[: nul + 1]
        abc = tag_body[nul + 1 :]
    else:
        prefix = b""
        abc = tag_body

    try:
        s_start, s_end, raw_strings = _locate_abc_string_pool(abc)
    except Exception:
        return tag_body, 0

    new_str_pool = bytearray(_write_u30(len(raw_strings) + 1 if raw_strings else 0))
    applied = 0
    for idx, raw_s in enumerate(raw_strings, start=1):
        try:
            src_text = raw_s.decode("utf-8")
        except UnicodeDecodeError:
            src_text = ""
        tr = replacements.get(f"AS3:{tag_idx}:{idx}") or (replacements.get(src_text) if src_text else None)
        if tr and tr.strip() and tr != src_text:
            raw_s = tr.encode("utf-8")
            applied += 1
        new_str_pool.extend(_write_u30(len(raw_s)))
        new_str_pool.extend(raw_s)

    if applied == 0:
        return tag_body, 0
    return prefix + abc[:s_start] + bytes(new_str_pool) + abc[s_end:], applied


def extract_swf_strings(swf_blob: bytes) -> list[tuple[str, str]]:
    """从 `.swf` 字节流抽取所有 AS2 / AS3 字符串常量，返回 `[(locator, text)]`。"""
    _magic, _ver, uncomp = unpack_swf(swf_blob)
    out: list[tuple[str, str]] = []
    for tag_idx, tag_type, body in _iter_swf_tags(uncomp):
        if tag_type == 12:  # DoAction
            for s_idx, text in _extract_as2_pool(body):
                out.append((f"AS2:{tag_idx}:{s_idx}", text))
        elif tag_type == 59 and len(body) > 2:  # DoInitAction (前 2 字节为 SpriteID)
            for s_idx, text in _extract_as2_pool(body[2:]):
                out.append((f"AS2:{tag_idx}:{s_idx}", text))
        elif tag_type in (72, 82):  # DoABCDefine / DoABC
            for s_idx, text in _extract_as3_abc(tag_type, body):
                out.append((f"AS3:{tag_idx}:{s_idx}", text))
    return out


def rebuild_swf_strings(swf_blob: bytes, replacements: dict[str, str]) -> tuple[bytes, int]:
    """将 `{locator_or_src: translated}` 写回 `.swf`，支持任意长度的 UTF-8 中文并自动重封装。"""
    magic, ver, uncomp = unpack_swf(swf_blob)
    header_part = uncomp[: _swf_tag_stream_offset(uncomp)]
    out_tags = bytearray()
    total_applied = 0

    for tag_idx, tag_type, body in _iter_swf_tags(uncomp):
        if tag_type == 12:
            body, c = _rebuild_as2_pool(body, replacements, tag_idx)
            total_applied += c
        elif tag_type == 59 and len(body) > 2:
            new_act, c = _rebuild_as2_pool(body[2:], replacements, tag_idx)
            body = body[:2] + new_act
            total_applied += c
        elif tag_type in (72, 82):
            body, c = _rebuild_as3_abc(tag_type, body, replacements, tag_idx)
            total_applied += c
        out_tags.extend(_encode_swf_tag(tag_type, body))

    if total_applied == 0:
        return swf_blob, 0
    return pack_swf(magic, ver, header_part + bytes(out_tags)), total_applied


def locate_projector_swf(exe_blob: bytes) -> tuple[int, int] | None:
    """检查 EXE 尾部是否带有 Flash Projector 的 `0xFA123456` + `uint32 swf_size` 封尾。"""
    if len(exe_blob) < 32:
        return None
    magic, swf_size = struct.unpack_from("<II", exe_blob, len(exe_blob) - 8)
    if magic != FLASH_PROJECTOR_FOOTER_MAGIC:
        return None
    start = len(exe_blob) - 8 - swf_size
    if start < 0 or exe_blob[start : start + 3] not in (b"FWS", b"CWS"):
        return None
    return start, swf_size


class FlashAdapter(Adapter):
    """Adobe / Macromedia Flash (`.swf` & Projector `.exe`) 游戏适配器。"""

    name = "flash"
    priority = 16

    def _find_targets(self, root: Path) -> list[tuple[Path, str]]:
        """返回 `[(文件路径, 'swf' | 'projector')]`。"""
        root = Path(root)
        candidates = [root] if root.is_file() else (
            [p for p in sorted(root.glob("*")) if p.is_file()] if root.is_dir() else []
        )
        targets: list[tuple[Path, str]] = []
        for p in candidates:
            if ".cn" in p.stem.lower():
                continue
            suf = p.suffix.lower()
            if suf == ".swf":
                targets.append((p, "swf"))
            elif suf == ".exe":
                try:
                    blob = p.read_bytes()
                except OSError:
                    continue
                if locate_projector_swf(blob) is not None:
                    targets.append((p, "projector"))
        return targets

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        targets = self._find_targets(root)
        if not targets:
            return ProbeReport(engine=self.name, root=root, notes=["未找到 .swf 文件或 Flash Projector 封装 .exe"])

        entries = self.collect(root)
        has_proj = any(k == "projector" for _, k in targets)
        if not entries:
            return ProbeReport(
                engine="Adobe / Macromedia Flash",
                root=root,
                readable=False,
                writable=False,
                container="projector-exe" if has_proj else "swf",
                notes=["检测到 Flash 游戏，但未在 AS2 ActionConstantPool 或 AS3 DoABC 中检出可翻译文本"],
            )

        return ProbeReport(
            engine="Adobe / Macromedia Flash (AS2 / AS3)",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container="projector-exe" if has_proj else "swf",
            text_format="SWF ActionConstantPool / AVM2 DoABC",
            unicode_engine=True,
            in_place=False,
            notes=[
                f"从 {len(targets)} 个 Flash 文件中抽出 {len(entries)} 条 AS2/AS3 文本常量。",
                "回写直接重建 SWF 字节码字符串常量池（支持任意长度 UTF-8 中文，自动重压缩 CWS 并重算 Projector 0xFA123456 封尾）。",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        entries: list[Entry] = []

        for path, mode in self._find_targets(root):
            rel = path.relative_to(base).as_posix()
            try:
                blob = path.read_bytes()
                if mode == "projector":
                    span = locate_projector_swf(blob)
                    if span is None:
                        continue
                    swf_blob = blob[span[0] : span[0] + span[1]]
                else:
                    swf_blob = blob
                extracted = extract_swf_strings(swf_blob)
            except Exception:
                continue

            for loc, text in extracted:
                entries.append(
                    Entry(
                        modid=path.stem,
                        key=f"{rel}#{loc}",
                        english=text,
                        source="game",
                        kind="swf",
                        rel_path=rel,
                        json_pointer=loc,
                    )
                )
        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        targets = self._find_targets(root)
        entries = self.collect(root)
        report = ApplyReport()
        if not entries or not targets:
            report.notes.append("未找到可回写的 Flash 文本单元")
            return report

        by_file: dict[str, list[Entry]] = {}
        for e in entries:
            by_file.setdefault(e.rel_path, []).append(e)

        undo = UndoLog.open(base, engine=self.name)
        for path, mode in targets:
            rel = path.relative_to(base).as_posix()
            file_entries = by_file.get(rel, [])
            if not file_entries:
                continue
            swf_map: dict[str, str] = {}
            for e in file_entries:
                tr = translations.get(e.key) or translations.get(e.english)
                if tr and tr.strip():
                    swf_map[e.json_pointer] = tr
                    swf_map[e.english] = tr
                else:
                    report.missed += 1
            if not swf_map:
                continue

            blob = path.read_bytes()
            if mode == "projector":
                span = locate_projector_swf(blob)
                if span is None:
                    continue
                swf_blob = blob[span[0] : span[0] + span[1]]
                new_swf, count = rebuild_swf_strings(swf_blob, swf_map)
                if count == 0:
                    continue
                out_blob = (
                    blob[: span[0]]
                    + new_swf
                    + struct.pack("<II", FLASH_PROJECTOR_FOOTER_MAGIC, len(new_swf))
                )
            else:
                new_swf, count = rebuild_swf_strings(blob, swf_map)
                if count == 0:
                    continue
                out_blob = new_swf

            default_out = path.with_name(f"{path.stem}.cn{path.suffix}")
            target_out = unique_output(Path(out) if out else default_out)
            target_out.write_bytes(out_blob)
            undo.did_copy(target_out)
            report.applied += count
            report.out_path = target_out
            report.notes.append(f"已生成 Flash 汉化副本：{target_out.name}（{count} 条）。")

        if report.applied > 0:
            report.undo_path = undo.finish()
        report.in_place = False
        return report
