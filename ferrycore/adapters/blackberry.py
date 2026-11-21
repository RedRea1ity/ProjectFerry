"""黑莓 BlackBerry 全世代（BBOS 4.x~7.1 `.cod/.jad/.alx` + BlackBerry 10 / PlayBook `.bar`）游戏适配器。

黑莓平台是塞班时代最热门的另一大移动王牌（Bold 9000/9700/9900、Curve 8300/8900、Pearl 8100、
Torch 9800、Storm 以及后期的 PlayBook / Z10 / Q10 / Passport）。它的游戏分为两代截然不同的容器，
本模块纯标准库一次性全部覆盖：

1. **经典黑莓 BBOS 4.x / 5.0 / 6.0 / 7.1 (`*.cod` + `*.jad` / `*.alx`)**：
   - RIM `rapc` 编译器产出的 `.cod` 有两种物理形态：
     * **单模块裸二进制 `.cod`**：文件头 4 字节魔数固定为 `DE C0 FF FF`（小端 `0xFFFFC0DE`）；
     * **多模块捆绑 `.cod`（超过 64KB 时）**：本质是一个后缀名叫 `.cod` 的标准 ZIP 归档，
       内部包含 `game.cod`、`game-1.cod`、`game-2.cod` 等多个 `DE C0 FF FF` 子模块及资源文件！
   - 回写产出 `<name>.cn.cod` 后，若同目录存在黑莓 `.jad` 安装引导文件，自动生成 `<name>.cn.jad` 并**同步重算
     `RIM-COD-Size:` 与 20 字节 `RIM-COD-SHA1:` 校验摘要**，保证 BlackBerry Desktop Manager / Javaloader 顺利刷入！

2. **后期黑莓 BlackBerry 10 & PlayBook (`*.bar` QNX BlackBerry Archive)**：
   - 本质是含 `META-INF/MANIFEST.MF`（带 `Package-Name:` / `Entry-Point-Type: Qnx/Elf`）与 `app/native/` 的 ZIP 包。
   - 支持直接抽取并无损重建包内的 **Qt Cascades `.qm` 语言包**、**`.qml` `qsTr(...)` 脚本**、
     `app/native/assets/` 下的 `.json / .xml / .csv / .po` 词表以及 QNX 二进制剧本，产出 `<name>.cn.bar`。
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path

from ferrygame.rawbin import EN_MENU_TERMS, JP_MENU_TERMS, patch_blob, scan_binary_strings
from ferrygame.units import Unit

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .container import unique_output
from .mobile import collect_entries_from_virtual_files, patch_virtual_files
from .symbian import _QML_QSTR_RE, QT_QM_MAGIC, parse_qt_qm, rebuild_qt_qm
from .undo import UNDO_NAME, UndoLog
from .unity import is_unity_translatable

RIM_COD_MAGIC = b"\xde\xc0\xff\xff"


def is_blackberry_cod(path: Path) -> tuple[bool, str]:
    """检测文件是否为黑莓 BBOS `.cod`（单模块 `0xDEC0FFFF` 或多模块 ZIP-in-COD）。"""
    if path.suffix.lower() != ".cod" or not path.is_file():
        return False, ""
    try:
        head = path.read_bytes()[:16]
    except OSError:
        return False, ""
    if head.startswith(RIM_COD_MAGIC):
        return True, "single-cod"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path, "r") as zf:
                if any(n.lower().endswith(".cod") for n in zf.namelist()):
                    return True, "zip-cod"
        except (OSError, zipfile.BadZipFile):
            pass
    return False, ""


def is_blackberry_bar(path: Path) -> bool:
    """检测文件是否为 BlackBerry 10 / PlayBook `.bar` 归档。"""
    if path.suffix.lower() != ".bar" or not path.is_file():
        return False
    try:
        with zipfile.ZipFile(path, "r") as zf:
            if "META-INF/MANIFEST.MF" in zf.namelist():
                mf = zf.read("META-INF/MANIFEST.MF").decode("utf-8", errors="replace").lower()
                if "package-name:" in mf or "qnx/elf" in mf or "archive-asset-name:" in mf:
                    return True
            return any(n.startswith("app/native/") for n in zf.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def _format_rim_sha1(blob: bytes) -> str:
    digest = hashlib.sha1(blob).digest()
    return " ".join(f"{b:02x}" for b in digest)


class BlackBerryAdapter(Adapter):
    """黑莓 BlackBerry 全世代（BBOS 4~7 `.cod/.jad` + BB10/PlayBook `.bar`）游戏适配器。"""

    name = "blackberry"
    priority = 13

    def _find_target(self, root: Path) -> tuple[Path | None, str]:
        """返回 `(目标文件路径, 'single-cod' | 'zip-cod' | 'bar' | '')`。"""
        root = Path(root)
        candidates = [root] if root.is_file() else (
            [p for p in sorted(root.glob("*")) if p.is_file()] if root.is_dir() else []
        )
        for p in candidates:
            if ".cn" in p.stem.lower():
                continue
            ok_cod, cod_kind = is_blackberry_cod(p)
            if ok_cod:
                return p, cod_kind
            if is_blackberry_bar(p):
                return p, "bar"
        return None, ""

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        target, kind = self._find_target(root)
        if target is None:
            return ProbeReport(
                engine=self.name,
                root=root,
                notes=["未找到黑莓 BBOS .cod 模块或 BlackBerry 10 / PlayBook .bar 安装包"],
            )

        entries = self.collect(root)
        label = (
            "BlackBerry 10 / PlayBook (.bar)"
            if kind == "bar"
            else f"BlackBerry OS 4~7 ({'多模块捆绑 .cod' if kind == 'zip-cod' else '单模块 .cod'})"
        )
        if not entries:
            return ProbeReport(
                engine=label,
                root=root,
                readable=False,
                writable=False,
                container=kind,
                notes=[f"检测到 {target.name}（{label}），但未检出可翻译文本"],
            )

        return ProbeReport(
            engine=label,
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container=kind,
            text_format="RIM COD / BB10 Cascades .qm & .qml",
            unicode_engine=True,
            in_place=False,
            notes=[
                f"检出黑莓游戏包 {target.name}（{label}），共抽出 {len(entries)} 条文本。",
                "回写产出 .cn.cod（同步重算 .cn.jad 的 RIM-COD-Size 与 RIM-COD-SHA1）或 .cn.bar 副本。",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        target, kind = self._find_target(root)
        if target is None:
            return []

        entries: list[Entry] = []
        if kind == "single-cod":
            raw = target.read_bytes()
            for u in scan_binary_strings(raw, rel_path=target.name):
                entries.append(
                    Entry(
                        modid=target.stem,
                        key=u.key,
                        english=u.src,
                        source="game",
                        kind="cod_bin",
                        rel_path=target.name,
                        json_pointer=str(u.meta.get("offset", 0)),
                    )
                )
            return entries

        if kind == "zip-cod":
            try:
                with zipfile.ZipFile(target, "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        raw = zf.read(info.filename)
                        for u in scan_binary_strings(raw, rel_path=info.filename):
                            entries.append(
                                Entry(
                                    modid=target.stem,
                                    key=u.key,
                                    english=u.src,
                                    source="game",
                                    kind="cod_bin",
                                    rel_path=info.filename,
                                    json_pointer=str(u.meta.get("offset", 0)),
                                )
                            )
            except (OSError, zipfile.BadZipFile):
                return []
            return entries

        if kind == "bar":
            struct_files: dict[str, bytes] = {}
            try:
                with zipfile.ZipFile(target, "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        rel = info.filename
                        low = rel.lower()
                        raw = zf.read(rel)
                        if low.endswith(".qm"):
                            try:
                                for msg in parse_qt_qm(raw):
                                    entries.append(
                                        Entry(
                                            modid=target.stem,
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
                        elif low.endswith(".qml"):
                            text = raw.decode("utf-8-sig", errors="replace")
                            for idx, m in enumerate(_QML_QSTR_RE.finditer(text)):
                                val = m.group("body")
                                if is_unity_translatable(val) or val in EN_MENU_TERMS or val in JP_MENU_TERMS:
                                    entries.append(
                                        Entry(
                                            modid=target.stem,
                                            key=f"{rel}#QML:{idx}",
                                            english=val,
                                            source="game",
                                            kind="qml",
                                            rel_path=rel,
                                            json_pointer=f"QML:{idx}",
                                        )
                                    )
                        elif low.endswith((".json", ".csv", ".po", ".strings", ".xml")):
                            struct_files[rel] = raw
                        elif low.endswith((".dat", ".bin")) or ("app/native/" in low and "." not in Path(low).name):
                            for u in scan_binary_strings(raw, rel_path=rel):
                                entries.append(
                                    Entry(
                                        modid=target.stem,
                                        key=u.key,
                                        english=u.src,
                                        source="game",
                                        kind="bar_bin",
                                        rel_path=rel,
                                        json_pointer=str(u.meta.get("offset", 0)),
                                    )
                                )
            except (OSError, zipfile.BadZipFile):
                return []
            if struct_files:
                entries.extend(collect_entries_from_virtual_files(target.stem, struct_files))
            return entries

        return []

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        target, kind = self._find_target(root)
        report = ApplyReport()
        if target is None:
            report.notes.append("未找到黑莓游戏包")
            return report

        entries = self.collect(root)
        if not entries:
            report.notes.append("黑莓包内无可回写条目")
            return report

        undo = UndoLog.open(base, engine=self.name)
        default_out = target.with_name(f"{target.stem}.cn{target.suffix}")
        target_out = unique_output(Path(out) if out else default_out)
        if target_out.is_dir():
            target_out = unique_output(target_out / default_out.name)

        if kind == "single-cod":
            raw = target.read_bytes()
            units = scan_binary_strings(raw, rel_path=target.name)
            for u in units:
                tr = translations.get(u.key) or translations.get(u.src)
                if tr and tr.strip():
                    u.dst = tr
                else:
                    report.missed += 1
            patched, c, conflicts = patch_blob(raw, units)
            report.conflicts.extend(conflicts)
            if c == 0:
                return report
            target_out.write_bytes(patched)
            report.applied = c
            self._sync_blackberry_jad(target, target_out, patched, undo, report)

        elif kind == "zip-cod":
            modified: dict[str, bytes] = {}
            with zipfile.ZipFile(target, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    raw = zf.read(info.filename)
                    units = scan_binary_strings(raw, rel_path=info.filename)
                    hit = False
                    for u in units:
                        tr = translations.get(u.key) or translations.get(u.src)
                        if tr and tr.strip():
                            u.dst = tr
                            hit = True
                    if hit:
                        patched, c, conflicts = patch_blob(raw, units)
                        report.conflicts.extend(conflicts)
                        if c > 0:
                            modified[info.filename] = patched
                            report.applied += c
            if not modified:
                return report
            with zipfile.ZipFile(target, "r") as zin, zipfile.ZipFile(target_out, "w") as zout:
                for info in zin.infolist():
                    data = modified.get(info.filename, zin.read(info.filename))
                    fresh = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
                    fresh.compress_type = info.compress_type
                    zout.writestr(fresh, data)
            self._sync_blackberry_jad(target, target_out, target_out.read_bytes(), undo, report)

        elif kind == "bar":
            modified = {}
            struct_files: dict[str, bytes] = {}
            struct_entries = [e for e in entries if e.kind not in ("qt_qm", "qml", "bar_bin")]
            with zipfile.ZipFile(target, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    rel = info.filename
                    low = rel.lower()
                    raw = zf.read(rel)
                    if low.endswith(".qm"):
                        qm_map = {k.split("#", 1)[-1]: v for k, v in translations.items()}
                        qm_map.update(translations)
                        new_qm, c = rebuild_qt_qm(raw, qm_map)
                        if c > 0:
                            modified[rel] = new_qm
                            report.applied += c
                    elif low.endswith(".qml"):
                        text = raw.decode("utf-8-sig", errors="replace")
                        idx_counter = -1

                        def _repl_qml(m: re.Match) -> str:
                            nonlocal idx_counter
                            idx_counter += 1
                            src_val = m.group("body")
                            tr = translations.get(f"{rel}#QML:{idx_counter}") or translations.get(src_val)
                            if tr and tr.strip() and tr != src_val:
                                report.applied += 1
                                return f"{m.group(1)}{tr}{m.group(3)}"
                            return m.group(0)

                        new_text = _QML_QSTR_RE.sub(_repl_qml, text)
                        if new_text != text:
                            modified[rel] = new_text.encode("utf-8")
                    elif low.endswith((".json", ".csv", ".po", ".strings", ".xml")):
                        struct_files[rel] = raw
                    else:
                        units = scan_binary_strings(raw, rel_path=rel)
                        hit = False
                        for u in units:
                            tr = translations.get(u.key) or translations.get(u.src)
                            if tr and tr.strip():
                                u.dst = tr
                                hit = True
                        if hit:
                            patched, c, conflicts = patch_blob(raw, units)
                            report.conflicts.extend(conflicts)
                            if c > 0:
                                modified[rel] = patched
                                report.applied += c

            if struct_files and struct_entries:
                s_mod, s_app, s_miss, s_conf = patch_virtual_files(struct_files, struct_entries, translations)
                modified.update(s_mod)
                report.applied += s_app
                report.missed += s_miss
                report.conflicts.extend(s_conf)

            if not modified:
                return report
            with zipfile.ZipFile(target, "r") as zin, zipfile.ZipFile(target_out, "w") as zout:
                for info in zin.infolist():
                    data = modified.get(info.filename, zin.read(info.filename))
                    fresh = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
                    fresh.compress_type = info.compress_type
                    fresh.external_attr = info.external_attr
                    zout.writestr(fresh, data)

        undo.did_copy(target_out)
        report.undo_path = undo.finish()
        report.out_path = target_out
        report.in_place = False
        report.notes.append(f"已生成黑莓汉化游戏包：{target_out.name}。")
        return report

    def _sync_blackberry_jad(
        self,
        src_cod: Path,
        out_cod: Path,
        new_cod_bytes: bytes,
        undo: UndoLog,
        report: ApplyReport,
    ) -> None:
        jad_path = src_cod.with_suffix(".jad")
        if not jad_path.is_file():
            return
        try:
            jad_text = jad_path.read_text(encoding="utf-8", errors="replace")
            new_size = len(new_cod_bytes)
            new_sha1 = _format_rim_sha1(new_cod_bytes)
            jad_text = re.sub(r"(?im)^RIM-COD-URL\s*:.*$", f"RIM-COD-URL: {out_cod.name}", jad_text)
            jad_text = re.sub(r"(?im)^RIM-COD-Size\s*:.*$", f"RIM-COD-Size: {new_size}", jad_text)
            jad_text = re.sub(r"(?im)^RIM-COD-SHA1\s*:.*$", f"RIM-COD-SHA1: {new_sha1}", jad_text)
            out_jad = out_cod.with_suffix(".jad")
            out_jad.write_text(jad_text, encoding="utf-8")
            undo.did_copy(out_jad)
            report.notes.append(f"已同步更新黑莓 {out_jad.name}（RIM-COD-Size: {new_size}，已重算 RIM-COD-SHA1）。")
        except OSError:
            pass
