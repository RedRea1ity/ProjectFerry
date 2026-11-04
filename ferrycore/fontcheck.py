"""字库覆盖检查：翻之前先问「这个字库装得下中文吗」。

为什么单独一层、不塞进 `adapters/`：这条检查跟引擎无关（Unity、RPG Maker、
Delphi、甚至 PC-98 模拟器都可能栽在字库上），而且它是**唯一能在花掉第一个
token 之前否掉整单**的判据。放进 adapters/ 会被误读成某个引擎的私事。

判据来自 2026-11-04 的两个样本：一个 Unity 游戏在 60MB 资源里塞的是韩文字库，
一个 Delphi 游戏界面文字根本没进字库。两个都是「文本有、显示不出来」的类型 ——
翻完才发现满屏方块，比认不出引擎更浪费。

三条规矩：

1. **找不到字体 ≠ 没有字库墙**。字体可能内嵌在 `.assets` / `.xp3` / exe 尾部，
   静态扫不到就如实说「未发现独立字体，无法判断」，不给绿灯。
2. **点阵字库不硬解析**。`.fnt` / `.fon` 先报「可能是点阵字库」，别拿 sfnt
   解析器去猜 —— 猜错比不猜更糟，会让人以为已经验过了。
3. **判据是 GB2312 一级字（3755 个）**。它由标准库 `codecs` 直接生成，不用
   内置字表；而且一级字覆盖现代汉语用字的 99% 以上，缺多少一眼就够判断了。

只用标准库：手写 sfnt 表目录 + cmap format 4/12，约两百行。
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

# 能确定是 sfnt 容器（TrueType / OpenType / 集合）的后缀
FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".otc"}
# 可能是点阵字库/字体驱动的后缀。**故意不含 .dat** —— 那个后缀太泛，
# nw.js 的 icudtl.dat、各种配置数据都叫 .dat，报成「点阵字库」纯属误导。
BITMAP_SUFFIXES = {".fnt", ".fon", ".fot"}

# 档位阈值。依据：GB2312 一级字覆盖日常文本 99% 以上，所以
# 缺 1% 大约是「每百字一个方块」——能读但扎眼；缺 10% 就是每十字一个方块。
_OK_RATIO = 0.99
_TIGHT_RATIO = 0.90

_MAX_SCAN_FILES = 400
_MAX_FONT_BYTES = 64 * 1024 * 1024
_SCAN_DEPTH = 4

_SFNT_TAGS = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1", b"ttcf")


def gb2312_chars(level: int = 1) -> str:
    """生成 GB2312 一级（3755）或二级（3008）汉字。

    一级字区位 16~55，二级 56~87；字节就是区位各加 0xA0。
    用标准库 codecs 反查，不内置字表 —— 内置字表迟早会和权威口径对不上。
    """
    if level == 1:
        rows = range(0xB0, 0xD8)
    elif level == 2:
        rows = range(0xD8, 0xF8)
    else:
        raise ValueError("level 只能是 1 或 2")
    out: list[str] = []
    for high in rows:
        for low in range(0xA1, 0xFF):
            try:
                out.append(bytes([high, low]).decode("gb2312"))
            except UnicodeDecodeError:
                continue
    return "".join(out)


@dataclass
class FontCoverage:
    """一个字体文件对中文一级字的覆盖情况。"""

    path: Path
    family: str = ""
    codepoints: int = 0
    total: int = 0
    covered: int = 0
    missing: list[str] = field(default_factory=list)
    level: str = "unknown"
    note: str = ""

    @property
    def ratio(self) -> float:
        return self.covered / self.total if self.total else 0.0

    def summary(self) -> str:
        name = f"{self.path.name}"
        if self.family:
            name += f"（{self.family}）"
        if self.level == "unknown":
            return f"{name}：{self.note or '读不出字形表'}"
        pct = self.ratio * 100
        head = f"{name}：{self.codepoints:,} 码位，中文一级字 {self.covered}/{self.total}（{pct:.1f}%）"
        tail = {"ok": "可显示中文", "tight": "零星缺字，能读但会有方块", "broken": "缺字过多，中文显示不了"}[self.level]
        return f"{head} — {tail}"


@dataclass
class FontScanResult:
    """一次目录扫描的汇总。"""

    found: list[FontCoverage] = field(default_factory=list)
    bitmap: list[Path] = field(default_factory=list)
    skipped: int = 0
    note: str = ""

    @property
    def verdict(self) -> str:
        """ok / tight / broken / unknown。取**最好的那个**字体档位。

        取最好而非最差：游戏通常有主字体和装饰字体两套，只用缺字那套判定
        会把能玩的游戏判死。
        """
        order = {"ok": 3, "tight": 2, "broken": 1}
        levels = [f.level for f in self.found if f.level in order]
        if not levels:
            return "unknown"
        return max(levels, key=lambda x: order[x])

    @property
    def best(self) -> FontCoverage | None:
        order = {"ok": 3, "tight": 2, "broken": 1}
        cands = [f for f in self.found if f.level in order]
        return max(cands, key=lambda f: order[f.level]) if cands else None

    def summary(self) -> str:
        if self.found:
            lines = [f"发现 {len(self.found)} 个字体文件"]
            best = self.best
            if best is not None:
                lines.append("  " + best.summary())
            for f in self.found:
                if f is not best:
                    lines.append("  " + f.summary())
            if self.bitmap:
                lines.append(f"  另有 {len(self.bitmap)} 个疑似点阵字库，未解析")
            return "\n".join(lines)
        if self.bitmap:
            return (
                f"只找到 {len(self.bitmap)} 个疑似点阵字库（{self.bitmap[0].name} 等），"
                "不解析点阵格式，无法判断中文覆盖"
            )
        return self.note or "未发现独立字体文件"


def _sfnt_bases(data: bytes) -> list[int]:
    """TTC 返回每个字体的偏移，单字体返回 [0]。"""
    if data[:4] == b"ttcf":
        count = struct.unpack_from(">I", data, 8)[0]
        if count <= 0 or count > 64:
            return []
        return [struct.unpack_from(">I", data, 12 + 4 * i)[0] for i in range(count)]
    return [0]


def _table_dir(data: bytes, base: int) -> dict[bytes, tuple[int, int]]:
    num = struct.unpack_from(">H", data, base + 4)[0]
    out: dict[bytes, tuple[int, int]] = {}
    for i in range(num):
        off = base + 12 + 16 * i
        if off + 16 > len(data):
            break
        tag = data[off : off + 4]
        toff, tlen = struct.unpack_from(">II", data, off + 8)
        out[tag] = (toff, tlen)
    return out


def _format4(data: bytes, off: int) -> set[int]:
    seg_x2 = struct.unpack_from(">H", data, off + 6)[0]
    seg = seg_x2 // 2
    if seg <= 0 or seg > 4096:
        return set()
    end_at = off + 14
    start_at = end_at + seg_x2 + 2  # 中间有 2 字节 reservedPad
    delta_at = start_at + seg_x2
    range_at = delta_at + seg_x2
    if range_at + seg_x2 > len(data):
        return set()
    out: set[int] = set()
    for i in range(seg):
        end = struct.unpack_from(">H", data, end_at + 2 * i)[0]
        start = struct.unpack_from(">H", data, start_at + 2 * i)[0]
        if start == 0xFFFF or start > end:
            continue
        delta = struct.unpack_from(">h", data, delta_at + 2 * i)[0]
        range_off = struct.unpack_from(">H", data, range_at + 2 * i)[0]
        for cp in range(start, min(end, 0xFFFE) + 1):
            if range_off == 0:
                glyph = (cp + delta) & 0xFFFF
            else:
                addr = range_at + 2 * i + range_off + 2 * (cp - start)
                if addr + 2 > len(data):
                    continue
                glyph = struct.unpack_from(">H", data, addr)[0]
                if glyph:
                    glyph = (glyph + delta) & 0xFFFF
            if glyph:
                out.add(cp)
    return out


def _format12(data: bytes, off: int) -> set[int]:
    groups = struct.unpack_from(">I", data, off + 12)[0]
    if groups > 0x10000:
        return set()
    out: set[int] = set()
    for i in range(groups):
        at = off + 16 + 12 * i
        if at + 12 > len(data):
            break
        start, end, _glyph = struct.unpack_from(">III", data, at)
        if end < start or end - start > 0x20000:
            continue  # 异常大的组不展开，避免吃内存
        out.update(range(start, end + 1))
    return out


def _cmap_points(data: bytes, cmap_off: int) -> set[int]:
    count = struct.unpack_from(">H", data, cmap_off + 2)[0]
    subs: list[int] = []
    for i in range(count):
        rec = cmap_off + 4 + 8 * i
        if rec + 8 > len(data):
            break
        _plat, _enc, sub = struct.unpack_from(">HHI", data, rec)
        subs.append(cmap_off + sub)
    points: set[int] = set()
    for sub in subs:
        if sub + 4 > len(data):
            continue
        fmt = struct.unpack_from(">H", data, sub)[0]
        if fmt == 4:
            points |= _format4(data, sub)
        elif fmt == 12:
            points |= _format12(data, sub)
    return points


def _family_name(data: bytes, name_off: int) -> str:
    try:
        _fmt, count, str_off = struct.unpack_from(">HHH", data, name_off)
    except struct.error:
        return ""
    family = ""
    for i in range(min(count, 256)):
        rec = name_off + 6 + 12 * i
        if rec + 12 > len(data):
            break
        plat, _enc, _lang, name_id, length, offset = struct.unpack_from(">HHHHHH", data, rec)
        if name_id != 1:
            continue
        raw = data[name_off + str_off + offset : name_off + str_off + offset + length]
        try:
            text = raw.decode("utf-16-be") if plat in (0, 3) else raw.decode("latin-1")
        except UnicodeDecodeError:
            continue
        text = text.strip("\x00").strip()
        if text and not family:
            family = text
    return family


def read_font_codepoints(path: Path) -> tuple[set[int], str] | None:
    """读一个字体文件的字符集。读不出就返回 None，不抛异常。"""
    try:
        if path.stat().st_size > _MAX_FONT_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 12 or data[:4] not in _SFNT_TAGS:
        return None
    points: set[int] = set()
    family = ""
    for base in _sfnt_bases(data):
        if base + 12 > len(data):
            continue
        try:
            tables = _table_dir(data, base)
        except struct.error:
            continue
        cmap = tables.get(b"cmap")
        if cmap:
            try:
                points |= _cmap_points(data, cmap[0])
            except struct.error:
                pass
        name = tables.get(b"name")
        if name and not family:
            family = _family_name(data, name[0])
    if not points:
        return None
    return points, family


def check_font(path: Path, level: int = 1) -> FontCoverage:
    """检查单个字体对中文一级字的覆盖。"""
    chars = gb2312_chars(level)
    parsed = read_font_codepoints(path)
    if parsed is None:
        return FontCoverage(path=Path(path), level="unknown", note="读不出字形表（不是 sfnt，或表损坏）")
    points, family = parsed
    missing = [ch for ch in chars if ord(ch) not in points]
    covered = len(chars) - len(missing)
    ratio = covered / len(chars) if chars else 0.0
    if ratio >= _OK_RATIO:
        verdict = "ok"
    elif ratio >= _TIGHT_RATIO:
        verdict = "tight"
    else:
        verdict = "broken"
    return FontCoverage(
        path=Path(path),
        family=family,
        codepoints=len(points),
        total=len(chars),
        covered=covered,
        missing=missing[:300],
        level=verdict,
    )


def scan_fonts(root: Path, max_files: int = _MAX_SCAN_FILES, depth: int = _SCAN_DEPTH) -> FontScanResult:
    """扫描游戏目录里的字体文件。

    只扫 `depth` 层、只认已知后缀，跳过大文件。找不到不等于没问题：
    字体可能内嵌在资源包里，那种情况只能报「无法判断」。
    """
    root = Path(root)
    result = FontScanResult()
    if root.is_file():
        result.found.append(check_font(root))
        return result

    base_depth = len(root.parts)
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        cur_depth = len(Path(dirpath).parts) - base_depth
        if cur_depth >= depth:
            dirnames[:] = []  # 到深度上限就剪枝，别白walk整棵树
        for name in filenames:
            path = Path(dirpath) / name
            suffix = path.suffix.lower()
            if suffix in FONT_SUFFIXES:
                if len(files) >= max_files:
                    result.skipped += 1
                    continue
                files.append(path)
            elif suffix in BITMAP_SUFFIXES:
                result.bitmap.append(path)

    for path in files:
        result.found.append(check_font(path))
    if not files and not result.bitmap:
        result.note = (
            "未发现独立字体文件 —— 字体可能内嵌在资源包或引擎字库里，"
            "这种情况静态扫不到，不能据此判断中文能显示"
        )
    return result
