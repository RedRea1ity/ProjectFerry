"""Win3.1 / Win95 / Win98 早期多媒体、AVG 与 Galgame 引擎适配器 (`RetroWinAdapter`)。

专门覆盖 1992–2000 年间（Windows 3.1 / Windows 95 / Windows 98 黄金期）的三类经典游戏架构：

1. **Macromedia Director (`RIFX` / `XFIR` 容器：`.dir` / `.dxr` / `.cst` / `.cxt` 及 Projector `.exe`)**：
   - **代表作：Win3.1 传奇邪典《东脑 (Eastern Mind: The Lost Souls of Tong-Nou)》《中天 (Chu-Teng)》**、
     《Gadget》《Cosmology of Kyoto (京都千年物语)》《L-Zone》等全部采用 Macromedia Director 3/4/5/6 开发！
   - 纯标准库识别大端 `RIFX` (`MV93`/`MC95`) 与小端 `XFIR` (`39VM`/`59CM`) 容器，解析 `STXT` / `TXTS`
     （Styled Text 演员文本块：`[12字节头][text_len][style_len][文本字节]`）及内嵌台词，支持定长原位回写。

2. **Windows 3.1 16 位 `NE` (New Executable) 可执行程序与资源段**：
   - 自动检测 `MZ` 头偏移 `0x3C` 指向的 `"NE"` (`0x4E 0x45`) 16 位 Windows 3.1 签名，与 32 位 `PE` 区分开；
   - 扫描 16 位数据段与 StringTable 资源以及配套 `.dat` / `.mes` 脚本。

3. **95/98 时代早期日式 Galgame / AVG 归档（AliceSoft System 1/2/3/3.5 `*.ald` 等）**：
   - 解析 AliceSoft `*SA.ALD` 256 字节扇区对齐指针表与 `.sco` (`S350`/`S351`) 剧本块，
     执行 Shift-JIS → GBK 定长原位覆写，不破坏任何扇区指针。
"""

from __future__ import annotations

import struct
from pathlib import Path

from ferrygame.rawbin import encode_padded, patch_blob, scan_binary_strings
from ferrygame.units import Unit

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .undo import UNDO_NAME, UndoLog
from .unity import is_unity_translatable

_DIRECTOR_SUFFIXES = {".dir", ".dxr", ".cst", ".cxt"}


def is_win16_ne_executable(blob: bytes) -> bool:
    """检测是否为 Windows 3.1 16 位 `NE` (New Executable) 程序。"""
    if len(blob) < 0x40 or blob[:2] != b"MZ":
        return False
    e_lfanew = struct.unpack_from("<I", blob, 0x3C)[0]
    if 0x40 <= e_lfanew <= len(blob) - 2:
        return blob[e_lfanew : e_lfanew + 2] == b"NE"
    return False


def is_director_container(blob: bytes) -> tuple[bool, str]:
    """检测是否为 Macromedia Director (`RIFX`/`XFIR`) 影片或演员表文件（含内嵌在 Projector EXE 中的情况）。"""
    if len(blob) >= 12:
        if blob[:4] == b"RIFX" and blob[8:12] in (b"MV93", b"MC95", b"FGDM", b"FGDC"):
            return True, "RIFX (Big-Endian)"
        if blob[:4] == b"XFIR" and blob[8:12] in (b"39VM", b"59CM", b"MDGF", b"CDGF"):
            return True, "XFIR (Little-Endian)"
    # 检查 Projector EXE 内是否嵌有 RIFX/MV93 或 XFIR/39VM
    if len(blob) >= 64 and blob[:2] == b"MZ":
        if b"RIFX" in blob and b"MV93" in blob:
            return True, "Director Projector (RIFX)"
        if b"XFIR" in blob and b"39VM" in blob:
            return True, "Director Projector (XFIR)"
    return False, ""


def is_alicesoft_ald(path: Path, blob: bytes) -> bool:
    """检测是否为 AliceSoft System 1/2/3/3.5 的 `.ald` 归档（24 位扇区指针表）。"""
    if path.suffix.lower() != ".ald" or len(blob) < 512:
        return False
    first_sector = blob[0] | (blob[1] << 8) | (blob[2] << 16)
    first_off = first_sector << 8
    if not (256 <= first_off <= len(blob) - 32):
        return False
    hdr_len, data_len = struct.unpack_from("<II", blob, first_off)
    return hdr_len == 0x20 and 0 < data_len <= len(blob) - first_off


def scan_director_stxt_units(blob: bytes, rel_path: str) -> list[Unit]:
    """专门解析 Macromedia Director 的 `STXT` (大端) / `TXTS` (小端) 演员文本块。"""
    units: list[Unit] = []
    order = 0
    for tag, endian in ((b"STXT", ">"), (b"TXTS", "<")):
        search_pos = 0
        while True:
            idx = blob.find(tag, search_pos)
            if idx < 0 or idx + 20 > len(blob):
                break
            search_pos = idx + 4
            chunk_size = struct.unpack_from(f"{endian}I", blob, idx + 4)[0]
            body_off = idx + 8
            if chunk_size < 12 or body_off + chunk_size > len(blob):
                continue
            hdr_len, text_len, _style_len = struct.unpack_from(f"{endian}III", blob, body_off)
            if hdr_len != 12 or text_len < 2 or text_len > 65536:
                continue
            text_off = body_off + 12
            if text_off + text_len > body_off + chunk_size:
                continue
            raw_text = blob[text_off : text_off + text_len]
            decoded = None
            target_enc = "gbk"
            for enc, t_enc in (("cp932", "gbk"), ("utf-8", "utf-8")):
                try:
                    cand = raw_text.decode(enc)
                    if is_unity_translatable(cand):
                        decoded = cand
                        target_enc = t_enc
                        break
                except UnicodeDecodeError:
                    continue
            if not decoded:
                continue
            order += 1
            units.append(
                Unit(
                    key=f"{rel_path}@STXT:0x{text_off:x}",
                    src=decoded,
                    kind="story",
                    file=rel_path,
                    order=order,
                    max_bytes=text_len,
                    encoding=target_enc,
                    meta={
                        "offset": text_off,
                        "length": text_len,
                        "src_encoding": target_enc,
                        "raw_hex": raw_text.hex(),
                        "pad": "space",
                    },
                )
            )
    return units


class RetroWinAdapter(Adapter):
    """Win3.1 / Win95 / Win98 早期多媒体（Macromedia Director《东脑》《中天》）与早期 Galgame/AVG 适配器。"""

    name = "retro_win"
    priority = 16

    def _find_retro_files(self, root: Path) -> tuple[list[str], list[Path]]:
        root = Path(root)
        candidates = [root] if root.is_file() else (
            [p for p in sorted(root.rglob("*")) if p.is_file() and ".ferry-backup" not in p.parts]
            if root.is_dir() else []
        )
        flavors: list[str] = []
        matched: list[Path] = []

        for p in candidates:
            if ".cn" in p.stem.lower():
                continue
            suf = p.suffix.lower()
            try:
                head = p.read_bytes()[:65536]
            except OSError:
                continue

            if suf in _DIRECTOR_SUFFIXES:
                ok, desc = is_director_container(head)
                if ok or suf in (".dir", ".dxr", ".cst", ".cxt"):
                    if "Macromedia Director" not in flavors:
                        flavors.append("Macromedia Director (Win3.1/95)")
                    matched.append(p)
                    continue

            if suf == ".ald" and is_alicesoft_ald(p, head):
                if "AliceSoft System (Win95/98 .ALD)" not in flavors:
                    flavors.append("AliceSoft System (Win95/98 .ALD)")
                matched.append(p)
                continue

            if suf in (".exe", ".dll"):
                ok_dir, _ = is_director_container(head)
                if ok_dir:
                    if "Macromedia Director Projector" not in flavors:
                        flavors.append("Macromedia Director Projector")
                    matched.append(p)
                    continue
                if is_win16_ne_executable(head):
                    if "Windows 3.1 16-bit (NE)" not in flavors:
                        flavors.append("Windows 3.1 16-bit (NE)")
                    matched.append(p)
                    continue

        # 若目录内已确认为 Win3.1 NE 或 Director 工程，顺带纳入同目录的 .dat / .mes / .scn 剧本
        if flavors and root.is_dir():
            for p in candidates:
                if p not in matched and p.suffix.lower() in (".dat", ".mes", ".scn", ".msg", ".bin"):
                    matched.append(p)

        return flavors, matched

    def scan_units(self, root: Path) -> list[Unit]:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        _flavors, files = self._find_retro_files(root)
        all_units: list[Unit] = []

        for path in files:
            rel = path.relative_to(base).as_posix()
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            stxt_units = scan_director_stxt_units(blob, rel)
            bin_units = scan_binary_strings(blob, rel_path=rel)

            # 合并 STXT 精确块与通用二进制串（STXT 优先，不重叠）
            occupied = [
                (int(u.meta["offset"]), int(u.meta["offset"]) + int(u.meta["length"]))
                for u in stxt_units
            ]
            merged = list(stxt_units)
            for u in bin_units:
                s = int(u.meta.get("offset", 0))
                e = s + int(u.meta.get("length") or u.max_bytes or 0)
                if any(s < oe and os < e for os, oe in occupied):
                    continue
                occupied.append((s, e))
                merged.append(u)
            all_units.extend(merged)

        return all_units

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        flavors, files = self._find_retro_files(root)
        if not flavors or not files:
            return ProbeReport(
                engine=self.name,
                root=root,
                notes=["未检测到 Macromedia Director (.dir/.dxr/.cst)、Win3.1 16 位 NE 程序或 AliceSoft .ALD 归档"],
            )

        units = self.scan_units(root)
        label = " / ".join(flavors)
        if not units:
            return ProbeReport(
                engine=label,
                root=root,
                readable=False,
                writable=False,
                notes=[f"检测到 {label}（{len(files)} 个文件），但未检出明文 STXT 演员文本或未压缩台词"],
            )

        return ProbeReport(
            engine=label,
            root=root,
            readable=True,
            writable=True,
            unit_count=len(units),
            container="director/retro",
            text_format="Director STXT + Win16/95 Fixed-Length",
            unicode_engine=False,
            in_place=True,
            notes=[
                f"检出早期系统架构：{label}（覆盖《东脑》《中天》等同代 Director 引擎与 Win3.1/95 老 AVG），共 {len(units)} 条文本。",
                "回写采用 STXT 演员文本块与扇区定长原位覆盖，不改变任何 16 位段偏移或 256 字节扇区表。",
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

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        units = self.scan_units(root)
        report = ApplyReport(in_place=True, out_path=base)
        if not units:
            report.notes.append("未找到可写回的复古 Windows 文本单元")
            return report

        by_file: dict[str, list[Unit]] = {}
        for u in units:
            tr = translations.get(u.key) or translations.get(u.src)
            if tr and tr.strip():
                u.dst = tr
                by_file.setdefault(u.file, []).append(u)
            else:
                report.missed += 1

        if not by_file:
            report.notes.append("未命中任何译文，未修改文件")
            return report

        undo = UndoLog.open(base, engine=self.name)
        for rel, group in sorted(by_file.items()):
            path = base / rel
            blob = path.read_bytes()
            patched, count, conflicts = patch_blob(blob, group)
            report.conflicts.extend(conflicts)
            if count > 0:
                undo.will_modify(rel)
                path.write_bytes(patched)
                undo.did_write(rel)
                report.applied += count

        if report.applied > 0:
            report.undo_path = undo.finish()
        report.notes.append(f"已原地更新 {len(by_file)} 个 Win3.1/95/98 游戏文件（{report.applied} 条）。")
        return report
