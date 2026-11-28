"""Unreal Engine 4 / 5（虚幻引擎 `.locres` + `.uasset` StringTable + `.pak -> *_P.pak`）适配器。

虚幻 4 与虚幻 5 是欧美及全球 3D 独立游戏与中大型单机游戏的第一大引擎。
游戏文本通常存放在两类标准资产中：
1. `Content/Localization/<Target>/<Lang>/<Target>.locres`（`FTextLocalizationResource` 编译本地化资源）；
2. `Content/.../*.uasset`（以及配套 `.uexp`）中的 **`StringTable`** 资产（键值对字符串表）。

本模块完全基于 Python 标准库实现：
- 解析与无损重建 `.locres`（Legacy v0 与 Optimized v1 / v2 / v3），回写中文自动按 Unreal `FString`
  规范切换为负数长度的 **UTF-16LE 宽字符串（`-(len + 1)`）**；
- 解析与无损重建 UE4 / UE5 **`StringTable` `.uasset`（含分离式 `.uexp`）**，同步修正导出表 `SerialSize`；
- 检测 `.pak` 索引/条目加密标志与 UE5 IoStore（`.utoc`/`.ucas`）容器，支持用户填入 **256 位 AES 密钥**
  （兼容 `0x`+64位十六进制或 44 位 Base64）交给 `repak` 自动解包；
- 回写 `.pak` 内资产时绝不修改几十 GB 原版大包，自动打包为高优先级补丁 `~mods/ProjectFerry_zh_CN_P.pak`。
"""

from __future__ import annotations

import base64
import hashlib
import re
import struct
import subprocess
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .. import deps
from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .undo import UNDO_NAME, UndoLog
from .unity import is_unity_translatable

LOCRES_MAGIC = bytes.fromhex("0E147475674A03FC4A15909DC3377F1B")
UE_PAK_MAGIC = 0x5A6F12E1
UASSET_MAGIC = 0x9E2A83C1
UASSET_MAGIC_BYTES = struct.pack("<I", UASSET_MAGIC)
UE_PATCH_PAK_NAME = "ProjectFerry_zh_CN_P.pak"
UE_AES_FILENAME = "ferry_ue_aes.txt"

_HEX_KEY_RE = re.compile(r"^(?:0x)?([0-9a-fA-F]{64})$")


def normalize_aes_key(raw: str | None) -> str:
    """校验并归一化 Unreal 256 位 AES 密钥为 `0x` + 64 位大写十六进制。

    支持：
    - 空串 -> `""`
    - `0x` + 64 位十六进制（或纯 64 位十六进制，忽略空格与连字符）
    - 44 位 Base64 编码的 32 字节密钥
    """
    text = (raw or "").strip()
    if not text:
        return ""
    compact = re.sub(r"[\s-]+", "", text)
    m = _HEX_KEY_RE.match(compact)
    if m:
        return "0x" + m.group(1).upper()
    try:
        decoded = base64.b64decode(compact, validate=True)
    except Exception as exc:
        raise ValueError("AES 密钥格式无效：需要 64 位十六进制（可带 0x 前缀）或 32 字节 Base64。") from exc
    if len(decoded) != 32:
        raise ValueError(f"AES 密钥长度无效：解码后为 {len(decoded)} 字节，需要 32 字节（256 位）。")
    return "0x" + decoded.hex().upper()


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


def _read_fstring_strict(blob: bytes, pos: int, max_chars: int = 8192) -> tuple[str, int]:
    """严格版 FString 读取：要求 NUL 终止且不包含异常控制字符，供 `.uasset` 扫描防误判。"""
    if pos + 4 > len(blob):
        raise ValueError("FString 越界")
    length = struct.unpack_from("<i", blob, pos)[0]
    pos += 4
    if length == 0:
        return "", pos
    if length > 0:
        if length > max_chars or pos + length > len(blob):
            raise ValueError("FString UTF-8 长度异常")
        raw = blob[pos : pos + length]
        if not raw.endswith(b"\x00"):
            raise ValueError("FString 缺少 NUL 终止符")
        text = raw[:-1].decode("utf-8")
        if "\x00" in text:
            raise ValueError("FString 含内部 NUL")
        return text, pos + length
    char_count = -length
    byte_len = char_count * 2
    if char_count > max_chars or pos + byte_len > len(blob):
        raise ValueError("FString UTF-16LE 长度异常")
    raw = blob[pos : pos + byte_len]
    if not raw.endswith(b"\x00\x00"):
        raise ValueError("FString UTF-16LE 缺少 NUL 终止符")
    text = raw[:-2].decode("utf-16-le")
    if "\x00" in text:
        raise ValueError("FString UTF-16LE 含内部 NUL")
    return text, pos + byte_len


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
            return bytes(prefix) + bytes(new_array), applied
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


# ---- UE4/UE5 `.uasset` StringTable 解析与无损重建 ----


@dataclass
class StringTableEntry:
    index: int
    key: str
    value: str


@dataclass
class UAssetStringTable:
    namespace: str
    entries: list[StringTableEntry]
    in_uexp: bool
    table_start: int  # FStringTable 在目标缓冲区（uexp 或 uasset）中的起始偏移
    table_end: int  # entries 结束位置（不含尾部 metadata 与 PACKAGE_FILE_TAG）
    serial_size_offset: int | None = None  # uasset 中对应的导出项 SerialSize (int64) 偏移
    old_serial_size: int = 0
    writable: bool = False


_NS_RE = re.compile(r"^[A-Za-z0-9_./:-]{1,128}$")
_KEY_RE = re.compile(r"^[\x20-\x7e]{1,200}$")


def _try_parse_fstringtable_at(buf: bytes, start: int) -> tuple[str, list[StringTableEntry], int] | None:
    """尝试从 `buf[start:]` 严格解析 `FStringTable::Serialize` 载荷。"""
    try:
        ns, pos = _read_fstring_strict(buf, start, max_chars=256)
    except (ValueError, UnicodeDecodeError):
        return None
    if not ns or not _NS_RE.match(ns):
        return None
    if pos + 4 > len(buf):
        return None
    num = struct.unpack_from("<i", buf, pos)[0]
    pos += 4
    if not (1 <= num <= 200_000):
        return None

    entries: list[StringTableEntry] = []
    for idx in range(num):
        try:
            k, pos = _read_fstring_strict(buf, pos, max_chars=512)
            v, pos = _read_fstring_strict(buf, pos, max_chars=8192)
        except (ValueError, UnicodeDecodeError):
            return None
        if not k or not _KEY_RE.match(k):
            return None
        entries.append(StringTableEntry(index=idx, key=k, value=v))

    # 校验紧随其后的尾部结构：int32 NumMetaData (通常 0) + 可选 4 字节 UASSET_MAGIC 结尾
    tail = buf[pos:]
    if len(tail) < 4:
        return None
    meta_count = struct.unpack_from("<i", tail, 0)[0]
    if not (0 <= meta_count <= num * 4):
        return None
    if meta_count == 0:
        rest = tail[4:]
        if rest not in (b"", UASSET_MAGIC_BYTES):
            return None
    return ns, entries, pos


def _locate_unique_serial_size(uasset_blob: bytes, expected_size: int) -> int | None:
    """在 `.uasset` 头部/导出表中定位唯一的 int64 `SerialSize == expected_size`。"""
    if expected_size <= 0:
        return None
    needle = struct.pack("<q", expected_size)
    hits: list[int] = []
    start = 24  # 跳过魔数与基础版本头
    while True:
        idx = uasset_blob.find(needle, start)
        if idx < 0:
            break
        hits.append(idx)
        start = idx + 1
        if len(hits) > 2:
            return None
    return hits[0] if len(hits) == 1 else None


def parse_uasset_stringtable(
    uasset_blob: bytes,
    uexp_blob: bytes | None = None,
) -> UAssetStringTable | None:
    """解析 UE4 / UE5 `StringTable` `.uasset`（支持分离式 `.uexp` 与单文件 `.uasset`）。"""
    if len(uasset_blob) < 32 or uasset_blob[:4] != UASSET_MAGIC_BYTES:
        return None
    if b"StringTable" not in uasset_blob:
        return None

    in_uexp = uexp_blob is not None and len(uexp_blob) >= 16
    target = uexp_blob if in_uexp else uasset_blob
    assert target is not None

    # 对于 .uexp，通常前 8 字节为 None FName 属性结束标记，紧接着就是 FStringTable；
    # 也允许前面有少量 UObject 属性字节，扫描前 256 字节（或单文件尾部区域）。
    if in_uexp:
        scan_range = range(0, min(len(target) - 12, 256))
    else:
        # 单文件 .uasset：StringTable 位于导出区（通常在文件后半段）
        scan_start = max(32, uasset_blob.rfind(b"StringTable") + len(b"StringTable"))
        scan_range = range(scan_start, max(scan_start, len(target) - 12))

    for off in scan_range:
        parsed = _try_parse_fstringtable_at(target, off)
        if parsed is None:
            continue
        ns, entries, end_pos = parsed
        # 确认导出 SerialSize：.uexp 的 SerialSize = len(uexp) - 4（不含尾部 UASSET_MAGIC）
        if in_uexp:
            expected_serial = len(target) - (4 if target.endswith(UASSET_MAGIC_BYTES) else 0)
        else:
            # 单文件模式：导出起始通常在 table_start 前 8 字节（None FName）或正好 table_start
            expected_serial = 0
            for candidate_lead in (8, 0, 12, 16):
                cand = len(target) - (off - candidate_lead) - (4 if target.endswith(UASSET_MAGIC_BYTES) else 0)
                if _locate_unique_serial_size(uasset_blob, cand) is not None:
                    expected_serial = cand
                    break
        ss_off = _locate_unique_serial_size(uasset_blob, expected_serial) if expected_serial > 0 else None
        return UAssetStringTable(
            namespace=ns,
            entries=entries,
            in_uexp=in_uexp,
            table_start=off,
            table_end=end_pos,
            serial_size_offset=ss_off,
            old_serial_size=expected_serial,
            writable=ss_off is not None,
        )
    return None


def rebuild_uasset_stringtable(
    uasset_blob: bytes,
    replacements: dict[str, str],
    uexp_blob: bytes | None = None,
) -> tuple[bytes, bytes | None, int]:
    """将译文无损写回 UE4 / UE5 `StringTable` `.uasset`（及 `.uexp`），同步更新 `SerialSize`。

    返回 `(新 uasset_bytes, 新 uexp_bytes 或 None, 成功条数)`。
    若无法安全定位唯一 `SerialSize` 偏移则拒绝写入（抛出 ValueError），绝不损坏资产包。
    """
    st = parse_uasset_stringtable(uasset_blob, uexp_blob=uexp_blob)
    if st is None:
        raise ValueError("不是可识别的 Unreal StringTable 资产")
    if not st.writable or st.serial_size_offset is None:
        raise ValueError("无法唯一确定导出表 SerialSize，拒绝盲改以保护资产完整性")

    applied = 0
    rebuilt_body = bytearray()
    rebuilt_body.extend(write_fstring(st.namespace))
    rebuilt_body.extend(struct.pack("<i", len(st.entries)))
    for item in st.entries:
        tr = (
            replacements.get(f"ST:{item.index}:{item.key}")
            or replacements.get(f"ST:{item.key}")
            or replacements.get(item.value)
        )
        if tr and tr.strip() and tr != item.value:
            final_val = tr
            applied += 1
        else:
            final_val = item.value
        rebuilt_body.extend(write_fstring(item.key))
        rebuilt_body.extend(write_fstring(final_val))

    if applied == 0:
        return uasset_blob, uexp_blob, 0

    if st.in_uexp:
        assert uexp_blob is not None
        new_uexp = uexp_blob[: st.table_start] + bytes(rebuilt_body) + uexp_blob[st.table_end :]
        delta = len(new_uexp) - len(uexp_blob)
        new_uasset = bytearray(uasset_blob)
        struct.pack_into("<q", new_uasset, st.serial_size_offset, st.old_serial_size + delta)
        return bytes(new_uasset), new_uexp, applied

    new_uasset = bytearray(uasset_blob[: st.table_start] + bytes(rebuilt_body) + uasset_blob[st.table_end :])
    delta = len(new_uasset) - len(uasset_blob)
    struct.pack_into("<q", new_uasset, st.serial_size_offset, st.old_serial_size + delta)
    return bytes(new_uasset), None, applied


def _is_datatable_uasset(blob: bytes) -> bool:
    return len(blob) >= 32 and blob[:4] == UASSET_MAGIC_BYTES and b"DataTable" in blob[:4096]


# ---- .pak 解析与补丁打包 ----


@dataclass
class PakInspectInfo:
    version: int
    index_offset: int
    index_size: int
    comp_methods: list[str]
    encrypted_index: bool = False
    has_encrypted_entries: bool = False


def _read_pak_footer_info(fp, file_size: int) -> PakInspectInfo | None:
    tail_len = min(file_size, 512)
    if tail_len < 44:
        return None
    fp.seek(file_size - tail_len)
    tail = fp.read(tail_len)
    magic_bytes = struct.pack("<I", UE_PAK_MAGIC)
    pos = tail.rfind(magic_bytes)
    if pos < 0 or pos + 44 > len(tail):
        return None
    version, index_offset, index_size = struct.unpack_from("<Iqq", tail, pos + 4)
    if not (1 <= version <= 11):
        return None
    if index_offset <= 0 or index_size <= 0 or index_offset + index_size > file_size:
        return None
    encrypted_index = bool(version >= 4 and pos >= 1 and tail[pos - 1] == 1)
    comp_methods: list[str] = ["None"]
    if version >= 8:
        scan_start = pos + 44
        if version == 9 and scan_start < len(tail):
            scan_start += 1
        while scan_start + 32 <= len(tail):
            raw_name = tail[scan_start : scan_start + 32].split(b"\x00", 1)[0]
            name = raw_name.decode("ascii", errors="ignore").strip()
            comp_methods.append(name or "None")
            scan_start += 32
    else:
        comp_methods = ["None", "Zlib", "Gzip", "Custom"]
    return PakInspectInfo(
        version=version,
        index_offset=index_offset,
        index_size=index_size,
        comp_methods=comp_methods,
        encrypted_index=encrypted_index,
    )


def _read_pak_footer(fp, file_size: int) -> tuple[int, int, int, list[str]] | None:
    """扫描 .pak 尾部魔数 0x5A6F12E1，返回 (version, index_offset, index_size, comp_methods)。"""
    info = _read_pak_footer_info(fp, file_size)
    if info is None:
        return None
    return info.version, info.index_offset, info.index_size, info.comp_methods


def inspect_pak(pak_path: Path) -> PakInspectInfo | None:
    """快速探测 .pak 版本、压缩算法与是否启用 AES 加密（不解压大包）。"""
    try:
        file_size = pak_path.stat().st_size
        with open(pak_path, "rb") as fp:
            info = _read_pak_footer_info(fp, file_size)
            if info is None or info.encrypted_index or info.version >= 10:
                return info
            fp.seek(info.index_offset)
            index_blob = fp.read(min(info.index_size, 4 * 1024 * 1024))
            _mount, pos = read_fstring(index_blob, 0)
            if pos + 4 <= len(index_blob):
                entry_count = min(struct.unpack_from("<I", index_blob, pos)[0], 2000)
                pos += 4
                for _ in range(entry_count):
                    if pos >= len(index_blob):
                        break
                    _rel, pos = read_fstring(index_blob, pos)
                    if pos + 48 > len(index_blob):
                        break
                    _ofs, _cs, _us, comp_method = struct.unpack_from("<qqqI", index_blob, pos)
                    pos += 28 + (8 if info.version <= 1 else 0) + 20
                    if info.version >= 3 and comp_method != 0:
                        if pos + 4 > len(index_blob):
                            break
                        bcount = struct.unpack_from("<I", index_blob, pos)[0]
                        pos += 4 + bcount * 16
                    if info.version >= 3:
                        if pos + 5 > len(index_blob):
                            break
                        if index_blob[pos]:
                            info.has_encrypted_entries = True
                            break
                        pos += 5
            return info
    except (OSError, ValueError, struct.error):
        return None


def _extract_pak_by_suffix_pure(pak_path: Path, suffixes: tuple[str, ...]) -> tuple[str, dict[str, bytes]]:
    """纯 Python 解析未加密的 UE4/UE5 .pak（v3~v9，无压缩或 Zlib），提取指定后缀文件。"""
    results: dict[str, bytes] = {}
    file_size = pak_path.stat().st_size
    with open(pak_path, "rb") as fp:
        info = _read_pak_footer_info(fp, file_size)
        if info is None or info.encrypted_index or info.version >= 10:
            return "../../../", results
        version = info.version
        comp_methods = info.comp_methods
        fp.seek(info.index_offset)
        index_blob = fp.read(info.index_size)
        mount_point, pos = read_fstring(index_blob, 0)
        if pos + 4 > len(index_blob):
            return mount_point or "../../../", results
        entry_count = struct.unpack_from("<I", index_blob, pos)[0]
        pos += 4
        if entry_count > 500_000:
            return mount_point or "../../../", results

        for _ in range(entry_count):
            if pos >= len(index_blob):
                break
            rel_name, pos = read_fstring(index_blob, pos)
            if pos + 48 > len(index_blob):
                break
            offset, comp_size, uncomp_size, comp_method = struct.unpack_from("<qqqI", index_blob, pos)
            pos += 28
            if version <= 1:
                pos += 8
            pos += 20
            blocks: list[tuple[int, int]] = []
            if version >= 3 and comp_method != 0:
                if pos + 4 > len(index_blob):
                    break
                bcount = struct.unpack_from("<I", index_blob, pos)[0]
                pos += 4
                for _ in range(bcount):
                    bstart, bend = struct.unpack_from("<qq", index_blob, pos)
                    pos += 16
                    blocks.append((bstart, bend))
            encrypted = 0
            if version >= 3:
                if pos + 5 > len(index_blob):
                    break
                encrypted = index_blob[pos]
                pos += 5

            low = rel_name.lower()
            if not low.endswith(suffixes) or encrypted or offset < 0 or comp_size <= 0:
                continue
            if uncomp_size > 16 * 1024 * 1024:
                continue  # 跳过巨型贴图/模型 uasset

            local_hdr = (
                28
                + (8 if version <= 1 else 0)
                + 20
                + (4 + 16 * len(blocks) if (version >= 3 and comp_method != 0) else 0)
                + (5 if version >= 3 else 0)
            )
            method_name = comp_methods[comp_method].lower() if comp_method < len(comp_methods) else ""
            if comp_method == 0 or method_name == "none":
                fp.seek(offset + local_hdr)
                data = fp.read(comp_size)
                if len(data) == comp_size:
                    results[rel_name.replace("\\", "/").lstrip("/")] = data
            elif method_name == "zlib":
                out_buf = bytearray()
                try:
                    if blocks:
                        for bstart, bend in blocks:
                            abs_start = (offset + bstart) if version >= 7 else bstart
                            fp.seek(abs_start)
                            out_buf.extend(zlib.decompress(fp.read(bend - bstart)))
                    else:
                        fp.seek(offset + local_hdr)
                        out_buf.extend(zlib.decompress(fp.read(comp_size)))
                except zlib.error:
                    continue
                if len(out_buf) == uncomp_size or out_buf:
                    results[rel_name.replace("\\", "/").lstrip("/")] = bytes(out_buf)
    return mount_point or "../../../", results


def _extract_pak_locres_pure(pak_path: Path) -> tuple[str, dict[str, bytes]]:
    """纯 Python 解析未加密的 UE4/UE5 .pak（v3~v9，无压缩或 Zlib），只提取其中的 .locres 文件。"""
    return _extract_pak_by_suffix_pure(pak_path, (".locres",))


def _build_repak_unpack_cmd(
    repak: Path,
    pak_path: Path,
    out_dir: Path,
    patterns: tuple[str, ...],
    aes_key: str = "",
) -> list[str]:
    cmd = [str(repak)]
    norm_key = normalize_aes_key(aes_key) if aes_key else ""
    if norm_key:
        cmd.extend(["--aes-key", norm_key])
    cmd.append("unpack")
    for pat in patterns:
        cmd.extend(["-i", pat])
    cmd.extend(["-o", str(out_dir), str(pak_path)])
    return cmd


def _extract_pak_via_repak(
    pak_path: Path,
    patterns: tuple[str, ...],
    aes_key: str = "",
) -> tuple[str, dict[str, bytes], str]:
    """调用 repak 从 UE5 v11 / Oodle / AES 加密 .pak 提取指定模式文件。

    返回 `(mount_point, {内部相对路径: bytes}, 错误提示)`。
    """
    repak = deps.REGISTRY["repak"].locate()
    if repak is None:
        return "../../../", {}, ""
    try:
        cmd = _build_repak_unpack_cmd(repak, pak_path, Path("__placeholder__"), patterns, aes_key=aes_key)
    except ValueError as exc:
        return "../../../", {}, str(exc)

    results: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory(prefix="ferry_repak_") as tmp:
        out_dir = Path(tmp) / "unpacked"
        cmd = _build_repak_unpack_cmd(repak, pak_path, out_dir, patterns, aes_key=aes_key)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                return "../../../", {}, err[:240]
            for file in sorted(out_dir.rglob("*")):
                if file.is_file():
                    rel = file.relative_to(out_dir).as_posix()
                    results[rel] = file.read_bytes()
        except (OSError, subprocess.SubprocessError) as exc:
            return "../../../", {}, str(exc)
    return "../../../", results, ""


def _extract_pak_locres_via_repak(pak_path: Path, aes_key: str = "") -> tuple[str, dict[str, bytes]]:
    """若系统/tools 已安装 repak，借它从 UE5 v11 / Oodle / AES .pak 中提取 .locres。"""
    mount, found, _err = _extract_pak_via_repak(pak_path, ("**/*.locres",), aes_key=aes_key)
    return mount, {k: v for k, v in found.items() if k.lower().endswith(".locres")}


def extract_pak_locres(pak_path: Path, aes_key: str = "") -> tuple[str, dict[str, bytes]]:
    """从 Unreal .pak 归档提取所有 .locres 文件，返回 `(mount_point, {内部相对路径: bytes})`。"""
    if not aes_key:
        try:
            mount, found = _extract_pak_locres_pure(pak_path)
            if found:
                return mount, found
        except (OSError, ValueError, struct.error):
            pass
    return _extract_pak_locres_via_repak(pak_path, aes_key=aes_key)


def extract_pak_stringtables(
    pak_path: Path,
    aes_key: str = "",
) -> tuple[str, list[tuple[str, bytes, str | None, bytes | None, UAssetStringTable]]]:
    """从 `.pak` 提取可解析的 `StringTable` `.uasset`（自动配对同路径 `.uexp`）。"""
    mount = "../../../"
    files: dict[str, bytes] = {}
    if not aes_key:
        try:
            mount, files = _extract_pak_by_suffix_pure(pak_path, (".uasset", ".uexp"))
        except (OSError, ValueError, struct.error):
            files = {}
    if not files:
        mount, files, _err = _extract_pak_via_repak(
            pak_path, ("**/*StringTable*.uasset", "**/*StringTable*.uexp", "**/ST_*.uasset", "**/ST_*.uexp"), aes_key=aes_key
        )
    out: list[tuple[str, bytes, str | None, bytes | None, UAssetStringTable]] = []
    lower_map = {k.lower(): k for k in files}
    for rel, blob in sorted(files.items()):
        if not rel.lower().endswith(".uasset"):
            continue
        uexp_rel = lower_map.get(rel[:-7].lower() + ".uexp")
        uexp_blob = files.get(uexp_rel) if uexp_rel else None
        st = parse_uasset_stringtable(blob, uexp_blob=uexp_blob)
        if st is not None:
            out.append((rel, blob, uexp_rel, uexp_blob, st))
    return mount, out


def build_ue_patch_pak(files: dict[str, bytes], mount_point: str = "../../../") -> bytes:
    """将修改后的 .locres / .uasset 打包为标准 UE4/UE5 高优先级未压缩补丁包（v4 `.pak`）。

    所有 UE4 与 UE5 版本均原生挂载标准 v4 `.pak`，放进 `Content/Paks/~mods/*_P.pak`
    即可无损覆盖原版几十 GB 大包里的语言文件，且卸载只需删除该补丁文件。
    """
    body = bytearray()
    records: list[tuple[str, int, int, bytes]] = []

    for raw_path, data in sorted(files.items()):
        rel = raw_path.replace("\\", "/").lstrip("/")
        offset = len(body)
        sha1 = hashlib.sha1(data).digest()
        body.extend(struct.pack("<qqqI20sBI", 0, len(data), len(data), 0, sha1, 0, 0))
        body.extend(data)
        records.append((rel, offset, len(data), sha1))

    index_offset = len(body)
    index = bytearray()
    index.extend(write_fstring(mount_point or "../../../"))
    index.extend(struct.pack("<I", len(records)))
    for rel, offset, size, sha1 in records:
        index.extend(write_fstring(rel))
        index.extend(struct.pack("<qqqI20sBI", offset, size, size, 0, sha1, 0, 0))

    index_hash = hashlib.sha1(index).digest()
    footer = struct.pack("<BIIqq20s", 0, UE_PAK_MAGIC, 4, index_offset, len(index), index_hash)
    return bytes(body) + bytes(index) + footer


class UnrealAdapter(Adapter):
    """Unreal Engine 4 / 5 `.locres`、`.uasset` StringTable 与 `.pak -> *_P.pak` 补丁适配器。"""

    name = "unreal"
    priority = 26

    def __init__(self, include_samples: bool = False, aes_key: str = "") -> None:
        self.include_samples = include_samples
        self.aes_key = normalize_aes_key(aes_key) if aes_key else ""

    def _effective_aes_key(self, root: Path) -> str:
        if self.aes_key:
            return self.aes_key
        base = root.parent if root.is_file() else root
        key_file = base / UE_AES_FILENAME
        if key_file.is_file():
            try:
                raw = key_file.read_text(encoding="utf-8-sig").strip().splitlines()[0].strip()
                return normalize_aes_key(raw)
            except Exception:
                return ""
        return ""

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

    def _find_loose_stringtables(self, root: Path) -> list[tuple[Path, Path | None, UAssetStringTable]]:
        root = Path(root)
        candidates: list[Path] = []
        if root.is_file() and root.suffix.lower() == ".uasset":
            candidates = [root]
        elif root.is_dir():
            candidates = sorted(
                p for p in root.rglob("*.uasset")
                if p.is_file() and ".ferry-backup" not in p.parts and p.stat().st_size <= 16 * 1024 * 1024
            )
        out: list[tuple[Path, Path | None, UAssetStringTable]] = []
        for path in candidates:
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            uexp = path.with_suffix(".uexp")
            uexp_blob = uexp.read_bytes() if uexp.is_file() else None
            st = parse_uasset_stringtable(blob, uexp_blob=uexp_blob)
            if st is not None:
                out.append((path, uexp if uexp_blob is not None else None, st))
        return out

    def _count_loose_datatables(self, root: Path) -> int:
        if not Path(root).is_dir():
            return 0
        count = 0
        for p in Path(root).rglob("*.uasset"):
            if ".ferry-backup" in p.parts or not p.is_file():
                continue
            try:
                head = p.read_bytes()[:4096]
                if _is_datatable_uasset(head):
                    count += 1
            except OSError:
                continue
        return count

    def _has_pak_files(self, root: Path) -> list[Path]:
        root = Path(root)
        if root.is_file() and root.suffix.lower() == ".pak":
            return [root]
        if not root.is_dir():
            return []
        return sorted(
            p for p in root.rglob("*.pak")
            if p.is_file() and p.name != UE_PATCH_PAK_NAME and ".ferry-backup" not in p.parts
        )

    def _has_iostore_files(self, root: Path) -> list[Path]:
        if not Path(root).is_dir():
            return []
        return sorted(
            p for p in Path(root).rglob("*.utoc")
            if p.is_file() and ".ferry-backup" not in p.parts
        )

    def _scan_pak_locres(self, root: Path) -> list[tuple[Path, str, str, bytes]]:
        aes = self._effective_aes_key(root)
        out: list[tuple[Path, str, str, bytes]] = []
        for pak in self._has_pak_files(root):
            mount, found = extract_pak_locres(pak, aes_key=aes)
            for inner_rel, blob in sorted(found.items()):
                out.append((pak, mount, inner_rel, blob))
        return out

    def _scan_pak_stringtables(
        self, root: Path
    ) -> list[tuple[Path, str, str, bytes, str | None, bytes | None, UAssetStringTable]]:
        aes = self._effective_aes_key(root)
        out: list[tuple[Path, str, str, bytes, str | None, bytes | None, UAssetStringTable]] = []
        for pak in self._has_pak_files(root):
            mount, found = extract_pak_stringtables(pak, aes_key=aes)
            for uasset_rel, blob, uexp_rel, uexp_blob, st in found:
                out.append((pak, mount, uasset_rel, blob, uexp_rel, uexp_blob, st))
        return out

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        aes = self._effective_aes_key(root)
        locres_files = self._find_locres_files(root)
        loose_sts = self._find_loose_stringtables(root)
        paks = self._has_pak_files(root)
        utocs = self._has_iostore_files(root)

        # 1. 散装 .locres 或散装 StringTable .uasset
        if locres_files or loose_sts:
            entries = self.collect(root)
            if not entries:
                return ProbeReport(
                    engine="Unreal Engine 4/5",
                    root=root,
                    readable=False,
                    writable=False,
                    notes=["找到 .locres / StringTable 资产，但未解析出可翻译文本"],
                )
            writable = bool(locres_files) or any(st.writable for _, _, st in loose_sts)
            notes: list[str] = []
            if locres_files:
                notes.append(f"检出 {len(locres_files)} 个 Unreal .locres 本地化文件。")
            if loose_sts:
                w_cnt = sum(1 for _, _, st in loose_sts if st.writable)
                notes.append(f"检出 {len(loose_sts)} 个 Unreal StringTable .uasset（其中 {w_cnt} 个可无损写回）。")
            notes.append(f"共 {len(entries)} 条文本；回写采用 FString UTF-16LE 宽字符无损重建。")
            return ProbeReport(
                engine="Unreal Engine 4/5 (.locres / StringTable)",
                root=root,
                readable=True,
                writable=writable,
                unit_count=len(entries),
                container="loose assets",
                text_format=".locres / StringTable .uasset",
                unicode_engine=True,
                in_place=True,
                notes=notes,
            )

        # 2. .pak 内部 .locres / StringTable
        pak_locres = self._scan_pak_locres(root) if paks else []
        pak_sts = self._scan_pak_stringtables(root) if (paks and not pak_locres) else []
        if pak_locres or pak_sts:
            entries = self.collect(root)
            if entries:
                writable = bool(pak_locres) or any(st.writable for *_, st in pak_sts)
                notes = []
                if pak_locres:
                    notes.append(
                        f"从 {len({p for p, *_ in pak_locres})} 个 .pak 归档提取到 {len(pak_locres)} 个 .locres。"
                    )
                if pak_sts:
                    notes.append(f"从 .pak 归档提取到 {len(pak_sts)} 个 StringTable .uasset。")
                if aes:
                    notes.append("已使用提供的 256 位 AES 密钥成功解密 .pak。")
                notes.append(
                    f"共 {len(entries)} 条文本；回写策略：不修改原版大包，自动生成高优先级补丁包 ~mods/{UE_PATCH_PAK_NAME}。"
                )
                return ProbeReport(
                    engine="Unreal Engine 4/5 (.pak)",
                    root=root,
                    readable=True,
                    writable=writable,
                    unit_count=len(entries),
                    container="pak -> *_P.pak",
                    text_format=".locres / StringTable (FString UTF-16LE)",
                    unicode_engine=True,
                    in_place=True,
                    notes=notes,
                )

        # 3. 识别到 UE 工程但未能直接拆出文本 —— 给出精准诊断（AES 加密 / 缺 repak / IoStore / DataTable）
        is_ue_dir = bool(paks or utocs) and (
            (root / "Engine").is_dir()
            or any("paks" in [part.lower() for part in p.parts] for p in [*paks, *utocs])
        )
        if is_ue_dir:
            need_repak = [] if deps.REGISTRY["repak"].installed else ["repak"]
            encrypted_paks = 0
            v10_plus = 0
            for pak in paks[:12]:
                info = inspect_pak(pak)
                if info is not None:
                    if info.encrypted_index or info.has_encrypted_entries:
                        encrypted_paks += 1
                    if info.version >= 10:
                        v10_plus += 1

            notes = [f"检测到 Unreal Engine 4/5 游戏（{len(paks)} 个 .pak" + (f"，{len(utocs)} 个 .utoc IoStore" if utocs else "") + "），但未能拆出 .locres / StringTable。"]
            if encrypted_paks:
                if not aes:
                    notes.append(
                        f"发现 {encrypted_paks} 个 AES 加密 .pak：请在界面「UE AES 密钥」框（或游戏目录下 {UE_AES_FILENAME}）填入 256 位密钥（支持 0x+64位十六进制或 Base64）后重新检测。"
                    )
                else:
                    notes.append("已提供 AES 密钥但仍未解出 .locres（密钥可能不匹配，或文本不在 .locres 中）。")
            if need_repak:
                notes.append("缺少外部解包工具 repak（约 4.5 MB）：安装后可支持 UE5 v11 / Oodle 压缩与 AES 加密 .pak 自动拆包。")
            elif v10_plus and not encrypted_paks:
                notes.append("已检测到 UE5 v10/v11 .pak 且已安装 repak，但包内无 .locres（文本可能位于 UE5 Zen IoStore 或蓝图 DataTable 中）。")
            if utocs:
                notes.append("检测到 UE5 Zen IoStore 容器（.utoc / .ucas）：其资产表与传统 .pak 不同，建议先确认是否存在独立 .locres 语言包。")
            dt_count = self._count_loose_datatables(root)
            if dt_count:
                notes.append(f"另在目录中探测到 {dt_count} 个 DataTable .uasset（当前暂不盲改自定义结构体 DataTable）。")
            return ProbeReport(
                engine="Unreal Engine 4/5",
                root=root,
                readable=False,
                writable=False,
                container="pak / iostore",
                needs=need_repak,
                notes=notes,
            )

        return ProbeReport(engine=self.name, root=root, notes=["未找到 Unreal Engine .locres 或 StringTable 资源文件"])

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        entries: list[Entry] = []

        # 1. 散装 .locres
        locres_files = self._find_locres_files(root)
        for path in locres_files:
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

        # 2. 散装 StringTable .uasset
        for path, _uexp, st in self._find_loose_stringtables(root):
            rel = path.relative_to(base).as_posix()
            for item in st.entries:
                if is_unity_translatable(item.value):
                    entries.append(
                        Entry(
                            modid=root.name,
                            key=f"{rel}#ST:{item.index}:{item.key}",
                            english=item.value,
                            source="game",
                            kind="uasset_st",
                            rel_path=rel,
                            json_pointer=f"ST:{item.index}:{item.key}",
                        )
                    )
        if entries:
            return entries

        # 3. .pak 内 .locres
        for pak, _mount, inner_rel, blob in self._scan_pak_locres(root):
            pak_rel = pak.relative_to(base).as_posix()
            try:
                loc = parse_locres(blob)
            except Exception:
                continue
            for item in loc.strings:
                if is_unity_translatable(item.text):
                    entries.append(
                        Entry(
                            modid=root.name,
                            key=f"pak:{pak_rel}!{inner_rel}#LOCRES:{item.index}",
                            english=item.text,
                            source="game",
                            kind="pak_locres",
                            book=pak_rel,
                            rel_path=inner_rel,
                            json_pointer=f"LOCRES:{item.index}",
                        )
                    )
        if entries:
            return entries

        # 4. .pak 内 StringTable .uasset
        for pak, _mount, uasset_rel, _blob, _uexp_rel, _uexp_blob, st in self._scan_pak_stringtables(root):
            pak_rel = pak.relative_to(base).as_posix()
            for item in st.entries:
                if is_unity_translatable(item.value):
                    entries.append(
                        Entry(
                            modid=root.name,
                            key=f"pak:{pak_rel}!{uasset_rel}#ST:{item.index}:{item.key}",
                            english=item.value,
                            source="game",
                            kind="pak_uasset_st",
                            book=pak_rel,
                            rel_path=uasset_rel,
                            json_pointer=f"ST:{item.index}:{item.key}",
                        )
                    )
        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        entries = self.collect(root)
        report = ApplyReport(in_place=True, out_path=base)
        if not entries:
            report.notes.append("未找到可回写的 .locres / StringTable 条目")
            return report

        kinds = {e.kind for e in entries}

        # 分流 A：.pak 内部资产 -> 统一打包为高优先级 ~mods/ProjectFerry_zh_CN_P.pak
        if kinds & {"pak_locres", "pak_uasset_st"}:
            patch_files: dict[str, bytes] = {}
            mount_point = "../../../"
            target_paks_dir: Path | None = None

            if "pak_locres" in kinds:
                pak_items = self._scan_pak_locres(root)
                by_pak_inner = {(pak.relative_to(base).as_posix(), inner_rel): (pak, mount, blob) for pak, mount, inner_rel, blob in pak_items}
                by_target: dict[tuple[str, str], list[Entry]] = {}
                for e in entries:
                    if e.kind == "pak_locres":
                        by_target.setdefault((e.book, e.rel_path), []).append(e)
                for (pak_rel, inner_rel), group in by_target.items():
                    meta = by_pak_inner.get((pak_rel, inner_rel))
                    if meta is None:
                        continue
                    pak_path, mount, raw = meta
                    mount_point = mount or mount_point
                    if target_paks_dir is None:
                        target_paks_dir = pak_path.parent
                    loc_map: dict[str, str] = {}
                    for e in group:
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
                        report.conflicts.append(f"{inner_rel}: {exc}")
                        continue
                    if count > 0:
                        patch_files[inner_rel] = rebuilt
                        report.applied += count

            if "pak_uasset_st" in kinds:
                st_items = self._scan_pak_stringtables(root)
                by_st = {
                    (pak.relative_to(base).as_posix(), u_rel): (pak, mount, u_blob, e_rel, e_blob)
                    for pak, mount, u_rel, u_blob, e_rel, e_blob, _st in st_items
                }
                by_target_st: dict[tuple[str, str], list[Entry]] = {}
                for e in entries:
                    if e.kind == "pak_uasset_st":
                        by_target_st.setdefault((e.book, e.rel_path), []).append(e)
                for (pak_rel, u_rel), group in by_target_st.items():
                    meta = by_st.get((pak_rel, u_rel))
                    if meta is None:
                        continue
                    pak_path, mount, u_blob, e_rel, e_blob = meta
                    mount_point = mount or mount_point
                    if target_paks_dir is None:
                        target_paks_dir = pak_path.parent
                    st_map: dict[str, str] = {}
                    for e in group:
                        tr = translations.get(e.key) or translations.get(e.english)
                        if tr and tr.strip():
                            st_map[e.json_pointer] = tr
                            st_map[e.english] = tr
                        else:
                            report.missed += 1
                    if not st_map:
                        continue
                    try:
                        new_u, new_e, count = rebuild_uasset_stringtable(u_blob, st_map, uexp_blob=e_blob)
                    except Exception as exc:
                        report.conflicts.append(f"{u_rel}: {exc}")
                        continue
                    if count > 0:
                        patch_files[u_rel] = new_u
                        if e_rel and new_e is not None:
                            patch_files[e_rel] = new_e
                        report.applied += count

            if not patch_files or target_paks_dir is None:
                report.notes.append("未命中任何译文，未生成补丁 .pak")
                return report

            patch_bytes = build_ue_patch_pak(patch_files, mount_point=mount_point)
            mods_dir = target_paks_dir / "~mods"
            mods_dir.mkdir(parents=True, exist_ok=True)
            patch_path = mods_dir / UE_PATCH_PAK_NAME
            patch_rel = patch_path.relative_to(base).as_posix()
            undo = UndoLog.open(base, engine=self.name)
            if patch_path.exists():
                undo.will_modify(patch_rel)
            else:
                undo.will_add(patch_rel)
            patch_path.write_bytes(patch_bytes)
            undo.did_write(patch_rel)
            report.undo_path = undo.finish()
            report.notes.append(
                f"已生成高优先级虚幻汉化补丁包 {patch_rel}（含 {len(patch_files)} 个资产文件，共 {report.applied} 条），未修改原版 .pak。"
            )
            return report

        # 分流 B：散装 .locres 与散装 StringTable .uasset 原地无损重建
        pending: dict[str, bytes] = {}

        loc_by_file: dict[str, list[Entry]] = {}
        st_by_file: dict[str, list[Entry]] = {}
        for e in entries:
            if e.kind == "uasset_st":
                st_by_file.setdefault(e.rel_path, []).append(e)
            else:
                loc_by_file.setdefault(e.rel_path, []).append(e)

        for rel, file_entries in loc_by_file.items():
            full_path = base / rel
            try:
                raw = full_path.read_bytes()
            except OSError as exc:
                report.conflicts.append(f"{rel}: {exc}")
                continue
            loc_map = {}
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

        for rel, file_entries in st_by_file.items():
            full_path = base / rel
            uexp_path = full_path.with_suffix(".uexp")
            try:
                raw = full_path.read_bytes()
                uexp_raw = uexp_path.read_bytes() if uexp_path.is_file() else None
            except OSError as exc:
                report.conflicts.append(f"{rel}: {exc}")
                continue
            st_map = {}
            for e in file_entries:
                tr = translations.get(e.key) or translations.get(e.english)
                if tr and tr.strip():
                    st_map[e.json_pointer] = tr
                    st_map[e.english] = tr
                else:
                    report.missed += 1
            if not st_map:
                continue
            try:
                new_u, new_e, count = rebuild_uasset_stringtable(raw, st_map, uexp_blob=uexp_raw)
            except Exception as exc:
                report.conflicts.append(f"{rel}: {exc}")
                continue
            if count > 0:
                pending[rel] = new_u
                if uexp_raw is not None and new_e is not None:
                    pending[uexp_path.relative_to(base).as_posix()] = new_e
                report.applied += count

        if not pending:
            report.notes.append("未命中任何译文，未修改 Unreal 资产")
            return report

        undo = UndoLog.open(base, engine=self.name)
        for rel, new_raw in pending.items():
            undo.will_modify(rel)
            (base / rel).write_bytes(new_raw)
            undo.did_write(rel)
        report.undo_path = undo.finish()
        report.notes.append(f"已原地重建 {len(pending)} 个 Unreal 语言资产文件（共 {report.applied} 条）。")
        return report
