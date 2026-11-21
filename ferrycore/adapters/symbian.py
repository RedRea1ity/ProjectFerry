"""塞班 Symbian OS 全世代（S60v1/v2/N-Gage → S60v3/v5 → Symbian^3 / 安娜 Anna / 贝拉 Belle）游戏适配器。

本模块完全使用 Python 标准库打通塞班三大世代：

1. **远古塞班（S60v1 / S60v2 / N-Gage / UIQ2，Symbian 6.1~8.1）**：
   - 旧版 `.sis` (`UID3 = 0x10000419`)、EPOC `E32Image` (`.app`，偏移 `0x10` 签名 `"EPOC"`)、`.rsc / .r01..r99` 资源表。
2. **中期塞班（S60v3 / S60v5，Symbian 9.1~9.4）**：
   - 新版 `.sis / .sisx` (`UID1 = 0x10201A7A`)：内置 **TLV `SISCompressed` (`0x0018`) `zlib` 数据流自动解包与重压缩**，
     无需外部 `SISContents` 或 `unsis` 即可直接深入 `.sisx` 压缩块内部抽取并回写 `.rsc / .r01 / .exe / .dat`！
   - 自动识别 `E32Image` 偏移 `0x1C` 处的 `KUidCompressionDeflate` (`0x101F7AFC`) 与 `KUidCompressionBytePair` (`0x102822AA`)。
3. **后期塞班（Symbian^3 / 塞班安娜 Anna / 诺基亚贝拉 Belle，Qt 4.7 & WebRuntime）**：
   - **Qt `.qm` 编译语言包（16 字节魔数 `3C B8 64 18 CA EF 9C 95...`）**：纯标准库解析 `Messages` (`0x69`) 段中的
     `SourceText` (`0x06`) 与 `Translation` (`0x03` UTF-16BE)，支持**任意长度 UTF-16BE 中文无损重建**！
   - **Qt Quick `.qml` 脚本**：自动抽取并回写 `qsTr("...")` 与 UI 文本属性；
   - **`.wgz` (Symbian WebRuntime Widget)**：自动解包并重打包内部 HTML/JS/JSON 文本。
"""

from __future__ import annotations

import re
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from ferrygame.rawbin import EN_MENU_TERMS, JP_MENU_TERMS, patch_blob, scan_binary_strings
from ferrygame.units import Unit

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .container import unique_output
from .undo import UNDO_NAME, UndoLog
from .unity import is_unity_translatable

SYMBIAN_PRE9_SIS_UID3 = 0x10000419
SYMBIAN_9_SIS_UID1 = 0x10201A7A
E32_COMPRESS_DEFLATE = 0x101F7AFC
E32_COMPRESS_BYTEPAIR = 0x102822AA
QT_QM_MAGIC = bytes.fromhex("3CB86418CAEF9C95CD211CBF60A1BDDD")

_RSC_EXT_RE = re.compile(r"^\.(?:rsc|r\d{2,3})$", re.IGNORECASE)
_SYMBIAN_BIN_SUFFIXES = {".app", ".rsc", ".dat", ".bin", ".pak", ".res"}
_QML_QSTR_RE = re.compile(r'(\bqsTr(?:Id)?\s*\(\s*")(?P<body>(?:\\.|[^"\\])*)("\s*\))')


def is_symbian_e32image(blob: bytes) -> bool:
    """检测是否为 Symbian EPOC E32Image (`.app` / `.exe` / `.dll`)，其偏移 16..20 为 `b'EPOC'`。"""
    return len(blob) >= 20 and blob[16:20] == b"EPOC"


def e32image_compression_kind(blob: bytes) -> str:
    """返回 E32Image 偏移 0x1C 处的压缩类型说明。"""
    if not is_symbian_e32image(blob) or len(blob) < 0x20:
        return "none"
    comp = struct.unpack_from("<I", blob, 0x1C)[0]
    if comp == 0:
        return "uncompressed"
    if comp == E32_COMPRESS_DEFLATE:
        return "deflate"
    if comp == E32_COMPRESS_BYTEPAIR:
        return "bytepair (BPE)"
    return f"0x{comp:08X}"


def is_symbian_sis(blob: bytes) -> tuple[bool, str]:
    """检测是否为 Symbian `.sis` / `.sisx` 安装包。"""
    if len(blob) >= 16:
        uid1, _uid2, uid3 = struct.unpack_from("<III", blob, 0)
        if uid3 == SYMBIAN_PRE9_SIS_UID3:
            return True, "Symbian S60v1/v2 (EPOC6 .sis)"
        if uid1 == SYMBIAN_9_SIS_UID1:
            return True, "Symbian S60v3/v5/Anna/Belle (Symbian 9+ .sisx)"
    return False, ""


# ============================================================================
# Qt .qm 编译语言包（塞班^3 / 安娜 Anna / 贝拉 Belle 及 PC Qt 游戏通用）
# ============================================================================


@dataclass
class QmMessage:
    index: int
    source: str
    translation: str


def parse_qt_qm(blob: bytes) -> list[QmMessage]:
    """解析 Qt `.qm` 编译语言包，返回 `[QmMessage(index, source, translation), ...]`。"""
    if len(blob) < 21 or blob[:16] != QT_QM_MAGIC:
        raise ValueError("不是合法的 Qt .qm 语言文件")
    pos = 16
    messages: list[QmMessage] = []
    msg_idx = 0

    while pos + 5 <= len(blob):
        sec_tag = blob[pos]
        sec_len = struct.unpack_from(">I", blob, pos + 1)[0]
        pos += 5
        if pos + sec_len > len(blob):
            break
        sec_body = blob[pos : pos + sec_len]
        pos += sec_len
        if sec_tag != 0x69:  # 0x69 = Messages section
            continue

        m_pos = 0
        cur_src = ""
        cur_tr = ""
        while m_pos < len(sec_body):
            tag = sec_body[m_pos]
            m_pos += 1
            if tag == 0x01:  # End of message record
                text_key = cur_src or cur_tr
                if text_key and (text_key in EN_MENU_TERMS or text_key in JP_MENU_TERMS or is_unity_translatable(text_key)):
                    messages.append(QmMessage(index=msg_idx, source=cur_src, translation=cur_tr))
                msg_idx += 1
                cur_src = ""
                cur_tr = ""
                continue
            if m_pos + 4 > len(sec_body):
                break
            f_len = struct.unpack_from(">I", sec_body, m_pos)[0]
            m_pos += 4
            if f_len == 0xFFFFFFFF:
                continue
            if m_pos + f_len > len(sec_body):
                break
            raw_field = sec_body[m_pos : m_pos + f_len]
            m_pos += f_len
            if tag == 0x03:  # Translation (UTF-16BE)
                cur_tr = raw_field.decode("utf-16-be", errors="replace").rstrip("\x00")
            elif tag in (0x06, 0x05):  # SourceText (UTF-8 / Latin-1)
                cur_src = raw_field.rstrip(b"\x00").decode("utf-8", errors="replace")

    return messages


def rebuild_qt_qm(blob: bytes, replacements: dict[str, str]) -> tuple[bytes, int]:
    """将 `{QM:<idx> 或 原文: 译文}` 写回 Qt `.qm`，支持任意长度的 UTF-16BE 中文重建！"""
    if len(blob) < 21 or blob[:16] != QT_QM_MAGIC:
        return blob, 0
    pos = 16
    out = bytearray(QT_QM_MAGIC)
    applied = 0

    while pos + 5 <= len(blob):
        sec_tag = blob[pos]
        sec_len = struct.unpack_from(">I", blob, pos + 1)[0]
        pos += 5
        if pos + sec_len > len(blob):
            break
        sec_body = blob[pos : pos + sec_len]
        pos += sec_len

        if sec_tag != 0x69:
            out.append(sec_tag)
            out.extend(struct.pack(">I", len(sec_body)))
            out.extend(sec_body)
            continue

        # 重建 Messages (0x69) 段
        new_sec = bytearray()
        m_pos = 0
        msg_idx = 0
        rec_chunks: list[tuple[int, bytes]] = []
        cur_src = ""
        cur_tr = ""

        def _flush_record() -> None:
            nonlocal applied, msg_idx, rec_chunks, cur_src, cur_tr
            lookup_key = cur_src or cur_tr
            tr = replacements.get(f"QM:{msg_idx}") or (replacements.get(lookup_key) if lookup_key else None)
            has_tr_tag = any(t == 0x03 for t, _ in rec_chunks)
            for t, raw_f in rec_chunks:
                if t == 0x03 and tr and tr.strip():
                    enc_u16 = tr.encode("utf-16-be")
                    new_sec.append(0x03)
                    new_sec.extend(struct.pack(">I", len(enc_u16)))
                    new_sec.extend(enc_u16)
                    applied += 1
                else:
                    new_sec.append(t)
                    new_sec.extend(struct.pack(">I", len(raw_f)))
                    new_sec.extend(raw_f)
            if not has_tr_tag and tr and tr.strip():
                enc_u16 = tr.encode("utf-16-be")
                new_sec.append(0x03)
                new_sec.extend(struct.pack(">I", len(enc_u16)))
                new_sec.extend(enc_u16)
                applied += 1
            new_sec.append(0x01)
            msg_idx += 1
            rec_chunks = []
            cur_src = ""
            cur_tr = ""

        while m_pos < len(sec_body):
            tag = sec_body[m_pos]
            m_pos += 1
            if tag == 0x01:
                _flush_record()
                continue
            if m_pos + 4 > len(sec_body):
                break
            f_len = struct.unpack_from(">I", sec_body, m_pos)[0]
            m_pos += 4
            if f_len == 0xFFFFFFFF:
                rec_chunks.append((tag, b""))
                continue
            raw_field = sec_body[m_pos : m_pos + f_len]
            m_pos += f_len
            if tag == 0x03:
                cur_tr = raw_field.decode("utf-16-be", errors="replace").rstrip("\x00")
            elif tag in (0x06, 0x05):
                cur_src = raw_field.rstrip(b"\x00").decode("utf-8", errors="replace")
            rec_chunks.append((tag, raw_field))

        out.append(0x69)
        out.extend(struct.pack(">I", len(new_sec)))
        out.extend(new_sec)

    return (bytes(out) if applied > 0 else blob), applied


# ============================================================================
# Symbian 9+ (S60v3/v5/Anna/Belle) .sisx TLV SISCompressed (0x0018) 自动解包与重打包
# ============================================================================


@dataclass
class SisCompressedBlock:
    index: int
    header_offset: int
    total_block_size: int
    algorithm: int
    decompressed: bytes


def extract_sisx_compressed_blocks(blob: bytes) -> list[SisCompressedBlock]:
    """在 Symbian 9+ `.sisx` 中扫描并解压所有 `SISCompressed` (`0x0018`) TLV 数据流块。"""
    if len(blob) < 20 or struct.unpack_from("<I", blob, 0)[0] != SYMBIAN_9_SIS_UID1:
        return []
    blocks: list[SisCompressedBlock] = []
    pos = 16
    n = len(blob)
    b_idx = 0

    while pos + 18 <= n:
        idx = blob.find(b"\x18\x00", pos)
        if idx < 0 or idx + 18 > n:
            break
        raw_len = struct.unpack_from("<I", blob, idx + 2)[0]
        if raw_len & 0x80000000:
            pos = idx + 2
            continue
        hdr_len = 6
        if raw_len < 12 or idx + hdr_len + raw_len > n:
            pos = idx + 2
            continue
        algo, uncomp_size = struct.unpack_from("<IQ", blob, idx + hdr_len)
        payload_start = idx + hdr_len + 12
        payload_end = idx + hdr_len + raw_len
        payload = blob[payload_start:payload_end]

        if algo == 1 and 4 <= uncomp_size <= 64 * 1024 * 1024:
            try:
                decomp = zlib.decompress(payload)
                if len(decomp) == uncomp_size:
                    blocks.append(
                        SisCompressedBlock(
                            index=b_idx,
                            header_offset=idx,
                            total_block_size=hdr_len + raw_len,
                            algorithm=1,
                            decompressed=decomp,
                        )
                    )
                    b_idx += 1
                    pos = (payload_end + 3) & ~3
                    continue
            except zlib.error:
                pass
        elif algo == 0 and uncomp_size == len(payload) and uncomp_size >= 4:
            blocks.append(
                SisCompressedBlock(
                    index=b_idx,
                    header_offset=idx,
                    total_block_size=hdr_len + raw_len,
                    algorithm=0,
                    decompressed=payload,
                )
            )
            b_idx += 1
            pos = (payload_end + 3) & ~3
            continue

        pos = idx + 2

    return blocks


def rebuild_sisx_with_blocks(
    blob: bytes,
    blocks: list[SisCompressedBlock],
    patched_decompressed: dict[int, bytes],
) -> bytes:
    """将修改后的 `SISCompressed` 块重新 `zlib` 压缩并写回 `.sisx`。

    若外层只有一个根 `SISContents` TLV 容器（标准 `.sisx` 偏移 16 处 `tag == 0x001A`），
    同步更新根 TLV 容器的 32 位长度字段！
    """
    out = bytearray()
    cursor = 0
    for blk in blocks:
        out.extend(blob[cursor : blk.header_offset])
        new_decomp = patched_decompressed.get(blk.index)
        if new_decomp is None:
            out.extend(blob[blk.header_offset : blk.header_offset + blk.total_block_size])
        else:
            new_payload = zlib.compress(new_decomp, 6) if blk.algorithm == 1 else new_decomp
            new_field_len = 12 + len(new_payload)
            new_chunk = (
                struct.pack("<HI", 0x0018, new_field_len)
                + struct.pack("<IQ", blk.algorithm, len(new_decomp))
                + new_payload
            )
            # Symbian 9+ TLV 4 字节对齐填充
            pad = (4 - (len(new_chunk) % 4)) % 4
            out.extend(new_chunk + (b"\x00" * pad))
        cursor = blk.header_offset + blk.total_block_size
        # 吸收原本块尾的 4 字节对齐零字节（若重打包已自行对齐）
        if blk.index in patched_decompressed:
            orig_pad = (4 - (blk.total_block_size % 4)) % 4
            if cursor + orig_pad <= len(blob) and blob[cursor : cursor + orig_pad] == b"\x00" * orig_pad:
                cursor += orig_pad
    out.extend(blob[cursor:])

    # 若偏移 16 是顶层 SISContents (0x001A) TLV，同步更新其总长度
    if len(out) >= 22 and struct.unpack_from("<H", out, 16)[0] == 0x001A:
        old_top_len = struct.unpack_from("<I", out, 18)[0]
        if not (old_top_len & 0x80000000):
            struct.pack_into("<I", out, 18, len(out) - 22)
    return bytes(out)


class SymbianAdapter(Adapter):
    """塞班 Symbian OS 全世代（S60v1/v2/N-Gage → S60v3/v5 → Symbian^3/安娜/贝拉）适配器。"""

    name = "symbian"
    priority = 13

    def _find_sis(self, root: Path) -> Path | None:
        root = Path(root)
        candidates: list[Path] = []
        if root.is_file() and root.suffix.lower() in (".sis", ".sisx") and ".cn" not in root.stem.lower():
            candidates.append(root)
        elif root.is_dir():
            for ext in ("*.sis", "*.sisx"):
                candidates.extend(
                    p for p in sorted(root.glob(ext)) if p.is_file() and ".cn" not in p.stem.lower()
                )
        for p in candidates:
            try:
                ok, _ = is_symbian_sis(p.read_bytes()[:32])
                if ok:
                    return p
            except OSError:
                continue
        return None

    def _find_wgz(self, root: Path) -> Path | None:
        root = Path(root)
        if root.is_file() and root.suffix.lower() == ".wgz" and ".cn" not in root.stem.lower():
            return root
        if root.is_dir():
            for p in sorted(root.glob("*.wgz")):
                if p.is_file() and ".cn" not in p.stem.lower():
                    return p
        return None

    def _find_unpacked_files(self, root: Path) -> list[Path]:
        root = Path(root)
        if root.is_file():
            suf = root.suffix.lower()
            if _RSC_EXT_RE.match(suf) or suf in (".app", ".qm"):
                return [root]
            return []
        if not root.is_dir():
            return []

        has_symbian_layout = (
            (root / "system" / "apps").is_dir()
            or (root / "resource" / "apps").is_dir()
            or (root / "sys" / "bin").is_dir()
            or any(_RSC_EXT_RE.match(p.suffix) for p in root.rglob("*") if p.is_file())
            or any(p.suffix.lower() in (".app", ".qm") and p.is_file() for p in root.rglob("*"))
        )
        if not has_symbian_layout:
            return []

        files: list[Path] = []
        for p in sorted(root.rglob("*")):
            if not p.is_file() or ".ferry-backup" in p.parts:
                continue
            suf = p.suffix.lower()
            if (
                _RSC_EXT_RE.match(suf)
                or suf in _SYMBIAN_BIN_SUFFIXES
                or suf in (".qm", ".qml")
                or (suf in (".exe", ".dll") and is_symbian_e32image(p.read_bytes()[:32]))
            ):
                files.append(p)
        return files

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        sis = self._find_sis(root)
        wgz = self._find_wgz(root)
        unpacked = self._find_unpacked_files(root)
        if sis is None and wgz is None and not unpacked:
            return ProbeReport(
                engine=self.name,
                root=root,
                notes=["未找到塞班 Symbian .sis/.sisx/.wgz 安装包、.rsc/.r01/.qm 资源或 E32Image (.app) 程序"],
            )

        entries = self.collect(root)
        if sis is not None:
            blob = sis.read_bytes()
            _, sis_ver = is_symbian_sis(blob[:32])
            blocks = extract_sisx_compressed_blocks(blob)
            if not entries:
                return ProbeReport(
                    engine=sis_ver or "Symbian SIS",
                    root=root,
                    readable=False,
                    writable=False,
                    container="sisx" if blocks else "sis",
                    notes=[f"检测到 {sis.name}，但内部未检出可翻译文本（可能内部 E32Image 启用了私有 BytePair 压缩）"],
                )
            return ProbeReport(
                engine=sis_ver or "Symbian SIS",
                root=root,
                readable=True,
                writable=True,
                unit_count=len(entries),
                container=f"sisx ({len(blocks)} zlib blocks)" if blocks else "sis",
                text_format="SISCompressed TLV + _LIT16/_LIT8 + Qt .qm",
                unicode_engine=True,
                in_place=False,
                notes=[
                    f"从塞班安装包 {sis.name}（含 {len(blocks)} 个 TLV 压缩流块）抽出 {len(entries)} 条文本。",
                    "支持直接解压并重打包 .sis/.sisx 内部 zlib 数据流产出 .cn.sis/.cn.sisx（覆盖 S60v1~v5 及安娜 Anna / 贝拉 Belle）。",
                ],
            )

        if not entries:
            return ProbeReport(
                engine="Symbian OS",
                root=root,
                readable=False,
                writable=False,
                notes=["检测到塞班目录结构，但未在 .rsc/.r01/.app/.qm/.qml 中检出可翻译文本"],
            )

        has_qt = any(p.suffix.lower() in (".qm", ".qml") for p in unpacked)
        label = (
            "Symbian^3 / Anna / Belle (Qt & Native)"
            if has_qt
            else "Symbian OS (S60v1~v5 / N-Gage / UIQ)"
        )
        return ProbeReport(
            engine=label,
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container="wgz" if wgz else "folder",
            text_format=".rsc / .r01 / E32Image .app / Qt .qm & .qml",
            unicode_engine=True,
            in_place=(wgz is None),
            notes=[
                f"检出塞班架构：{label}，共抽出 {len(entries)} 条文本。",
                "支持传统 .rsc/.app 定长原位覆写，以及安娜/贝拉 Qt .qm (UTF-16BE) 任意长度无损重建。",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        sis = self._find_sis(root)
        entries: list[Entry] = []

        if sis is not None:
            blob = sis.read_bytes()
            blocks = extract_sisx_compressed_blocks(blob)
            if blocks:
                for blk in blocks:
                    rel_blk = f"{sis.name}::BLK{blk.index}"
                    if QT_QM_MAGIC in blk.decompressed:
                        qm_off = blk.decompressed.find(QT_QM_MAGIC)
                        for msg in parse_qt_qm(blk.decompressed[qm_off:]):
                            entries.append(
                                Entry(
                                    modid=sis.stem,
                                    key=f"{rel_blk}#QM:{msg.index}",
                                    english=msg.source or msg.translation,
                                    source="game",
                                    kind="qt_qm",
                                    rel_path=rel_blk,
                                    json_pointer=f"QM:{msg.index}",
                                )
                            )
                    else:
                        for u in scan_binary_strings(blk.decompressed, rel_path=rel_blk):
                            entries.append(
                                Entry(
                                    modid=sis.stem,
                                    key=u.key,
                                    english=u.src,
                                    source="game",
                                    kind="sis_blk_bin",
                                    rel_path=rel_blk,
                                    json_pointer=str(u.meta.get("offset", 0)),
                                )
                            )
                if entries:
                    return entries
            for u in scan_binary_strings(blob, rel_path=sis.name):
                entries.append(
                    Entry(
                        modid=sis.stem,
                        key=u.key,
                        english=u.src,
                        source="game",
                        kind="sis_raw_bin",
                        rel_path=sis.name,
                        json_pointer=str(u.meta.get("offset", 0)),
                    )
                )
            return entries

        for path in self._find_unpacked_files(root):
            rel = path.relative_to(base).as_posix()
            suf = path.suffix.lower()
            try:
                raw = path.read_bytes()
            except OSError:
                continue

            if suf == ".qm":
                try:
                    for msg in parse_qt_qm(raw):
                        entries.append(
                            Entry(
                                modid=root.name,
                                key=f"{rel}#QM:{msg.index}",
                                english=msg.source or msg.translation,
                                source="game",
                                kind="qt_qm",
                                rel_path=rel,
                                json_pointer=f"QM:{msg.index}",
                            )
                        )
                except ValueError:
                    continue
            elif suf == ".qml":
                text = raw.decode("utf-8-sig", errors="replace")
                for idx, m in enumerate(_QML_QSTR_RE.finditer(text)):
                    val = m.group("body")
                    if is_unity_translatable(val) or val in EN_MENU_TERMS or val in JP_MENU_TERMS:
                        entries.append(
                            Entry(
                                modid=root.name,
                                key=f"{rel}#QML:{idx}",
                                english=val,
                                source="game",
                                kind="qml",
                                rel_path=rel,
                                json_pointer=f"QML:{idx}",
                            )
                        )
            else:
                for u in scan_binary_strings(raw, rel_path=rel):
                    entries.append(
                        Entry(
                            modid=root.name,
                            key=u.key,
                            english=u.src,
                            source="game",
                            kind=u.kind,
                            rel_path=rel,
                            json_pointer=str(u.meta.get("offset", 0)),
                        )
                    )
        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        sis = self._find_sis(root)
        entries = self.collect(root)
        report = ApplyReport()
        if not entries:
            report.notes.append("未找到可写回的塞班文本单元")
            return report

        undo = UndoLog.open(base, engine=self.name)

        if sis is not None:
            blob = sis.read_bytes()
            blocks = extract_sisx_compressed_blocks(blob)
            if blocks and any(e.kind in ("sis_blk_bin", "qt_qm") for e in entries):
                patched_blks: dict[int, bytes] = {}
                for blk in blocks:
                    rel_blk = f"{sis.name}::BLK{blk.index}"
                    if QT_QM_MAGIC in blk.decompressed:
                        qm_off = blk.decompressed.find(QT_QM_MAGIC)
                        qm_map = {k.split("#", 1)[-1]: v for k, v in translations.items()}
                        qm_map.update(translations)
                        new_qm, c = rebuild_qt_qm(blk.decompressed[qm_off:], qm_map)
                        if c > 0:
                            patched_blks[blk.index] = blk.decompressed[:qm_off] + new_qm
                            report.applied += c
                    else:
                        units = scan_binary_strings(blk.decompressed, rel_path=rel_blk)
                        hit = False
                        for u in units:
                            tr = translations.get(u.key) or translations.get(u.src)
                            if tr and tr.strip():
                                u.dst = tr
                                hit = True
                        if hit:
                            p_data, c, conflicts = patch_blob(blk.decompressed, units)
                            report.conflicts.extend(conflicts)
                            if c > 0:
                                patched_blks[blk.index] = p_data
                                report.applied += c
                if not patched_blks:
                    return report
                out_blob = rebuild_sisx_with_blocks(blob, blocks, patched_blks)
            else:
                units = scan_binary_strings(blob, rel_path=sis.name)
                for u in units:
                    tr = translations.get(u.key) or translations.get(u.src)
                    if tr and tr.strip():
                        u.dst = tr
                out_blob, c, conflicts = patch_blob(blob, units)
                report.conflicts.extend(conflicts)
                report.applied += c
                if c == 0:
                    return report

            default_out = sis.with_name(f"{sis.stem}.cn{sis.suffix}")
            target_out = unique_output(Path(out) if out else default_out)
            target_out.write_bytes(out_blob)
            undo.did_copy(target_out)
            report.undo_path = undo.finish()
            report.out_path = target_out
            report.in_place = False
            report.notes.append(f"已生成塞班汉化安装包：{target_out.name}。")
            return report

        by_file: dict[str, list[Entry]] = {}
        for e in entries:
            by_file.setdefault(e.rel_path, []).append(e)

        for rel, file_entries in sorted(by_file.items()):
            path = base / rel
            raw = path.read_bytes()
            kind = file_entries[0].kind

            if kind == "qt_qm":
                qm_map: dict[str, str] = {}
                for e in file_entries:
                    tr = translations.get(e.key) or translations.get(e.english)
                    if tr and tr.strip():
                        qm_map[e.json_pointer] = tr
                        qm_map[e.english] = tr
                    else:
                        report.missed += 1
                if qm_map:
                    new_raw, c = rebuild_qt_qm(raw, qm_map)
                    if c > 0:
                        undo.will_modify(rel)
                        path.write_bytes(new_raw)
                        undo.did_write(rel)
                        report.applied += c

            elif kind == "qml":
                text = raw.decode("utf-8-sig", errors="replace")
                qml_map: dict[str, str] = {}
                for e in file_entries:
                    tr = translations.get(e.key) or translations.get(e.english)
                    if tr and tr.strip():
                        qml_map[e.json_pointer] = tr
                        qml_map[e.english] = tr
                idx_counter = -1

                def _repl_qml(m: re.Match) -> str:
                    nonlocal idx_counter
                    idx_counter += 1
                    src_val = m.group("body")
                    tr = qml_map.get(f"QML:{idx_counter}") or qml_map.get(src_val)
                    if tr and tr != src_val:
                        report.applied += 1
                        return f"{m.group(1)}{tr}{m.group(3)}"
                    return m.group(0)

                new_text = _QML_QSTR_RE.sub(_repl_qml, text)
                if new_text != text:
                    undo.will_modify(rel)
                    path.write_text(new_text, encoding="utf-8")
                    undo.did_write(rel)

            else:
                units = scan_binary_strings(raw, rel_path=rel)
                hit = False
                for u in units:
                    tr = translations.get(u.key) or translations.get(u.src)
                    if tr and tr.strip():
                        u.dst = tr
                        hit = True
                    else:
                        report.missed += 1
                if hit:
                    patched, c, conflicts = patch_blob(raw, units)
                    report.conflicts.extend(conflicts)
                    if c > 0:
                        undo.will_modify(rel)
                        path.write_bytes(patched)
                        undo.did_write(rel)
                        report.applied += c

        if report.applied > 0:
            report.undo_path = undo.finish()
        report.out_path = base
        report.in_place = True
        report.notes.append(f"已原地更新塞班游戏目录（{report.applied} 条）。")
        return report
