"""通用二进制榨汁机 + 定长原位回写器（rawbin）。

专治两类最麻烦的东西：
1. **单 EXE 游戏**（Delphi / VC++ / VB / 自研 C++，台词和菜单直接编在 `.rdata` / `.data` 里）
2. **稀奇古怪草逼蛋引擎的数据包**（`.dat` / `.bin` / `.mes` / `.scn` / `.ovl` / `.pac`，没有公开文档，
   里面是「控制字节 + Shift-JIS 字符串 + 指针表」混着排）

为什么敢动未知格式？因为守住了一条铁律：

    **绝不改变任何一个字节的偏移。**

所有替换都在原字符串的字节区间 `[offset, offset + max_bytes)` 内完成：
- 译文编码后的字节数 **必须 ≤ `max_bytes`**（中文比日文短 25%~40%，天然占便宜）
- 短出来的尾巴用全角空格（GBK `0xA1 0xA1` / SJIS `0x81 0x40`）、半角空格 `0x20` 或 `0x00` 补齐，
  **补完总字节数跟原来一模一样**
- 回写前先核对 `[offset, offset + max_bytes)` 里的原始字节是否与扫描时记录的 `raw_hex` 完全一致；
  只要差一个字节，当场拒绝写入 —— 宁可漏翻一句，绝不写坏偏移表。
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

from ferrycore.adapters.base import Adapter, ApplyReport, ProbeReport
from ferrycore.adapters.container import unique_output
from ferrycore.adapters.undo import UndoLog
from ferrycore.models import Entry

from . import controls
from .units import Unit, classify

# 值得扫的二进制/脚本后缀。图音视频一律不碰，省得在 2 GB 贴图里捞出随机假名。
RAW_SUFFIXES = {
    ".exe", ".dll", ".dat", ".bin", ".mes", ".msg", ".scn", ".scr",
    ".pac", ".pak", ".arc", ".ax", ".ovl", ".tbl", ".stb", ".bsd",
    ".ldb", ".lmt", ".lmu", ".mps", ".wolf", ".lsb", ".nsa", ".dpm",
}

_MEDIA_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".ico", ".cur",
    ".ogg", ".wav", ".mp3", ".flac", ".mid", ".midi", ".wma",
    ".mp4", ".avi", ".webm", ".mpg", ".mpeg", ".wmv",
    ".ttf", ".otf", ".ttc", ".fon", ".fnt",
    ".zip", ".7z", ".rar",
}

_SKIP_DIRS = {
    ".git", "__pycache__", ".ferry-backup", "savedata", "save", "saves",
    "log", "logs", "cache", "temp", "node_modules",
}

# 日系游戏高频操作菜单与状态词：在二进制常量池里短词不敢乱捞，但命中这张表的 100% 是给玩家看的核心词汇。
JP_MENU_TERMS = {
    "はじめから", "つづきから", "最初から", "続きから", "ニューゲーム", "コンティニュー",
    "セーブ", "ロード", "クイックセーブ", "クイックロード", "オート", "スキップ",
    "コンフィグ", "オプション", "環境設定", "設定", "操作説明", "ヘルプ",
    "タイトルへ", "タイトルに戻る", "ゲーム終了", "終了", "戻る", "閉じる",
    "はい", "いいえ", "決定", "キャンセル", "確認",
    "ステータス", "アイテム", "スキル", "装備", "編成", "クエスト", "ショップ",
    "音量", "BGM", "SE", "ボイス", "メッセージ速度", "ウィンドウ", "フルスクリーン",
    "健康", "疲労", "仮死", "死亡", "病気", "Ｑ熱", "中毒", "修行", "布教活動", "投資", "休憩",
}

_KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_JP_PUNCT_RE = re.compile(r"[。、！？「」『』…‥・ー～（）【】]")
_CTRL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FILE_LIKE_RE = re.compile(
    r"^[A-Za-z0-9_./\\:-]+\.(?:bmp|png|jpg|wav|ogg|mp3|mid|dat|bin|mes|exe|dll|txt|csv|ini|fon|ttf)$",
    re.IGNORECASE,
)
_CODE_NOISE_RE = re.compile(
    r"(?:^\s*(?:SELECT|INSERT|UPDATE|DELETE|CREATE|DROP)\b"
    r"|https?://|HKEY_[A-Z_]+|Software\\|Microsoft\\|Windows\\"
    r"|__cdecl|__stdcall|GetProcAddress|LoadLibrary|MessageBox|CreateWindow"
    r"|\.[A-Za-z]{2,4}$)",
    re.IGNORECASE,
)
# 去控制符后，中日文字符与半角英文字母/乱码符号直接紧挨（如「夢Om屬」「礎継かd」「靤;鐺*.」），是位图/音频/机器码的典型假阳性
_ADJACENT_ASCII_GARBAGE_RE = re.compile(
    r"(?:[\u3040-\u30ff\u4e00-\u9fff][A-Za-z;*&$@#%<>\"'\\`~|+={}\[\]_]"
    r"|[A-Za-z;*&$@#%<>\"'\\`~|+={}\[\]_][\u3040-\u30ff\u4e00-\u9fff])"
)
_JP_FULLWIDTH_RUN_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\u3000-\u303f\uff01-\uff5e\u2010-\u203f─]{3,}")
# C 级高速预筛：只找含至少一个 Shift-JIS 双字节的可打印片段
_SJIS_SPAN_RE = re.compile(
    rb"(?:[\x20-\x7e\t\r\n]*[\x81-\x9f\xe0-\xef][\x40-\x7e\x80-\xfc])+[\x20-\x7e\t\r\n]*"
)


def _is_valid_sjis_text(text: str, raw: bytes = b"") -> bool:
    """过滤二进制里碰巧落在 Shift-JIS 码区的随机字节垃圾（位图、ADPCM 音频、x86 机器码）。"""
    if not text or len(text) < 2:
        return False
    if _CTRL_CHAR_RE.search(text):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if stripped in JP_MENU_TERMS:
        return True
    if _FILE_LIKE_RE.match(stripped) or _CODE_NOISE_RE.search(stripped):
        return False

    # 先剥掉已知控制码（^m ^s c1 B$ \C[1] 等），看剩下的纯文本是不是人话
    visible = controls.strip_controls(stripped).strip()
    if not visible:
        return False
    had_controls = visible != stripped

    half_kana = sum(1 for ch in visible if "\uff61" <= ch <= "\uff9f")
    full_kana = len(_KANA_RE.findall(visible))
    cjk = len(_CJK_RE.findall(visible))
    punct = len(_JP_PUNCT_RE.findall(visible))
    if half_kana > 0 and full_kana == 0 and punct == 0:
        return False
    if full_kana + cjk == 0:
        return False
    # 没有任何假名、没有任何日文标点、也没有游戏控制码的纯汉字串（如「甥沿甥埆」「鈷孤釈実」），
    # 在二进制里 99.9% 是位图或机器码巧合；真菜单词由 JP_MENU_TERMS 放行。
    if full_kana == 0 and punct == 0 and not had_controls:
        return False

    # 拦截「日文字符紧挨半角英文字母/乱码符号」的位图/机器码噪音（如 夢Om屬、礎継かd、けU!）
    if _ADJACENT_ASCII_GARBAGE_RE.search(visible):
        return False
    if any(ch in visible for ch in ";*<>`\\|@#%&") and punct == 0:
        return False

    # 检查 JIS 二级生僻汉字（首字节 >= 0x99）占比：x86 机器码和位图里全是生僻字
    try:
        vis_raw = visible.encode("cp932")
    except UnicodeEncodeError:
        vis_raw = raw
    if vis_raw:
        lvl1 = 0
        lvl2 = 0
        idx = 0
        while idx < len(vis_raw):
            b = vis_raw[idx]
            if (0x81 <= b <= 0x9F) or (0xE0 <= b <= 0xEF):
                if 0x88 <= b <= 0x98:
                    lvl1 += 1
                elif b >= 0x99:
                    lvl2 += 1
                idx += 2
            else:
                idx += 1
        if lvl2 > 0 and (lvl1 == 0 or lvl2 / (lvl1 + lvl2) > 0.25) and punct == 0:
            return False

    hira = sum(1 for ch in visible if "\u3040" <= ch <= "\u309f")
    if not _JP_FULLWIDTH_RUN_RE.search(visible):
        # 短于 3 个连续全角字符时，除非带控制码（如「数年後^m」「年B$c1月C$c1日」）或纯全角假名叹词（如「か～」）
        if not (had_controls and lvl1 >= 1) and not (len(visible) == 2 and hira >= 1 and visible.isprintable()):
            return False
        # 2 字短串绝不允许夹杂任何半角 ASCII（拦截「おB3」「けUr」「がU@」）
        if len(visible) <= 3 and not had_controls and any(ord(c) < 0x80 for c in visible):
            return False

    good = sum(
        1
        for ch in visible
        if ("\u3040" <= ch <= "\u30ff")
        or ("\u4e00" <= ch <= "\u9fff")
        or ("\u3000" <= ch <= "\u303f")
        or ("\u2010" <= ch <= "\u203f")
        or ("\uff01" <= ch <= "\uff5e")
        or (" " <= ch <= "~")
        or ch in "\r\n\t…─―・"
    )
    if good / len(visible) < 0.92:
        return False
    return True


def scan_sjis_strings(blob: bytes, rel_path: str = "", target_encoding: str = "gbk") -> list[Unit]:
    """在原始字节流里扫描以 `0x00` 结尾的 Shift-JIS 剧情与菜单字符串。

    为什么盯紧 `0x00`（C 字符串结尾）或固定分隔符：
    因为无论是 VC++ / Delphi 单 EXE 的 `.rdata` 段，还是 `.mes` / `.dat` 脚本包，
    字符串在二进制里都有明确的终止字节，而且从起点到终止符之前的字节数就是它的 `max_bytes`！
    """
    units: list[Unit] = []
    n = len(blob)
    # 16 位 DOS .COM 程序若以 `E9 <disp16>` 开头（跳过数据区直接进代码段），
    # 跳转目标之后全是 x86 机器码，绝不能当文本扫、更不能改。
    if rel_path.lower().endswith(".com") and n >= 1024 and blob[0] == 0xE9:
        jmp_target = 3 + struct.unpack_from("<H", blob, 1)[0]
        if 0x0400 <= jmp_target < n:
            n = jmp_target
    i = 0
    order = 0
    while i < n:
        b = blob[i]
        # Shift-JIS 双字节首字节：0x81-0x9F 或 0xE0-0xEF；或者可打印 ASCII + 后面紧跟 SJIS
        is_lead = (0x81 <= b <= 0x9F) or (0xE0 <= b <= 0xEF)
        is_ascii = 0x20 <= b <= 0x7E
        if not (is_lead or is_ascii):
            i += 1
            continue

        start = i
        has_sjis = False
        while i < n:
            cur = blob[i]
            if cur == 0x00:
                break
            if (0x81 <= cur <= 0x9F) or (0xE0 <= cur <= 0xEF):
                if i + 1 < n and ((0x40 <= blob[i + 1] <= 0x7E) or (0x80 <= blob[i + 1] <= 0xFC)):
                    has_sjis = True
                    i += 2
                    continue
                break
            if 0x20 <= cur <= 0x7E or cur in (0x09, 0x0A, 0x0D):
                i += 1
                continue
            break

        end = i
        clamped = False
        # 二进制文件里字符串前面经常紧挨着 1~3 字节的指令码/长度字节（恰好落在 0x20..0x7E 可打印 ASCII 区）。
        # 只要开头的 ASCII 不是控制标签（\C[...] / [ruby] / <tag> / {w}），就剥到第一个真正的 SJIS 双字节处，
        # 绝不误改前面的指令码字节。
        if has_sjis and start < end and (0x20 <= blob[start] <= 0x7E):
            lead_pos = start
            while lead_pos < end and (0x20 <= blob[lead_pos] <= 0x7E or blob[lead_pos] in (0x09, 0x0A, 0x0D)):
                lead_pos += 1
            ascii_prefix = blob[start:lead_pos]
            if not ascii_prefix.startswith((b"\\", b"[", b"<", b"{", b"%")):
                start = lead_pos
        # 长度前缀识别（LCF / Delphi / Pascal / 各类定长表）：
        # 若 start 前一字节恰好声明了 [4, end-start] 的长度，且该长度处正好是完整日文字符结尾，
        # 后面紧跟着 [字段ID + 控制字节]，则按声明长度精确截断，绝不把下一字段的头部吞进来。
        if has_sjis and start > 0:
            decl = blob[start - 1]
            if 4 <= decl < (end - start):
                cut = start + decl
                try:
                    prefix_text = blob[start:cut].decode("cp932")
                except UnicodeDecodeError:
                    prefix_text = ""
                if prefix_text and ord(prefix_text[-1]) > 0x7F and (cut + 1 >= n or blob[cut + 1] < 0x20 or cut + 1 == end):
                    end = cut
                    clamped = True
        # 要求正常碰到 0x00（或换行/控制符边界），且里头真有 SJIS 双字节
        if has_sjis and 4 <= (end - start) <= 2048:
            raw = blob[start:end]
            try:
                text = raw.decode("cp932")
            except UnicodeDecodeError:
                text = ""
            if _is_valid_sjis_text(text):
                order += 1
                kind = "menu" if text.strip() in JP_MENU_TERMS else classify(text)
                key = f"{rel_path}@0x{start:x}" if rel_path else f"0x{start:x}"
                # 原串以 0x00 结尾（C / DOS 字符串）时，缩短后的尾巴补 0x00 提前终止最干净；
                # 定长无 \0 记录（如 LCF 长度前缀表）才补空格。
                pad_mode = "nul" if (not clamped and end < len(blob) and blob[end] == 0x00) else "space"
                units.append(
                    Unit(
                        key=key,
                        src=text,
                        kind=kind,
                        file=rel_path,
                        order=order,
                        max_bytes=len(raw),
                        encoding=target_encoding,
                        meta={
                            "offset": start,
                            "length": len(raw),
                            "src_encoding": "cp932",
                            "raw_hex": raw.hex(),
                            "pad": pad_mode,
                        },
                    )
                )
        i = end if clamped else max(i + 1, end + 1)
    return units


def encode_padded(text: str, max_bytes: int, encoding: str = "gbk", pad: str = "space") -> bytes | None:
    """把译文编码并严格补齐到 `max_bytes` 字节。

    - 超出 `max_bytes` → 返回 None（绝不截断，截断必出半个汉字乱码）
    - 短于 `max_bytes`：
      - `pad="nul"`：尾巴补 `0x00`（适用于 C 字符串，提前结束完全合法）
      - `pad="space"`：优先用全角空格（2 字节）补齐偶数差额，剩 1 字节补半角空格 `0x20`
        （适用于没有 `\0` 终止符、按固定长度读字节的 PC-98 / 老脚本引擎）
    """
    try:
        raw = text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return None
    if len(raw) > max_bytes:
        return None
    diff = max_bytes - len(raw)
    if diff == 0:
        return raw
    if pad == "nul":
        return raw + (b"\x00" * diff)
    # 全角空格在 GBK 是 0xA1A1，在 Shift-JIS 是 0x8140
    try:
        fw_space = "\u3000".encode(encoding)
    except UnicodeEncodeError:
        fw_space = b"  "
    if len(fw_space) == 2:
        pairs, rem = divmod(diff, 2)
        return raw + (fw_space * pairs) + (b" " * rem)
    return raw + (b" " * diff)


def patch_blob(blob: bytes, units: list[Unit]) -> tuple[bytes, int, list[str]]:
    """把一批有 `offset` 的 `Unit` 原位写进字节流，返回 (新字节流, 成功条数, 冲突列表)。"""
    buf = bytearray(blob)
    applied = 0
    conflicts: list[str] = []
    for unit in units:
        if not unit.dst or not unit.translatable:
            continue
        meta = unit.meta or {}
        offset = int(meta.get("offset", -1))
        length = int(meta.get("length") or unit.max_bytes or 0)
        raw_hex = str(meta.get("raw_hex") or "")
        pad = str(meta.get("pad") or "space")
        if offset < 0 or length <= 0 or offset + length > len(buf):
            conflicts.append(f"{unit.key}：偏移越界（offset={offset}, length={length}）")
            continue
        current = bytes(buf[offset : offset + length])
        if raw_hex and current.hex() != raw_hex:
            conflicts.append(f"{unit.key}：原文件该位置的字节已变动，拒绝盲写")
            continue
        padded = encode_padded(unit.dst, length, unit.encoding or "gbk", pad=pad)
        if padded is None or len(padded) != length:
            conflicts.append(f"{unit.key}：译文编码后超出 {length} 字节上限，已跳过")
            continue
        buf[offset : offset + length] = padded
        applied += 1
    return bytes(buf), applied, conflicts


def candidate_files(root: Path, max_file_mb: int = 64) -> list[Path]:
    """挑出目录里可能藏着剧情和菜单的二进制/数据文件。"""
    root = Path(root)
    if root.is_file():
        return [root]
    limit = max_file_mb * 1024 * 1024
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = {p.lower() for p in path.relative_to(root).parts[:-1]}
        if rel_parts & _SKIP_DIRS or any(p.startswith(".") for p in rel_parts):
            continue
        low = path.name.lower()
        if low.endswith(".cn.exe") or low.startswith(".ferry") or low == "ferry_tl.json":
            continue
        suf = path.suffix.lower()
        if suf in _MEDIA_SUFFIXES:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= 16 or size > limit:
            continue
        if suf in RAW_SUFFIXES or suf == "":
            out.append(path)
    return out


class RawBinaryAdapter(Adapter):
    """兜底盲扫适配器：当所有已知引擎都不认领时，直接从 exe / dat / mes 字节流里榨取剧情和菜单。"""

    name = "rawbin"
    priority = 90  # 排在所有具名引擎后面，只做兜底

    def __init__(self, target_encoding: str = "gbk", include_samples: bool = False) -> None:
        self.target_encoding = target_encoding
        self.include_samples = include_samples

    def scan_units(self, root: Path) -> list[Unit]:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        all_units: list[Unit] = []
        for path in candidate_files(root):
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            rel = path.relative_to(base).as_posix()
            found = scan_sjis_strings(blob, rel_path=rel, target_encoding=self.target_encoding)
            # 一个文件里至少要有 3 条正经台词或菜单才算数，防止在纯代码 DLL 里抓出 1 个巧合词
            if len(found) >= 3:
                all_units.extend(found)
        return all_units

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        units = self.scan_units(root)
        if not units:
            return ProbeReport(
                engine="未知二进制 / 单 EXE",
                root=root,
                readable=False,
                writable=False,
                notes=["二进制盲扫未发现成规模的 Shift-JIS 剧情或菜单字符串"],
            )
        files = sorted({u.file for u in units})
        stories = sum(1 for u in units if u.kind == "story")
        menus = sum(1 for u in units if u.kind == "menu")
        return ProbeReport(
            engine="通用二进制 / 单 EXE (RawBin)",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(units),
            container="binary",
            text_format="cp932->gbk (定长原位覆盖)",
            in_place=True,
            notes=[
                f"在 {len(files)} 个二进制文件中扫出 {len(units)} 条文本（剧情 {stories} / 菜单 {menus}）",
                "回写采用「定长原位覆盖 + 全角/半角空格对齐」，不改变任何文件长度与指针偏移",
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
                json_pointer=str(u.meta.get("offset", 0)),
            )
            for u in self.scan_units(root)
        ]

    def apply_units(self, root: Path, units: list[Unit], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        by_file: dict[str, list[Unit]] = {}
        for u in units:
            if u.dst and u.translatable:
                by_file.setdefault(u.file, []).append(u)
        if not by_file:
            return ApplyReport(notes=["没有可写回的译文"])

        report = ApplyReport(in_place=out is None, out_path=out or base)
        undo = UndoLog.open(base, engine=self.name) if out is None else None
        for rel, group in sorted(by_file.items()):
            src_path = base / rel
            if not src_path.is_file():
                report.conflicts.append(f"找不到文件：{rel}")
                continue
            blob = src_path.read_bytes()
            patched, count, conflicts = patch_blob(blob, group)
            report.conflicts.extend(conflicts)
            if count == 0:
                continue
            if out is None:
                assert undo is not None
                undo.will_modify(rel)
                src_path.write_bytes(patched)
                undo.did_write(rel)
            else:
                target = unique_output(Path(out)) if Path(out).suffix else (Path(out) / rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(patched)
                report.out_path = target
            report.applied += count

        if undo is not None and report.applied > 0:
            report.undo_path = undo.finish()
        return report

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        units = self.scan_units(root)
        for u in units:
            if u.key in translations:
                u.dst = translations[u.key]
            elif u.src in translations:
                u.dst = translations[u.src]
        return self.apply_units(root, units, out=out)
