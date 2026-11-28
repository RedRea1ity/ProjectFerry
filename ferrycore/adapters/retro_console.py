"""经典家用主机平台适配器 —— 世嘉土星 Sega Saturn (`.bin`/`.cue`/`.iso`/`IP.BIN`) 与 任天堂 N64 (`.z64`/`.v64`/`.n64`)。

本模块完全使用 Python 标准库实现两个 32/64 位经典主机平台的底层容器解析、校验和重算与定长原位回写：

1. **世嘉土星 Sega Saturn (`SaturnAdapter`)**：
   - 支持 **Redump 标准 2352 字节原始扇区镜像（`.bin` + `.cue`，MODE1/2352 与 MODE2/2352 Form 1）**、
     **2048 字节 Cooked `.iso`** 以及 **已解包的 `IP.BIN` 目录**。
   - 纯标准库校验第 0 扇区 `IP.BIN` 硬件头（`SEGA SEGASATURN `），读取游戏标题、产品编号、区域码与发行日期；
     再从第 16 扇区（`\\x01CD001`）跨扇区解析 ISO-9660 文件系统，定位所有脚本/剧本/可执行文件。
   - **黄皮书 Yellow Book (ECMA-130) EDC + Reed-Solomon ECC P/Q 校验重算 (`recompute_cdrom_edc_ecc`)**：
     在 2352 字节 `.bin` 中修改任意扇区后，自动重算 4 字节 EDC（多项式 `0xD8018001`）与 276 字节 GF(2^8)
     Reed-Solomon P/Q 纠错码（`0x11D`），并同步生成配套 `.cn.cue`，确保 Mednafen / Kronos / Yabause / SSF
     以及 Fenrir / Satiator / Saroo 光驱模拟器直读不报错。

2. **任天堂 Nintendo 64 (`N64Adapter`)**：
   - 自动识别并无损转换全部三种卡带字节序：**`.z64`（大端 `80 37 12 40`）**、**`.v64`（16 位交换 `37 80 40 12`）**、
     **`.n64`（32 位小端 `40 12 37 80`）**，回写后自动还原为原格式副本。
   - 严格保护头部（`0x00..0x40`）与 IPL3 引导代码（`0x40..0x1000`），只扫描/定长原位覆写 `0x1000` 之后的正文数据；
     同时自动探测 `Yaz0` / `MIO0` / `Yay0` 压缩块数量，诚实告知压缩覆盖情况。
   - **完整 N64 CIC 芯片识别与 CRC1/CRC2 重算 (`detect_n64_cic` / `compute_n64_crc` / `fix_n64_crc`)**：
     根据 `IPL3` 的 CRC32 自动识别 CIC-6101 / 6102 / 6103 / 6105 / 6106，按官方 `chksum64` 算法重算
     `0x10..0x18` 的双 32 位校验和，确保实机（EverDrive64 / SummerCart64）与 Ares / Mupen64Plus / Project64 正常引导。
"""

from __future__ import annotations

import hashlib
import re
import struct
import subprocess
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from ferrygame.rawbin import patch_blob, scan_sjis_strings, scan_utf8_strings
from ferrygame.tbl import ThingyTable, find_tbl_pair, is_thingy_tbl_file, scan_tbl_strings
from ferrygame.units import Unit

from .. import deps
from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .console import ISO_SECTOR_SIZE, _STRUCTURED_SUFFIXES
from .container import unique_output
from .mobile import collect_entries_from_virtual_files, patch_virtual_files
from .undo import UndoLog

CHD_MAGIC = b"MComprHD"

# ============================================================================
# Yellow Book (ECMA-130) CD-ROM 2352 字节扇区 EDC / ECC (P & Q) 校验引擎
# ============================================================================

CDROM_RAW_SECTOR_SIZE = 2352
CDROM_SYNC = b"\x00\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x00"
SATURN_MAGIC = b"SEGA SEGASATURN "

# 延迟初始化一次查找表（Neill Corlett ecmtools / ECMA-130 标准表）
_ECC_F_LUT: list[int] = []
_ECC_B_LUT: list[int] = []
_EDC_LUT: list[int] = []


def _init_cdrom_luts() -> None:
    if _EDC_LUT:
        return
    ecc_f = [0] * 256
    ecc_b = [0] * 256
    edc_t = [0] * 256
    for i in range(256):
        j = (i << 1) ^ (0x11D if (i & 0x80) else 0)
        ecc_f[i] = j & 0xFF
        ecc_b[(i ^ j) & 0xFF] = i
        edc = i
        for _ in range(8):
            edc = (edc >> 1) ^ (0xD8018001 if (edc & 1) else 0)
        edc_t[i] = edc & 0xFFFFFFFF
    _ECC_F_LUT.extend(ecc_f)
    _ECC_B_LUT.extend(ecc_b)
    _EDC_LUT.extend(edc_t)


def compute_cdrom_edc(data: bytes | bytearray) -> int:
    """计算黄皮书 CD-ROM 32 位 EDC（反射多项式 `0xD8018001`）。"""
    _init_cdrom_luts()
    lut = _EDC_LUT
    edc = 0
    for b in data:
        edc = (edc >> 8) ^ lut[(edc ^ b) & 0xFF]
    return edc & 0xFFFFFFFF


def _compute_ecc_block(
    address: bytes | bytearray,
    src: bytes | bytearray,
    major_count: int,
    minor_count: int,
    major_mult: int,
    minor_inc: int,
    dest: bytearray,
    dest_off: int,
) -> None:
    _init_cdrom_luts()
    f_lut = _ECC_F_LUT
    b_lut = _ECC_B_LUT
    total = major_count * minor_count
    for major in range(major_count):
        index = (major >> 1) * major_mult + (major & 1)
        ecc_a = 0
        ecc_b = 0
        for _ in range(minor_count):
            temp = address[index] if index < 4 else src[index - 4]
            index += minor_inc
            if index >= total:
                index -= total
            ecc_a ^= temp
            ecc_b ^= temp
            ecc_a = f_lut[ecc_a]
        ecc_a = b_lut[f_lut[ecc_a] ^ ecc_b]
        dest[dest_off + major] = ecc_a
        dest[dest_off + major + major_count] = ecc_a ^ ecc_b


def recompute_cdrom_edc_ecc(sector: bytearray, mode: int = 1) -> bytearray:
    """原位重算单个 2352 字节 CD-ROM 扇区的 EDC 与 Reed-Solomon ECC (P/Q)。

    - `mode == 1` (MODE1/2352): EDC 覆盖 `0..2064` 写至 `2064..2068`，清零 `2068..2076`，
      ECC 使用 `sector[12:16]` 作为地址计算 P (`2076..2248`) 与 Q (`2248..2352`)。
    - `mode == 2` (MODE2/2352 Form 1): EDC 覆盖 `16..2072` 写至 `2072..2076`，
      ECC 使用全零 4 字节地址计算 P (`2076..2248`) 与 Q (`2248..2352`)。
    """
    if len(sector) != CDROM_RAW_SECTOR_SIZE:
        raise ValueError(f"CD-ROM 原始扇区必须为 2352 字节，实际 {len(sector)}")
    if mode == 1:
        edc = compute_cdrom_edc(sector[0:2064])
        struct.pack_into("<I", sector, 2064, edc)
        sector[2068:2076] = b"\x00" * 8
        addr = bytes(sector[12:16])
        _compute_ecc_block(addr, sector[16:2076], 86, 24, 2, 86, sector, 2076)
        _compute_ecc_block(addr, sector[16:2248], 52, 43, 86, 88, sector, 2248)
    elif mode == 2:
        edc = compute_cdrom_edc(sector[16:2072])
        struct.pack_into("<I", sector, 2072, edc)
        zero_addr = b"\x00\x00\x00\x00"
        _compute_ecc_block(zero_addr, sector[16:2076], 86, 24, 2, 86, sector, 2076)
        _compute_ecc_block(zero_addr, sector[16:2248], 52, 43, 86, 88, sector, 2248)
    return sector


# ============================================================================
# Sega Saturn 光盘镜像 & IP.BIN & ISO-9660 解析器
# ============================================================================

_SATURN_SKIP_EXTS = {
    ".cpk", ".avi", ".mpg", ".adx", ".pcm", ".wav", ".aif", ".snd", ".ton", ".seq",
    ".map", ".spr", ".tim", ".cg", ".chr", ".pal", ".vram", ".s3m", ".mod", ".xa",
    ".str", ".mov", ".mp3", ".ogg", ".flac", ".png", ".bmp", ".jpg", ".tga",
}
_SATURN_BIN_EXTS = {
    ".bin", ".dat", ".mes", ".scr", ".scn", ".msg", ".txt", ".tbl", ".ovl",
    ".prs", ".rel", ".prg", ".exe", ".abs", ".0", ".1", ".2", ".3",
}
_CUE_FILE_RE = re.compile(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))\s+BINARY\b', re.IGNORECASE)
_CUE_MODE_RE = re.compile(r"^\s*TRACK\s+0?1\s+MODE([12])/(2048|2352)\b", re.IGNORECASE)


@dataclass
class SaturnHeader:
    hardware_id: str = ""
    maker_id: str = ""
    product_no: str = ""
    version: str = ""
    release_date: str = ""
    area_codes: str = ""
    title: str = ""


@dataclass
class SaturnIsoEntry:
    path: str
    lba: int
    size: int
    dir_lba: int
    dir_offset_in_sector: int  # 指向该目录记录中 data_length (both-endian uint32) 在扇区内的字节偏移


@dataclass
class SaturnDiscLayout:
    sector_size: int = ISO_SECTOR_SIZE  # 2048 或 2352
    data_offset: int = 0  # 0 (ISO) / 16 (MODE1/2352) / 24 (MODE2/2352)
    mode: int = 0  # 0 (cooked ISO) / 1 (MODE1/2352) / 2 (MODE2/2352)
    header: SaturnHeader = field(default_factory=SaturnHeader)
    files: dict[str, SaturnIsoEntry] = field(default_factory=dict)


def detect_cdrom_layout(blob: bytes) -> tuple[int, int, int]:
    """根据第 0 扇区判断镜像是 Cooked ISO (2048) 还是 Raw BIN (2352 MODE1/MODE2)。"""
    if len(blob) >= CDROM_RAW_SECTOR_SIZE and blob[:12] == CDROM_SYNC:
        mode = blob[15]
        if mode == 2:
            return CDROM_RAW_SECTOR_SIZE, 24, 2
        return CDROM_RAW_SECTOR_SIZE, 16, 1
    return ISO_SECTOR_SIZE, 0, 0


def read_cdrom_sector(blob: bytes, lba: int, sector_size: int, data_offset: int) -> bytes:
    start = lba * sector_size + data_offset
    end = start + ISO_SECTOR_SIZE
    if start < 0 or end > len(blob):
        raise ValueError(f"CD-ROM 扇区 LBA {lba} 越界")
    return blob[start:end]


def read_cdrom_file(blob: bytes, lba: int, size: int, sector_size: int, data_offset: int) -> bytes:
    """从 CD-ROM 镜像按 LBA 跨扇区读取连续文件数据（兼容 2048 ISO 与 2352 RAW BIN）。"""
    if size <= 0:
        return b""
    if sector_size == ISO_SECTOR_SIZE and data_offset == 0:
        start = lba * ISO_SECTOR_SIZE
        if start + size > len(blob):
            raise ValueError(f"ISO 文件 LBA {lba} 长度 {size} 越界")
        return blob[start : start + size]
    sectors = (size + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE
    out = bytearray()
    for i in range(sectors):
        out.extend(read_cdrom_sector(blob, lba + i, sector_size, data_offset))
    return bytes(out[:size])


def write_cdrom_file(
    img: bytearray,
    lba: int,
    new_data: bytes,
    sector_size: int,
    data_offset: int,
    mode: int,
) -> None:
    """把修改后的文件数据写回 CD-ROM 镜像的对应 LBA 扇区；若为 2352 RAW 则重算每扇区 EDC/ECC。"""
    sectors = (len(new_data) + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE
    for i in range(sectors):
        chunk = new_data[i * ISO_SECTOR_SIZE : (i + 1) * ISO_SECTOR_SIZE].ljust(ISO_SECTOR_SIZE, b"\x00")
        sec_start = (lba + i) * sector_size
        img[sec_start + data_offset : sec_start + data_offset + ISO_SECTOR_SIZE] = chunk
        if sector_size == CDROM_RAW_SECTOR_SIZE and mode in (1, 2):
            sec_buf = bytearray(img[sec_start : sec_start + CDROM_RAW_SECTOR_SIZE])
            recompute_cdrom_edc_ecc(sec_buf, mode=mode)
            img[sec_start : sec_start + CDROM_RAW_SECTOR_SIZE] = sec_buf


def parse_saturn_header(sector0: bytes) -> SaturnHeader:
    """解析世嘉土星 `IP.BIN` 第 0 扇区硬件引导头。"""
    if len(sector0) < 0xD0 or sector0[:16] != SATURN_MAGIC:
        raise ValueError("不是有效的世嘉土星 IP.BIN 头（缺少 'SEGA SEGASATURN ' 魔数）")

    def _dec(raw: bytes) -> str:
        raw = raw.rstrip(b"\x00 ")
        try:
            return raw.decode("cp932").strip()
        except UnicodeDecodeError:
            return raw.decode("ascii", errors="replace").strip()

    return SaturnHeader(
        hardware_id=_dec(sector0[0x00:0x10]),
        maker_id=_dec(sector0[0x10:0x20]),
        product_no=_dec(sector0[0x20:0x2A]),
        version=_dec(sector0[0x2A:0x30]),
        release_date=_dec(sector0[0x30:0x38]),
        area_codes=_dec(sector0[0x40:0x50]),
        title=_dec(sector0[0x60:0xD0]),
    )


def parse_saturn_disc(blob: bytes) -> SaturnDiscLayout:
    """解析世嘉土星光盘镜像（支持 2352 RAW BIN 与 2048 ISO），返回 IP.BIN 头与 ISO-9660 文件表。"""
    sector_size, data_offset, mode = detect_cdrom_layout(blob)
    if len(blob) < 17 * sector_size:
        raise ValueError("镜像体积过小，不是世嘉土星光盘镜像")
    sector0 = read_cdrom_sector(blob, 0, sector_size, data_offset)
    header = parse_saturn_header(sector0)

    pvd = read_cdrom_sector(blob, 16, sector_size, data_offset)
    if pvd[:6] != b"\x01CD001":
        raise ValueError("世嘉土星镜像第 16 扇区未见 ISO-9660 主卷描述符 (CD001)")

    root_rec = pvd[156:190]
    root_lba = struct.unpack_from("<I", root_rec, 2)[0]
    root_size = struct.unpack_from("<I", root_rec, 10)[0]

    files: dict[str, SaturnIsoEntry] = {}
    visited: set[int] = set()
    total_sectors = len(blob) // sector_size

    def _walk_dir(dir_lba: int, dir_size: int, prefix: str) -> None:
        if dir_lba in visited or dir_lba <= 0 or dir_lba >= total_sectors:
            return
        visited.add(dir_lba)
        sec_count = max(1, (dir_size + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE)
        for s_idx in range(sec_count):
            cur_lba = dir_lba + s_idx
            if cur_lba >= total_sectors:
                break
            sec = read_cdrom_sector(blob, cur_lba, sector_size, data_offset)
            pos = 0
            while pos < ISO_SECTOR_SIZE:
                rec_len = sec[pos]
                if rec_len == 0:
                    break
                if pos + rec_len > ISO_SECTOR_SIZE or rec_len < 34:
                    break
                ext_lba = struct.unpack_from("<I", sec, pos + 2)[0]
                data_len = struct.unpack_from("<I", sec, pos + 10)[0]
                flags = sec[pos + 25]
                name_len = sec[pos + 32]
                raw_name = sec[pos + 33 : pos + 33 + name_len]
                if raw_name not in (b"\x00", b"\x01"):
                    name = raw_name.decode("ascii", errors="replace")
                    if ";" in name:
                        name = name.split(";", 1)[0]
                    if name.endswith("."):
                        name = name[:-1]
                    rel = f"{prefix}/{name}" if prefix else name
                    if flags & 0x02:
                        _walk_dir(ext_lba, data_len, rel)
                    else:
                        end_lba = ext_lba + ((data_len + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE)
                        if 0 <= ext_lba and end_lba <= total_sectors:
                            files[rel] = SaturnIsoEntry(
                                path=rel,
                                lba=ext_lba,
                                size=data_len,
                                dir_lba=cur_lba,
                                dir_offset_in_sector=pos + 10,
                            )
                pos += rec_len

    _walk_dir(root_lba, root_size, "")
    return SaturnDiscLayout(
        sector_size=sector_size,
        data_offset=data_offset,
        mode=mode,
        header=header,
        files=files,
    )


def parse_cue_first_bin(cue_path: Path) -> Path | None:
    """从 `.cue` 描述文件中解析第一条数据轨道对应的 `.bin`/`.iso` 文件路径。"""
    try:
        text = cue_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        m = _CUE_FILE_RE.match(line)
        if m:
            fname = m.group(1) or m.group(2)
            cand = cue_path.parent / fname
            if cand.is_file():
                return cand
    return None


def rewrite_cue_for_patched_bin(cue_text: str, old_bin_name: str, new_bin_name: str) -> str:
    """把 `.cue` 中第一条指向原 `.bin` 的 `FILE` 指令替换为新 `.cn.bin` 文件名。"""
    replaced = False
    out_lines: list[str] = []
    for line in cue_text.splitlines():
        if not replaced:
            m = _CUE_FILE_RE.match(line)
            if m:
                fname = m.group(1) or m.group(2)
                if fname.lower() == old_bin_name.lower() or not replaced:
                    out_lines.append(f'FILE "{new_bin_name}" BINARY')
                    replaced = True
                    continue
        out_lines.append(line)
    newline = "\r\n" if "\r\n" in cue_text else "\n"
    return newline.join(out_lines) + newline


def _is_saturn_candidate_file(rel: str, size: int) -> bool:
    if size <= 0 or size > 12 * 1024 * 1024:
        return False
    low = rel.lower()
    fname = Path(low).name
    if fname in ("ip.bin", "abs.txt", "bib.txt", "cpy.txt"):
        return False
    suf = Path(low).suffix
    if suf in _SATURN_SKIP_EXTS:
        return False
    if suf in _STRUCTURED_SUFFIXES or suf in _SATURN_BIN_EXTS:
        return True
    # 土星主程序与脚本经常没有后缀（如 0 / 1ST_READ / MAIN / SLPS 等）
    return suf == "" and size <= 4 * 1024 * 1024


def _is_clean_retro_text(text: str) -> bool:
    """过滤 SH-2 / MIPS 机器码中偶发的 2–4 字假名/伪控制符短噪声（如 'チに'、'鋪^M'、'唐Q$'）。"""
    s = text.strip()
    if not s:
        return False
    # 含半角控制符符号且总长极短，必为机器码巧合
    if len(s) <= 6 and any(ch in s for ch in "^$%\\<>|@#`~_"):
        return False
    # 至少 4 个可见字符（或包含日文句读标点）
    vis = [ch for ch in s if not ch.isspace()]
    if len(vis) < 4:
        return False
    kana = sum(1 for ch in vis if "\u3040" <= ch <= "\u30ff")
    cjk = sum(1 for ch in vis if "\u4e00" <= ch <= "\u9fff")
    ascii_alnum = sum(1 for ch in vis if ch.isascii() and ch.isalnum())
    # 纯日文短串必须至少有 3 个假名或带日文标点（拦截 5 字全生僻汉字+1假名如 'め衣釈悲壤'）
    if kana + cjk > 0:
        punct = sum(1 for ch in vis if ch in "。、！？「」『』…・ー～（）【】")
        if kana < 2 and punct == 0:
            return False
        if len(vis) <= 6 and kana < 2 and cjk >= 3:
            return False
        if ascii_alnum > 0 and punct == 0 and len(vis) <= 8:
            return False
    return True


_RETRO_SDK_NOISE_RE = re.compile(
    r"(?:RSP\s+(?:Gfx|SW|Audio)|F3DEX|F3DLP|S2DEX|L3DEX|Yoshitaka\s+Yasumoto|SGI\s+U64"
    r"|SEGA\s+ENTERPRISES|SEGA\s+SEGASATURN|CRI\s+ADXF|Sofdec|Cinepak|libultra"
    r"|Copyright\s+\(C\)|All\s+Rights\s+Reserved)",
    re.IGNORECASE,
)
_RETRO_ASCII_SPAN_RE = re.compile(rb"[A-Za-z0-9 .,!?:'\"/%\-\n]{4,160}\x00")
_RETRO_WORD_RE = re.compile(r"[A-Za-z]{2,20}")
_RETRO_COMMON_WORDS = {
    "a", "an", "to", "of", "in", "on", "by", "or", "at", "is", "it", "be", "no", "ok",
    "the", "and", "for", "are", "not", "you", "all", "can", "may", "was", "has", "use",
    "new", "old", "yes", "off", "key", "pak", "end", "one", "two", "red", "run", "hit",
    "load", "save", "quit", "exit", "back", "next", "play", "mode", "time", "life",
    "game", "over", "menu", "help", "auto", "easy", "hard", "name", "rank", "info",
    "item", "card", "data", "full", "free", "note", "page", "pack", "blue", "open",
    "start", "press", "enter", "stage", "level", "score", "music", "sound", "audio",
    "video", "clear", "reset", "retry", "again", "pause", "bonus", "extra", "rules",
    "kills", "found", "error", "empty", "erase", "multi", "light", "heavy", "power",
    "armor", "super", "hyper", "ultra", "final", "world", "earth", "space", "enemy",
    "option", "options", "return", "cancel", "select", "delete", "create", "insert",
    "single", "player", "players", "normal", "master", "health", "weapon", "shield",
    "attack", "defend", "status", "memory", "config", "button", "rumble", "damage",
    "repair", "yellow", "secret", "secrets", "mission", "control", "controls",
    "continue", "password", "complete", "completed", "settings", "graphics", "credits",
    "controller", "difficulty", "checkpoint", "objectives", "shotgun", "blaster",
    "machinegun", "chaingun", "railgun", "rocket", "grenade", "station", "defense",
    "freighter", "charges", "explosive", "requires", "contents", "properly", "damaged",
}
_VOWELS = set("aeiouyAEIOUY")


def _is_valid_retro_ascii(text: str) -> bool:
    """识别 90 年代主机 ROM 中的欧美英文菜单/台词（支持全大写、全小写与句首大写，排除 MIPS/SH-2 噪声）。"""
    s = text.strip()
    if not s or len(s) < 4 or len(s) > 160:
        return False
    if _RETRO_SDK_NOISE_RE.search(s):
        return False
    if any(ch in s for ch in "{}[]<>|\\^~`@#$&_=*+"):
        return False
    # 拒绝过长无空格随机串（单字母词或大写菜单词除外）
    words = _RETRO_WORD_RE.findall(s)
    if not words:
        return False
    # 每个英文词必须有合理元音结构（拦截 ReUgwz / tdMaG / FVFVEEF 这类位图/机器码巧合）
    good_words = 0
    known_hits = 0
    for w in words:
        lw = w.lower()
        if lw in _RETRO_COMMON_WORDS:
            known_hits += 1
            good_words += 1
            continue
        # 合法自然英文词：要么全大写/全小写/首字母大写，且含元音且无连续 4 个辅音/混合大小写
        is_case_clean = w.islower() or w.isupper() or (w[0].isupper() and w[1:].islower())
        if not is_case_clean:
            continue
        if len(w) >= 3 and any(c in _VOWELS for c in w) and not re.search(r"[^aeiouyAEIOUY]{5,}", w):
            good_words += 1
    if good_words < len(words) * 0.7:
        return False
    # 单词菜单项（如 OPTIONS / PASSWORD / RETURN / SINGLE / MULTI）：必须命中常用词且纯大小写干净
    if " " not in s and "\n" not in s:
        return len(words) == 1 and known_hits == 1 and len(s) >= 5 and (s.isupper() or s.islower() or s.istitle())
    # 多词短语/句子（如 'START GAME', 'delete game note?', 'controller pak full.', 'yellow key'）
    if len(words) >= 2 and (known_hits >= 1 or (len(words) >= 3 and good_words >= 3)):
        # 防止随机数字/符号堆叠
        alpha_ratio = sum(1 for c in s if c.isalpha() or c in " \n") / len(s)
        return alpha_ratio >= 0.72
    return False


def _scan_retro_ascii_strings(blob: bytes, rel_path: str = "") -> list[Unit]:
    units: list[Unit] = []
    order = 0
    for m in _RETRO_ASCII_SPAN_RE.finditer(blob):
        raw = m.group(0)[:-1]
        # 去掉前导非正文垃圾字节（如 2 字节控制头 + 正文），仅当紧跟起点干净时保留
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            continue
        if not _is_valid_retro_ascii(text):
            continue
        start = m.start()
        order += 1
        key = f"{rel_path}@0x{start:x}" if rel_path else f"0x{start:x}"
        units.append(
            Unit(
                key=key,
                src=text,
                kind="menu" if len(text.split()) <= 3 and not text.endswith((".", "?", "!")) else "story",
                file=rel_path,
                order=order,
                max_bytes=len(raw),
                encoding="utf-8",
                meta={
                    "offset": start,
                    "length": len(raw),
                    "src_encoding": "ascii",
                    "raw_hex": raw.hex(),
                    "pad": "nul",
                },
            )
        )
    return units


def _scan_retro_console_binary(
    blob: bytes,
    rel_path: str = "",
    src_tbl: ThingyTable | None = None,
    *,
    allow_sjis: bool = True,
) -> list[Unit]:
    """90 年代大端主机（世嘉土星 SH-2 / N64 MIPS）专用二进制扫描器。

    - 若外挂了 Thingy `.tbl`，严格按自定义码表扫描；
    - 否则扫描 Shift-JIS 日文（日版/未知区）与主机风格英文菜单/对白（支持全大写与全小写），
      绝不对大端 SH-2/MIPS 机器码跑 UTF-16LE 或西里尔 UTF-8，且自动剔除 SDK 微码串。
    """
    if src_tbl is not None:
        return scan_tbl_strings(blob, src_tbl, rel_path=rel_path)
    sjis_units = (
        [
            u for u in scan_sjis_strings(blob, rel_path=rel_path, target_encoding="gbk")
            if _is_clean_retro_text(u.src)
        ]
        if allow_sjis
        else []
    )
    ascii_units = _scan_retro_ascii_strings(blob, rel_path=rel_path)
    # 兼容原有 scan_utf8_strings 中带长句的英文
    extra_en = [
        u for u in scan_utf8_strings(blob, rel_path=rel_path)
        if all(ord(ch) < 0x80 for ch in u.src) and not _RETRO_SDK_NOISE_RE.search(u.src)
    ]

    # O(N log N) 去重合并（Shift-JIS > 复古英文 > 通用英文，彻底避免大剧本 O(N^2) 卡死）
    tagged: list[tuple[int, int, int, Unit]] = []
    for prio, group in ((0, sjis_units), (1, ascii_units), (2, extra_en)):
        for u in group:
            off = int(u.meta.get("offset", 0))
            ln = int(u.meta.get("length") or u.max_bytes or 0)
            if ln > 0:
                tagged.append((off, prio, off + ln, u))
    tagged.sort(key=lambda item: (item[0], item[1]))

    merged: list[Unit] = []
    last_end = -1
    for off, _prio, end, u in tagged:
        if off < last_end:
            continue
        last_end = end
        merged.append(u)
    for idx, u in enumerate(merged, start=1):
        u.order = idx
    return merged


def _collect_saturn_units(
    modid: str,
    virtual_files: dict[str, bytes],
    *,
    src_tbl: ThingyTable | None = None,
) -> tuple[list[Entry], list[Unit], dict[str, list[Unit]]]:
    structured_files = {
        k: v for k, v in virtual_files.items() if k.lower().endswith(_STRUCTURED_SUFFIXES)
    }
    entries = collect_entries_from_virtual_files(modid, structured_files)
    all_units: list[Unit] = []
    order = 0
    for e in entries:
        order += 1
        all_units.append(
            Unit(
                key=e.key,
                src=e.english,
                kind="story",
                file=e.rel_path,
                order=order,
                encoding="utf-8",
            )
        )

    bin_units_by_file: dict[str, list[Unit]] = {}
    for rel, raw in sorted(virtual_files.items()):
        if rel in structured_files:
            continue
        units = _scan_retro_console_binary(raw, rel_path=rel, src_tbl=src_tbl)
        if not units:
            continue
        bin_units_by_file[rel] = units
        for u in units:
            order += 1
            u.order = order
            all_units.append(u)
            entries.append(
                Entry(
                    modid=modid,
                    key=u.key,
                    english=u.src,
                    source="game",
                    kind=f"bin_{u.kind}",
                    rel_path=rel,
                    json_pointer=f"BIN:{u.meta.get('offset', 0)}:{u.max_bytes}",
                )
            )
    return entries, all_units, bin_units_by_file


def is_chd_file(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(CHD_MAGIC)) == CHD_MAGIC
    except OSError:
        return False


def _chd_extract_cached(chd_path: Path, chdman_exe: Path) -> tuple[Path, Path]:
    """用 `chdman extractcd` 把 `.chd` 解包到按大小+时间戳命名的临时缓存目录，避免重复解包。"""
    st = chd_path.stat()
    sig = hashlib.sha1(f"{chd_path.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8")).hexdigest()[:12]
    cache_dir = Path(tempfile.gettempdir()) / "ferry_chd_cache" / f"{chd_path.stem}_{sig}"
    cue_out = cache_dir / f"{chd_path.stem}.cue"
    bin_out = cache_dir / f"{chd_path.stem}.bin"
    if cue_out.is_file() and bin_out.is_file():
        return bin_out, cue_out
    cache_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [str(chdman_exe), "extractcd", "-i", str(chd_path), "-o", str(cue_out), "-ob", str(bin_out), "-f"],
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0 or not bin_out.is_file():
        raise ValueError(f"chdman 解包失败：{proc.stderr.strip() or proc.stdout.strip()}")
    return bin_out, cue_out


class SaturnAdapter(Adapter):
    """世嘉土星 Sega Saturn（`.chd`/`.bin`/`.cue`/`.iso` 与 `IP.BIN` 解包目录）适配器。"""

    name = "saturn"
    priority = 14

    def _find_chd(self, root: Path) -> Path | None:
        root = Path(root)
        if root.is_file() and root.suffix.lower() == ".chd" and ".cn" not in root.stem.lower():
            return root if is_chd_file(root) else None
        if root.is_dir():
            for p in sorted(root.glob("*.chd")):
                if p.is_file() and ".cn" not in p.stem.lower() and is_chd_file(p):
                    return p
        return None

    def _find_disc(self, root: Path) -> tuple[Path | None, Path | None, Path | None]:
        """返回 `(image_path, cue_path, chd_path)`。"""
        root = Path(root)
        chd_p = self._find_chd(root)
        if chd_p is not None:
            chd_dep = deps.REGISTRY.get("chdman")
            chdman_exe = chd_dep.locate() if chd_dep else None
            if chdman_exe is None:
                return None, None, chd_p
            try:
                bin_p, cue_p = _chd_extract_cached(chd_p, chdman_exe)
                with open(bin_p, "rb") as fh:
                    head = fh.read(CDROM_RAW_SECTOR_SIZE)
                _, data_off, _ = detect_cdrom_layout(head)
                if head[data_off : data_off + 16] == SATURN_MAGIC:
                    return bin_p, cue_p, chd_p
            except (OSError, ValueError):
                return None, None, None
            return None, None, None

        candidates: list[tuple[Path, Path | None]] = []
        if root.is_file():
            suf = root.suffix.lower()
            if ".cn" in root.stem.lower():
                return None, None, None
            if suf == ".cue":
                bin_p = parse_cue_first_bin(root)
                if bin_p is not None:
                    candidates.append((bin_p, root))
            elif suf in (".bin", ".iso", ".img"):
                sib_cue = root.with_suffix(".cue")
                candidates.append((root, sib_cue if sib_cue.is_file() else None))
        elif root.is_dir():
            seen: set[Path] = set()
            for cue in sorted(root.glob("*.cue")):
                if ".cn" in cue.stem.lower():
                    continue
                bin_p = parse_cue_first_bin(cue)
                if bin_p is not None and bin_p not in seen:
                    seen.add(bin_p)
                    candidates.append((bin_p, cue))
            for ext in ("*.bin", "*.iso", "*.img"):
                for p in sorted(root.glob(ext)):
                    if not p.is_file() or ".cn" in p.stem.lower() or p in seen:
                        continue
                    seen.add(p)
                    sib_cue = p.with_suffix(".cue")
                    candidates.append((p, sib_cue if sib_cue.is_file() else None))

        for img_p, cue_p in candidates:
            try:
                sz = img_p.stat().st_size
                if sz < 17 * ISO_SECTOR_SIZE:
                    continue
                with open(img_p, "rb") as fh:
                    head = fh.read(CDROM_RAW_SECTOR_SIZE)
                sec_size, data_off, _ = detect_cdrom_layout(head)
                if head[data_off : data_off + 16] == SATURN_MAGIC:
                    return img_p, cue_p, None
            except OSError:
                continue
        return None, None, None

    def _find_unpacked_ip(self, root: Path) -> Path | None:
        root = Path(root)
        if not root.is_dir():
            return None
        for name in ("IP.BIN", "ip.bin"):
            cand = root / name
            if cand.is_file():
                try:
                    if cand.read_bytes()[:16] == SATURN_MAGIC:
                        return cand
                except OSError:
                    continue
        return None

    def _load_disc(
        self, root: Path
    ) -> tuple[Path | None, Path | None, Path | None, bytes | None, SaturnDiscLayout | None, dict[str, bytes], SaturnHeader | None]:
        img_p, cue_p, chd_p = self._find_disc(root)
        if chd_p is not None and img_p is None:
            return None, None, chd_p, None, None, {}, None
        if img_p is not None:
            blob = img_p.read_bytes()
            layout = parse_saturn_disc(blob)
            vfiles: dict[str, bytes] = {}
            for rel, ent in layout.files.items():
                if _is_saturn_candidate_file(rel, ent.size):
                    vfiles[rel] = read_cdrom_file(blob, ent.lba, ent.size, layout.sector_size, layout.data_offset)
            return img_p, cue_p, chd_p, blob, layout, vfiles, layout.header

        ip_path = self._find_unpacked_ip(root)
        if ip_path is not None:
            hdr = parse_saturn_header(ip_path.read_bytes()[:0x100])
            vfiles = {}
            for p in sorted(root.rglob("*")):
                if not p.is_file() or ".ferry-backup" in p.parts:
                    continue
                rel = p.relative_to(root).as_posix()
                try:
                    sz = p.stat().st_size
                except OSError:
                    continue
                if _is_saturn_candidate_file(rel, sz):
                    if p.suffix.lower() == ".tbl" and is_thingy_tbl_file(p):
                        continue
                    try:
                        vfiles[rel] = p.read_bytes()
                    except OSError:
                        continue
            return None, None, None, None, None, vfiles, hdr

        return None, None, None, None, None, {}, None

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        try:
            img_p, _cue_p, chd_p, _blob, layout, files, hdr = self._load_disc(root)
        except ValueError as exc:
            return ProbeReport(engine=self.name, root=root, notes=[f"世嘉土星镜像解析失败：{exc}"])
        if chd_p is not None and img_p is None:
            return ProbeReport(
                engine="Sega Saturn (.chd)",
                root=root,
                readable=False,
                writable=False,
                container="chd",
                needs=["chdman"],
                notes=[
                    f"检测到 MAME 压缩光盘镜像：{chd_p.name}",
                    "需要安装开源工具 chdman（约 2.7 MB）解出原始轨道并验证是否为世嘉土星光盘。",
                ],
            )
        if hdr is None:
            return ProbeReport(engine=self.name, root=root, notes=["未找到世嘉土星光盘镜像 (.chd/.bin/.cue/.iso) 或 IP.BIN"])

        src_tbl, dst_tbl = find_tbl_pair(root)
        entries, _, _ = _collect_saturn_units(root.name, files, src_tbl=src_tbl)
        label = f"Sega Saturn ({hdr.title or root.name} [{hdr.product_no or 'SS'}])"
        container = (
            "saturn-chd"
            if chd_p is not None
            else (
                f"saturn-mode{layout.mode}-{layout.sector_size}"
                if layout is not None and layout.sector_size == CDROM_RAW_SECTOR_SIZE
                else ("saturn-iso-2048" if img_p is not None else "saturn-folder")
            )
        )
        if not entries:
            return ProbeReport(
                engine=label,
                root=root,
                readable=False,
                writable=False,
                container=container,
                notes=[
                    f"世嘉土星游戏：{hdr.title} [{hdr.product_no}]（发行 {hdr.release_date or '未知'}，区域 {hdr.area_codes or '未知'}）。",
                    f"已解析光盘 ISO-9660（共 {len(layout.files) if layout else len(files)} 个文件），"
                    "但未检出明文台词（街机射击/动作类多为位图贴图文字；若为 RPG/AVG 自定义码表，可在镜像旁放入 ferry.tbl 后再扫）。",
                ],
            )

        notes = [
            f"世嘉土星游戏：{hdr.title} [{hdr.product_no}]（区域 {hdr.area_codes or '未知'}），共检出 {len(entries)} 条可翻译文本。",
        ]
        if src_tbl is not None:
            notes.append(f"已启用外挂 Thingy 码表：{src_tbl.name} → {dst_tbl.name if dst_tbl else src_tbl.name}。")
        if chd_p is not None:
            notes.append(
                "回写将对解出的 MODE1/2352 扇区执行定长覆写 + 重算黄皮书 EDC/ECC，并经 chdman 无损重封为 .cn.chd（保留全部 CDDA 音轨）。"
            )
        elif layout is not None and layout.sector_size == CDROM_RAW_SECTOR_SIZE:
            notes.append(
                f"回写将对 MODE{layout.mode}/2352 原始光盘扇区执行定长原位覆写，并自动重算黄皮书 EDC + ECC (P/Q) 校验码与配套 .cn.cue。"
            )
        elif img_p is not None:
            notes.append("回写采用 ISO-9660 LBA 原位定长覆写产出 .cn.iso 副本，不破坏扇区布局。")
        else:
            notes.append("回写将原地更新解包目录并通过 UndoLog 备份可撤销。")
        notes.append("提示：土星原版内建字库多为日文 Shift-JIS / 自定义点阵字库；如中文字形缺失，需要对应字库贴图补丁。")

        return ProbeReport(
            engine=label,
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container=container,
            text_format="ISO-9660 + SJIS/ASCII" + (f" + {src_tbl.name}" if src_tbl else ""),
            unicode_engine=False,
            in_place=img_p is None,
            notes=notes,
        )

    def scan_units(self, root: Path) -> list[Unit]:
        root = Path(root).resolve()
        try:
            _img, _cue, _chd, _blob, _layout, files, _hdr = self._load_disc(root)
        except ValueError:
            return []
        src_tbl, _ = find_tbl_pair(root)
        _, units, _ = _collect_saturn_units(root.name, files, src_tbl=src_tbl)
        return units

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        try:
            _img, _cue, _chd, _blob, _layout, files, _hdr = self._load_disc(root)
        except ValueError:
            return []
        src_tbl, _ = find_tbl_pair(root)
        entries, _, _ = _collect_saturn_units(root.name, files, src_tbl=src_tbl)
        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        report = ApplyReport()
        try:
            img_p, cue_p, chd_p, blob, layout, files, hdr = self._load_disc(root)
        except ValueError as exc:
            report.conflicts.append(str(exc))
            return report
        if hdr is None:
            report.notes.append("未找到世嘉土星镜像或 IP.BIN 目录")
            return report

        src_tbl, dst_tbl = find_tbl_pair(root)
        entries, _, bin_units = _collect_saturn_units(root.name, files, src_tbl=src_tbl)
        if not entries:
            report.notes.append("世嘉土星镜像内无可用文本单元")
            return report

        structured_entries = [e for e in entries if not e.kind.startswith("bin_")]
        modified, applied, missed, conflicts = patch_virtual_files(files, structured_entries, translations)
        for rel, units in bin_units.items():
            raw = files.get(rel)
            if raw is None:
                continue
            hit = False
            for u in units:
                tr = translations.get(u.key) or translations.get(u.src)
                if tr and tr.strip():
                    u.dst = tr
                    hit = True
                else:
                    missed += 1
            if not hit:
                continue
            patched, count, bin_conflicts = patch_blob(raw, units, tbl_encode=dst_tbl)
            conflicts.extend(bin_conflicts)
            if count > 0:
                modified[rel] = patched
                applied += count

        report.applied = applied
        report.missed = missed
        report.conflicts.extend(conflicts)
        if not modified:
            report.notes.append("未命中任何译文，未写出土星镜像")
            return report

        undo = UndoLog.open(base, engine=self.name)
        if img_p is not None and blob is not None and layout is not None:
            new_img = bytearray(blob)
            for rel, new_raw in modified.items():
                ent = layout.files.get(rel)
                if ent is None:
                    continue
                allocated = ((ent.size + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE) * ISO_SECTOR_SIZE
                if len(new_raw) > max(ent.size, allocated):
                    report.conflicts.append(
                        f"{rel}：修改后大小 ({len(new_raw)}) 超出光盘扇区预留容量 ({allocated})"
                    )
                    continue
                write_cdrom_file(
                    new_img,
                    ent.lba,
                    new_raw.ljust(allocated, b"\x00"),
                    layout.sector_size,
                    layout.data_offset,
                    layout.mode,
                )
                if len(new_raw) != ent.size:
                    # 同步更新 ISO-9660 目录扇区中的文件长度，并在 2352 RAW 下重算该目录扇区 EDC/ECC
                    dir_sec = bytearray(
                        read_cdrom_sector(new_img, ent.dir_lba, layout.sector_size, layout.data_offset)
                    )
                    struct.pack_into("<I", dir_sec, ent.dir_offset_in_sector, len(new_raw))
                    struct.pack_into(">I", dir_sec, ent.dir_offset_in_sector + 4, len(new_raw))
                    write_cdrom_file(
                        new_img,
                        ent.dir_lba,
                        bytes(dir_sec),
                        layout.sector_size,
                        layout.data_offset,
                        layout.mode,
                    )

            if chd_p is not None and cue_p is not None:
                chd_dep = deps.REGISTRY.get("chdman")
                chdman_exe = chd_dep.locate() if chd_dep else None
                if chdman_exe is None:
                    report.conflicts.append("缺少 chdman 工具，无法将修改后的镜像重新封回 .chd")
                    return report
                with tempfile.TemporaryDirectory(prefix="ferry_chd_repack_") as tmp_repack:
                    tmp_dir = Path(tmp_repack)
                    tmp_bin = tmp_dir / img_p.name
                    tmp_cue = tmp_dir / cue_p.name
                    tmp_bin.write_bytes(bytes(new_img))
                    old_cue = cue_p.read_text(encoding="utf-8-sig", errors="replace")
                    tmp_cue.write_text(rewrite_cue_for_patched_bin(old_cue, img_p.name, tmp_bin.name), encoding="utf-8")

                    default_chd = chd_p.with_name(f"{chd_p.stem}.cn.chd")
                    target_chd = unique_output(Path(out) if out else default_chd)
                    if target_chd.is_dir():
                        target_chd = unique_output(target_chd / default_chd.name)
                    proc = subprocess.run(
                        [str(chdman_exe), "createcd", "-i", str(tmp_cue), "-o", str(target_chd), "-f"],
                        capture_output=True,
                        text=True,
                        errors="replace",
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    if proc.returncode != 0 or not target_chd.is_file():
                        report.conflicts.append(f"chdman 重打包失败：{proc.stderr.strip() or proc.stdout.strip()}")
                        return report
                undo.did_copy(target_chd)
                report.undo_path = undo.finish()
                report.out_path = target_chd
                report.in_place = False
                report.notes.append(f"已生成世嘉土星汉化 CHD 镜像 [{hdr.product_no}]：{target_chd.name}。")
                return report

            default_out = img_p.with_name(f"{img_p.stem}.cn{img_p.suffix}")
            target_out = unique_output(Path(out) if out else default_out)
            if target_out.is_dir():
                target_out = unique_output(target_out / default_out.name)
            target_out.write_bytes(bytes(new_img))
            undo.did_copy(target_out)

            cue_note = ""
            if cue_p is not None and cue_p.is_file():
                try:
                    old_cue = cue_p.read_text(encoding="utf-8-sig", errors="replace")
                    new_cue_text = rewrite_cue_for_patched_bin(old_cue, img_p.name, target_out.name)
                    new_cue_path = target_out.with_suffix(".cue")
                    new_cue_path.write_text(new_cue_text, encoding="utf-8")
                    undo.did_copy(new_cue_path)
                    cue_note = f"（已同步生成 {new_cue_path.name}）"
                except OSError:
                    pass

            report.undo_path = undo.finish()
            report.out_path = target_out
            report.in_place = False
            report.notes.append(f"已生成世嘉土星汉化光盘镜像 [{hdr.product_no}]：{target_out.name}{cue_note}。")
            return report

        # 解包 IP.BIN 目录原地写回
        for rel, new_raw in modified.items():
            undo.will_modify(rel)
            (root / rel).write_bytes(new_raw)
            undo.did_write(rel)
        report.undo_path = undo.finish()
        report.out_path = root
        report.in_place = True
        report.notes.append(f"已原地更新世嘉土星解包目录 [{hdr.product_no}] 共 {len(modified)} 个文件。")
        return report

    def apply_units(self, root: Path, units: list[Unit], out: Path | None = None) -> ApplyReport:
        mapping = {u.key: u.dst for u in units if u.done and u.translatable}
        return self.apply(root, mapping, out=out)


# ============================================================================
# Nintendo 64 (.z64 / .v64 / .n64) 卡带解析、字节序还原与 CIC CRC1/CRC2 重算
# ============================================================================

N64_MAGIC_Z64 = b"\x80\x37\x12\x40"  # Big-Endian (native)
N64_MAGIC_V64 = b"\x37\x80\x40\x12"  # Byte-Swapped (16-bit)
N64_MAGIC_N64 = b"\x40\x12\x37\x80"  # Little-Endian (32-bit word-swapped)

N64_HEADER_SIZE = 0x40
N64_IPL3_END = 0x1000
N64_CRC_START = 0x1000
N64_CRC_LENGTH = 0x100000  # 1 MiB

# IPL3 (0x40..0x1000) 的标准 IEEE CRC32 -> CIC 型号与初始种子
_N64_IPL3_CRC_MAP: dict[int, int] = {
    0x6170A4A1: 6101,
    0x90BB6CB5: 6102,
    0x0B050EE0: 6103,
    0x98BC2C86: 6105,
    0xACC8580A: 6106,
}
_N64_CIC_SEEDS: dict[int, int] = {
    6101: 0xF8CA4DDC,
    6102: 0xF8CA4DDC,
    6103: 0xA3886759,
    6105: 0xDF26F436,
    6106: 0x1FEA617A,
}
_N64_REGION_NAMES: dict[str, str] = {
    "J": "日版 (NTSC-J)",
    "E": "美版 (NTSC-U)",
    "P": "欧版 (PAL)",
    "D": "德版",
    "F": "法版",
    "I": "意版",
    "S": "西版",
    "U": "澳版",
    "A": "全区",
}


@dataclass
class N64Header:
    byte_order: str  # "z64" | "v64" | "n64"
    title: str
    game_code: str
    region: str
    rom_version: int
    crc1: int
    crc2: int
    cic: int
    cic_known: bool


def detect_n64_format(blob: bytes) -> str | None:
    """识别 N64 ROM 字节序：返回 `'z64'` / `'v64'` / `'n64'`，不匹配返回 None。"""
    if len(blob) < N64_IPL3_END:
        return None
    head = blob[:4]
    if head == N64_MAGIC_Z64:
        return "z64"
    if head == N64_MAGIC_V64:
        return "v64"
    if head == N64_MAGIC_N64:
        return "n64"
    return None


def normalize_n64_rom(blob: bytes) -> tuple[bytes, str]:
    """把 `.z64` / `.v64` / `.n64` 统一规范化为大端 `.z64` 字节流。"""
    fmt = detect_n64_format(blob)
    if fmt is None:
        raise ValueError("不是有效的 Nintendo 64 ROM（缺少 z64/v64/n64 魔数）")
    if fmt == "z64":
        return blob, fmt
    buf = bytearray(blob)
    if fmt == "v64":
        if len(buf) % 2:
            buf.append(0)
        buf[0::2], buf[1::2] = buf[1::2], buf[0::2]
        return bytes(buf), fmt
    # fmt == "n64" (32-bit word byte-reverse)
    rem = len(buf) % 4
    if rem:
        buf.extend(b"\x00" * (4 - rem))
    buf[0::4], buf[1::4], buf[2::4], buf[3::4] = buf[3::4], buf[2::4], buf[1::4], buf[0::4]
    return bytes(buf), fmt


def restore_n64_format(z64_blob: bytes, target_fmt: str) -> bytes:
    """把大端 `.z64` 字节流还原回原始 `.z64` / `.v64` / `.n64` 字节序。"""
    if target_fmt == "z64":
        return z64_blob
    buf = bytearray(z64_blob)
    if target_fmt == "v64":
        if len(buf) % 2:
            buf.append(0)
        buf[0::2], buf[1::2] = buf[1::2], buf[0::2]
        return bytes(buf)
    if target_fmt == "n64":
        rem = len(buf) % 4
        if rem:
            buf.extend(b"\x00" * (4 - rem))
        buf[0::4], buf[1::4], buf[2::4], buf[3::4] = buf[3::4], buf[2::4], buf[1::4], buf[0::4]
        return bytes(buf)
    raise ValueError(f"未知 N64 字节序：{target_fmt}")


def detect_n64_cic(z64_blob: bytes) -> tuple[int, bool]:
    """通过 IPL3 (`0x40..0x1000`) 的 CRC32 识别 N64 CIC 芯片型号，未知时回退 CIC-6102。"""
    if len(z64_blob) < N64_IPL3_END:
        return 6102, False
    ipl_crc = zlib.crc32(z64_blob[N64_HEADER_SIZE:N64_IPL3_END]) & 0xFFFFFFFF
    cic = _N64_IPL3_CRC_MAP.get(ipl_crc)
    if cic is not None:
        return cic, True
    return 6102, False


def compute_n64_crc(z64_blob: bytes, cic: int | None = None) -> tuple[int, int]:
    """按官方 `chksum64` / IPL3 算法计算 N64 大端 ROM 的 `(CRC1, CRC2)`。

    校验范围固定为 `0x001000 .. 0x101000`（1 MiB）；若测试 ROM 短于 1 MiB + 4 KiB，
    按实机读取行为以 `0x00` 补足剩余字节。
    """
    if cic is None:
        cic, _ = detect_n64_cic(z64_blob)
    seed = _N64_CIC_SEEDS.get(cic, _N64_CIC_SEEDS[6102])
    t1 = t2 = t3 = t4 = t5 = t6 = seed

    end = N64_CRC_START + N64_CRC_LENGTH
    if len(z64_blob) < end:
        region = z64_blob[N64_CRC_START:] + b"\x00" * (end - len(z64_blob))
    else:
        region = z64_blob[N64_CRC_START:end]

    words = struct.unpack(f">{N64_CRC_LENGTH // 4}I", region)
    if cic == 6105:
        table_words = struct.unpack_from(">64I", z64_blob, 0x0750) if len(z64_blob) >= 0x0850 else (0,) * 64
        for idx, d in enumerate(words):
            s = (t6 + d) & 0xFFFFFFFF
            if s < t6:
                t4 = (t4 + 1) & 0xFFFFFFFF
            t6 = s
            t3 ^= d
            shift = d & 0x1F
            r = ((d << shift) | (d >> (32 - shift))) & 0xFFFFFFFF if shift else d
            t5 = (t5 + r) & 0xFFFFFFFF
            if t2 > d:
                t2 ^= r
            else:
                t2 ^= t6 ^ d
            t1 = (t1 + (table_words[idx & 0x3F] ^ d)) & 0xFFFFFFFF
    else:
        for d in words:
            s = (t6 + d) & 0xFFFFFFFF
            if s < t6:
                t4 = (t4 + 1) & 0xFFFFFFFF
            t6 = s
            t3 ^= d
            shift = d & 0x1F
            r = ((d << shift) | (d >> (32 - shift))) & 0xFFFFFFFF if shift else d
            t5 = (t5 + r) & 0xFFFFFFFF
            if t2 > d:
                t2 ^= r
            else:
                t2 ^= t6 ^ d
            t1 = (t1 + (t5 ^ d)) & 0xFFFFFFFF

    if cic == 6103:
        crc1 = ((t6 ^ t4) + t3) & 0xFFFFFFFF
        crc2 = ((t5 ^ t2) + t1) & 0xFFFFFFFF
    elif cic == 6106:
        crc1 = (((t6 * t4) & 0xFFFFFFFF) + t3) & 0xFFFFFFFF
        crc2 = (((t5 * t2) & 0xFFFFFFFF) + t1) & 0xFFFFFFFF
    else:
        crc1 = (t6 ^ t4 ^ t3) & 0xFFFFFFFF
        crc2 = (t5 ^ t2 ^ t1) & 0xFFFFFFFF
    return crc1, crc2


def fix_n64_crc(z64_buf: bytearray, cic: int | None = None) -> tuple[int, int]:
    """重算并原位回写 N64 大端 ROM 头部 `0x10..0x18` 的 `CRC1` 与 `CRC2`。"""
    crc1, crc2 = compute_n64_crc(bytes(z64_buf), cic=cic)
    struct.pack_into(">II", z64_buf, 0x10, crc1, crc2)
    return crc1, crc2


def parse_n64_header(blob: bytes) -> tuple[N64Header, bytes]:
    """解析 N64 ROM（支持 `.z64`/`.v64`/`.n64`），返回 `(N64Header, 规范化大端 z64 字节)`。"""
    z64, fmt = normalize_n64_rom(blob)
    crc1, crc2 = struct.unpack_from(">II", z64, 0x10)
    raw_title = z64[0x20:0x34].rstrip(b"\x00 ")
    try:
        title = raw_title.decode("cp932").strip()
    except UnicodeDecodeError:
        title = raw_title.decode("ascii", errors="replace").strip()
    code = z64[0x3B:0x3F].decode("ascii", errors="replace").strip("\x00 ")
    region_ch = code[3:4].upper() if len(code) >= 4 else ""
    region = _N64_REGION_NAMES.get(region_ch, region_ch or "未知")
    rom_ver = z64[0x3F]
    cic, cic_known = detect_n64_cic(z64)
    return (
        N64Header(
            byte_order=fmt,
            title=title,
            game_code=code,
            region=region,
            rom_version=rom_ver,
            crc1=crc1,
            crc2=crc2,
            cic=cic,
            cic_known=cic_known,
        ),
        z64,
    )


def count_n64_compressed_blocks(z64_blob: bytes) -> dict[str, int]:
    """统计 N64 ROM 中常见的任天堂压缩块（Yaz0 / MIO0 / Yay0）数量，用于精准诊断。"""
    body = z64_blob[N64_IPL3_END:]
    counts: dict[str, int] = {}
    for magic in (b"Yaz0", b"MIO0", b"Yay0"):
        n = body.count(magic)
        if n:
            counts[magic.decode("ascii")] = n
    return counts


def _read_n64_rom_bytes(path: Path) -> tuple[bytes, str, bool]:
    """读取 N64 ROM 字节；自动兼容 ZIP 封装（含被改名成 `.n64` 的 ZIP 归档）。

    返回 `(rom_bytes, inner_name, was_zipped)`。
    """
    try:
        with open(path, "rb") as fh:
            head4 = fh.read(4)
    except OSError as exc:
        raise ValueError(str(exc)) from exc

    if head4[:2] == b"PK" and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir() or info.file_size < N64_IPL3_END:
                    continue
                low = info.filename.lower()
                if low.endswith((".z64", ".v64", ".n64", ".rom")):
                    with zf.open(info, "r") as member:
                        head = member.read(N64_IPL3_END)
                    if detect_n64_format(head) is not None:
                        return zf.read(info.filename), Path(info.filename).name, True
        raise ValueError("ZIP 压缩包内未找到有效的 N64 ROM (.z64/.v64/.n64)")

    raw = path.read_bytes()
    if detect_n64_format(raw) is None:
        raise ValueError("不是有效的 Nintendo 64 ROM（缺少 z64/v64/n64 魔数）")
    return raw, path.name, False


class N64Adapter(Adapter):
    """任天堂 Nintendo 64（`.z64` / `.v64` / `.n64` 及 ZIP 封装 ROM）卡带镜像适配器。"""

    name = "n64"
    priority = 14

    def _find_rom(self, root: Path) -> Path | None:
        root = Path(root)
        candidates: list[Path] = []
        if root.is_file() and root.suffix.lower() in (".z64", ".v64", ".n64", ".zip") and ".cn" not in root.stem.lower():
            candidates.append(root)
        elif root.is_dir():
            for ext in ("*.z64", "*.v64", "*.n64", "*.zip"):
                candidates.extend(
                    p for p in sorted(root.glob(ext)) if p.is_file() and ".cn" not in p.stem.lower()
                )
        for p in candidates:
            try:
                with open(p, "rb") as fh:
                    head = fh.read(N64_IPL3_END)
                if detect_n64_format(head) is not None:
                    return p
                if head[:2] == b"PK" and zipfile.is_zipfile(p):
                    with zipfile.ZipFile(p, "r") as zf:
                        for info in zf.infolist():
                            if info.is_dir() or info.file_size < N64_IPL3_END:
                                continue
                            if info.filename.lower().endswith((".z64", ".v64", ".n64", ".rom")):
                                with zf.open(info, "r") as mf:
                                    if detect_n64_format(mf.read(N64_IPL3_END)) is not None:
                                        return p
            except (OSError, zipfile.BadZipFile):
                continue
        return None

    def _scan_rom_units(
        self,
        rom_name: str,
        z64_blob: bytes,
        *,
        src_tbl: ThingyTable | None = None,
        region_code: str = "",
    ) -> list[Unit]:
        """跳过 `0x00..0x1000` 头部与 IPL3，仅扫描 ROM 正文并保留精确绝对偏移。"""
        body = z64_blob[N64_IPL3_END:]
        # 欧美版卡带（E/P/D/F/I/S/U）不含 Shift-JIS 日文，跳过 SJIS 扫以避免 MIPS 机器码误报
        allow_sjis = (region_code.upper() in ("", "J", "A"))
        units = _scan_retro_console_binary(body, rel_path=rom_name, src_tbl=src_tbl, allow_sjis=allow_sjis)
        shifted: list[Unit] = []
        for idx, u in enumerate(units, start=1):
            abs_off = int(u.meta.get("offset", 0)) + N64_IPL3_END
            u.meta["offset"] = abs_off
            u.key = f"{rom_name}@0x{abs_off:x}"
            u.order = idx
            shifted.append(u)
        return shifted

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        rom_path = self._find_rom(root)
        if rom_path is None:
            return ProbeReport(engine=self.name, root=root, notes=["未找到合法的 Nintendo 64 (.z64/.v64/.n64) 卡带镜像"])

        try:
            raw_bytes, inner_name, was_zipped = _read_n64_rom_bytes(rom_path)
            hdr, z64 = parse_n64_header(raw_bytes)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            return ProbeReport(engine=self.name, root=root, notes=[f"解析 N64 ROM 失败：{exc}"])

        src_tbl, dst_tbl = find_tbl_pair(rom_path)
        region_ch = hdr.game_code[3:4] if len(hdr.game_code) >= 4 else ""
        units = self._scan_rom_units(inner_name, z64, src_tbl=src_tbl, region_code=region_ch)
        comp_stats = count_n64_compressed_blocks(z64)
        label = f"Nintendo 64 ({hdr.title or rom_path.stem} [{hdr.game_code or 'N64'}])"
        cic_tag = f"CIC-{hdr.cic}" + ("" if hdr.cic_known else "（回退默认）")
        container = f"n64-zip ({hdr.byte_order})" if was_zipped else f"n64 ({hdr.byte_order})"

        comp_note = ""
        if comp_stats:
            detail = " / ".join(f"{k}×{v}" for k, v in comp_stats.items())
            comp_note = f"检测到任天堂压缩块（{detail}）；压缩块内部的台词需专用解包器，本适配器仅处理未压缩区明文。"

        if not units:
            notes = [
                f"N64 卡带：{hdr.title} [{hdr.game_code}]（{hdr.region}，{hdr.byte_order.upper()}，{cic_tag}）。",
                comp_note or "未在未压缩 ROM 区检出明文台词（若该作使用自定义码表，可在 ROM 旁放入 ferry.tbl 后再扫）。",
            ]
            return ProbeReport(
                engine=label,
                root=root,
                readable=False,
                writable=False,
                container=container,
                notes=notes,
            )

        notes = [
            f"N64 卡带：{hdr.title} [{hdr.game_code}]（{hdr.region}，{hdr.byte_order.upper()}，{cic_tag}），共检出 {len(units)} 条未压缩文本。",
            "回写将在大端规范化空间内执行定长原位覆写（严格避开 0x00..0x1000 引导区），自动重算 CIC CRC1/CRC2 并还原原字节序产出 .cn 副本。",
        ]
        if was_zipped:
            notes.append(f"检测到 ZIP 封装归档（内含 {inner_name}），已自动免解压读取，回写将输出可直接被模拟器加载的解压版 .cn.{hdr.byte_order}。")
        if src_tbl is not None:
            notes.append(f"已启用外挂 Thingy 码表：{src_tbl.name} → {dst_tbl.name if dst_tbl else src_tbl.name}。")
        if comp_note:
            notes.append(comp_note)

        return ProbeReport(
            engine=label,
            root=root,
            readable=True,
            writable=True,
            unit_count=len(units),
            container=container,
            text_format=f"N64 ROM + {cic_tag} CRC" + (f" + {src_tbl.name}" if src_tbl else ""),
            unicode_engine=False,
            in_place=False,
            notes=notes,
        )

    def scan_units(self, root: Path) -> list[Unit]:
        root = Path(root).resolve()
        rom_path = self._find_rom(root)
        if rom_path is None:
            return []
        try:
            raw_bytes, inner_name, _was_zipped = _read_n64_rom_bytes(rom_path)
            hdr, z64 = parse_n64_header(raw_bytes)
        except (OSError, ValueError, zipfile.BadZipFile):
            return []
        src_tbl, _ = find_tbl_pair(rom_path)
        region_ch = hdr.game_code[3:4] if len(hdr.game_code) >= 4 else ""
        return self._scan_rom_units(inner_name, z64, src_tbl=src_tbl, region_code=region_ch)

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        units = self.scan_units(root)
        return [
            Entry(
                modid=root.name,
                key=u.key,
                english=u.src,
                source="game",
                kind=u.kind,
                rel_path=u.file,
                json_pointer=f"ROM:0x{int(u.meta.get('offset', 0)):x}:{u.max_bytes}",
            )
            for u in units
        ]

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        report = ApplyReport()
        rom_path = self._find_rom(root)
        if rom_path is None:
            report.notes.append("未找到 N64 ROM")
            return report

        try:
            raw_bytes, inner_name, was_zipped = _read_n64_rom_bytes(rom_path)
            hdr, z64 = parse_n64_header(raw_bytes)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            report.conflicts.append(str(exc))
            return report

        src_tbl, dst_tbl = find_tbl_pair(rom_path)
        region_ch = hdr.game_code[3:4] if len(hdr.game_code) >= 4 else ""
        units = self._scan_rom_units(inner_name, z64, src_tbl=src_tbl, region_code=region_ch)
        if not units:
            report.notes.append("N64 ROM 内无可用文本单元")
            return report

        hit = False
        for u in units:
            tr = translations.get(u.key) or translations.get(u.src)
            if tr and tr.strip():
                u.dst = tr
                hit = True
            else:
                report.missed += 1

        if not hit:
            report.notes.append("未命中任何译文，未生成 N64 副本")
            return report

        patched_z64, applied, conflicts = patch_blob(z64, units, tbl_encode=dst_tbl)
        report.applied = applied
        report.conflicts.extend(conflicts)
        if applied == 0:
            report.notes.append("无有效字节变更，未生成 N64 副本")
            return report

        z64_buf = bytearray(patched_z64)
        new_crc1, new_crc2 = fix_n64_crc(z64_buf, cic=hdr.cic)
        final_bytes = restore_n64_format(bytes(z64_buf), hdr.byte_order)

        out_ext = f".{hdr.byte_order}" if was_zipped else rom_path.suffix
        default_out = rom_path.with_name(f"{rom_path.stem}.cn{out_ext}")
        target_out = unique_output(Path(out) if out else default_out)
        if target_out.is_dir():
            target_out = unique_output(target_out / default_out.name)
        target_out.write_bytes(final_bytes)

        undo = UndoLog.open(base, engine=self.name)
        undo.did_copy(target_out)
        report.undo_path = undo.finish()
        report.out_path = target_out
        report.in_place = False
        report.notes.append(
            f"已生成 N64 汉化卡带 [{hdr.game_code}]：{target_out.name}"
            f"（已按 CIC-{hdr.cic} 重算校验和 CRC1=0x{new_crc1:08X}, CRC2=0x{new_crc2:08X}）。"
        )
        return report

    def apply_units(self, root: Path, units: list[Unit], out: Path | None = None) -> ApplyReport:
        mapping = {u.key: u.dst for u in units if u.done and u.translatable}
        return self.apply(root, mapping, out=out)
