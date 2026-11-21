"""把一款 TTF 的家族名改成目标名（给 RGSS 字体替换用）。

RPG Maker 按**字体内部家族名**找字，不按文件名。日文/像素字没汉字时，
社区常见做法是：用一款中文字体覆盖 `Fonts/xxx.ttf`，并把 name 表改成
游戏要的那个名字（例如 `Coder's Crux`）——Scripts 一行都不用改。

只支持普通 TTF（单 SFNT），不碰 TTC。只用标准库。
"""

from __future__ import annotations

import struct
from pathlib import Path


class TtfRenameError(ValueError):
    pass


def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from(">I", data, off)[0]


def _u16(data: bytes, off: int) -> int:
    return struct.unpack_from(">H", data, off)[0]


def _checksum(data: bytes) -> int:
    # OpenType 表校验：按 4 字节大端累加；末尾不足补 0
    total = 0
    padded = data + b"\x00" * ((4 - (len(data) % 4)) % 4)
    for i in range(0, len(padded), 4):
        total = (total + _u32(padded, i)) & 0xFFFFFFFF
    return total


def rename_family(src: Path | bytes, family: str) -> bytes:
    """返回改名后的 TTF 字节。family 会写进 name ID 1/4/6（Win Unicode）。"""
    data = bytearray(Path(src).read_bytes() if not isinstance(src, (bytes, bytearray)) else src)
    if data[:4] == b"ttcf":
        raise TtfRenameError("不支持 TTC 字体集合，请用单独的 .ttf")
    if data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        raise TtfRenameError("不是可识别的 TTF/OTF")

    num_tables = _u16(data, 4)
    tables = []
    for i in range(num_tables):
        off = 12 + i * 16
        tag = bytes(data[off : off + 4])
        check = _u32(data, off + 4)
        offset = _u32(data, off + 8)
        length = _u32(data, off + 12)
        tables.append([tag, check, offset, length])

    name_index = next((i for i, t in enumerate(tables) if t[0] == b"name"), None)
    if name_index is None:
        raise TtfRenameError("没有 name 表")

    tag, _old_sum, offset, length = tables[name_index]
    name_blob = bytes(data[offset : offset + length])
    new_name = _rebuild_name_table(name_blob, family)
    # 新表放到文件末尾，改目录项；旧表空间废弃（字体仍合法）
    new_offset = len(data)
    # 4 字节对齐
    pad = (4 - (new_offset % 4)) % 4
    data.extend(b"\x00" * pad)
    new_offset = len(data)
    data.extend(new_name)
    # 尾部对齐
    data.extend(b"\x00" * ((4 - (len(new_name) % 4)) % 4))

    tables[name_index][1] = _checksum(new_name)
    tables[name_index][2] = new_offset
    tables[name_index][3] = len(new_name)

    for i, (t, check, off, leng) in enumerate(tables):
        entry = 12 + i * 16
        struct.pack_into(">4sIII", data, entry, t, check, off, leng)

    # head 表 checkSumAdjustment 重算
    _fix_checksum_adjustment(data, tables)
    return bytes(data)


def _rebuild_name_table(blob: bytes, family: str) -> bytes:
    """在原 name 表上打补丁（保留结构），只改 Win/Mac 的家族名。

    注意：整表精简重建会导致 Windows AddFontResourceEx 返回 0，
    RGSS 就报 UNABLE TO FIND xxx FONT。必须走补丁路径。
    """
    format_ = _u16(blob, 0)
    count = _u16(blob, 2)
    string_offset = _u16(blob, 4)
    if format_ > 1:
        raise TtfRenameError(f"不支持的 name 表格式 {format_}")

    records: list[list[int]] = []
    for i in range(count):
        o = 6 + i * 12
        records.append(list(struct.unpack_from(">6H", blob, o)))

    storage = bytearray(blob[string_offset:])
    encoded = family.encode("utf-16-be")
    # name ID: 1 Family, 4 Full name, 6 PostScript（去空格）
    targets = {
        1: encoded,
        4: encoded,
        6: family.replace(" ", "").encode("utf-16-be"),
    }
    patched_win = set()
    for rec in records:
        platform, encoding, _language, name_id, _length, _offset = rec
        if platform == 3 and encoding == 1 and name_id in targets:
            payload = targets[name_id]
            new_off = len(storage)
            storage.extend(payload)
            rec[4] = len(payload)
            rec[5] = new_off
            patched_win.add(name_id)

    # 补上缺失的 Win 记录（有的字只有中文语种名）
    for name_id, payload in targets.items():
        if name_id in patched_win:
            continue
        new_off = len(storage)
        storage.extend(payload)
        records.append([3, 1, 1033, name_id, len(payload), new_off])

    # Mac Roman 家族名：RGSS 私有字体枚举有时会看平台 1
    has_mac_family = any(r[0] == 1 and r[3] == 1 for r in records)
    if not has_mac_family:
        mac_fam = family.encode("latin-1", "replace")
        for name_id, payload in (
            (1, mac_fam),
            (2, b"Regular"),
            (4, mac_fam + b" Regular"),
            (6, family.replace(" ", "").encode("latin-1", "replace")),
        ):
            new_off = len(storage)
            storage.extend(payload)
            records.append([1, 0, 0, name_id, len(payload), new_off])
    else:
        mac_targets = {
            1: family.encode("latin-1", "replace"),
            4: family.encode("latin-1", "replace") + b" Regular",
            6: family.replace(" ", "").encode("latin-1", "replace"),
        }
        for rec in records:
            platform, _enc, _lang, name_id, _length, _offset = rec
            if platform == 1 and name_id in mac_targets:
                payload = mac_targets[name_id]
                new_off = len(storage)
                storage.extend(payload)
                rec[4] = len(payload)
                rec[5] = new_off

    out = bytearray()
    out.extend(struct.pack(">HHH", format_, len(records), 6 + len(records) * 12))
    for rec in records:
        out.extend(struct.pack(">6H", *rec))
    out.extend(storage)
    return bytes(out)


def _fix_checksum_adjustment(data: bytearray, tables: list) -> None:
    head = next((t for t in tables if t[0] == b"head"), None)
    if head is None:
        return
    _tag, _c, offset, length = head
    if length < 12:
        return
    # 先把 checkSumAdjustment 置 0
    struct.pack_into(">I", data, offset + 8, 0)
    total = _checksum(data)
    adjust = (0xB1B0AFBA - total) & 0xFFFFFFFF
    struct.pack_into(">I", data, offset + 8, adjust)


def write_renamed(src: Path, dest: Path, family: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(rename_family(src, family))
