"""标准 Thingy 码表（`.tbl`）通用编解码器与相对搜索（Relative Search）。

主机（世嘉土星 / N64 / NDS / PSP / PS1）与老 PC 游戏常使用自定义字库码表，
不可能也不应该为每一款游戏硬编码专属解析器。ROM Hacking 社区通行三十年的通用解就是
**标准 `.tbl`（Thingy Table）外挂码表**：

    # 解码表：放在 ROM 或游戏目录旁，命名为 `ferry.tbl` 或 `<ROM名>.tbl`
    # 回写表（可选）：若中文字库码位与原版不同，再放一份 `ferry_cn.tbl` 或 `<ROM名>_cn.tbl`
    8140=　
    82A0=あ
    82A2=い
    /FF=[END]      # '/' 前缀声明字符串结束符
    *FE=\\n         # '*' 前缀声明换行符

此外提供经典 **相对搜索（`relative_search`）**：在游戏画面里看到任意连续英文（如 `START`）
或假名，即可按字符差值 1 秒定位 ROM 中的自定义码表偏移并吐出初始 `.tbl` 草稿。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .units import Unit, classify

_HEX_LINE_RE = re.compile(r"^([/*]?)([0-9A-Fa-f]{2,8})(?:=(.*))?$")
_CN_TBL_STEM_RE = re.compile(r"(?:^|_|-)(?:cn|zh|chs|cht|out|target)$", re.IGNORECASE)


@dataclass
class ThingyTable:
    """解析后的标准 Thingy `.tbl` 码表。"""

    name: str = "custom.tbl"
    decode_map: dict[bytes, str] = field(default_factory=dict)
    encode_map: dict[str, bytes] = field(default_factory=dict)
    end_tokens: set[bytes] = field(default_factory=set)
    # 预计算按字节长度降序排列的键长，用于最长匹配解码/编码
    decode_lengths: tuple[int, ...] = ()
    encode_lengths: tuple[int, ...] = ()
    # 预计算有效首字节集合（加速二进制扫描）
    lead_bytes: frozenset[int] = field(default_factory=frozenset)

    def finalize(self) -> "ThingyTable":
        if not self.end_tokens:
            self.end_tokens.add(b"\x00")
        self.decode_lengths = tuple(sorted({len(k) for k in self.decode_map}, reverse=True))
        self.encode_lengths = tuple(sorted({len(k) for k in self.encode_map}, reverse=True))
        self.lead_bytes = frozenset(k[0] for k in self.decode_map if k)
        return self

    def decode_bytes(self, raw: bytes) -> str | None:
        """把一整段原始字节按最长匹配解码为字符串；遇到未知字节返回 None。"""
        out: list[str] = []
        pos = 0
        n = len(raw)
        dmap = self.decode_map
        dlens = self.decode_lengths
        while pos < n:
            matched = False
            for ln in dlens:
                if pos + ln <= n:
                    tok = dmap.get(raw[pos : pos + ln])
                    if tok is not None:
                        out.append(tok)
                        pos += ln
                        matched = True
                        break
            if not matched:
                return None
        return "".join(out)

    def encode_text(self, text: str) -> bytes | None:
        """把文本按最长匹配编码为自定义码表字节；存在码表缺失字符时返回 None。"""
        out = bytearray()
        pos = 0
        n = len(text)
        emap = self.encode_map
        elens = self.encode_lengths
        while pos < n:
            matched = False
            for ln in elens:
                if pos + ln <= n:
                    code = emap.get(text[pos : pos + ln])
                    if code is not None:
                        out.extend(code)
                        pos += ln
                        matched = True
                        break
            if not matched:
                return None
        return bytes(out)

    @property
    def pad_unit(self) -> bytes | None:
        """优先选择码表里的空格/结束符作为定长补齐字节。"""
        for candidate in ("　", " "):
            if candidate in self.encode_map:
                return self.encode_map[candidate]
        for tok in sorted(self.end_tokens, key=len):
            if tok:
                return tok
        return b"\x00"


def parse_tbl(text: str, name: str = "custom.tbl") -> ThingyTable:
    """解析标准 Thingy `.tbl` 文本。"""
    tbl = ThingyTable(name=name)
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//")):
            continue
        m = _HEX_LINE_RE.match(line)
        if not m:
            continue
        prefix, hex_str, val = m.group(1), m.group(2), m.group(3)
        if len(hex_str) % 2 != 0:
            continue
        try:
            code = bytes.fromhex(hex_str)
        except ValueError:
            continue

        if prefix == "/":
            tbl.end_tokens.add(code)
            continue

        if prefix == "*":
            mapped = "\n" if (val is None or val == "" or val == r"\n") else val
            tbl.decode_map[code] = mapped
            tbl.encode_map.setdefault(mapped, code)
            continue

        if val is None:
            continue
        # 支持 \n / \t 写法，同时原样保留空格（如 "20= " 或 "8140=　"）
        mapped = val.replace(r"\n", "\n").replace(r"\t", "\t")
        if not mapped:
            continue
        tbl.decode_map[code] = mapped
        tbl.encode_map.setdefault(mapped, code)

    return tbl.finalize()


def is_thingy_tbl_file(path: Path) -> bool:
    """判断 `.tbl` 文件是否是纯文本 Thingy 码表（而非游戏的二进制 `.tbl` 数据表）。"""
    path = Path(path)
    if not path.is_file() or path.suffix.lower() != ".tbl":
        return False
    try:
        size = path.stat().st_size
        if size < 16 or size > 4 * 1024 * 1024:
            return False
        raw = path.read_bytes()
    except OSError:
        return False
    if b"\x00" in raw[:4096]:
        return False
    text = None
    for enc in ("utf-8-sig", "cp932", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return False
    tbl = parse_tbl(text, name=path.name)
    return len(tbl.decode_map) >= 6


def load_tbl(path: Path) -> ThingyTable:
    """从磁盘加载 Thingy `.tbl` 码表（自动兼容 UTF-8 / Shift-JIS / GBK）。"""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp932", "gbk"):
        try:
            text = raw.decode(enc)
            return parse_tbl(text, name=Path(path).name)
        except UnicodeDecodeError:
            continue
    return parse_tbl(raw.decode("utf-8", errors="replace"), name=Path(path).name)


def find_tbl_pair(root: Path) -> tuple[ThingyTable | None, ThingyTable | None]:
    """在游戏目录或 ROM 同级目录自动查找 `(解码表 src_tbl, 编码回写表 dst_tbl)`。

    搜索优先级：
    1. `<ROM文件名>.tbl` + `<ROM文件名>_cn.tbl`（若 `root` 是单个镜像/ROM）
    2. `ferry.tbl` + `ferry_cn.tbl` / `ferry_zh.tbl`
    3. 目录内唯一的非中文 Thingy `.tbl` + 对应的 `*_cn.tbl` / `*_zh.tbl`
    若未提供单独的中文回写表，则 `dst_tbl` 默认复用 `src_tbl`。
    """
    root = Path(root)
    base = root.parent if root.is_file() else root
    if not base.is_dir():
        return None, None

    src_candidates: list[Path] = []
    dst_candidates: list[Path] = []

    if root.is_file():
        stem = root.stem
        src_candidates.append(base / f"{stem}.tbl")
        dst_candidates.extend([base / f"{stem}_cn.tbl", base / f"{stem}_zh.tbl"])

    src_candidates.append(base / "ferry.tbl")
    dst_candidates.extend([base / "ferry_cn.tbl", base / "ferry_zh.tbl"])

    try:
        for p in sorted(base.glob("*.tbl")):
            if not p.is_file():
                continue
            if _CN_TBL_STEM_RE.search(p.stem):
                if p not in dst_candidates:
                    dst_candidates.append(p)
            else:
                if p not in src_candidates:
                    src_candidates.append(p)
    except OSError:
        pass

    src_tbl: ThingyTable | None = None
    for cand in src_candidates:
        if is_thingy_tbl_file(cand):
            src_tbl = load_tbl(cand)
            break

    if src_tbl is None:
        return None, None

    dst_tbl: ThingyTable | None = None
    for cand in dst_candidates:
        if is_thingy_tbl_file(cand):
            dst_tbl = load_tbl(cand)
            break

    return src_tbl, (dst_tbl or src_tbl)


def encode_tbl_padded(text: str, max_bytes: int, table: ThingyTable) -> bytes | None:
    """用自定义 `.tbl` 码表把译文编码并严格补齐到 `max_bytes` 字节；超长或缺失字符返回 None。"""
    raw = table.encode_text(text)
    if raw is None or len(raw) > max_bytes:
        return None
    diff = max_bytes - len(raw)
    if diff == 0:
        return raw

    # 优先使用 1 字节结束符，其次使用码表空格
    end_1b = next((t for t in sorted(table.end_tokens) if len(t) == 1), None)
    if end_1b is not None:
        return raw + (end_1b * diff)

    pad = table.pad_unit or b"\x00"
    if len(pad) == 1:
        return raw + (pad * diff)
    if len(pad) > 1:
        reps, rem = divmod(diff, len(pad))
        if rem == 0:
            return raw + (pad * reps)
        # 剩余 1 字节尝试半角空格或 0x00
        sp1 = table.encode_map.get(" ")
        if sp1 is not None and len(sp1) == 1:
            return raw + (pad * reps) + sp1
        return raw + (pad * reps) + (b"\x00" * rem)
    return raw + (b"\x00" * diff)


def scan_tbl_strings(
    blob: bytes,
    table: ThingyTable,
    rel_path: str = "",
    *,
    min_chars: int = 3,
    require_terminator: bool = True,
) -> list[Unit]:
    """按 Thingy `.tbl` 自定义码表在二进制流中扫描成句文本，记录精确偏移与字节预算。"""
    if not table.decode_map or not blob:
        return []

    units: list[Unit] = []
    n = len(blob)
    dmap = table.decode_map
    dlens = table.decode_lengths
    lead = table.lead_bytes
    end_tokens = table.end_tokens
    end_lens = tuple(sorted({len(t) for t in end_tokens if t}, reverse=True))
    order = 0
    pos = 0

    while pos < n:
        if blob[pos] not in lead:
            pos += 1
            continue

        start = pos
        cur = pos
        chars: list[str] = []
        terminated = False

        while cur < n:
            # 先检查结束符（避免结束符恰好也在 decode_map 里导致吞掉串尾）
            hit_end = 0
            for eln in end_lens:
                if cur + eln <= n and blob[cur : cur + eln] in end_tokens:
                    hit_end = eln
                    break
            if hit_end:
                terminated = True
                break

            matched = False
            for ln in dlens:
                if cur + ln <= n:
                    tok = dmap.get(blob[cur : cur + ln])
                    if tok is not None:
                        chars.append(tok)
                        cur += ln
                        matched = True
                        break
            if not matched:
                break
            if cur - start > 2048:
                break

        raw_len = cur - start
        if raw_len > 0 and (terminated or not require_terminator):
            text = "".join(chars)
            visible = "".join(ch for ch in text if not ch.isspace())
            # 过滤纯空白、过短或单字符重复噪声（如 "AAAA"）
            if len(visible) >= min_chars and len(set(visible)) >= 2:
                raw = blob[start:cur]
                order += 1
                key = f"{rel_path}@0x{start:x}" if rel_path else f"0x{start:x}"
                units.append(
                    Unit(
                        key=key,
                        src=text,
                        kind=classify(text),
                        file=rel_path,
                        order=order,
                        max_bytes=raw_len,
                        encoding=f"tbl:{table.name}",
                        meta={
                            "offset": start,
                            "length": raw_len,
                            "src_encoding": f"tbl:{table.name}",
                            "raw_hex": raw.hex(),
                            "pad": "tbl",
                            "tbl_mode": True,
                        },
                    )
                )
            pos = cur + (hit_end if terminated else 1)
        else:
            pos = max(start + 1, cur + 1)

    return units


@dataclass
class RelativeHit:
    """相对搜索命中结果：定位未知码表的字母/假名起始码位。"""

    offset: int
    stride: int  # 1（单字节）或 2（16 位大端）
    base_code: int  # `pattern[0]` 对应的原始码值
    anchor_char: str  # `pattern[0]`

    def to_tbl_snippet(self) -> str:
        """根据命中的锚点生成一段可直接粘贴进 `ferry.tbl` 的字母表/假名表草稿。"""
        ch0 = self.anchor_char
        lines = [f"# 相对搜索命中于 0x{self.offset:X}（步长 {self.stride} 字节，锚点 '{ch0}' = 0x{self.base_code:0{self.stride * 2}X}）"]
        if "A" <= ch0 <= "Z":
            start_ord, end_ord = ord("A"), ord("Z")
        elif "a" <= ch0 <= "z":
            start_ord, end_ord = ord("a"), ord("z")
        elif "\u3041" <= ch0 <= "\u3093":
            start_ord, end_ord = ord("あ"), ord("ん")
        elif "\u30a1" <= ch0 <= "\u30f3":
            start_ord, end_ord = ord("ア"), ord("ン")
        else:
            start_ord, end_ord = ord(ch0), ord(ch0) + 15

        anchor_ord = ord(ch0)
        max_code = (1 << (8 * self.stride)) - 1
        for cp in range(start_ord, end_ord + 1):
            code = self.base_code + (cp - anchor_ord)
            if 0 <= code <= max_code:
                lines.append(f"{code:0{self.stride * 2}X}={chr(cp)}")
        return "\n".join(lines)


def relative_search(blob: bytes, pattern: str, *, max_hits: int = 20) -> list[RelativeHit]:
    """经典 ROM Hacking 相对搜索：按相邻字符的码点差值定位未知自定义码表。

    例如输入 `"START"`（长度 >= 3），只要 ROM 中 A..Z 连续排布，无论 `'A'` 被映射到
    `0x01`、`0x80` 还是 `0x8260`，都能在 1 秒内反推出精确偏移与起始码位。
    """
    if len(pattern) < 3 or not blob:
        return []
    deltas = [ord(pattern[i + 1]) - ord(pattern[i]) for i in range(len(pattern) - 1)]
    plen = len(pattern)
    hits: list[RelativeHit] = []
    n = len(blob)

    # 1) 单字节步长搜索
    d0 = deltas[0]
    for i in range(0, n - plen + 1):
        b0 = blob[i]
        if blob[i + 1] - b0 != d0:
            continue
        ok = True
        for k in range(1, plen - 1):
            if blob[i + k + 1] - blob[i + k] != deltas[k]:
                ok = False
                break
        if ok:
            hits.append(RelativeHit(offset=i, stride=1, base_code=b0, anchor_char=pattern[0]))
            if len(hits) >= max_hits:
                return hits

    # 2) 16 位大端双字节步长搜索（土星 / N64 常见 16-bit 码表）
    if n >= plen * 2:
        for i in range(0, n - plen * 2 + 1, 2):
            w0 = (blob[i] << 8) | blob[i + 1]
            w1 = (blob[i + 2] << 8) | blob[i + 3]
            if w1 - w0 != d0:
                continue
            ok = True
            for k in range(1, plen - 1):
                wk = (blob[i + 2 * k] << 8) | blob[i + 2 * k + 1]
                wk1 = (blob[i + 2 * (k + 1)] << 8) | blob[i + 2 * (k + 1) + 1]
                if wk1 - wk != deltas[k]:
                    ok = False
                    break
            if ok:
                hits.append(RelativeHit(offset=i, stride=2, base_code=w0, anchor_char=pattern[0]))
                if len(hits) >= max_hits:
                    return hits

    return hits
