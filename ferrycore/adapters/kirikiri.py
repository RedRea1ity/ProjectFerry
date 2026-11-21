"""Kirikiri 2 / Kirikiri Z（吉里吉里，.xp3 归档 + KAG .ks 剧本）适配器。

吉里吉里是视觉小说 / Galgame 第一大主流引擎。它的两条核心机制：

1. **容器是 `.xp3`（`XP3\\r\\n \\n\\x1a\\x8b\\x67\\x01` 11 字节魔数）**：
   文件末尾（或头部指针指向的位置）有一张索引表（明文或 zlib 压缩），由若干 `File` chunk
   组成，每个 `File` 下含 `info`（文件名、原始大小）、`segm`（段偏移与长度）、`adlr`（Adler32 校验）。
   本模块纯标准库解析并重建 `.xp3`（对带自定义 tpm/cxdec 加密滤镜的 xp3 则在 `probe` 阶段
   通过试读剧本字符可读性提前识别，读不出明文绝不瞎改）。

2. **编码破壁：一律以 UTF-16LE with BOM (`0xFF 0xFE`) 回写剧本**：
   哪怕原版 `.ks` 是 Shift-JIS，吉里吉里 2/Z 内核只要看到文件头是 `FF FE`，就会自动切换为
   Unicode 宽字符解析 —— **不需要编任何 DLL 代理、也不需要转区**，简体中文直接原生渲染！
"""

from __future__ import annotations

import io
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .tyrano import _ATTR, _replace_attr, has_translatable
from .undo import UNDO_NAME, UndoLog

XP3_MAGIC = b"XP3\r\n \n\x1a\x8b\x67\x01"
_KRKR_SCRIPT_BEGIN = re.compile(r"^\s*(?:\[iscript\b|@iscript\b)", re.I)
_KRKR_SCRIPT_END = re.compile(r"^\s*(?:\[endscript\b|@endscript\b)", re.I)
_KRKR_COMMENT = re.compile(r"^\s*(?:;|//)")
_KRKR_LABEL = re.compile(r"^(\s*\*[^|\r\n]+\|)(.+?)(\r?)$")
_KRKR_TAG_LINE = re.compile(r"^\s*(?:\[|@)")
_KRKR_TEXT_ATTRS = {"text", "title", "caption", "hint", "exp_text"}


@dataclass
class KsUnit:
    lineno: int
    kind: str
    text: str
    locator: str


def parse_ks(text: str) -> list[KsUnit]:
    """解析吉里吉里 KAG `.ks` 剧本，抽取标签标题 `*label|标题`、指令属性与正文行。"""
    units: list[KsUnit] = []
    in_script = False
    for index, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if not line or _KRKR_COMMENT.match(line):
            continue
        if _KRKR_SCRIPT_BEGIN.match(line):
            in_script = True
            continue
        if _KRKR_SCRIPT_END.match(line):
            in_script = False
            continue
        if in_script:
            continue

        m_label = _KRKR_LABEL.match(raw)
        if m_label:
            title = m_label.group(2).strip()
            if has_translatable(title):
                units.append(KsUnit(index, "label", title, f"#{index}#label"))
            continue

        if _KRKR_TAG_LINE.match(line):
            seen: dict[str, int] = {}
            for match in _ATTR.finditer(line):
                attr = match.group(1).lower()
                if attr not in _KRKR_TEXT_ATTRS:
                    continue
                order = seen.get(attr, 0)
                seen[attr] = order + 1
                val = match.group(2)
                if has_translatable(val):
                    units.append(KsUnit(index, "choice", val, f"#{index}#{attr}#{order}"))
            continue

        if not line.startswith("*") and has_translatable(line):
            units.append(KsUnit(index, "dialogue", line, f"#{index}#-1"))
    return units


def apply_ks(text: str, mapping: dict[str, str]) -> tuple[str, int, list[str]]:
    """把译文写回吉里吉里 `.ks` 剧本，支持按 `#行号#定位` 或按原文直接匹配。"""
    lines = text.split("\n")
    applied = 0
    conflicts: list[str] = []
    for unit in parse_ks(text):
        tr = mapping.get(unit.locator) or mapping.get(unit.text)
        if not tr or not tr.strip():
            continue
        raw_line = lines[unit.lineno - 1]
        trailing_cr = "\r" if raw_line.endswith("\r") else ""
        clean_line = raw_line[:-1] if trailing_cr else raw_line
        if unit.kind == "label":
            m_label = _KRKR_LABEL.match(raw_line)
            if m_label:
                lines[unit.lineno - 1] = f"{m_label.group(1)}{tr}{m_label.group(3)}"
                applied += 1
        elif unit.locator.endswith("#-1"):
            if "\n" in tr or "\r" in tr:
                conflicts.append(f"{unit.locator}：译文含换行")
                continue
            indent = clean_line[: len(clean_line) - len(clean_line.lstrip())]
            lines[unit.lineno - 1] = indent + tr + trailing_cr
            applied += 1
        else:
            if '"' in tr or "\n" in tr:
                conflicts.append(f"{unit.locator}：属性译文含引号或换行")
                continue
            parts = unit.locator.lstrip("#").split("#")
            attr, order = parts[1], int(parts[2])
            try:
                lines[unit.lineno - 1] = _replace_attr(clean_line, attr, order, tr) + trailing_cr
                applied += 1
            except ValueError as exc:
                conflicts.append(f"{unit.locator}：{exc}")
    return "\n".join(lines), applied, conflicts


@dataclass
class Xp3Segment:
    compressed: bool
    offset: int
    orig_size: int
    arc_size: int


@dataclass
class Xp3Entry:
    name: str
    flags: int
    orig_size: int
    arc_size: int
    adler32: int
    segments: list[Xp3Segment]


def _iter_xp3_chunks(buf: bytes):
    pos = 0
    n = len(buf)
    while pos + 12 <= n:
        tag = buf[pos : pos + 4]
        size = struct.unpack_from("<Q", buf, pos + 4)[0]
        body_start = pos + 12
        body_end = body_start + size
        if body_end > n:
            break
        yield tag, buf[body_start:body_end]
        pos = body_end


def parse_xp3(blob: bytes) -> list[Xp3Entry]:
    """解析标准 XP3 归档索引表。"""
    if len(blob) < 19 or blob[:11] != XP3_MAGIC:
        raise ValueError("不是 XP3 归档（魔数不匹配）")
    idx_off = struct.unpack_from("<Q", blob, 11)[0]
    # Kirikiri 2.23+ 在偏移 11 处可能放一个 0x17 标记（后跟版本号 + 真正的 8 字节索引偏移）
    if idx_off == 0x17 and len(blob) >= 32:
        idx_off = struct.unpack_from("<Q", blob, 23)[0]
    if idx_off >= len(blob):
        raise ValueError("XP3 索引偏移越界")

    flag = blob[idx_off]
    if flag == 1:
        if idx_off + 17 > len(blob):
            raise ValueError("XP3 压缩索引头不完整")
        _comp_size, _uncomp_size = struct.unpack_from("<QQ", blob, idx_off + 1)
        idx_data = zlib.decompress(blob[idx_off + 17 :])
    elif flag == 0:
        if idx_off + 9 > len(blob):
            raise ValueError("XP3 未压缩索引头不完整")
        uncomp_size = struct.unpack_from("<Q", blob, idx_off + 1)[0]
        idx_data = blob[idx_off + 9 : idx_off + 9 + uncomp_size]
    else:
        raise ValueError(f"未知的 XP3 索引标志：0x{flag:02x}")

    entries: list[Xp3Entry] = []
    for tag, body in _iter_xp3_chunks(idx_data):
        if tag != b"File":
            continue
        name = ""
        flags = 0
        orig_size = 0
        arc_size = 0
        adler = 0
        segs: list[Xp3Segment] = []
        for sub_tag, sub in _iter_xp3_chunks(body):
            if sub_tag == b"info" and len(sub) >= 22:
                flags, orig_size, arc_size, name_len = struct.unpack_from("<IQQH", sub, 0)
                raw_name = sub[22 : 22 + name_len * 2]
                name = raw_name.decode("utf-16-le", errors="replace").replace("\\", "/")
            elif sub_tag == b"segm":
                for s_off in range(0, len(sub) - 27, 28):
                    s_flags, start, s_orig, s_arc = struct.unpack_from("<IQQQ", sub, s_off)
                    segs.append(Xp3Segment(bool(s_flags & 1), start, s_orig, s_arc))
            elif sub_tag == b"adlr" and len(sub) >= 4:
                adler = struct.unpack_from("<I", sub, 0)[0]
        if name and segs:
            entries.append(Xp3Entry(name, flags, orig_size, arc_size, adler, segs))
    return entries


def read_xp3_file(blob: bytes, entry: Xp3Entry) -> bytes:
    """从 XP3 字节流中读取单个条目的解压后内容。"""
    if entry.flags & (1 << 31):
        raise ValueError(f"{entry.name} 带有 XP3 加密标志（需专用解密滤镜）")
    chunks: list[bytes] = []
    for seg in entry.segments:
        raw = blob[seg.offset : seg.offset + seg.arc_size]
        chunks.append(zlib.decompress(raw) if seg.compressed else raw)
    return b"".join(chunks)


def build_xp3(files: dict[str, bytes], compress: bool = True) -> bytes:
    """把 `{相对路径: 原始字节}` 打包成标准 XP3 归档。"""
    out = io.BytesIO()
    out.write(XP3_MAGIC)
    out.write(struct.pack("<Q", 0))  # 先占位索引偏移

    file_chunks: list[bytes] = []
    for name, raw in files.items():
        offset = out.tell()
        adler = zlib.adler32(raw) & 0xFFFFFFFF
        arc_data = zlib.compress(raw) if compress else raw
        is_comp = 1 if compress else 0
        out.write(arc_data)

        name_u16 = name.encode("utf-16-le")
        info_body = struct.pack("<IQQH", 0, len(raw), len(arc_data), len(name)) + name_u16 + b"\x00\x00"
        segm_body = struct.pack("<IQQQ", is_comp, offset, len(raw), len(arc_data))
        adlr_body = struct.pack("<I", adler)

        def _chunk(t: bytes, b: bytes) -> bytes:
            return t + struct.pack("<Q", len(b)) + b

        f_body = _chunk(b"info", info_body) + _chunk(b"segm", segm_body) + _chunk(b"adlr", adlr_body)
        file_chunks.append(_chunk(b"File", f_body))

    idx_raw = b"".join(file_chunks)
    idx_comp = zlib.compress(idx_raw)
    idx_offset = out.tell()
    out.write(b"\x01" + struct.pack("<QQ", len(idx_comp), len(idx_raw)) + idx_comp)

    result = bytearray(out.getvalue())
    struct.pack_into("<Q", result, 11, idx_offset)
    return bytes(result)


def _decode_ks(raw: bytes) -> str | None:
    if raw.startswith(b"\xff\xfe"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            return None
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            text = raw.decode(enc)
            # 简单防加密误判：控制字符占比过高说明被 XOR 混淆过
            bad = sum(1 for c in text[:500] if ord(c) < 0x20 and c not in "\r\n\t")
            if bad == 0:
                return text
        except UnicodeDecodeError:
            continue
    return None


_SKIP_KS_NAMES = {"macro.ks", "macros.ks", "system.ks", "config.ks", "init.ks"}
_CJK_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")


class KirikiriAdapter(Adapter):
    """吉里吉里 2 / Z 适配器（支持 .xp3 归档与散装 .ks/.txt 剧本）。"""

    name = "kirikiri"
    priority = 18  # 排在 Ren'Py 前、Tyrano 后（Tyrano 有 tyrano/ 标志）

    def __init__(self, include_samples: bool = False) -> None:
        self.include_samples = include_samples

    def _xp3_archives(self, root: Path) -> list[Path]:
        root = Path(root)
        if root.is_file() and root.suffix.lower() == ".xp3":
            return [root]
        if not root.is_dir():
            return []
        # 跳过摆渡自己生成的 patch.xp3 与备份
        return sorted(
            p for p in root.glob("*.xp3")
            if p.is_file() and p.name.lower() not in ("patch.xp3",) and not p.name.lower().endswith(".bak")
        )

    def _has_krkr_signature(self, root: Path) -> bool:
        root = Path(root)
        if self._xp3_archives(root):
            return True
        if not root.is_dir():
            return False
        names = {p.name.lower() for p in root.iterdir()}
        if names & {"krkr.eexe", "tvpwin32.exe", "krkrz.exe"}:
            return True
        # 散装 Kirikiri 目录（有 data/system/Initialize.tjs 或 Config.tjs，且没有 tyrano/）
        if not (root / "tyrano").is_dir() and (
            (root / "data" / "system" / "Initialize.tjs").is_file()
            or (root / "data" / "system" / "Config.tjs").is_file()
            or any(root.glob("*.tjs"))
        ):
            return True
        return False

    def _iter_scenarios(self, root: Path) -> list[tuple[str, str]]:
        """返回 [(标识路径, 剧本文本), ...]。"""
        root = Path(root).resolve()
        if not self._has_krkr_signature(root):
            return []
        out: list[tuple[str, str]] = []
        # 1. 先读 .xp3 归档里的 .ks / scenario/*.txt
        for xp3_path in self._xp3_archives(root):
            try:
                blob = xp3_path.read_bytes()
                entries = parse_xp3(blob)
            except Exception:
                continue
            for entry in entries:
                low = entry.name.lower()
                fname = Path(low).name
                if fname in _SKIP_KS_NAMES:
                    continue
                if not (low.endswith(".ks") or ("scenario" in low and low.endswith(".txt"))):
                    continue
                try:
                    raw = read_xp3_file(blob, entry)
                except Exception:
                    continue
                text = _decode_ks(raw)
                if text and _CJK_RE.search(text):
                    out.append((f"{xp3_path.name}::{entry.name}", text))
        # 2. 散装 .ks 文件
        base = root.parent if root.is_file() else root
        if base.is_dir() and not (base / "tyrano").is_dir():
            for ks_path in sorted(base.rglob("*.ks")):
                if ks_path.name.lower() in _SKIP_KS_NAMES:
                    continue
                if ".ferry-backup" in ks_path.parts:
                    continue
                try:
                    text = _decode_ks(ks_path.read_bytes())
                except OSError:
                    continue
                if text and _CJK_RE.search(text):
                    out.append((ks_path.relative_to(base).as_posix(), text))
        return out

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        if not self._has_krkr_signature(root):
            return ProbeReport(engine=self.name, root=root, notes=["未检测到 .xp3 归档或吉里吉里 .tjs 标志"])
        scenarios = self._iter_scenarios(root)
        if not scenarios:
            return ProbeReport(
                engine="Kirikiri 2 / Z（吉里吉里）",
                root=root,
                readable=False,
                writable=False,
                container="xp3",
                notes=["检测到 .xp3 归档，但剧本经过私有加密滤镜（tpm/cxdec），未解出明文 .ks"],
            )
        entries = self.collect(root)
        has_xp3 = any("::" in p for p, _ in scenarios)
        return ProbeReport(
            engine="Kirikiri 2 / Z（吉里吉里）",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container="xp3" if has_xp3 else "folder",
            text_format=".ks (UTF-16LE 回写)",
            unicode_engine=True,
            in_place=True,
            notes=[
                f"共读取 {len(scenarios)} 个剧本文件，抽出 {len(entries)} 条台词/选项",
                "回写采用 UTF-16LE with BOM 编码（并打包高优先级 patch.xp3 或原地替换），天然支持简体中文",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        entries: list[Entry] = []
        for rel, text in self._iter_scenarios(root):
            for unit in parse_ks(text):
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}{unit.locator}",
                        english=unit.text,
                        source="game",
                        kind=unit.kind,
                        rel_path=rel,
                        json_pointer=unit.locator,
                    )
                )
        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        scenarios = self._iter_scenarios(root)
        if not scenarios:
            return ApplyReport(notes=["未找到可回写的吉里吉里剧本"])

        report = ApplyReport(in_place=True, out_path=base)
        undo = UndoLog.open(base, engine=self.name)
        patch_files: dict[str, bytes] = {}

        for rel, text in scenarios:
            file_map: dict[str, str] = {}
            prefix = f"{rel}#"
            for k, v in translations.items():
                if k.startswith(prefix):
                    file_map["#" + k[len(prefix) :]] = v
                elif "#" not in k:
                    file_map[k] = v
            new_text, count, conflicts = apply_ks(text, file_map)
            report.conflicts.extend(f"{rel}{c}" for c in conflicts)
            if count == 0:
                continue
            # UTF-16LE with BOM：吉里吉里原生支持，彻底绕开 Shift-JIS 字库限制
            encoded = b"\xff\xfe" + new_text.encode("utf-16-le")
            if "::" in rel:
                _arc_name, inner_name = rel.split("::", 1)
                patch_files[inner_name] = encoded
                # 同时按吉里吉里平面检索惯例放一份裸文件名，兼容开/关目录层级的两类配置
                bare = Path(inner_name).name
                patch_files.setdefault(bare, encoded)
            else:
                target = base / rel
                undo.will_modify(rel)
                target.write_bytes(encoded)
                undo.did_write(rel)
            report.applied += count

        if patch_files:
            patch_xp3 = base / "patch.xp3"
            if patch_xp3.exists():
                undo.will_modify("patch.xp3")
            else:
                undo.will_add("patch.xp3")
            patch_xp3.write_bytes(build_xp3(patch_files))
            undo.did_write("patch.xp3")
            report.notes.append(f"已生成吉里吉里增量补丁包 → {patch_xp3.name}（含 {len(patch_files)} 个剧本条目）")

        if report.applied > 0:
            undo.finish()
            report.undo_path = base / UNDO_NAME
        else:
            candidate = base / UNDO_NAME
            if candidate.is_file():
                report.undo_path = candidate
        return report
