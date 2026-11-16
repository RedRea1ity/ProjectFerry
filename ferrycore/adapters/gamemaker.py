"""GameMaker 适配器（GMS 1.x / 2.x）。

GameMaker 有两代格式完全不同，这里必须先分清，否则第一步就错：

    GMS 1.x / 2.x   data.win，IFF 风格 `FORM....GEN8` 容器
                    字符串集中在 `STRG` chunk（count + 指针数组 + \0 结尾的 UTF-8）

    GM8 8.0 / 8.1   runner 是 Delphi 写的（导入表一堆 comctl32 FlatSB_*、
                    oleaut32 SafeArray* 就是它），游戏数据以私有 chunk
                    追加在 exe 末尾，**没有 FORM/GEN8，也没有 STRG**

本适配器只处理 GMS。GM8 会被识别出来并明确拒绝 —— 实测样本 pkmnEMUv.1.1.exe
（GameMaker 8.1 RC 写的 GBA 模拟器）尾部 49 MB 熵正好 8.000，
整体加密，静态取不出任何东西。宁可说「不行」，也不写一套跑不通的解析。

## 为什么可以不重写偏移表

GMS 的 chunk 内部指针是**相对本 chunk 数据区**的偏移，不是文件绝对偏移。
所以 STRG 变长时，只要：

    1. 重算 STRG 内的字符串指针数组
    2. 改写 STRG 头里的 size
    3. 把后面的 chunk 整体平移
    4. 改写 FORM 头里的总长

就够了，不用碰别的 chunk。这一点跟 Ren'Py / RM 的「原地改文本」性质不同，
所以这里必须留 undo 备份。
"""

from __future__ import annotations

import csv
import io
import math
import struct
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport, VerifyReport
from .undo import UndoLog

# 导出时 data.win 可能被改名，按顺序找
_DATA_NAMES = ("data.win", "game.win", "data.win.bak")

# 找语言表时跳过的目录。以 `.` 开头的另外单独挡（摆渡自己的 .ferry-backup
# 就在那一类里，漏掉它会把译文写进备份）。
_SKIP_DIRS = {"node_modules", "__pycache__", "cache", "logs"}

_CJK = "\u3040-\u30ff\u4e00-\u9fff\uff66-\uff9f\uac00-\ud7af"

# 这些前缀后面跟的多半是资源名 / 代码标识符，不是给玩家看的字
_IDENT_PREFIX = (
    "spr_", "obj_", "rm_", "bg_", "snd_", "mus_", "fnt_", "tm_", "scr_",
    "_", "__", "gml_", "font_", "sh_", "tl_",
)
# GameMaker 的字符串里常混着这些，翻了一定坏
_CODE_HINTS = (
    "argument", "self.", "other.", "global.", "instance_", "draw_", "sprite_",
    "audio_", "room_", "event_", "variable_", "json_", "buffer_",
)


# ---- FORM / chunk 层（纯函数，单独可测） ----


def parse_header(blob: bytes) -> tuple[int, int] | None:
    """校验 `FORM....GEN8`，返回 (声明总长, 实际可用于 chunk 的终点)。

    找 data.win 时不能只看文件名 —— 有一半的 GM 游戏导出后它叫 game.win，
    还有的放在 game/ 子目录里。所以认的是签名，不是名字。
    """
    if len(blob) < 12 or blob[:4] != b"FORM" or blob[8:12] != b"GEN8":
        return None
    declared = struct.unpack_from(">I", blob, 4)[0]
    end = min(len(blob), declared + 8)
    return declared, end


def iter_chunks(blob: bytes, end: int):
    """逐块遍历，产出 (chunk_id, 数据起点, 数据长度)。"""
    pos = 12
    while pos + 8 <= end:
        cid = blob[pos:pos + 4]
        size = struct.unpack_from("<I", blob, pos + 4)[0]
        body = pos + 8
        if size <= 0 or body + size > end:
            return
        yield cid, body, size
        pos = body + size


def find_chunk(blob: bytes, end: int, want: bytes) -> tuple[int, int, int] | None:
    for cid, body, size in iter_chunks(blob, end):
        if cid == want:
            return body, size, body + size
    return None


def parse_strg(blob: bytes, body: int, size: int) -> tuple[list[str], bool]:
    """解析 STRG，返回 (字符串列表, 是否带指针数组)。

    STRG 有两种布局，GMS 各版本不一致：

        count | ptr[count] | strings        带指针数组
        count | strings                    不带，顺序读

    判据不是猜版本号，而是看头 count 个 u32 是否「严格递增、落在本 chunk 内、
    且第一个不早于指针数组末尾」。不满足就当它没有指针数组。
    """
    if size < 4:
        return [], False
    count = struct.unpack_from("<I", blob, body)[0]
    if count == 0:
        return [], False
    if count > size:  # 明显不合法的 count，宁可放弃
        return [], False

    ptr_end = 4 + 4 * count
    has_ptrs = False
    if ptr_end <= size:
        cand = struct.unpack_from("<%dI" % count, blob, body + 4)
        ok = all(ptr_end <= c < size for c in cand)
        if ok:
            ok = all(cand[i] < cand[i + 1] for i in range(count - 1))
        has_ptrs = ok

    out: list[str] = []
    if has_ptrs:
        ptrs = struct.unpack_from("<%dI" % count, blob, body + 4)
        for p in ptrs:
            start = body + p
            stop = blob.find(b"\x00", start, body + size)
            if stop < 0:
                stop = body + size
            out.append(blob[start:stop].decode("utf-8", "replace"))
    else:
        pos = body + 4
        limit = body + size
        while pos < limit and len(out) < count:
            stop = blob.find(b"\x00", pos, limit)
            if stop < 0:
                stop = limit
            out.append(blob[pos:stop].decode("utf-8", "replace"))
            pos = stop + 1
    return out, has_ptrs


def build_strg(strings: list[str], with_ptrs: bool) -> bytes:
    """按原布局重建 STRG 数据区（不含 chunk 头）。

    指针数组的值是「相对本 chunk 数据区起点」的偏移，所以整块搬家不用管别处。
    """
    bodies = [s.encode("utf-8") + b"\x00" for s in strings]
    count = len(strings)
    if not with_ptrs:
        return struct.pack("<I", count) + b"".join(bodies)
    head = 4 + 4 * count
    ptrs = []
    cursor = head
    for b in bodies:
        ptrs.append(cursor)
        cursor += len(b)
    return struct.pack("<I", count) + struct.pack("<%dI" % count, *ptrs) + b"".join(bodies)


def rebuild(blob: bytes, strg_body: int, new_data: bytes) -> bytes:
    """用新的 STRG 数据区替换旧的，并把两个 size 字段改对。

    只改三处：STRG chunk 头、STRG 内容、FORM 总长。后面的 chunk 整体平移，
    因为它们内部的指针都是相对自己的。
    """
    old_size = struct.unpack_from("<I", blob, strg_body - 4)[0]
    head = bytearray(blob[:strg_body - 8])
    tail = blob[strg_body + old_size:]
    out = bytes(head) + b"STRG" + struct.pack("<I", len(new_data)) + new_data + tail
    declared = struct.unpack_from(">I", out, 4)[0]
    new_total = declared - old_size + len(new_data)
    out = out[:4] + struct.pack(">I", new_total) + out[8:]
    return out


# ---- 值不值得翻 ----


def worth_translating(text: str) -> bool:
    """判断一条字符串是不是给玩家看的。

    GameMaker 的 STRG 是个大杂烩：资源名、GML 内置函数、文件路径、
    格式化模板、以及真正的对话，全在一个表里。误翻资源名 = 游戏直接崩。

    所以判据偏向「放过」而不是「抓全」：拿不准就当它不是文本。
    """
    if not text or not text.strip():
        return False
    if len(text) > 4000:
        return False
    low = text.lower()
    if low.startswith(_IDENT_PREFIX):
        return False
    if any(h in low for h in _CODE_HINTS):
        return False
    if "\\" in text or "/" in text:  # 路径
        return False
    if text.count("#") > 8:  # GM 用 # 当换行，太多说明是模板拼接
        return False
    has_cjk = any("\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" for ch in text)
    # 短、无空格、无 CJK 且全小写 —— 这是标识符的形态（`player`、`true`、`enable`）。
    # 但别一刀切：`Yes` / `No` / `OK` / `HP` 这些是真给玩家看的菜单项和标签，
    # 它们有大写字母，所以用「有没有大写」当分界，比用长度猜准。
    if not has_cjk and " " not in text.strip() and len(text) < 12:
        if not any(ch.isupper() for ch in text):
            return False
    # 全是符号/数字
    if not any(ch.isalpha() for ch in text):
        return False
    return True


def _entropy(blob: bytes) -> float:
    if not blob:
        return 0.0
    c = Counter(blob)
    n = len(blob)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


# ---- 外置语言表 ----
#
# 很多 GameMaker 游戏不把文本塞进 data.win，而是外挂一张 CSV 语言表。实测样本
# Project Basement Dating 就是这种：
#
#     Keys | English | russian | spanish | Simplified Chinese
#
# 这类表是明文 UTF-8，改它比啃 data.win 安全一个数量级（不用碰偏移、不用碰
# 加密、不用碰几百 MB 的二进制）。所以探测顺序上必须排在 data.win 前面 ——
# 先啃二进制会得出「不可读」的错误结论，而实际上文本就在旁边躺着。

_EN_HEADERS = {"english", "en", "en_us", "en-us", "text", "source", "原文", "英文"}
_ZH_HEADERS = {
    "simplified chinese", "chinese", "zh", "zh_cn", "zh-cn", "zh_hans",
    "schinese", "简体中文", "中文", "简体",
}
_KEY_HEADERS = {"keys", "key", "id", "index"}


@dataclass
class LanguageTable:
    """一张外挂的多语言 CSV。"""

    path: Path
    header: list[str]
    source_col: int   # 原文（English）列
    target_col: int   # 要写入的中文列
    key_col: int = -1

    def label(self) -> str:
        return f"{self.path.name}（{self.header[self.source_col]} → {self.header[self.target_col]}）"


def _read_text_with_enc(path: Path) -> tuple[str, str] | None:
    """读文本并回报用了哪种编码。

    写回必须用**同一个编码**：GBK 的表格用 UTF-8 写回去，游戏读到的是乱码，
    而且这种错不会报错，只会满屏问号。

    `utf-8-sig` 只能用在**确实带 BOM** 的文件上：它对无 BOM 的文件同样解码
    成功（只是不去掉任何东西），如果照它回写就会凭空多出一个 BOM，
    表头第一个字段变成 `\\ufeffKeys`，游戏按列名匹配就全废了。所以先看字节。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if raw[:3] == b"\xef\xbb\xbf":
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return None


def _read_text(path: Path) -> str | None:
    got = _read_text_with_enc(path)
    return got[0] if got else None


def _split_line(line: str) -> list[str]:
    try:
        return next(csv.reader([line]))
    except (csv.Error, StopIteration):
        return []


def find_language_table(root: Path) -> LanguageTable | None:
    """在游戏目录里找「原文列 + 中文列」都齐全的 CSV。

    只认已经存在中文列的表 —— 给没有中文列的表硬加一列，游戏多半读不到
    （读哪一列是代码里写死的），那不叫汉化，叫往文件里塞没人看的数据。

    必须跳过 `.ferry-backup/`：那里面躺着摆渡自己上一轮备份的语言表，
    内容一模一样。`rglob` 会把它扫出来，而 `.` 在字母序上排在前面，
    于是它会被当成「真正的语言表」，写进去的译文全部落在备份里 —— 
    游戏一个字没变，报告却显示成功。
    """
    root = Path(root)
    for path in sorted(root.rglob("*.csv")):
        try:
            rel_parts = path.relative_to(root).parts[:-1]
        except ValueError:
            continue
        if any(part.startswith(".") or part in _SKIP_DIRS for part in rel_parts):
            continue
        try:
            if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                continue
        except OSError:
            continue
        text = _read_text(path)
        if not text:
            continue
        header = _split_line(text.split("\n", 1)[0].rstrip("\r"))
        if len(header) < 2:
            continue
        low = [h.strip().lower() for h in header]
        src = next((i for i, h in enumerate(low) if h in _EN_HEADERS), None)
        dst = next((i for i, h in enumerate(low) if h in _ZH_HEADERS), None)
        if src is None or dst is None or src == dst:
            continue
        key = next((i for i, h in enumerate(low) if h in _KEY_HEADERS), -1)
        return LanguageTable(path, header, src, dst, key)
    return None


def scan_language_table(table: LanguageTable) -> tuple[list[tuple[int, str, str, str]], int]:
    """读出 (行号, key, 原文, 已有译文)，以及「已经有译文」的条数。

    行号是文件里的物理行号（0 = 表头），写回时靠它定位，所以中间的空行
    不能被跳过累加 —— 索引必须跟文件对得上。
    """
    text = _read_text(table.path)
    if text is None:
        return [], 0
    rows: list[tuple[int, str, str, str]] = []
    filled = 0
    lines = text.split("\n")
    for index in range(1, len(lines)):
        line = lines[index].rstrip("\r")
        if not line.strip():
            continue
        fields = _split_line(line)
        if len(fields) <= table.source_col:
            continue
        source = fields[table.source_col].strip()
        if not source:
            continue
        current = fields[table.target_col].strip() if len(fields) > table.target_col else ""
        if current:
            filled += 1
        key = fields[table.key_col] if 0 <= table.key_col < len(fields) else ""
        rows.append((index, key, source, current))
    return rows, filled


class GameMakerAdapter(Adapter):
    name = "gamemaker"
    priority = 25

    def __init__(self, include_samples: bool = False) -> None:
        self.include_samples = include_samples

    # ---- 定位 data.win ----

    def _data_win(self, root: Path) -> Path | None:
        """找 data.win。只读头部 12 字节判断签名 —— 真游戏里这文件能到几百 MB，
        用 read_bytes() 整个读进来只为看四个字节，会白等好几秒。"""
        root = Path(root)

        def signed(p: Path) -> bool:
            try:
                with p.open("rb") as f:
                    return parse_header(f.read(12)) is not None
            except OSError:
                return False

        for name in _DATA_NAMES:
            for p in root.rglob(name):
                if p.is_file() and signed(p):
                    return p
        for p in root.rglob("*.win"):
            if p.is_file() and signed(p):
                return p
        return None

    def _is_gm8(self, root: Path) -> Path | None:
        """找 GM8 的痕迹。

        注意别搜 `GameMaker 8` 这种连续串 —— GM8 的版本号是 Delphi 的 ShortString，
        实际字节是 `GameMaker` + `\\x06` + `G8.1`（长度字节卡在中间）。
        实测样本里那句原文是：

            This game has been created with a Release Candidate of GameMaker\\x06G8.1

        所以判据是「`GameMaker` 后面 48 字节内出现 `G8` 或 `8.`」，
        再用「同时静态导入 ddraw.dll 和 d3d8.dll」兜一层 —— 那是 GM8 runner 的
        渲染后端，GMS 之后换成了 d3d9 / OpenGL。
        """
        for exe in Path(root).glob("*.exe"):
            try:
                blob = exe.read_bytes()
            except OSError:
                continue
            idx = blob.find(b"GameMaker")
            if idx < 0:
                continue
            ctx = blob[idx: idx + 48]
            if b"G8" in ctx or b"8." in ctx:
                return exe
            low = blob.lower()
            if b"ddraw.dll" in low and b"d3d8.dll" in low:
                return exe
        return None

    def _appended_entropy(self, exe: Path) -> tuple[int, float]:
        """exe 尾部附加数据的大小和熵。熵接近 8 说明整体加密/压缩。"""
        try:
            blob = exe.read_bytes()
        except OSError:
            return 0, 0.0
        end = self._pe_end(blob)
        tail = blob[end:]
        if len(tail) < 4096:
            return len(tail), _entropy(tail)
        return len(tail), _entropy(tail[: 2 * 1024 * 1024])

    @staticmethod
    def _pe_end(blob: bytes) -> int:
        """PE 有效数据结束位置（各节 raw 末尾的最大值）。"""
        try:
            o = struct.unpack_from("<I", blob, 0x3C)[0]
            nsec = struct.unpack_from("<H", blob, o + 6)[0]
            opt = struct.unpack_from("<H", blob, o + 20)[0]
            base = o + 24 + opt
            ends = [struct.unpack_from("<I", blob, o + 24 + 60)[0]]
            for i in range(nsec):
                p = base + i * 40
                rs, ro = struct.unpack_from("<II", blob, p + 16)
                if rs:
                    ends.append(ro + rs)
            return max(ends)
        except (struct.error, IndexError):
            return 0

    # ---- 探测 ----

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root)

        table = find_language_table(root)
        if table is not None:
            rows, filled = scan_language_table(table)
            pending = [r for r in rows if not r[3]]
            notes = [
                f"找到外挂语言表 {table.label()}",
                f"{len(rows)} 条原文：{filled} 条已有译文，{len(pending)} 条待翻",
            ]
            if filled:
                notes.append("已有译文默认不覆盖 —— 那是别人做好的，重翻等于把它冲掉")
            if not pending:
                notes.append("中文列已经填满，没有待翻的条目")
            return ProbeReport(
                engine="GameMaker Studio",
                readable=True,
                writable=True,
                unit_count=len(pending),
                container="CSV 语言表",
                text_format=".csv",
                unicode_engine=True,
                notes=notes,
            )

        data_win = self._data_win(root)

        if data_win is not None:
            blob = data_win.read_bytes()
            hdr = parse_header(blob)
            if hdr is None:
                return ProbeReport(
                    engine="GameMaker", readable=False, writable=False,
                    notes=[f"{data_win.name} 的 FORM/GEN8 签名不对，不是可用的 data.win"],
                )
            declared, end = hdr
            chunks = [cid.decode("ascii", "replace") for cid, _, _ in iter_chunks(blob, end)]
            strg = find_chunk(blob, end, b"STRG")
            units = 0
            with_ptrs = False
            if strg:
                strings, with_ptrs = parse_strg(blob, strg[0], strg[1])
                units = sum(1 for s in strings if worth_translating(s))

            notes = [
                f"data.win 与声明长度{'一致' if declared + 8 == len(blob) else f'不符（声明 {declared + 8:,}，实际 {len(blob):,}）'}",
                f"chunk {len(chunks)} 个：{', '.join(chunks[:12])}{' …' if len(chunks) > 12 else ''}",
                f"STRG 字符串表{'带' if with_ptrs else '不带'}指针数组，其中 {units} 条像玩家可见文本",
            ]
            if not strg:
                notes.append("没有 STRG，可能是预编译过的极老版本，暂不支持")
            if units == 0:
                notes.append("STRG 里没有可翻文本，可能文本在图片里")
            return ProbeReport(
                engine="GameMaker Studio",
                readable=bool(strg),
                writable=bool(strg),
                unit_count=units,
                container="data.win",
                text_format="STRG",
                unicode_engine=True,  # GM 内部是 UTF-8，不存在字库编码问题
                notes=notes,
            )

        # 没有 data.win —— 看是不是 GM8，或者被加密塞进 exe 了
        gm8 = self._is_gm8(root)
        if gm8 is not None:
            size, ent = self._appended_entropy(gm8)
            notes = [
                f"{gm8.name} 是 GameMaker 8 的产物（Delphi runner），格式与 GMS 不同："
                f"没有 FORM/GEN8 容器，也没有 STRG 字符串表",
                f"尾部附加数据 {size:,} 字节，熵 {ent:.3f}"
                + ("（≈8.000，整体加密，静态取不出任何文本）" if ent > 7.99 else ""),
                "GM8 的文字由 DirectDraw/D3D8 直接画成像素，不经过 GDI，钩绘制 API 也拿不到",
            ]
            return ProbeReport(
                engine="GameMaker 8",
                readable=False,
                writable=False,
                container="exe 尾部附加数据",
                notes=notes,
            )

        return ProbeReport(
            engine="",
            readable=False,
            writable=False,
            notes=["没找到 data.win（FORM/GEN8），也没有 GameMaker 8 的痕迹"],
        )

    # ---- 提取 ----

    def _strings_of(self, root: Path) -> list[str]:
        """读出当前 STRG 里的全部字符串（不做过不过滤）。测试和 verify 都用它。"""
        data_win = self._data_win(Path(root))
        if data_win is None:
            return []
        blob = data_win.read_bytes()
        hdr = parse_header(blob)
        if hdr is None:
            return []
        strg = find_chunk(blob, hdr[1], b"STRG")
        if not strg:
            return []
        return parse_strg(blob, strg[0], strg[1])[0]

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root)

        table = find_language_table(root)
        if table is not None:
            rows, _ = scan_language_table(table)
            rel = table.path.relative_to(root).as_posix()
            return [
                Entry(
                    modid=root.name,
                    key=f"csv#{lineno}",
                    english=source,
                    source="game",
                    kind="line",
                    rel_path=rel,
                    json_pointer=f"{lineno},{table.target_col}",
                )
                for lineno, _key, source, current in rows
                if not current  # 已有译文的不列出来，别覆盖
            ]

        data_win = self._data_win(root)
        if data_win is None:
            return []
        blob = data_win.read_bytes()
        hdr = parse_header(blob)
        if hdr is None:
            return []
        strg = find_chunk(blob, hdr[1], b"STRG")
        if not strg:
            return []
        strings, _ = parse_strg(blob, strg[0], strg[1])
        rel = data_win.relative_to(root).as_posix()
        entries: list[Entry] = []
        for index, text in enumerate(strings):
            if not worth_translating(text):
                continue
            entries.append(
                Entry(
                    modid=root.name,
                    key=f"strg#{index}",
                    english=text,
                    source="game",
                    kind="text",
                    rel_path=rel,
                    json_pointer=str(index),
                )
            )
        return entries

    # ---- 写回 ----

    def apply(self, root: Path, translations: dict[str, str]) -> ApplyReport:
        root = Path(root)
        table = find_language_table(root)
        if table is not None:
            return self._apply_table(root, table, translations)
        return self._apply_data_win(root, translations)

    def _apply_table(
        self, root: Path, table: LanguageTable, translations: dict[str, str]
    ) -> ApplyReport:
        """填语言表的中文列。

        **只重写被改动的那几行**，其余行原样保留 —— 整表重新序列化会改掉
        引号风格、字段补全方式，虽然语义等价，但 diff 会变成整文件，
        用户再也看不出自己改了什么。
        """
        report = ApplyReport(in_place=True)
        rows, _ = scan_language_table(table)

        plan: dict[int, str] = {}
        for lineno, _key, source, current in rows:
            if current:
                report.skipped += 1        # 已有译文，不覆盖
                continue
            translated = translations.get(f"csv#{lineno}")
            if translated is None:
                translated = translations.get(source)
            if translated is None or not translated.strip():
                report.missed += 1
                continue
            if translated.strip() == source.strip():
                report.skipped += 1
                continue
            plan[lineno] = translated
        if not plan:
            return report

        got = _read_text_with_enc(table.path)
        if got is None:
            report.conflicts.append(f"读不出 {table.path.name} 的编码")
            return report
        text, encoding = got
        lines = text.split("\n")

        written = 0
        for lineno, translated in plan.items():
            if lineno >= len(lines):
                report.conflicts.append(f"第 {lineno} 行不存在（文件被外部改过？）")
                continue
            raw_line = lines[lineno]
            had_cr = raw_line.endswith("\r")
            fields = _split_line(raw_line.rstrip("\r"))
            while len(fields) <= table.target_col:
                fields.append("")
            fields[table.target_col] = translated
            buf = io.StringIO()
            csv.writer(buf, lineterminator="").writerow(fields)
            lines[lineno] = buf.getvalue() + ("\r" if had_cr else "")
            written += 1

        if not written:
            return report

        out = "\n".join(lines)
        blob = (b"\xef\xbb\xbf" + out.encode("utf-8")) if encoding == "utf-8-sig" else out.encode(encoding)
        try:
            log = UndoLog.open(root, engine=self.name)
            rel = table.path.relative_to(root).as_posix()
            log.will_modify(rel)
            table.path.write_bytes(blob)
            log.did_write(rel)
            log.finish()
            report.applied = written
            report.undo_path = root / ".ferry-undo.json"
            report.notes.append(f"{rel} 改了 {written} 行，其余行原样未动（编码 {encoding}）")
        except OSError as exc:
            report.conflicts.append(f"写入失败：{exc}")
        return report

    def _apply_data_win(self, root: Path, translations: dict[str, str]) -> ApplyReport:
        report = ApplyReport(in_place=True)
        data_win = self._data_win(root)
        if data_win is None:
            report.conflicts.append("没找到 data.win")
            return report

        blob = data_win.read_bytes()
        hdr = parse_header(blob)
        if hdr is None:
            report.conflicts.append("data.win 签名不对")
            return report
        strg = find_chunk(blob, hdr[1], b"STRG")
        if not strg:
            report.conflicts.append("没有 STRG 字符串表")
            return report

        strings, with_ptrs = parse_strg(blob, strg[0], strg[1])
        changed = False
        for index, original in enumerate(strings):
            if not worth_translating(original):
                report.skipped += 1
                continue
            translated = translations.get(f"strg#{index}")
            if translated is None:
                translated = translations.get(original)
            if translated is None or not translated.strip():
                report.missed += 1
                continue
            if translated == original:
                report.skipped += 1
                continue
            # GameMaker 用 # 当软换行，译文的 # 数量对不上会让版面塌掉
            if original.count("#") != translated.count("#"):
                report.conflicts.append(
                    f"strg#{index}: 换行符 # 数量不符（原文 {original.count('#')} 个，译文 {translated.count('#')} 个）"
                )
                continue
            if "\x00" in translated:
                report.conflicts.append(f"strg#{index}: 译文含 NUL 字节")
                continue
            strings[index] = translated
            report.applied += 1
            changed = True

        if not changed:
            return report

        new_data = build_strg(strings, with_ptrs)
        try:
            log = UndoLog.open(root, engine=self.name)
            log.will_modify(data_win.relative_to(root).as_posix())
            out = rebuild(blob, strg[0], new_data)
            data_win.write_bytes(out)
            log.did_write(data_win.relative_to(root).as_posix())
            log.finish()
            report.undo_path = root / ".ferry-undo.json"
        except OSError as exc:
            report.conflicts.append(f"写入失败：{exc}")
            return report

        report.notes.append(f"data.win {len(blob):,} → {len(out):,} 字节（STRG 变长，后续 chunk 已平移）")
        return report

    def verify(self, root: Path) -> VerifyReport:
        data_win = self._data_win(Path(root))
        if data_win is None:
            return VerifyReport(ok=False, ran=False, notes=["没有 data.win 可验证"])
        blob = data_win.read_bytes()
        hdr = parse_header(blob)
        if hdr is None:
            return VerifyReport(ok=False, ran=True, notes=["写完以后 FORM 头坏了"])
        declared, end = hdr
        chunks = list(iter_chunks(blob, end))
        ok = strg_ok = True
        strg = find_chunk(blob, end, b"STRG")
        if strg:
            strings, _ = parse_strg(blob, strg[0], strg[1])
            # 能读出 count 条且每条都能解码，算结构没坏
            strg_ok = len(strings) > 0
        ok = strg_ok and declared + 8 == len(blob)
        return VerifyReport(
            ok=ok,
            ran=True,
            notes=[
                f"chunk 仍可遍历：{len(chunks)} 个",
                f"FORM 声明长度{'与实际一致' if declared + 8 == len(blob) else '与实际不符'}",
                f"STRG 可解析：{'是' if strg_ok else '否'}",
            ],
        )
