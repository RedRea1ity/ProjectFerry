"""塞班 Symbian OS（S60v1/v2/v3/v5 / UIQ / N-Gage）老游戏适配器（`.sis` / `.sisx` / `.rsc` / `.r01` / `.app`）。

塞班平台（诺基亚 N-Gage、S60v2 7610/N70、S60v3 N73/N95、S60v5 5800，以及现代模拟器 EKA2L1）
的游戏以两类形态存在：

1. **解包后的塞班系统目录（`system/apps/<App>/` 或 `sys/bin/` + `resource/apps/`）与单资源文件**：
   - **编译资源表 `*.rsc` / `*.r01`..`*.r99`（如 `.r01` 英文、`.r31` 简中）**：存放菜单、对话框与提示文本；
   - **EPOC E32Image 可执行程序 `*.app` / `*.exe`（偏移 `0x10` 处含 `"EPOC"` 签名）与 `*.dat` / `*.bin` 数据包**：
     内部字符串以 `_LIT16`（UTF-16LE）或 `_LIT8`（UTF-8 / Shift-JIS / Latin-1）常量池存放。
2. **安装包 `*.sis` / `*.sisx`**：
   - S60v1/v2 (EPOC Release 6) `.sis` 文件偏移 `0x08` 处 UID3 为 `0x10000419`；
   - S60v3/v5 (Symbian 9+) `.sis` / `.sisx` 文件偏移 `0x00` 处 UID1 为 `0x10201A7A`。
   - 对未压缩或明文段 `.sis` 支持直接在镜像副本 `<name>.cn.sis` 内执行定长原位覆写，
     对高熵 zlib 压缩包则在 `probe` 阶段提示先用 `unsis` / `SISContents` 解包出 `system/` 或 `resource/` 目录再原地汉化。
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

from ferrygame.rawbin import patch_blob, scan_binary_strings
from ferrygame.recon import shannon_entropy
from ferrygame.units import Unit

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .container import unique_output
from .undo import UNDO_NAME, UndoLog

SYMBIAN_PRE9_SIS_UID3 = 0x10000419
SYMBIAN_9_SIS_UID1 = 0x10201A7A
_RSC_EXT_RE = re.compile(r"^\.(?:rsc|r\d{2,3})$", re.IGNORECASE)
_SYMBIAN_BIN_SUFFIXES = {".app", ".rsc", ".dat", ".bin", ".pak", ".res"}


def is_symbian_e32image(blob: bytes) -> bool:
    """检测是否为 Symbian EPOC E32Image (`.app` / `.exe` / `.dll`)，其偏移 16..20 为 `b'EPOC'`。"""
    return len(blob) >= 20 and blob[16:20] == b"EPOC"


def is_symbian_sis(blob: bytes) -> tuple[bool, str]:
    """检测是否为 Symbian `.sis` / `.sisx` 安装包。"""
    if len(blob) >= 16:
        uid1, _uid2, uid3 = struct.unpack_from("<III", blob, 0)
        if uid3 == SYMBIAN_PRE9_SIS_UID3:
            return True, "Symbian S60v1/v2 (EPOC6 .sis)"
        if uid1 == SYMBIAN_9_SIS_UID1:
            return True, "Symbian S60v3/v5 (Symbian 9+ .sisx)"
    return False, ""


class SymbianAdapter(Adapter):
    """塞班 Symbian OS (`.sis` / `.sisx` / `.rsc` / `.r01` / `.app`) 游戏适配器。"""

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

    def _find_unpacked_files(self, root: Path) -> list[Path]:
        root = Path(root)
        if root.is_file():
            if _RSC_EXT_RE.match(root.suffix) or root.suffix.lower() == ".app":
                return [root]
            return []
        if not root.is_dir():
            return []

        has_symbian_layout = (
            (root / "system" / "apps").is_dir()
            or (root / "resource" / "apps").is_dir()
            or (root / "sys" / "bin").is_dir()
            or any(_RSC_EXT_RE.match(p.suffix) for p in root.rglob("*") if p.is_file())
            or any(
                p.suffix.lower() == ".app" and p.is_file()
                for p in root.rglob("*.app")
            )
        )
        if not has_symbian_layout:
            return []

        files: list[Path] = []
        for p in sorted(root.rglob("*")):
            if not p.is_file() or ".ferry-backup" in p.parts:
                continue
            suf = p.suffix.lower()
            if _RSC_EXT_RE.match(suf) or suf in _SYMBIAN_BIN_SUFFIXES or (
                suf in (".exe", ".dll") and is_symbian_e32image(p.read_bytes()[:32])
            ):
                files.append(p)
        return files

    def scan_units(self, root: Path) -> list[Unit]:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        sis = self._find_sis(root)
        if sis is not None:
            try:
                blob = sis.read_bytes()
            except OSError:
                return []
            return scan_binary_strings(blob, rel_path=sis.name)

        units: list[Unit] = []
        for path in self._find_unpacked_files(root):
            rel = path.relative_to(base).as_posix()
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            units.extend(scan_binary_strings(blob, rel_path=rel))
        return units

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        sis = self._find_sis(root)
        unpacked = self._find_unpacked_files(root)
        if sis is None and not unpacked:
            return ProbeReport(
                engine=self.name,
                root=root,
                notes=["未找到塞班 Symbian .sis/.sisx 安装包、.rsc/.r01 资源文件或 E32Image (.app) 程序"],
            )

        units = self.scan_units(root)
        if sis is not None:
            blob = sis.read_bytes()
            _, sis_ver = is_symbian_sis(blob[:32])
            if not units:
                ent = shannon_entropy(blob)
                return ProbeReport(
                    engine=sis_ver or "Symbian SIS",
                    root=root,
                    readable=False,
                    writable=False,
                    container="sis",
                    notes=[
                        f"检测到 {sis.name}（熵值 {ent:.2f}），内部文件流已做 zlib 压缩；请先用 SISContents / unsis 解包出 system/ 或 resource/ 目录后再用摆渡汉化。"
                    ],
                )
            return ProbeReport(
                engine=sis_ver or "Symbian SIS",
                root=root,
                readable=True,
                writable=True,
                unit_count=len(units),
                container="sis",
                text_format="Symbian _LIT16/_LIT8 & LTEXT",
                unicode_engine=True,
                in_place=False,
                notes=[
                    f"从塞班安装包 {sis.name} 抽出 {len(units)} 条文本单元。",
                    "回写产出 .cn.sis 副本（EKA2L1 模拟器或诺基亚实机直装）。",
                ],
            )

        if not units:
            return ProbeReport(
                engine="Symbian OS (Unpacked)",
                root=root,
                readable=False,
                writable=False,
                notes=["检测到塞班目录结构，但未在 .rsc/.r01/.app 中检出可翻译文本"],
            )

        return ProbeReport(
            engine="Symbian OS (S60 / N-Gage / UIQ)",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(units),
            container="folder",
            text_format=".rsc / .r01 / E32Image .app",
            unicode_engine=True,
            in_place=True,
            notes=[
                f"从 {len(unpacked)} 个塞班资源与 E32Image 程序文件中抽出 {len(units)} 条文本。",
                "支持 UTF-16LE (_LIT16)、UTF-8 与 Shift-JIS 定长原位回写。",
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
        sis = self._find_sis(root)
        units = self.scan_units(root)
        report = ApplyReport()
        if not units:
            report.notes.append("未找到可写回的塞班文本单元")
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
        if sis is not None:
            blob = sis.read_bytes()
            patched, count, conflicts = patch_blob(blob, by_file.get(sis.name, []))
            report.conflicts.extend(conflicts)
            if count == 0:
                return report
            default_out = sis.with_name(f"{sis.stem}.cn{sis.suffix}")
            target_out = unique_output(Path(out) if out else default_out)
            target_out.write_bytes(patched)
            undo.did_copy(target_out)
            report.applied = count
            report.undo_path = undo.finish()
            report.out_path = target_out
            report.in_place = False
            report.notes.append(f"已生成塞班汉化安装包：{target_out.name}。")
            return report

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
        report.out_path = base
        report.in_place = True
        report.notes.append(f"已原地更新塞班游戏目录（{report.applied} 条）。")
        return report
