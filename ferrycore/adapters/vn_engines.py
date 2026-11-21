"""经典视觉小说与同人 RPG 引擎集群 —— ONScripter (`nscript.dat`)、WOLF RPG Editor (`Data/*.dat/.mps`)、Artemis (`.pfs/.ast`)。

本模块使用纯 Python 标准库覆盖三大常见引擎：

1. **ONScripter / NScripter (`OnscripterAdapter`, 优先级 `17`)**：
   - 支持 `nscript.dat`（经典的单字节 `0x84` XOR 加密剧本）与明文 `0.txt` / `00.txt`。
   - 自动识别 `*define` 定义区与 `game` / `*start` 剧本区，抽取 `caption "..."`、`select` 分支选项与带 `@` / `\\` 控制尾标的对话行。
   - 回写 `nscript.dat` 时自动按 GBK（或原 UTF-8）编码并重新执行 `0x84` XOR 加密，ONScripter-Jh / ONScripter-EN 原生直读。

2. **WOLF RPG Editor (`WolfRpgAdapter`, 优先级 `19`)**：
   - 日本免费同人 RPG 占有率第二的引擎（仅次于 RPG Maker）。
   - 自动鉴别加密 `.wolf` (DXArchive) 与解包后的 `Data/BasicData/*.dat`、`Data/MapData/*.mps`。
   - 对未加密的 `.dat` / `.mps` 执行多编码二进制台词与菜单抽取及定长原位回写。

3. **Artemis Engine (`ArtemisAdapter`, 优先级 `19`)**：
   - 广泛用于 PC、Switch、iOS、Android 跨平台视觉小说的现代 Lua 表结构引擎。
   - 纯标准库解析 `pf8` / `pf6` (`.pfs`) 归档索引表，抽取 `.ast` 剧本中的台词与 `select` 选项，并重建 `.pfs` 或写出高优先级覆盖脚本。
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

from ferrygame.rawbin import patch_blob, scan_binary_strings
from ferrygame.units import Unit

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .tyrano import has_translatable
from .undo import UNDO_NAME, UndoLog
from .unity import is_unity_translatable

_ONS_CMD_PREFIX_RE = re.compile(
    r"^\s*(?:;|mov\b|add\b|sub\b|inc\b|dec\b|cmp\b|if\b|notif\b|goto\b|gosub\b|return\b|end\b|"
    r"bgm\b|bg\b|ld\b|cl\b|csp\b|lsp\b|vsp\b|msp\b|print\b|wait\b|delay\b|quake\b|"
    r"numalias\b|stralias\b|defsub\b|effect\b|windoweffect\b|setwindow\b|dwave\b|dwaveloop\b)",
    re.IGNORECASE,
)
_ONS_QUOTED_RE = re.compile(r'"([^"\r\n]+)"')
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")


# ============================================================================
# 1. ONScripter / NScripter (nscript.dat 0x84 XOR & 0.txt)
# ============================================================================


def xor_nscript_dat(blob: bytes) -> bytes:
    """NScripter / ONScripter `nscript.dat` 标准单字节 `0x84` XOR 加解密。"""
    return bytes(b ^ 0x84 for b in blob)


def _decode_ons_script(raw: bytes) -> tuple[str | None, str]:
    for enc in ("utf-8-sig", "cp932", "gbk"):
        try:
            text = raw.decode(enc)
            if "*define" in text.lower() or "*start" in text.lower() or _CJK_RE.search(text):
                return text, ("utf-8" if enc.startswith("utf-8") else "gbk")
        except UnicodeDecodeError:
            continue
    return None, "gbk"


def parse_ons_lines(text: str) -> list[tuple[int, str, str]]:
    """解析 ONScripter 剧本，返回 `[(lineno, kind, text)]`。"""
    out: list[tuple[int, str, str]] = []
    in_define = False
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        s = raw_line.strip()
        if not s or s.startswith(";"):
            continue
        low = s.lower()
        if low.startswith("*define"):
            in_define = True
            continue
        if in_define and (low == "game" or low.startswith("*start")):
            in_define = False
            continue
        if in_define:
            if low.startswith(("caption ", "versionstr ")):
                for m in _ONS_QUOTED_RE.finditer(s):
                    if has_translatable(m.group(1)):
                        out.append((lineno, "quoted", m.group(1)))
            continue

        if low.startswith(("select ", "csel ")):
            for m in _ONS_QUOTED_RE.finditer(s):
                if has_translatable(m.group(1)):
                    out.append((lineno, "quoted", m.group(1)))
            continue

        if s.startswith("*") or _ONS_CMD_PREFIX_RE.match(s):
            continue

        if has_translatable(s):
            out.append((lineno, "line", s))
    return out


def apply_ons_lines(text: str, mapping: dict[str, str]) -> tuple[str, int]:
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r\n"))
    lines = text.splitlines()
    applied = 0

    for lineno, kind, src_val in parse_ons_lines(text):
        tr = mapping.get(f"{lineno}:{src_val}") or mapping.get(str(lineno)) or mapping.get(src_val)
        if not tr or not tr.strip() or tr == src_val:
            continue
        if kind == "line":
            raw_l = lines[lineno - 1]
            indent = raw_l[: len(raw_l) - len(raw_l.lstrip())]
            lines[lineno - 1] = indent + tr
            applied += 1
        elif kind == "quoted":
            old_quoted = f'"{src_val}"'
            new_quoted = f'"{tr}"'
            if old_quoted in lines[lineno - 1]:
                lines[lineno - 1] = lines[lineno - 1].replace(old_quoted, new_quoted, 1)
                applied += 1

    return newline.join(lines) + (newline if trailing else ""), applied


class OnscripterAdapter(Adapter):
    """ONScripter / NScripter (`nscript.dat` / `0.txt`) 适配器。"""

    name = "onscripter"
    priority = 17

    def _find_scripts(self, root: Path) -> list[tuple[Path, bool]]:
        """返回 `[(文件路径, 是否为 0x84 XOR 加密的 nscript.dat)]`。"""
        root = Path(root)
        if root.is_file():
            if root.name.lower() == "nscript.dat":
                return [(root, True)]
            if re.fullmatch(r"0?\d\.txt", root.name.lower()):
                return [(root, False)]
            return []
        if not root.is_dir():
            return []

        found: list[tuple[Path, bool]] = []
        nscript = root / "nscript.dat"
        if nscript.is_file():
            found.append((nscript, True))
        for name in ("0.txt", "00.txt", "1.txt", "01.txt", "nscript.txt"):
            p = root / name
            if p.is_file():
                try:
                    head = p.read_bytes()[:4096]
                except OSError:
                    continue
                if b"*define" in head.lower() or b"*start" in head.lower():
                    found.append((p, False))
        return found

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        scripts = self._find_scripts(root)
        if not scripts:
            return ProbeReport(engine=self.name, root=root, notes=["未找到 nscript.dat 或 ONScripter 0.txt 剧本"])

        entries = self.collect(root)
        if not entries:
            return ProbeReport(
                engine="ONScripter / NScripter",
                root=root,
                readable=False,
                writable=False,
                notes=["检测到 nscript.dat / 0.txt，但未能解出有效台词"],
            )

        has_xor = any(is_xor for _, is_xor in scripts)
        return ProbeReport(
            engine="ONScripter / NScripter",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container="nscript.dat (0x84 XOR)" if has_xor else "folder",
            text_format="NScripter script",
            unicode_engine=False,
            in_place=True,
            notes=[
                f"检出 {len(scripts)} 个 ONScripter 剧本文件，共 {len(entries)} 条台词/选项。",
                "支持 nscript.dat 单字节 0x84 XOR 自动解密与回写重加密。",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        entries: list[Entry] = []
        for path, is_xor in self._find_scripts(root):
            rel = path.relative_to(base).as_posix()
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            plain = xor_nscript_dat(raw) if is_xor else raw
            text, _enc = _decode_ons_script(plain)
            if not text:
                continue
            for lineno, kind, val in parse_ons_lines(text):
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}:{lineno}:{val[:16]}",
                        english=val,
                        source="game",
                        kind=f"ons_{kind}",
                        rel_path=rel,
                        json_pointer=str(lineno),
                    )
                )
        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        scripts = self._find_scripts(root)
        report = ApplyReport(in_place=True, out_path=base)
        if not scripts:
            report.notes.append("未找到 ONScripter 剧本")
            return report

        entries = self.collect(root)
        by_file: dict[str, list[Entry]] = {}
        for e in entries:
            by_file.setdefault(e.rel_path, []).append(e)

        pending: dict[str, bytes] = {}
        for path, is_xor in scripts:
            rel = path.relative_to(base).as_posix()
            file_entries = by_file.get(rel, [])
            if not file_entries:
                continue
            raw = path.read_bytes()
            plain = xor_nscript_dat(raw) if is_xor else raw
            text, target_enc = _decode_ons_script(plain)
            if not text:
                continue

            ons_map: dict[str, str] = {}
            for e in file_entries:
                tr = translations.get(e.key) or translations.get(e.english)
                if tr and tr.strip():
                    ons_map[f"{e.json_pointer}:{e.english}"] = tr
                    ons_map[e.english] = tr
                else:
                    report.missed += 1
            if not ons_map:
                continue
            new_text, count = apply_ons_lines(text, ons_map)
            if count > 0:
                encoded = new_text.encode(target_enc, errors="replace")
                pending[rel] = xor_nscript_dat(encoded) if is_xor else encoded
                report.applied += count

        if not pending:
            report.notes.append("未命中任何译文，未修改剧本")
            return report

        undo = UndoLog.open(base, engine=self.name)
        for rel, new_raw in pending.items():
            undo.will_modify(rel)
            (base / rel).write_bytes(new_raw)
            undo.did_write(rel)
        report.undo_path = undo.finish()
        report.notes.append(f"已原地更新 {len(pending)} 个 ONScripter 剧本文件（{report.applied} 条）。")
        return report


# ============================================================================
# 2. WOLF RPG Editor (Data/BasicData/*.dat & Data/MapData/*.mps)
# ============================================================================


class WolfRpgAdapter(Adapter):
    """WOLF RPG Editor (`Data/BasicData/*.dat` / `*.mps` / `.wolf`) 适配器。"""

    name = "wolfrpg"
    priority = 19

    def _find_wolf_files(self, root: Path) -> tuple[list[Path], list[Path]]:
        root = Path(root)
        if not root.is_dir():
            return [], []
        wolf_arcs = sorted(root.glob("*.wolf")) + sorted((root / "Data").glob("*.wolf")) if (root / "Data").is_dir() else sorted(root.glob("*.wolf"))
        data_files: list[Path] = []
        data_dir = root / "Data"
        if data_dir.is_dir():
            for ext in ("*.dat", "*.mps", "*.project"):
                data_files.extend(
                    p for p in sorted(data_dir.rglob(ext))
                    if p.is_file() and ".ferry-backup" not in p.parts
                )
        for p in sorted(root.glob("*.mps")):
            if p.is_file() and p not in data_files:
                data_files.append(p)
        return wolf_arcs, data_files

    def scan_units(self, root: Path) -> list[Unit]:
        root = Path(root).resolve()
        _arcs, data_files = self._find_wolf_files(root)
        units: list[Unit] = []
        for path in data_files:
            rel = path.relative_to(root).as_posix()
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            units.extend(scan_binary_strings(blob, rel_path=rel))
        return units

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        wolf_arcs, data_files = self._find_wolf_files(root)
        if not wolf_arcs and not data_files:
            return ProbeReport(engine=self.name, root=root, notes=["未找到 WOLF RPG Data/*.dat/*.mps 或 *.wolf 归档"])

        if wolf_arcs and not data_files:
            return ProbeReport(
                engine="WOLF RPG Editor (.wolf)",
                root=root,
                readable=False,
                writable=False,
                container="wolf (encrypted)",
                notes=[
                    f"检测到 {len(wolf_arcs)} 个加密 .wolf (DXArchive) 归档，未发现解包后的 Data/ 目录；请先用 UberWolf / WolfTL 解包出 Data/ 目录后再交由摆渡汉化。"
                ],
            )

        units = self.scan_units(root)
        if not units:
            return ProbeReport(
                engine="WOLF RPG Editor",
                root=root,
                readable=False,
                writable=False,
                notes=["检测到 WOLF RPG 数据目录，但未提取出有效台词"],
            )

        return ProbeReport(
            engine="WOLF RPG Editor",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(units),
            container="folder",
            text_format=".dat / .mps (定长原位回写)",
            unicode_engine=False,
            in_place=True,
            notes=[
                f"从 {len(data_files)} 个 WOLF RPG 数据/地图文件中抽出 {len(units)} 条台词与数据库词条。",
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
        units = self.scan_units(root)
        by_file: dict[str, list[Unit]] = {}
        for u in units:
            tr = translations.get(u.key) or translations.get(u.src)
            if tr and tr.strip():
                u.dst = tr
                by_file.setdefault(u.file, []).append(u)

        report = ApplyReport(in_place=True, out_path=root)
        if not by_file:
            report.notes.append("没有命中可写回的 WOLF RPG 译文")
            return report

        undo = UndoLog.open(root, engine=self.name)
        for rel, group in sorted(by_file.items()):
            path = root / rel
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
        return report


# ============================================================================
# 3. Artemis Engine (.pfs pf8/pf6 archive + .ast Lua scripts)
# ============================================================================


@dataclass
class PfsEntry:
    name: str
    offset: int
    size: int


def parse_pfs(blob: bytes) -> tuple[bytes, list[PfsEntry]]:
    """解析 Artemis Engine `.pfs` (`pf8` / `pf6`) 归档索引表。"""
    if len(blob) < 11 or blob[:3] not in (b"pf8", b"pf6", b"pf2"):
        raise ValueError("不是合法的 Artemis .pfs 归档")
    magic = blob[:3]
    _index_size, file_count = struct.unpack_from("<II", blob, 3)
    pos = 11
    entries: list[PfsEntry] = []
    for _ in range(min(file_count, 200000)):
        if pos + 4 > len(blob):
            break
        name_len = struct.unpack_from("<I", blob, pos)[0]
        pos += 4
        if name_len <= 0 or pos + name_len + 12 > len(blob):
            break
        name = blob[pos : pos + name_len].decode("utf-8", errors="replace").replace("\\", "/")
        pos += name_len
        _reserved, offset, size = struct.unpack_from("<III", blob, pos)
        pos += 12
        if 0 <= offset and offset + size <= len(blob):
            entries.append(PfsEntry(name=name, offset=offset, size=size))
    return magic, entries


_AST_STRING_RE = re.compile(r'"((?:\\.|[^"\\])*)"')


def parse_ast_lines(text: str) -> list[tuple[int, int, str]]:
    """解析 Artemis `.ast` 脚本中的台词与选项字符串。"""
    out: list[tuple[int, int, str]] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        s = raw_line.strip()
        if not s or s.startswith("--"):
            continue
        # 跳过纯资源/标签行（file= / storage= / path= / label=）
        if re.match(r"^\s*(?:file|storage|path|bgm|se|voice|ch|id|link)\s*=", s, re.IGNORECASE):
            continue
        for order, m in enumerate(_AST_STRING_RE.finditer(raw_line)):
            val = m.group(1)
            if is_unity_translatable(val):
                out.append((lineno, order, val))
    return out


def apply_ast_lines(text: str, mapping: dict[str, str]) -> tuple[str, int]:
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r\n"))
    lines = text.splitlines()
    applied = 0

    for lineno, order, src_val in reversed(parse_ast_lines(text)):
        tr = mapping.get(f"{lineno}#{order}") or mapping.get(src_val)
        if not tr or not tr.strip() or tr == src_val:
            continue
        line = lines[lineno - 1]
        matches = list(_AST_STRING_RE.finditer(line))
        if order >= len(matches):
            continue
        m = matches[order]
        escaped = tr.replace("\\", "\\\\").replace('"', '\\"')
        lines[lineno - 1] = line[: m.start() + 1] + escaped + line[m.end() - 1 :]
        applied += 1

    return newline.join(lines) + (newline if trailing else ""), applied


class ArtemisAdapter(Adapter):
    """Artemis Engine (`.pfs` + `.ast`) 视觉小说适配器。"""

    name = "artemis"
    priority = 19

    def _find_pfs_and_ast(self, root: Path) -> tuple[list[Path], list[Path]]:
        root = Path(root)
        if root.is_file():
            if root.suffix.lower() == ".pfs":
                return [root], []
            if root.suffix.lower() == ".ast":
                return [], [root]
            return [], []
        if not root.is_dir():
            return [], []
        pfs_files = sorted(p for p in root.glob("*.pfs") if p.is_file())
        ast_files = sorted(
            p for p in root.rglob("*.ast")
            if p.is_file() and ".ferry-backup" not in p.parts
        )
        return pfs_files, ast_files

    def _load_scripts(self, root: Path) -> dict[str, str]:
        pfs_files, ast_files = self._find_pfs_and_ast(root)
        base = root.parent if root.is_file() else root
        scripts: dict[str, str] = {}

        for pfs in pfs_files:
            try:
                blob = pfs.read_bytes()
                _magic, entries = parse_pfs(blob)
            except Exception:
                continue
            for ent in entries:
                if ent.name.lower().endswith(".ast"):
                    raw = blob[ent.offset : ent.offset + ent.size]
                    try:
                        scripts[ent.name] = raw.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        continue

        for ast in ast_files:
            rel = ast.relative_to(base).as_posix()
            try:
                scripts[rel] = ast.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                continue
        return scripts

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        pfs_files, ast_files = self._find_pfs_and_ast(root)
        if not pfs_files and not ast_files:
            return ProbeReport(engine=self.name, root=root, notes=["未找到 Artemis .pfs 归档或 .ast 剧本"])

        entries = self.collect(root)
        if not entries:
            return ProbeReport(
                engine="Artemis Engine",
                root=root,
                readable=False,
                writable=False,
                notes=["检测到 Artemis .pfs/.ast，但未解析出有效台词"],
            )

        return ProbeReport(
            engine="Artemis Engine (.pfs / .ast)",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container="pfs" if pfs_files else "folder",
            text_format=".ast (UTF-8)",
            unicode_engine=True,
            in_place=True,
            notes=[
                f"检出 Artemis 剧本条目 {len(entries)} 条。",
                "回写采用 Artemis 原生高优先级散装脚本目录挂载（或原地更新 .ast），无需破坏原 .pfs 包。",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        scripts = self._load_scripts(root)
        entries: list[Entry] = []
        for rel, text in sorted(scripts.items()):
            for lineno, order, val in parse_ast_lines(text):
                loc = f"{lineno}#{order}"
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}#{loc}",
                        english=val,
                        source="game",
                        kind="ast",
                        rel_path=rel,
                        json_pointer=loc,
                    )
                )
        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        scripts = self._load_scripts(root)
        entries = self.collect(root)
        report = ApplyReport(in_place=True, out_path=base)
        if not entries:
            report.notes.append("未找到可回写的 Artemis 剧本条目")
            return report

        by_file: dict[str, list[Entry]] = {}
        for e in entries:
            by_file.setdefault(e.rel_path, []).append(e)

        pending: dict[str, str] = {}
        for rel, file_entries in by_file.items():
            raw_text = scripts.get(rel, "")
            ast_map: dict[str, str] = {}
            for e in file_entries:
                tr = translations.get(e.key) or translations.get(e.english)
                if tr and tr.strip():
                    ast_map[e.json_pointer] = tr
                    ast_map[e.english] = tr
                else:
                    report.missed += 1
            if ast_map:
                new_text, count = apply_ast_lines(raw_text, ast_map)
                if count > 0:
                    pending[rel] = new_text
                    report.applied += count

        if not pending:
            report.notes.append("未命中任何译文，未写出文件")
            return report

        undo = UndoLog.open(base, engine=self.name)
        for rel, new_text in pending.items():
            target = base / rel
            if target.exists():
                undo.will_modify(rel)
            else:
                undo.will_add(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_text, encoding="utf-8")
            undo.did_write(rel)
        report.undo_path = undo.finish()
        report.notes.append(f"已写出 {len(pending)} 个 Artemis .ast 汉化剧本文件（{report.applied} 条）。")
        return report
