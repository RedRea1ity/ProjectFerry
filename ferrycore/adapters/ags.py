"""Adventure Game Studio (AGS) 编译版游戏适配器 (`AgsAdapter`)。

AGS 是一类非常经典的独立点击冒险引擎（代表作：《Sowjet-Unterzögersdorf》《Blackwell》
《Gemini Rue》《Primordia》《Kathy Rain》《Technobabylon》《Unavowed》等）。

它的编译成品目录通常包含：
- 主程序 `*.exe`（或 `*.ags`）—— 尾部追加 `CLIB\\x1a` 多卷归档，内含 `ac2game.dta`、
  `room*.crm` 与 `agsfnt*.ttf` / `agsfnt*.wfn` 字库；
- 配置文件 `acsetup.cfg`（其中的 `[language] / translation=` 指定当前生效的语言包）；
- 编译后的语言包 `*.tra`（魔数 `AGSTranslation\\0`，字符串采用经典的 `"Avis Durgan"` 加法混淆）。

此前摆渡只支持未编译的 `.trs` 源码（`NarrativeAdapter`），碰到只有 `.tra` + `CLIB` 的成品游戏时
会退化到 `rawbin` 盲扫，只能从 `winsetup.exe` 里扫出几条启动器报错。

本适配器完整打通编译版 AGS：
1. **解析 `.tra` 二进制语言包**：正确处理 AGS 2.x 历史怪癖（Block 2 块头长度少算 1 字节 NUL）、
   Block 1 原文/译文字典、Block 3 字体与 RTL 选项以及 AGS 3.6+ 扩展块。
2. **保留语音编号与原始匹配键**：
   - AGS 台词开头的 `&1013 ` 是 `speech.vox` 语音配音索引；提取时自动剥离前缀让翻译更干净，
     回写时自动补回原始 `&1013 `，确保配音不丢。
   - 若游戏原版是德语/法语等小语种、附带 `ENGLISH.tra`（如《Sowjet-Unterzögersdorf》），
     提取层优先把 `ENGLISH.tra` 里的英文译文交给翻译模型，但写入 `Chinese.tra` 时**严格保留
     原始德语 `src_raw` 字节作为查找键**，保证引擎运行时 100% 命中。
3. **无损回写 + 一键撤销**：
   - 生成独立 `Chinese.tra`（不覆盖原版 `ENGLISH.tra`），并在 `acsetup.cfg` 的 `[language]`
     节写入 `translation=Chinese`，全程通过 `UndoLog` 登记可撤。
4. **诚实告知旧版引擎字库限制**：
   - 自动探测 `CLIB` 内的 `ac2game.dta` 版本号：AGS 3.6+ 原生支持 UTF-8；
     早期 AGS 2.x/3.0–3.5（如 2.62）自带旧 exe 为单字节 ANSI，无法直接渲染中文，
     `probe` 会明确提示使用 **ScummVM** 或 **AGS 3.6+ 运行时** + 替换 `agsfnt*.ttf` 中文字体运行。
"""

from __future__ import annotations

import os
import re
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .. import deps
from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .undo import UndoLog

TRA_MAGIC = b"AGSTranslation\x00"
AVIS_KEY = b"Avis Durgan"
CLIB_MAGIC = b"CLIB\x1a"
CLIB_V15_KEY = b"My\x01\xde\x04Jibzle"
DTA_MAGIC = b"Adventure Creator Game File v2"

# AGS 语音编号前缀，如 "&1013 Hello!" 或 "&1083  Sehr hübsch."
_VOICE_PREFIX_RE = re.compile(r"^(&\d+[ \t]+)")

# 跳过已生成的中文翻译包，避免重复扫描自己产出的文件
_CHINESE_TRA_STEMS = {"chinese", "zh", "zh_cn", "zh-cn", "chs", "cht", "cn", "sc", "tc"}

# 回写产出的目标语言包名（对应 acsetup.cfg 中的 translation=Chinese）
OUTPUT_TRA_NAME = "Chinese"
LAUNCHER_BAT_NAME = "启动中文版.bat"
CJK_FONT_BASE_NAME = "ferry_cjk.ttf"


def avis_decrypt(data: bytes) -> bytes:
    """解密 AGS 'Avis Durgan' 字节加法混淆。"""
    key = AVIS_KEY
    klen = len(key)
    return bytes((b - key[i % klen]) & 0xFF for i, b in enumerate(data))


def avis_encrypt(data: bytes) -> bytes:
    """加密为 AGS 'Avis Durgan' 字节加法混淆。"""
    key = AVIS_KEY
    klen = len(key)
    return bytes((b + key[i % klen]) & 0xFF for i, b in enumerate(data))


def _strip_voice_prefix(text: str) -> tuple[str, str]:
    """拆出开头的 '&1234 ' 语音前缀，返回 (voice_tag, body)。"""
    m = _VOICE_PREFIX_RE.match(text)
    if not m:
        return "", text
    return m.group(1), text[m.end():]


def _normalize_voice_tag(tag: str) -> str:
    """把 '&1083  ' 规范成 '&1083 '（保留原编号，单空格分隔）。"""
    if not tag:
        return ""
    return tag.strip() + " "


def _detect_tra_encoding(raw_chunks: list[bytes], ext_options: dict[str, str]) -> str:
    declared = ext_options.get("encoding", "").strip().lower()
    if declared in ("utf-8", "utf8"):
        return "utf-8"
    if declared in ("ascii", "ansi", "cp1252", "latin-1", "latin1"):
        return "cp1252"

    has_high_byte = False
    for chunk in raw_chunks:
        if any(b >= 0x80 for b in chunk):
            has_high_byte = True
            try:
                chunk.decode("utf-8")
            except UnicodeDecodeError:
                return "cp1252"
    return "utf-8" if has_high_byte else "cp1252"


@dataclass
class TraEntry:
    """`.tra` 字典里的一条原文→译文映射。"""

    index: int  # 1-based 序号
    src_raw: bytes  # 不含结尾 NUL 的原始字节（用于 100% 精确回写查找键）
    src_text: str
    dst_raw: bytes  # 不含结尾 NUL 的译文字节
    dst_text: str
    voice_tag: str = ""  # 来自 src（或 dst）的 '&1234 ' 语音前缀


@dataclass
class TraDocument:
    """解析后的 `.tra` 二进制语言包。"""

    game_uid: int = 0
    game_name_raw: bytes = b""
    game_name: str = ""
    header_block_raw: bytes = b""
    entries: list[TraEntry] = field(default_factory=list)
    normal_font: int = -1
    speech_font: int = -1
    right_to_left: int = -1
    encoding: str = "cp1252"
    ext_options: dict[str, str] = field(default_factory=dict)
    extra_blocks: list[bytes] = field(default_factory=list)


def _parse_ext_sopts(payload: bytes) -> dict[str, str]:
    """尽力解析 AGS 3.6+ `ext_sopts` 字符串键值表。"""
    opts: dict[str, str] = {}
    if len(payload) < 4:
        return opts
    pos = 0
    count = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    if count > 4096:
        return opts
    for _ in range(count):
        if pos + 4 > len(payload):
            break
        klen = struct.unpack_from("<I", payload, pos)[0]
        pos += 4
        if klen > 4096 or pos + klen > len(payload):
            break
        key = payload[pos : pos + klen].decode("utf-8", "replace")
        pos += klen
        if pos + 4 > len(payload):
            break
        vlen = struct.unpack_from("<I", payload, pos)[0]
        pos += 4
        if vlen > 65536 or pos + vlen > len(payload):
            break
        val = payload[pos : pos + vlen].decode("utf-8", "replace")
        pos += vlen
        opts[key] = val
    return opts


def parse_tra(blob: bytes) -> TraDocument:
    """解析 AGS 编译版 `.tra` 二进制文件。不是合法 `.tra` 时抛出 ValueError。"""
    if len(blob) < len(TRA_MAGIC) or not blob.startswith(TRA_MAGIC):
        raise ValueError("不是有效的 AGS .tra 文件（缺少 AGSTranslation 头）")

    doc = TraDocument()
    raw_pairs: list[tuple[bytes, bytes]] = []
    pos = len(TRA_MAGIC)
    total = len(blob)

    while pos + 4 <= total:
        block_type = struct.unpack_from("<i", blob, pos)[0]
        pos += 4
        if block_type == -1:
            break

        if block_type == 0:
            # AGS 3.6+ DataExtParser 扩展块：16 字节 ASCII 标识 + int64 长度（data_ext.cpp 规定 block==0 一律 64 位）+ 内容
            if pos + 24 > total:
                break
            block_start = pos - 4
            ext_id = blob[pos : pos + 16].split(b"\x00", 1)[0].decode("ascii", "replace")
            pos += 16
            ext_size = struct.unpack_from("<q", blob, pos)[0]
            pos += 8
            if ext_size < 0 or pos + ext_size > total:
                break
            ext_payload = blob[pos : pos + ext_size]
            pos += ext_size
            if ext_id.lower() == "ext_sopts":
                doc.ext_options.update(_parse_ext_sopts(ext_payload))
            else:
                doc.extra_blocks.append(blob[block_start:pos])
            continue

        if pos + 4 > total:
            break
        block_size = struct.unpack_from("<i", blob, pos)[0]
        pos += 4
        if block_size < 0:
            break

        if block_type == 2:
            # Block 2: GameID（uid + name_len + enc_name）
            # 历史怪癖：AGS 2.x 写入的 block_size 是 4 + 4 + (name_len - 1)，漏算了结尾 NUL。
            if pos + 8 > total:
                break
            header_start = pos - 8
            doc.game_uid, name_len = struct.unpack_from("<ii", blob, pos)
            if name_len < 0 or pos + 8 + name_len > total:
                raise ValueError(f"AGS .tra Block 2 游戏名长度异常：{name_len}")
            enc_name = blob[pos + 8 : pos + 8 + name_len]
            doc.game_name_raw = avis_decrypt(enc_name).rstrip(b"\x00")
            actual_body_len = 8 + name_len
            if block_size > actual_body_len and pos + block_size <= total:
                step = block_size
            else:
                step = actual_body_len
            doc.header_block_raw = blob[header_start : pos + step]
            pos += step

        elif block_type == 1:
            # Block 1: 原文/译文字典
            end = min(total, pos + block_size)
            cur = pos
            while cur + 4 <= end:
                slen = struct.unpack_from("<i", blob, cur)[0]
                cur += 4
                if slen < 0 or cur + slen > end:
                    break
                src_enc = blob[cur : cur + slen]
                cur += slen
                if cur + 4 > end:
                    break
                tlen = struct.unpack_from("<i", blob, cur)[0]
                cur += 4
                if tlen < 0 or cur + tlen > end:
                    break
                dst_enc = blob[cur : cur + tlen]
                cur += tlen
                if slen == 0 and tlen == 0:
                    break
                src_dec = avis_decrypt(src_enc).rstrip(b"\x00")
                dst_dec = avis_decrypt(dst_enc).rstrip(b"\x00")
                raw_pairs.append((src_dec, dst_dec))
            pos = end

        elif block_type == 3:
            # Block 3: 字体与文字方向选项
            end = min(total, pos + block_size)
            if pos + 12 <= end:
                doc.normal_font, doc.speech_font, doc.right_to_left = struct.unpack_from("<iii", blob, pos)
            pos = end

        else:
            # 未知块：按长度跳过并原样留存
            end = min(total, pos + block_size)
            doc.extra_blocks.append(blob[pos - 8 : end])
            pos = end

    src_chunks = [doc.game_name_raw] + [s for s, _ in raw_pairs]
    dst_chunks = [d for _, d in raw_pairs]
    src_enc = _detect_tra_encoding(src_chunks, {})
    dst_enc = _detect_tra_encoding(dst_chunks, doc.ext_options)
    doc.encoding = dst_enc
    doc.game_name = doc.game_name_raw.decode(src_enc, "replace")

    for idx, (s_raw, d_raw) in enumerate(raw_pairs, start=1):
        s_txt = s_raw.decode(src_enc, "replace")
        d_txt = d_raw.decode(dst_enc, "replace")
        v_src, _ = _strip_voice_prefix(s_txt)
        v_dst, _ = _strip_voice_prefix(d_txt)
        voice_tag = _normalize_voice_tag(v_src or v_dst)
        doc.entries.append(
            TraEntry(
                index=idx,
                src_raw=s_raw,
                src_text=s_txt,
                dst_raw=d_raw,
                dst_text=d_txt,
                voice_tag=voice_tag,
            )
        )

    return doc


def _build_ext_sopts_block(options: dict[str, str]) -> bytes:
    """按 AGS 3.6+ `data_ext.cpp` + `StrUtil::WriteStringMap` 规范编码 `ext_sopts` 扩展块。"""
    payload = bytearray(struct.pack("<i", len(options)))
    for k, v in options.items():
        kb = k.encode("utf-8")
        vb = v.encode("utf-8")
        payload.extend(struct.pack("<i", len(kb)))
        payload.extend(kb)
        payload.extend(struct.pack("<i", len(vb)))
        payload.extend(vb)
    out = bytearray()
    out.extend(struct.pack("<i", 0))
    out.extend(b"ext_sopts".ljust(16, b"\x00"))
    out.extend(struct.pack("<q", len(payload)))
    out.extend(payload)
    return bytes(out)


def rebuild_tra(
    doc: TraDocument,
    translations: dict[int, str],
    *,
    target_encoding: str = "utf-8",
    emit_utf8_ext: bool = True,
) -> tuple[bytes, int]:
    """根据 `doc` 与 `{1-based index: 译文}` 重新编译 `.tra` 二进制。

    - 严格保留原始 `src_raw` 查找键字节（包括原版 CP1252 字符与语音标签）；
    - 自动补回被翻译层剥离的 `&1234 ` 语音编号前缀；
    - 保留原始 Block 2 GameID 头与 Block 3 字体选项；
    - 写入 AGS 3.6+ 标准 `ext_sopts` (`encoding=utf-8`) 扩展块，确保 AGS 3.6+ / ScummVM
      对旧版 ANSI 游戏也能正确按 UTF-8 解码中文译文。
    """
    out = bytearray(TRA_MAGIC)

    # Block 2: GameID
    if doc.header_block_raw:
        out.extend(doc.header_block_raw)
    else:
        name_nul = doc.game_name_raw + b"\x00"
        enc_name = avis_encrypt(name_nul)
        body = struct.pack("<ii", doc.game_uid, len(enc_name)) + enc_name
        out.extend(struct.pack("<ii", 2, len(body)))
        out.extend(body)

    # Block 1: 翻译字典
    dict_body = bytearray()
    applied = 0
    for entry in doc.entries:
        if not entry.src_raw:
            continue
        src_nul = entry.src_raw + b"\x00"
        enc_src = avis_encrypt(src_nul)

        tr = translations.get(entry.index)
        if tr is not None and tr.strip():
            final_text = tr
            if entry.voice_tag and not _VOICE_PREFIX_RE.match(final_text):
                final_text = f"{entry.voice_tag}{final_text}"
            dst_nul = final_text.encode(target_encoding, "replace") + b"\x00"
            applied += 1
        else:
            dst_nul = entry.dst_raw + b"\x00"
        enc_dst = avis_encrypt(dst_nul)

        dict_body.extend(struct.pack("<i", len(enc_src)))
        dict_body.extend(enc_src)
        dict_body.extend(struct.pack("<i", len(enc_dst)))
        dict_body.extend(enc_dst)

    # AGS 标准字典结束哨兵：一对空串（1 字节加密 NUL）
    enc_empty = avis_encrypt(b"\x00")
    dict_body.extend(struct.pack("<i", len(enc_empty)))
    dict_body.extend(enc_empty)
    dict_body.extend(struct.pack("<i", len(enc_empty)))
    dict_body.extend(enc_empty)
    out.extend(struct.pack("<ii", 1, len(dict_body)))
    out.extend(dict_body)

    # Block 3: 字体与排版选项
    out.extend(struct.pack("<iiiii", 3, 12, doc.normal_font, doc.speech_font, doc.right_to_left))

    # 原样保留其余扩展块
    for raw_ext in doc.extra_blocks:
        out.extend(raw_ext)

    # Block 0: ext_sopts（向 AGS 3.6+ / ScummVM 声明 encoding=utf-8，同时不设 gameencoding 以保留原字节查找键）
    sopts = dict(doc.ext_options)
    if emit_utf8_ext and target_encoding.lower() in ("utf-8", "utf8"):
        sopts["encoding"] = "utf-8"
        sopts.pop("gameencoding", None)
    if sopts:
        out.extend(_build_ext_sopts_block(sopts))

    # EOF 哨兵
    out.extend(struct.pack("<i", -1))
    return bytes(out), applied


@dataclass
class AgsArchiveInfo:
    """从 `.exe` / `.ags` / `ac2game.dta` 探测到的 AGS 引擎元数据。"""

    archive_file: str = ""
    clib_version: int = 0
    dta_version: int = 0
    engine_version: str = ""
    game_name: str = ""
    ttf_fonts: list[str] = field(default_factory=list)
    wfn_fonts: list[str] = field(default_factory=list)

    @property
    def unicode_native(self) -> bool:
        """AGS 3.6.0+（dta_version >= 50）才原生支持 UTF-8 渲染。"""
        if self.dta_version >= 50:
            return True
        m = re.match(r"^(\d+)\.(\d+)", self.engine_version)
        if m:
            major, minor = int(m.group(1)), int(m.group(2))
            return (major, minor) >= (3, 6)
        return False


def _parse_dta_header(dta: bytes, info: AgsArchiveInfo) -> None:
    if len(dta) < 40 or not dta.startswith(DTA_MAGIC):
        return
    info.dta_version = struct.unpack_from("<i", dta, 30)[0]
    vlen = struct.unpack_from("<i", dta, 34)[0]
    if 0 < vlen <= 32 and 38 + vlen <= len(dta):
        info.engine_version = dta[38 : 38 + vlen].split(b"\x00", 1)[0].decode("ascii", "replace")
        g_off = 38 + vlen
        if g_off + 50 <= len(dta):
            raw_gname = dta[g_off : g_off + 50].split(b"\x00", 1)[0]
            try:
                info.game_name = raw_gname.decode("utf-8")
            except UnicodeDecodeError:
                info.game_name = raw_gname.decode("cp1252", "replace")


def _dec_clib_v15_name(raw: bytes) -> str:
    k = CLIB_V15_KEY
    dec = bytes((b - k[i % len(k)]) & 0xFF for i, b in enumerate(raw))
    return dec.split(b"\x00", 1)[0].decode("latin1", "replace")


def _inspect_clib_file(path: Path) -> AgsArchiveInfo | None:
    try:
        size = path.stat().st_size
        if size < 64:
            return None
        with open(path, "rb") as fh:
            head = fh.read(32)
            clib_off = -1
            if head.startswith(CLIB_MAGIC):
                clib_off = 0
            else:
                fh.seek(max(0, size - 64))
                tail = fh.read(64)
                idx = tail.find(b"CLIB\x01\x02\x03\x04")
                if idx >= 4:
                    cand = struct.unpack_from("<I", tail, idx - 4)[0]
                    if 0 <= cand < size - 16:
                        fh.seek(cand)
                        if fh.read(5) == CLIB_MAGIC:
                            clib_off = cand
            if clib_off < 0:
                return None

            fh.seek(clib_off + 5)
            ver_bytes = fh.read(2)
            if len(ver_bytes) < 2:
                return None
            ver = ver_bytes[0]
            info = AgsArchiveInfo(archive_file=path.name, clib_version=ver)

            names: list[str] = []
            offsets: list[int] = []
            vols: list[int] = []

            if ver in (10, 11, 15, 20):
                # CLIB v15（AGS 2.6x-2.7x）和 v20（AGS 3.x 早期）定长目录表
                if ver == 15:
                    num_df = struct.unpack("<i", fh.read(4))[0]
                    if 0 < num_df <= 64:
                        fh.seek(num_df * 20, 1)
                        num_files = struct.unpack("<i", fh.read(4))[0]
                        if 0 < num_files <= 20000:
                            names = [_dec_clib_v15_name(fh.read(25)) for _ in range(num_files)]
                            offsets = list(struct.unpack(f"<{num_files}I", fh.read(num_files * 4)))
                            fh.seek(num_files * 4, 1)  # lengths
                            vols = list(fh.read(num_files))
                elif ver == 20:
                    num_df = struct.unpack("<i", fh.read(4))[0]
                    if 0 < num_df <= 64:
                        for _ in range(num_df):
                            while fh.read(1) not in (b"\x00", b""):
                                pass
                        num_files = struct.unpack("<i", fh.read(4))[0]
                        if 0 < num_files <= 20000:
                            for _ in range(num_files):
                                buf = bytearray()
                                while True:
                                    ch = fh.read(1)
                                    if not ch or ch == b"\x00":
                                        break
                                    buf.append(ch[0])
                                names.append(buf.decode("latin1", "replace"))
                            offsets = list(struct.unpack(f"<{num_files}I", fh.read(num_files * 4)))
                            fh.seek(num_files * 4, 1)
                            vols = list(fh.read(num_files))
            elif ver >= 21:
                # CLIB v21/v30（AGS 3.x）带伪随机种子混淆
                if ver >= 30:
                    fh.seek(clib_off + 6)
                    ver = struct.unpack("<i", fh.read(4))[0]
                    info.clib_version = ver
                    fh.read(1)  # reserved
                seed = struct.unpack("<i", fh.read(4))[0]
                state = (seed + 9338638) & 0xFFFFFFFF

                def _next_byte() -> int:
                    nonlocal state
                    state = (state * 214013 + 2531011) & 0xFFFFFFFF
                    return ((state >> 16) & 0x7FFF) >> 8

                def _dec_bytes(n: int) -> bytes:
                    raw = fh.read(n)
                    return bytes((b - _next_byte()) & 0xFF for b in raw)

                def _dec_int() -> int:
                    return struct.unpack("<i", _dec_bytes(4))[0]

                def _dec_str() -> str:
                    buf = bytearray()
                    for _ in range(260):
                        b = _dec_bytes(1)
                        if not b or b[0] == 0:
                            break
                        buf.append(b[0])
                    return buf.decode("latin1", "replace")

                num_df = _dec_int()
                if 0 < num_df <= 64:
                    for _ in range(num_df):
                        _dec_str()
                    num_files = _dec_int()
                    if 0 < num_files <= 20000:
                        names = [_dec_str() for _ in range(num_files)]
                        if ver >= 30:
                            offsets = [struct.unpack("<q", _dec_bytes(8))[0] for _ in range(num_files)]
                            for _ in range(num_files):
                                _dec_bytes(8)
                        else:
                            offsets = [_dec_int() for _ in range(num_files)]
                            for _ in range(num_files):
                                _dec_int()
                        vols = list(_dec_bytes(num_files))

            for idx, name in enumerate(names):
                low = name.lower()
                if low.endswith(".ttf"):
                    info.ttf_fonts.append(name)
                elif low.endswith(".wfn"):
                    info.wfn_fonts.append(name)
                elif low in ("ac2game.dta", "game28.dta") and idx < len(offsets):
                    vol = vols[idx] if idx < len(vols) else 0
                    if vol == 0:
                        file_pos = clib_off + offsets[idx]
                        if 0 <= file_pos < size - 40:
                            fh.seek(file_pos)
                            _parse_dta_header(fh.read(256), info)
            return info
    except (OSError, struct.error, ValueError):
        return None


def inspect_ags_archive(root: Path) -> AgsArchiveInfo | None:
    """在游戏目录里寻找 AGS 的 `ac2game.dta` 或内嵌 `CLIB\\x1a` 归档，返回引擎信息。"""
    root = Path(root)
    if root.is_file():
        low = root.name.lower()
        if low in ("ac2game.dta", "game28.dta"):
            try:
                with root.open("rb") as fh:
                    head = fh.read(256)
            except OSError:
                return None
            if head.startswith(DTA_MAGIC):
                info = AgsArchiveInfo(archive_file=root.name)
                _parse_dta_header(head, info)
                return info
            return None
        if low.endswith((".ags", ".exe")) and low != "winsetup.exe":
            return _inspect_clib_file(root)
        if not low.endswith(".tra"):
            return None
        base = root.parent
    else:
        base = root
    if not base.is_dir():
        return None

    # 1) 散装 ac2game.dta / game28.dta
    for dta_name in ("ac2game.dta", "game28.dta"):
        dta_path = base / dta_name
        if dta_path.is_file():
            try:
                with dta_path.open("rb") as fh:
                    head = fh.read(256)
            except OSError:
                continue
            if head.startswith(DTA_MAGIC):
                info = AgsArchiveInfo(archive_file=dta_name)
                _parse_dta_header(head, info)
                return info

    # 2) 内嵌 CLIB 的 .ags 或 .exe（按文件大小降序，主程序总是最大的）
    candidates: list[tuple[int, Path]] = []
    try:
        for p in base.iterdir():
            if not p.is_file():
                continue
            low = p.name.lower()
            if low.endswith((".ags", ".exe")) and low != "winsetup.exe":
                try:
                    candidates.append((p.stat().st_size, p))
                except OSError:
                    continue
    except OSError:
        return None

    candidates.sort(key=lambda item: -item[0])
    for _, cand in candidates[:6]:
        info = _inspect_clib_file(cand)
        if info is not None:
            return info
    return None


def update_acsetup_language(text: str, translation_name: str) -> str:
    """在 `acsetup.cfg` 的 `[language]` 节中写入或更新 `translation=<name>`。"""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    out: list[str] = []
    in_lang = False
    found_section = False
    wrote_key = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_lang and not wrote_key:
                out.append(f"translation={translation_name}")
                wrote_key = True
            in_lang = stripped.lower() == "[language]"
            if in_lang:
                found_section = True
            out.append(line)
            continue

        if in_lang and re.match(r"^\s*translation\s*=", line, re.IGNORECASE):
            out.append(f"translation={translation_name}")
            wrote_key = True
            continue

        out.append(line)

    if in_lang and not wrote_key:
        out.append(f"translation={translation_name}")
        wrote_key = True
    elif not found_section:
        if out and out[-1].strip():
            out.append("")
        out.append("[language]")
        out.append(f"translation={translation_name}")

    return newline.join(out) + newline


class AgsAdapter(Adapter):
    """Adventure Game Studio (AGS) 编译版游戏适配器（`.tra` + `acsetup.cfg` + `CLIB`）。"""

    name = "ags"
    priority = 28

    def _find_tra_files(self, root: Path) -> list[Path]:
        root = Path(root)
        if root.is_file():
            try:
                if root.suffix.lower() == ".tra" and root.read_bytes()[:15] == TRA_MAGIC:
                    return [root]
            except OSError:
                return []
            return []
        if not root.is_dir():
            return []

        found: list[Path] = []
        try:
            for p in sorted(root.iterdir()):
                if not p.is_file() or p.suffix.lower() != ".tra":
                    continue
                try:
                    with open(p, "rb") as fh:
                        if fh.read(len(TRA_MAGIC)) == TRA_MAGIC:
                            found.append(p)
                except OSError:
                    continue
        except OSError:
            return []
        return found

    def _pick_source_tra(self, root: Path) -> Path | None:
        tra_files = self._find_tra_files(root)
        if not tra_files:
            return None
        non_cn = [p for p in tra_files if p.stem.lower() not in _CHINESE_TRA_STEMS]
        pool = non_cn or tra_files

        # 优先选择英语语言包（如 ENGLISH.tra），否则取体积最大的 .tra
        def _rank(p: Path) -> tuple[int, int, str]:
            stem = p.stem.lower()
            is_en = 0 if stem in ("english", "en", "en_us", "en_gb") or stem.startswith("english") else 1
            try:
                sz = p.stat().st_size
            except OSError:
                sz = 0
            return (is_en, -sz, stem)

        pool.sort(key=_rank)
        return pool[0]

    def _entry_display_text(self, entry: TraEntry) -> str:
        """返回交给翻译层的干净原文（剥离 `&1234 ` 语音前缀）。"""
        raw = entry.dst_text if entry.dst_text.strip() else entry.src_text
        _, body = _strip_voice_prefix(raw)
        return body

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        tra_path = self._pick_source_tra(root)
        archive_info = inspect_ags_archive(root)
        has_acsetup = (
            (root.name.lower() == "acsetup.cfg")
            if root.is_file()
            else (base / "acsetup.cfg").is_file()
        )

        if tra_path is None and archive_info is None and not has_acsetup:
            return ProbeReport(
                engine=self.name,
                root=root,
                notes=["目录里未见 AGS 语言包（*.tra）、acsetup.cfg 或 CLIB 归档"],
            )

        if tra_path is None:
            notes = ["识别为 Adventure Game Studio (AGS) 游戏"]
            if archive_info is not None:
                ver = archive_info.engine_version or f"dta v{archive_info.dta_version}"
                gname = f"《{archive_info.game_name}》" if archive_info.game_name else ""
                notes.append(f"主归档 {archive_info.archive_file}（CLIB v{archive_info.clib_version}，AGS {ver}）{gname}")
            notes.append("目录里没有现成的 .tra 语言包；请先用 AGS Editor / agstool 导出 .trs/.tra 后再扫描。")
            return ProbeReport(
                engine=self.name,
                root=root,
                readable=False,
                writable=False,
                container="ags-clib",
                text_format="ags-dta",
                unicode_engine=archive_info.unicode_native if archive_info else False,
                notes=notes,
            )

        try:
            doc = parse_tra(tra_path.read_bytes())
        except (OSError, ValueError) as exc:
            return ProbeReport(
                engine=self.name,
                root=root,
                readable=False,
                writable=False,
                container="ags-tra",
                notes=[f"解析 {tra_path.name} 失败：{exc}"],
            )

        entries = self._collect_from_doc(root, base, tra_path, doc)
        unicode_ok = archive_info.unicode_native if archive_info is not None else (doc.encoding == "utf-8")

        notes = [
            f"识别为 Adventure Game Studio (AGS) 游戏"
            + (f"《{doc.game_name}》" if doc.game_name else "")
            + f"，从 {tra_path.name} 解出 {len(entries)} 条文本（GameID={doc.game_uid}）。",
            f"回写将生成独立 {OUTPUT_TRA_NAME}.tra（不覆盖 {tra_path.name}），并在 acsetup.cfg 中启用 translation={OUTPUT_TRA_NAME}。",
        ]
        voice_count = sum(1 for e in doc.entries if e.voice_tag and self._entry_display_text(e).strip())
        if voice_count:
            notes.append(f"已自动剥离并保护 {voice_count} 条语音配音索引（&编号），回写时原样补回。")

        needs: list[str] = []
        if archive_info is not None:
            ver = archive_info.engine_version or f"dta v{archive_info.dta_version}"
            if not archive_info.unicode_native:
                needs.append("ags36")
                ags_dep = deps.REGISTRY.get("ags36")
                ags_exe = ags_dep.locate() if ags_dep else None
                font_hint = ""
                if archive_info.ttf_fonts:
                    font_hint = f"（将自动覆盖 {len(archive_info.ttf_fonts)} 个外置 TTF 字体槽）"
                if ags_exe is not None:
                    notes.append(
                        f"原版主程序 {archive_info.archive_file} 为旧版 AGS {ver}（8 位 ANSI）；"
                        f"已检测到 AGS 3.6+ 运行时（{ags_exe}），回写时将自动铺入中文字体{font_hint}并生成「{LAUNCHER_BAT_NAME}」。"
                    )
                else:
                    notes.append(
                        f"注意：原版主程序 {archive_info.archive_file} 为旧版 AGS {ver}（8 位 ANSI 引擎），"
                        f"直接启动无法显示 UTF-8 中文；请按弹窗提示安装「AGS 3.6+ 中文兼容运行时」"
                        f"（或使用 ScummVM），回写时将自动铺入中文字体{font_hint}并生成「{LAUNCHER_BAT_NAME}」。"
                    )

        return ProbeReport(
            engine=self.name,
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container="ags-clib" if archive_info else "ags-folder",
            text_format="ags-tra",
            unicode_engine=unicode_ok,
            needs=needs,
            in_place=True,
            notes=notes,
        )

    def _collect_from_doc(self, root: Path, base: Path, tra_path: Path, doc: TraDocument) -> list[Entry]:
        rel = tra_path.relative_to(base).as_posix()
        entries: list[Entry] = []
        for te in doc.entries:
            body = self._entry_display_text(te)
            if not body.strip():
                continue
            entries.append(
                Entry(
                    modid=root.name,
                    key=f"{rel}#{te.index}",
                    english=body,
                    source="game",
                    kind="line",
                    rel_path=rel,
                    json_pointer=str(te.index),
                )
            )
        return entries

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        tra_path = self._pick_source_tra(root)
        if tra_path is None:
            return []
        try:
            doc = parse_tra(tra_path.read_bytes())
        except (OSError, ValueError):
            return []
        return self._collect_from_doc(root, base, tra_path, doc)

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        tra_path = self._pick_source_tra(root)
        report = ApplyReport(in_place=True, out_path=base)
        if tra_path is None:
            report.notes.append("未找到源 .tra 语言包")
            return report

        try:
            doc = parse_tra(tra_path.read_bytes())
        except (OSError, ValueError) as exc:
            report.conflicts.append(f"{tra_path.name}: {exc}")
            return report

        rel = tra_path.relative_to(base).as_posix()
        index_map: dict[int, str] = {}
        for te in doc.entries:
            body = self._entry_display_text(te)
            if not body.strip():
                continue
            key = f"{rel}#{te.index}"
            tr = translations.get(key) or translations.get(body) or translations.get(te.dst_text) or translations.get(te.src_text)
            if tr and tr.strip():
                index_map[te.index] = tr
            else:
                report.missed += 1

        if not index_map:
            report.notes.append("译文表里还没有命中任何 AGS 条目，未写入文件")
            return report

        new_blob, applied = rebuild_tra(doc, index_map, target_encoding="utf-8")
        if applied == 0:
            report.notes.append("未应用任何译文")
            return report

        out_tra_name = f"{OUTPUT_TRA_NAME}.tra"
        out_tra_path = base / out_tra_name
        cfg_name = "acsetup.cfg"
        cfg_path = base / cfg_name

        undo = UndoLog.open(base, engine=self.name)

        # 1) 写 Chinese.tra（不破坏原版 ENGLISH.tra）
        if out_tra_path.exists():
            undo.will_modify(out_tra_name)
        else:
            undo.will_add(out_tra_name)
        out_tra_path.write_bytes(new_blob)
        undo.did_write(out_tra_name)

        # 2) 更新 acsetup.cfg 启用 translation=Chinese
        if cfg_path.exists():
            undo.will_modify(cfg_name)
            try:
                old_cfg = cfg_path.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                old_cfg = cfg_path.read_text(encoding="cp1252", errors="replace")
            new_cfg = update_acsetup_language(old_cfg, OUTPUT_TRA_NAME)
            cfg_path.write_bytes(new_cfg.encode("utf-8"))
            undo.did_write(cfg_name)
        else:
            undo.will_add(cfg_name)
            cfg_path.write_bytes(f"[language]\r\ntranslation={OUTPUT_TRA_NAME}\r\n".encode("utf-8"))
            undo.did_write(cfg_name)

        # 3) 外置中文字体覆盖（AGS 3.6+ 会优先加载游戏目录下的 agsfnt*.ttf 覆盖 CLIB 内置字库）
        archive_info = inspect_ags_archive(root)
        patched_fonts = self._inject_cjk_fonts(base, archive_info, undo)

        # 4) 旧版 AGS (< 3.6) 生成无损「启动中文版.bat」，调用 D:/tools/ags36/acwin.exe 启动原版归档
        launcher_written = False
        if archive_info is not None and not archive_info.unicode_native:
            launcher_written = self._write_launcher_bat(base, archive_info, undo)

        report.applied = applied
        report.undo_path = undo.finish()
        report.notes.append(f"已生成 {out_tra_name}（{applied} 条，含 UTF-8 扩展声明），并更新 {cfg_name} 指向 translation={OUTPUT_TRA_NAME}。")
        if patched_fonts:
            report.notes.append(f"已铺入外置中文字体覆盖：{', '.join(patched_fonts[:4])}{' 等' if len(patched_fonts) > 4 else ''}（共 {len(patched_fonts)} 个槽位）。")
        if launcher_written:
            ags_dep = deps.REGISTRY.get("ags36")
            ags_exe = ags_dep.locate() if ags_dep else None
            if ags_exe is not None:
                report.notes.append(f"已生成「{LAUNCHER_BAT_NAME}」（调用 {ags_exe}），直接双击即可玩中文版。")
            else:
                report.notes.append(
                    f"已生成「{LAUNCHER_BAT_NAME}」，但本机尚未安装 AGS 3.6+ 运行时；"
                    f"请在摆渡弹窗确认下载或手动放入 D:\\tools\\ags36\\acwin.exe 后双击启动。"
                )
        return report

    def _font_slots(self, archive_info: AgsArchiveInfo | None) -> list[str]:
        """计算需要铺入的外置 `agsfntN.ttf` 槽位列表。"""
        indices: set[int] = set()
        if archive_info is not None:
            for fname in (*archive_info.ttf_fonts, *archive_info.wfn_fonts):
                m = re.match(r"^agsfnt(\d+)\.(?:ttf|wfn)$", fname, re.IGNORECASE)
                if m:
                    indices.add(int(m.group(1)))
        if not indices:
            indices = {0, 1, 2}
        return [f"agsfnt{i}.ttf" for i in sorted(indices)]

    def _inject_cjk_fonts(
        self,
        base: Path,
        archive_info: AgsArchiveInfo | None,
        undo: UndoLog,
    ) -> list[str]:
        """向游戏目录铺入完整中文字体 `agsfnt*.ttf`（NTFS 下自动硬链接到同一份 `ferry_cjk.ttf` 以省空间）。"""
        try:
            from ..fontpatch import pick_cjk_font

            cjk_font, _family = pick_cjk_font(require_ttf=True)
        except Exception:
            cjk_font = None
        if cjk_font is None or not cjk_font.is_file():
            return []

        slots = self._font_slots(archive_info)
        written: list[str] = []

        # 先落一份单一基准 TTF，再用 NTFS 硬链接挂出多个 agsfntN.ttf（省下 7/8 磁盘占用）
        base_font_path = base / CJK_FONT_BASE_NAME
        if base_font_path.exists():
            undo.will_modify(CJK_FONT_BASE_NAME)
        else:
            undo.will_add(CJK_FONT_BASE_NAME)
        shutil.copy2(cjk_font, base_font_path)
        undo.did_write(CJK_FONT_BASE_NAME)

        for slot in slots:
            slot_path = base / slot
            if slot_path.exists():
                undo.will_modify(slot)
                slot_path.unlink(missing_ok=True)
            else:
                undo.will_add(slot)
            try:
                os.link(base_font_path, slot_path)
            except OSError:
                shutil.copy2(base_font_path, slot_path)
            undo.did_write(slot)
            written.append(slot)
        return written

    def _write_launcher_bat(
        self,
        base: Path,
        archive_info: AgsArchiveInfo,
        undo: UndoLog,
    ) -> bool:
        """为旧版 AGS 游戏生成 `启动中文版.bat`，免改原 exe 直接由 AGS 3.6+ 拉起中文版。"""
        ags_dep = deps.REGISTRY.get("ags36")
        ags_exe = ags_dep.locate() if ags_dep else None
        default_exe = str(ags_exe) if ags_exe is not None else str((ags_dep.target / "acwin.exe") if ags_dep else Path(r"D:\tools\ags36\acwin.exe"))
        game_file = archive_info.archive_file or ""
        target_arg = f'"%~dp0{game_file}"' if game_file else '"%~dp0."'

        bat_content = (
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            f'set "AGS36={default_exe}"\r\n'
            'if not exist "%AGS36%" set "AGS36=D:\\tools\\ags36\\acwin.exe"\r\n'
            'if not exist "%AGS36%" (\r\n'
            '    echo 未找到 AGS 3.6+ 兼容运行时："%AGS36%"\r\n'
            "    echo 请在摆渡计划中扫描本游戏目录并确认下载「AGS 3.6+ 中文兼容运行时」。\r\n"
            "    pause\r\n"
            "    exit /b 1\r\n"
            ")\r\n"
            'pushd "%~dp0"\r\n'
            f'start "" "%AGS36%" --localuserconf --translation {OUTPUT_TRA_NAME} {target_arg}\r\n'
            "popd\r\n"
        )
        bat_path = base / LAUNCHER_BAT_NAME
        if bat_path.exists():
            undo.will_modify(LAUNCHER_BAT_NAME)
        else:
            undo.will_add(LAUNCHER_BAT_NAME)
        bat_path.write_bytes(bat_content.encode("utf-8"))
        undo.did_write(LAUNCHER_BAT_NAME)
        return True
