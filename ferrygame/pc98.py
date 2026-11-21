"""PC-98 磁盘镜像（.hdi / .fdi / .nhd）与 JIS 字库借壳器。

PC-98 游戏跟 Windows 游戏有两道本质区别：

1. **游戏装在磁盘镜像里**（`.hdi` 硬盘镜像 / `.fdi` 软盘镜像 / `.nhd` T98 硬盘镜像），
   不是普通文件夹。但剥掉头部（`.hdi`/`.fdi` 是 4096 字节头，`.nhd` 是 512 字节头 + 分区表），
   里面就是标准的 **FAT12 / FAT16 文件系统**。
   既然我们的回写策略是「定长原位覆盖」（一字节都不增减），那连簇链和 FAT 表都不用改 ——
   只需要顺着 FAT 簇链把文件读出来、扫出剧情和菜单、改完按原簇链原位写回扇区即可！

2. **PC-98 硬件不认 GBK，只认 Shift-JIS（JIS X 0208）双字节码位**。
   直接往 PC-98 游戏里写 GBK，首字节 `0xA1-0xDF` 会被 PC-98 当成半角片假名，画面全乱。
   解法是汉化圈经典的**「字库借壳（SJIS 码位重映射）」**：
   - 原本就在 `cp932`（Shift-JIS）里的汉字与标点（占中文常用字约 71%），直接编成原 SJIS 码位；
   - `cp932` 里没有的简体字（如 `的、是、了、们、你、她、这、那、说`），自动分配到
     JIS 二级汉字尾部的生僻字槽位（`0xE440` 起，游戏剧情里永远用不到的死字区），
     产出伪 Shift-JIS 字节流 + 一张 `pc98_charmap.json` 对照表（供模拟器 `FONT.BMP` 打点阵补丁）。
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from ferrycore.adapters.base import Adapter, ApplyReport, ProbeReport
from ferrycore.adapters.container import unique_output
from ferrycore.adapters.undo import UndoLog
from ferrycore.models import Entry

from .rawbin import JP_MENU_TERMS, scan_sjis_strings
from .units import Unit

PC98_IMAGE_SUFFIXES = {".hdi", ".fdi", ".nhd"}

# PC-98 游戏里装剧本/菜单的常见后缀（COM/EXE 本体 + 各类脚本数据）
PC98_SCRIPT_SUFFIXES = {
    ".exe", ".com", ".ovl", ".mes", ".msg", ".dat", ".bin", ".scn",
    ".scr", ".txt", ".tbl", ".adv", ".sys", ".doc",
}

# MS-DOS 系统引导文件，绝不该当成游戏剧情去翻
_DOS_SYSTEM_FILES = {
    "io.sys", "msdos.sys", "command.com", "dblspace.bin", "drvspace.bin",
    "ibmbio.com", "ibmdos.com", "autoexec.bat", "config.sys", "fdisk.exe",
    "format.com", "sys.com", "himem.sys", "emm386.exe", "neccd.sys",
}


def _iter_spare_sjis_codes():
    """生成 Shift-JIS 二级汉字尾部的安全借壳码位（0xE440 .. 0xEAA0）。

    全部满足：
    - 首字节在 `0xE4..0xEA`（标准 SJIS 双字节区，引擎绝不会当成半角假名或控制码）
    - 尾字节在 `0x40..0x7E` 或 `0x80..0xFC`（避开 `0x7F` DEL 与 ASCII 控制区）
    共约 1200 个槽位，足够装下整部游戏所有缺席的简体字。
    """
    for lead in range(0xE4, 0xEB):
        for trail in list(range(0x40, 0x7F)) + list(range(0x80, 0xFD)):
            raw = bytes([lead, trail])
            try:
                ch = raw.decode("cp932")
            except UnicodeDecodeError:
                continue
            if len(ch) == 1:
                yield ch, raw


class PC98FontMapper:
    """简体中文 → Shift-JIS 借壳编码器。"""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        # {简体字: 借用的日文生僻字}
        self.char_to_host: dict[str, str] = dict(mapping or {})
        self._used_hosts: set[str] = set(self.char_to_host.values())
        self._pool = [ch for ch, _ in _iter_spare_sjis_codes() if ch not in self._used_hosts]

    def encode_char(self, ch: str) -> bytes | None:
        # 常见中文标点先映射成 cp932 原生标点，省下宝贵的字库槽位
        ch = _PUNCT_NORMALIZE.get(ch, ch)
        if ch in self.char_to_host:
            return self.char_to_host[ch].encode("cp932")
        try:
            return ch.encode("cp932")
        except UnicodeEncodeError:
            pass
        if not self._pool:
            return None
        host = self._pool.pop(0)
        self.char_to_host[ch] = host
        self._used_hosts.add(host)
        return host.encode("cp932")

    def encode_padded(self, text: str, max_bytes: int, pad: str = "nul") -> bytes | None:
        aligned = align_pc98_words(text)
        out = bytearray()
        for ch in aligned:
            b = self.encode_char(ch)
            if b is None:
                return None
            out.extend(b)
        if len(out) > max_bytes and "万日元" in aligned:
            # 补齐双空格后若刚好超 2 字节（如 0x280 状态栏），把「万日元」缩为等义的「万元」
            return self.encode_padded(aligned.replace("万日元", "万元"), max_bytes, pad=pad)
        if len(out) > max_bytes:
            return None
        diff = max_bytes - len(out)
        if diff:
            if pad == "nul":
                out.extend(b"\x00" * diff)
            else:
                pairs, rem = divmod(diff, 2)
                # 0x8140 是 Shift-JIS 全角空格
                out.extend(b"\x81\x40" * pairs)
                out.extend(b" " * rem)
        return bytes(out)

    def to_json(self) -> dict[str, str]:
        return dict(self.char_to_host)


def sjis_to_jis(s1: int, s2: int) -> tuple[int, int]:
    """Shift-JIS 双字节 (s1, s2) 转 JIS X 0208 区位码字节 (j1, j2)。"""
    s1_adj = s1 - (0x70 if s1 < 0xA0 else 0xB0)
    j1 = (s1_adj << 1) - (1 if s2 < 0x9F else 0)
    if s2 >= 0x9F:
        j2 = s2 - 0x7E
    elif s2 >= 0x80:
        j2 = s2 - 0x20
    else:
        j2 = s2 - 0x1F
    return j1, j2


def render_glyph_16x16(ch: str, font_name: str = "SimSun") -> list[int]:
    """用 Windows GDI 渲染单个汉字的 16×16 1-bit 点阵（返回 16 个 uint16，前景位=1）。

    SimSun（宋体）在 16px 下自带内嵌 EBDT 点阵表，关闭抗锯齿后每一笔都是跟 PC-98
    原装字库完全同风格的 1-bit 像素字。非 Windows 环境退化为外框占位。
    """
    try:
        import ctypes

        gdi = ctypes.windll.gdi32
        dc = gdi.CreateCompatibleDC(0)
        if not dc:
            raise OSError("CreateCompatibleDC failed")
        try:
            bmi = (ctypes.c_byte * 40)()
            ints = ctypes.cast(bmi, ctypes.POINTER(ctypes.c_int))
            shorts = ctypes.cast(bmi, ctypes.POINTER(ctypes.c_short))
            ints[0] = 40
            ints[1] = 16
            ints[2] = -16
            shorts[6] = 1
            shorts[7] = 32
            bits = ctypes.c_void_p()
            bmp = gdi.CreateDIBSection(dc, ctypes.byref(bmi), 0, ctypes.byref(bits), 0, 0)
            old_bmp = gdi.SelectObject(dc, bmp)
            # 134 = GB2312_CHARSET, 3 = NONANTIALIASED_QUALITY
            fnt = gdi.CreateFontW(16, 0, 0, 0, 400, 0, 0, 0, 134, 0, 0, 3, 0, font_name)
            old_fnt = gdi.SelectObject(dc, fnt)
            gdi.SetBkColor(dc, 0x000000)
            gdi.SetTextColor(dc, 0xFFFFFF)
            gdi.SetBkMode(dc, 2)
            gdi.TextOutW(dc, 0, 0, ch, 1)
            arr = (ctypes.c_uint32 * 256).from_address(bits.value)
            rows: list[int] = []
            for r in range(16):
                word = 0
                for c in range(16):
                    if arr[r * 16 + c] & 0xFF:
                        word |= 0x8000 >> c
                rows.append(word)
            gdi.SelectObject(dc, old_fnt)
            gdi.DeleteObject(fnt)
            gdi.SelectObject(dc, old_bmp)
            gdi.DeleteObject(bmp)
            return rows
        finally:
            gdi.DeleteDC(dc)
    except Exception:
        return [0x7FFE if r in (0, 15) else 0x4002 for r in range(16)]


def patch_pc98_font_bmp(bmp_blob: bytes, char_to_host: dict[str, str]) -> bytes:
    """在 Anex86 / Neko Project II 的 2048×2048 1-bit 字库位图（524,350 字节）上打简体字补丁。

    Anex86 `anex86.bmp` 与 NP2 `font.bmp` 坐标公式（已在真实 anex86.bmp 上实测核对）：
    - JIS (j1, j2) 对应格子列 `cx = j1 - 0x20`、行 `cy = j2`
    - 1-bit BMP 为底向顶存储（height=+2048），每行 256 字节（2048 / 8）
    - 像素位 `0` 是白色前景字形，位 `1` 是黑色透明背景
    """
    if len(bmp_blob) < 62 or bmp_blob[:2] != b"BM":
        raise ValueError("不是合法的 BMP 字库文件")
    off_bits = struct.unpack_from("<I", bmp_blob, 10)[0]
    width, height, _planes, bpp = struct.unpack_from("<iiHH", bmp_blob, 18)
    if width != 2048 or abs(height) != 2048 or bpp != 1:
        raise ValueError(f"PC-98 字库必须是 2048x2048 1-bit BMP（当前 {width}x{height} {bpp}bpp）")
    bottom_up = height > 0
    buf = bytearray(bmp_blob)
    for cn_char, host_char in char_to_host.items():
        try:
            raw = host_char.encode("cp932")
        except UnicodeEncodeError:
            continue
        if len(raw) != 2:
            continue
        j1, j2 = sjis_to_jis(raw[0], raw[1])
        cx = j1 - 0x20
        cy = j2
        if not (0 <= cx < 128 and 0 <= cy < 128):
            continue
        rows = render_glyph_16x16(cn_char)
        for dy, word in enumerate(rows):
            # 前景=0，背景=1
            inv = (~word) & 0xFFFF
            py = (2047 - (cy * 16 + dy)) if bottom_up else (cy * 16 + dy)
            pos = off_bits + py * 256 + cx * 2
            if pos + 2 <= len(buf):
                buf[pos] = (inv >> 8) & 0xFF
                buf[pos + 1] = inv & 0xFF
    return bytes(buf)


def load_pc98_font_bmp(root: Path) -> tuple[bytes, str]:
    """优先用游戏目录里现成的 `anex86.bmp`/`font.bmp`；没有就直接用摆渡内置的标准底板。"""
    root = Path(root).resolve()
    base = root.parent if root.is_file() else root
    for c in (base / "anex86.bmp", base / "font.bmp"):
        try:
            if c.is_file() and c.stat().st_size == 524350:
                return c.read_bytes(), c.name
        except OSError:
            continue
    from .pc98_font_assets import load

    return load(), "内置 anex86.bmp 底板"


_PUNCT_NORMALIZE = {
    "，": "、",
    "“": "「",
    "”": "」",
    "‘": "『",
    "’": "』",
    "—": "―",
    "～": "〜",
    "·": "・",
}

_PC98_CTRL_RE = re.compile(r"(\^[A-Za-z]|[A-Z]\$|c[1-9]|\r\n)")
_ASCII_RUN_RE = re.compile(r"[\x20-\x7e]+")
_HALF_TO_FULL = {chr(i): chr(i + 0xFEE0) for i in range(0x21, 0x7F)}


def align_pc98_words(text: str) -> str:
    """保证 PC-98 文本中每一段非控制码的半角 ASCII 字节长度都是偶数（2 字节对齐）。

    实机踩出来的铁律：PC-98 很多 16 位汇编引擎用 `LODSW` 每次读 2 字节（1 个 word）：
    - 控制码（`^m`、`^s`、`c1`、`B$`）固定是 2 字节；
    - 全角汉字/假名固定是 2 字节；
    - 一旦大模型译文里出现奇数个半角字符（比如把双空格 `"  "` 缩成单空格 `" "`、
      把全角 `"Ｑ熱"` 翻成半角 `"Q热"`、把 `" 400"` 翻成 `"400"`），后面整串汉字和控制码
      全部错位 1 字节，在屏幕上直接变成半角假名乱码（`あﾌきﾍD$c1`）。
    """
    parts = _PC98_CTRL_RE.split(text)
    out: list[str] = []
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            out.append(part)
            continue

        def _fix_run(match: re.Match[str]) -> str:
            run = match.group(0)
            if len(run) % 2 == 0:
                return run
            if run.strip(" ") == "":
                # 奇数个空格（如 1 个空格）补成偶数个空格
                return run + " "
            if len(run) == 1 and run in _HALF_TO_FULL:
                # 单个半角字母/数字/符号（如 Q热、7人、&）直接转全角（2 字节）
                return _HALF_TO_FULL[run]
            # 3/5 字节的半角数字或缩写（如 400、7.2、(?)、PSI）：数字前补空格，其余尾补空格
            return (" " + run) if run[0].isdigit() else (run + " ")

        out.append(_ASCII_RUN_RE.sub(_fix_run, part))
    return "".join(out)


@dataclass
class FatFileEntry:
    path: str
    size: int
    first_cluster: int
    spans: list[tuple[int, int]]  # [(镜像内绝对字节偏移, 字节长度), ...]


def _find_fat_boot_sector(blob: bytes) -> int | None:
    """在镜像头部寻找 FAT12 / FAT16 的引导扇区（BPB）。

    兼容：
    - `.hdi` / `.fdi`（Anex86 4096 字节头，或 0 偏移裸分区，或 PC-98 分区表后）
    - `.nhd`（`T98HDDIMAGE.R0` 512 字节头）
    """
    candidates = [0, 0x200, 0x400, 0x1000, 0x2000]
    # 再顺手扫前 256 KB 里所有 512/1024 对齐的位置
    for off in range(0, min(len(blob), 256 * 1024), 512):
        if off not in candidates:
            candidates.append(off)
    for off in candidates:
        if off + 64 > len(blob):
            continue
        jmp = blob[off]
        if jmp not in (0xEB, 0xE9):
            continue
        bps = struct.unpack_from("<H", blob, off + 11)[0]
        spc = blob[off + 13]
        reserved = struct.unpack_from("<H", blob, off + 14)[0]
        fats = blob[off + 16]
        root_entries = struct.unpack_from("<H", blob, off + 17)[0]
        spf = struct.unpack_from("<H", blob, off + 22)[0]
        if bps not in (256, 512, 1024, 2048):
            continue
        if spc not in (1, 2, 4, 8, 16, 32, 64) or reserved == 0 or not (1 <= fats <= 2):
            continue
        if not (16 <= root_entries <= 4096) or not (1 <= spf <= 1024):
            continue
        return off
    return None


def parse_fat_image(blob: bytes) -> list[FatFileEntry]:
    """纯标准库解析 FAT12 / FAT16 镜像里的所有文件及其在镜像中的物理字节区间。"""
    bpb = _find_fat_boot_sector(blob)
    if bpb is None:
        return []
    bps = struct.unpack_from("<H", blob, bpb + 11)[0]
    spc = blob[bpb + 13]
    reserved = struct.unpack_from("<H", blob, bpb + 14)[0]
    fats = blob[bpb + 16]
    root_entries = struct.unpack_from("<H", blob, bpb + 17)[0]
    total16 = struct.unpack_from("<H", blob, bpb + 19)[0]
    spf = struct.unpack_from("<H", blob, bpb + 22)[0]
    total32 = struct.unpack_from("<I", blob, bpb + 32)[0]
    total_sectors = total16 or total32

    fat_offset = bpb + reserved * bps
    root_offset = fat_offset + fats * spf * bps
    root_bytes = root_entries * 32
    root_sectors = (root_bytes + bps - 1) // bps
    data_offset = root_offset + root_sectors * bps
    cluster_bytes = spc * bps

    data_sectors = max(0, total_sectors - (reserved + fats * spf + root_sectors))
    total_clusters = data_sectors // spc if spc else 0
    is_fat12 = total_clusters < 4085

    fat_blob = blob[fat_offset : fat_offset + spf * bps]

    def next_cluster(c: int) -> int:
        if is_fat12:
            idx = c + (c // 2)
            if idx + 2 > len(fat_blob):
                return 0xFFF
            val = struct.unpack_from("<H", fat_blob, idx)[0]
            return (val >> 4) if (c & 1) else (val & 0x0FFF)
        idx = c * 2
        if idx + 2 > len(fat_blob):
            return 0xFFFF
        return struct.unpack_from("<H", fat_blob, idx)[0]

    eof_mark = 0xFF8 if is_fat12 else 0xFFF8

    def cluster_spans(first: int, size: int) -> list[tuple[int, int]]:
        if first < 2 or size <= 0:
            return []
        spans: list[tuple[int, int]] = []
        rem = size
        cur = first
        seen: set[int] = set()
        while rem > 0 and 2 <= cur < eof_mark and cur not in seen:
            seen.add(cur)
            off = data_offset + (cur - 2) * cluster_bytes
            take = min(rem, cluster_bytes)
            if off + take > len(blob):
                break
            if spans and spans[-1][0] + spans[-1][1] == off:
                spans[-1] = (spans[-1][0], spans[-1][1] + take)
            else:
                spans.append((off, take))
            rem -= take
            cur = next_cluster(cur)
        return spans if rem == 0 else []

    files: list[FatFileEntry] = []

    def walk_dir(dir_bytes: bytes, prefix: str = "", depth: int = 0) -> None:
        if depth > 6:
            return
        for pos in range(0, len(dir_bytes) - 31, 32):
            entry = dir_bytes[pos : pos + 32]
            first_byte = entry[0]
            if first_byte == 0x00:
                break
            if first_byte in (0xE5, 0x2E):
                continue
            attr = entry[11]
            if attr & 0x08:  # 卷标或 LFN
                continue
            raw_name = entry[:8].rstrip(b" ")
            raw_ext = entry[8:11].rstrip(b" ")
            try:
                name = raw_name.decode("cp932", errors="ignore")
                ext = raw_ext.decode("cp932", errors="ignore")
            except Exception:
                continue
            if not name:
                continue
            full_name = f"{name}.{ext}" if ext else name
            rel = f"{prefix}/{full_name}" if prefix else full_name
            cluster = struct.unpack_from("<H", entry, 26)[0]
            size = struct.unpack_from("<I", entry, 28)[0]
            if attr & 0x10:  # 子目录
                if cluster >= 2:
                    sub_spans = cluster_spans(cluster, cluster_bytes * 16)
                    sub_blob = b"".join(blob[o : o + l] for o, l in sub_spans)
                    walk_dir(sub_blob, rel, depth + 1)
            else:
                spans = cluster_spans(cluster, size)
                if spans:
                    files.append(FatFileEntry(path=rel, size=size, first_cluster=cluster, spans=spans))

    walk_dir(blob[root_offset : root_offset + root_bytes])
    return files


def read_fat_file(blob: bytes, entry: FatFileEntry) -> bytes:
    return b"".join(blob[off : off + length] for off, length in entry.spans)


def file_offset_to_image_offset(entry: FatFileEntry, file_offset: int, length: int) -> int | None:
    """把文件内偏移换算成磁盘镜像内的绝对字节偏移。跨簇边界非连续时返回 None（跳过不写，确保安全）。"""
    cur = 0
    for img_off, span_len in entry.spans:
        if cur <= file_offset and file_offset + length <= cur + span_len:
            return img_off + (file_offset - cur)
        cur += span_len
    return None


class PC98Adapter(Adapter):
    """PC-98 磁盘镜像适配器（.hdi / .fdi / .nhd）。"""

    name = "pc98"
    priority = 15  # 磁盘镜像后缀极其明确，排在最前

    def __init__(self, include_samples: bool = False) -> None:
        self.include_samples = include_samples

    def _images(self, root: Path) -> list[Path]:
        root = Path(root)
        if root.is_file() and root.suffix.lower() in PC98_IMAGE_SUFFIXES:
            return [root]
        if not root.is_dir():
            return []
        return sorted(
            p for p in root.iterdir()
            if p.is_file()
            and p.suffix.lower() in PC98_IMAGE_SUFFIXES
            and not p.stem.lower().endswith(".cn")
        )

    def scan_units(self, root: Path) -> list[Unit]:
        units: list[Unit] = []
        for img_path in self._images(root):
            try:
                blob = img_path.read_bytes()
            except OSError:
                continue
            entries = parse_fat_image(blob)
            if entries:
                for entry in entries:
                    name_low = Path(entry.path).name.lower()
                    if name_low in _DOS_SYSTEM_FILES:
                        continue
                    suf = Path(entry.path).suffix.lower()
                    if suf and suf not in PC98_SCRIPT_SUFFIXES:
                        continue
                    data = read_fat_file(blob, entry)
                    # PC-98 的 .PI 16 色位图包与 4-bit ADPCM 语音包以 0x1A 开头；
                    # 动里面的字节会破坏图像解压流与音频采样，一律跳过。
                    if suf == ".dat" and data[:1] == b"\x1a":
                        continue
                    vpath = f"{img_path.name}::{entry.path}"
                    found = scan_sjis_strings(data, rel_path=vpath, target_encoding="cp932-remap")
                    for u in found:
                        foff = int(u.meta["offset"])
                        flen = int(u.meta["length"])
                        imgoff = file_offset_to_image_offset(entry, foff, flen)
                        if imgoff is None:
                            continue
                        u.meta["image"] = img_path.name
                        u.meta["image_offset"] = imgoff
                        units.append(u)
            else:
                # 退化：非标准 FAT（自定义软盘轨道镜像），直接在镜像层找连续 SJIS 文本
                found = scan_sjis_strings(blob, rel_path=img_path.name, target_encoding="cp932")
                for u in found:
                    u.meta["image"] = img_path.name
                    u.meta["image_offset"] = int(u.meta["offset"])
                    units.append(u)
        return units

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        images = self._images(root)
        if not images:
            return ProbeReport(
                engine=self.name,
                root=root,
                notes=["未发现 .hdi / .fdi / .nhd 磁盘镜像"],
            )
        units = self.scan_units(root)
        if not units:
            return ProbeReport(
                engine="PC-98 磁盘镜像",
                root=root,
                readable=False,
                writable=False,
                container=images[0].suffix.lower(),
                notes=[f"找到镜像 {images[0].name}，但未扫出明文 Shift-JIS 台词（可能脚本被 LZSS/霍夫曼压缩）"],
            )
        stories = sum(1 for u in units if u.kind == "story")
        menus = sum(1 for u in units if u.kind == "menu")
        return ProbeReport(
            engine="PC-98 (FAT 镜像 + JIS 字库借壳)",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(units),
            container="hdi/fdi",
            text_format="cp932-remap",
            in_place=False,
            notes=[
                f"镜像 {', '.join(p.name for p in images)}：共扫出 {len(units)} 条文本（剧情 {stories} / 菜单 {menus}）",
                "回写产出 <镜像名>.cn.hdi 副本 + pc98_charmap.json 字库借壳表，原镜像不动",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        return [
            Entry(
                modid=root.name,
                key=u.key,
                english=u.src,
                source="game",
                kind=u.kind,
                rel_path=u.file,
                json_pointer=str(u.meta.get("image_offset", 0)),
            )
            for u in self.scan_units(root)
        ]

    def apply_units(self, root: Path, units: list[Unit], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        images = {p.name: p for p in self._images(root)}
        if not images:
            return ApplyReport(notes=["找不到 PC-98 磁盘镜像"])

        mapper = PC98FontMapper()
        by_img: dict[str, list[Unit]] = {}
        for u in units:
            if u.dst and u.translatable:
                img_name = str((u.meta or {}).get("image") or u.file.split("::")[0])
                by_img.setdefault(img_name, []).append(u)

        report = ApplyReport(in_place=False)
        undo = UndoLog.open(base, engine=self.name)
        for img_name, group in by_img.items():
            img_path = images.get(img_name) or (base / img_name)
            if not img_path.is_file():
                report.conflicts.append(f"找不到镜像文件：{img_name}")
                continue
            buf = bytearray(img_path.read_bytes())
            applied_here = 0
            for unit in group:
                meta = unit.meta or {}
                off = int(meta.get("image_offset", meta.get("offset", -1)))
                length = int(meta.get("length") or unit.max_bytes or 0)
                raw_hex = str(meta.get("raw_hex") or "")
                if off < 0 or length <= 0 or off + length > len(buf):
                    report.conflicts.append(f"{unit.key}：镜像偏移越界")
                    continue
                if raw_hex and bytes(buf[off : off + length]).hex() != raw_hex:
                    report.conflicts.append(f"{unit.key}：镜像原字节不匹配，拒绝写入")
                    continue
                pad_mode = str(meta.get("pad") or "nul")
                encoded = mapper.encode_padded(unit.dst, length, pad=pad_mode)
                if encoded is None or len(encoded) != length:
                    report.conflicts.append(f"{unit.key}：借壳编码后超出 {length} 字节上限")
                    continue
                buf[off : off + length] = encoded
                applied_here += 1

            if applied_here > 0:
                default_out = img_path.with_name(f"{img_path.stem}.cn{img_path.suffix}")
                target = unique_output(Path(out) if out else default_out)
                target.write_bytes(bytes(buf))
                undo.did_copy(target)
                report.applied += applied_here
                report.out_path = target

        if report.applied > 0:
            charmap_path = base / "pc98_charmap.json"
            undo.will_add(charmap_path.name)
            charmap_path.write_text(
                json.dumps(mapper.to_json(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            undo.did_write(charmap_path.name)
            if mapper.char_to_host:
                try:
                    raw_bmp, src_label = load_pc98_font_bmp(base)
                    patched_bmp = patch_pc98_font_bmp(raw_bmp, mapper.char_to_host)
                    bmp_out = base / "anex86.cn.bmp"
                    undo.will_add(bmp_out.name)
                    bmp_out.write_bytes(patched_bmp)
                    undo.did_write(bmp_out.name)
                    report.notes.append(f"已基于{src_label}生成打好简体字补丁的字库 → {bmp_out.name}")
                except Exception as exc:
                    report.notes.append(f"生成 PC-98 字库位图失败：{exc}")
            report.undo_path = undo.finish()
            report.notes.append(f"借壳映射了 {len(mapper.char_to_host)} 个缺席简体字 → {charmap_path.name}")
        return report

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        units = self.scan_units(root)
        for u in units:
            if u.key in translations:
                u.dst = translations[u.key]
            elif u.src in translations:
                u.dst = translations[u.src]
        return self.apply_units(root, units, out=out)
