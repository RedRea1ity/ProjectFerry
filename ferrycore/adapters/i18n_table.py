"""通用 i18n 本地化词表适配器（GNU Gettext `.po` / 独立游戏 `locales/*.json` / `i18n/*.csv`）。

许多 Godot、Unreal、Cocos、Electron、自研 C++/Lua 引擎和独立游戏不把文本硬编码在脚本里，
而是直接采用标准的外部本地化表：
- GNU Gettext `.po`（`msgid "..."` / `msgstr "..."`，支持多行拼接）
- `locales/`、`locale/`、`i18n/`、`localization/`、`languages/` 下的 `.json` 键值表或 `.csv` 多语言表

本适配器在专用引擎（Kirikiri / Ren'Py / RPG Maker / Godot PCK / Unity / GameMaker）之后运行，
专门认领标准外部多语言词表并支持 `UndoLog` 无损还原。
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .rpgmaker import set_pointer
from .undo import UNDO_NAME, UndoLog

_I18N_DIR_NAMES = {"locale", "locales", "i18n", "localization", "localisation", "languages", "lang"}
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def _is_translatable_value(text: str) -> bool:
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s or len(s) > 4000:
        return False
    if _CJK_RE.search(s):
        return True
    if re.fullmatch(r"[0-9A-Za-z_./\\:-]+", s) and " " not in s:
        return False
    return bool(_LATIN_WORD_RE.search(s))


def _unescape_po(s: str) -> str:
    return (
        s.replace(r"\n", "\n")
        .replace(r"\r", "\r")
        .replace(r"\t", "\t")
        .replace(r"\"", '"')
        .replace(r"\\", "\\")
    )


def _escape_po(s: str) -> str:
    return (
        s.replace("\\", r"\\")
        .replace('"', r"\"")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", r"\n")
        .replace("\t", r"\t")
    )


def parse_po_entries(text: str) -> list[tuple[int, str, str]]:
    """解析 `.po` 文件，返回 `[(msgid_line_no, msgid, msgstr), ...]`（跳过头部空 `msgid ""`）。"""
    lines = text.splitlines()
    entries: list[tuple[int, str, str]] = []
    state: str | None = None
    cur_line = 0
    msgid_parts: list[str] = []
    msgstr_parts: list[str] = []

    def flush() -> None:
        nonlocal state, cur_line, msgid_parts, msgstr_parts
        if state is not None and msgid_parts:
            msgid = "".join(msgid_parts)
            msgstr = "".join(msgstr_parts)
            if msgid and _is_translatable_value(msgid):
                entries.append((cur_line, msgid, msgstr))
        state = None
        cur_line = 0
        msgid_parts = []
        msgstr_parts = []

    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            if state == "msgstr":
                flush()
            continue
        if stripped.startswith("msgid "):
            if state == "msgstr":
                flush()
            state = "msgid"
            cur_line = lineno
            m = re.match(r'^msgid\s+"((?:[^"\\]|\\.)*)"\s*$', stripped)
            if m:
                msgid_parts.append(_unescape_po(m.group(1)))
        elif stripped.startswith("msgstr "):
            state = "msgstr"
            m = re.match(r'^msgstr\s+"((?:[^"\\]|\\.)*)"\s*$', stripped)
            if m:
                msgstr_parts.append(_unescape_po(m.group(1)))
        elif stripped.startswith('"') and stripped.endswith('"'):
            m = re.match(r'^"((?:[^"\\]|\\.)*)"$', stripped)
            if m:
                piece = _unescape_po(m.group(1))
                if state == "msgid":
                    msgid_parts.append(piece)
                elif state == "msgstr":
                    msgstr_parts.append(piece)
    if state == "msgstr":
        flush()
    return entries


def apply_po_translations(text: str, mapping: dict[str, str]) -> tuple[str, int]:
    """将 `{msgid: translated}` 写回 `.po` 文件的 `msgstr`，保持注释与上下文不变。"""
    lines = text.splitlines()
    out: list[str] = []
    applied = 0
    i = 0
    n = len(lines)

    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        if stripped.startswith("msgid "):
            id_lines = [raw]
            msgid_parts: list[str] = []
            m = re.match(r'^msgid\s+"((?:[^"\\]|\\.)*)"\s*$', stripped)
            if m:
                msgid_parts.append(_unescape_po(m.group(1)))
            i += 1
            while i < n and lines[i].strip().startswith('"'):
                id_lines.append(lines[i])
                m2 = re.match(r'^"((?:[^"\\]|\\.)*)"$', lines[i].strip())
                if m2:
                    msgid_parts.append(_unescape_po(m2.group(1)))
                i += 1
            msgid = "".join(msgid_parts)
            out.extend(id_lines)

            if i < n and lines[i].strip().startswith("msgstr "):
                str_lines = [lines[i]]
                i += 1
                while i < n and lines[i].strip().startswith('"'):
                    str_lines.append(lines[i])
                    i += 1
                tr = mapping.get(msgid)
                if msgid and tr and tr.strip():
                    out.append(f'msgstr "{_escape_po(tr)}"')
                    applied += 1
                else:
                    out.extend(str_lines)
            continue

        out.append(raw)
        i += 1

    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), applied


def _iter_json_i18n(node, base: str = ""):
    if isinstance(node, dict):
        for k, v in node.items():
            token = str(k).replace("~", "~0").replace("/", "~1")
            ptr = f"{base}/{token}"
            if isinstance(v, str):
                if _is_translatable_value(v):
                    yield ptr, v
            elif isinstance(v, (dict, list)):
                yield from _iter_json_i18n(v, ptr)
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            ptr = f"{base}/{idx}"
            if isinstance(item, str):
                if _is_translatable_value(item):
                    yield ptr, item
            elif isinstance(item, (dict, list)):
                yield from _iter_json_i18n(item, ptr)


class I18nTableAdapter(Adapter):
    name = "i18n_table"
    priority = 35

    def _find_files(self, root: Path) -> tuple[list[Path], list[Path], list[Path]]:
        root = Path(root)
        if not root.is_dir():
            return [], [], []
        # 避开 Minecraft 模组资源目录
        if (root / "assets").is_dir() and any((root / "assets").glob("*/lang")):
            return [], [], []

        po_files = sorted(
            p for p in root.rglob("*.po")
            if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)
        )
        json_files: list[Path] = []
        csv_files: list[Path] = []
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            rel_parts = [part.lower() for part in p.relative_to(root).parts]
            if any(part.startswith(".") for part in rel_parts):
                continue
            in_i18n_dir = any(part in _I18N_DIR_NAMES for part in rel_parts[:-1])
            if not in_i18n_dir:
                continue
            if p.suffix.lower() == ".json":
                json_files.append(p)
            elif p.suffix.lower() == ".csv":
                csv_files.append(p)
        return po_files, json_files, csv_files

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root)
        po_files, json_files, csv_files = self._find_files(root)
        if not po_files and not json_files and not csv_files:
            return ProbeReport(
                engine=self.name,
                root=root,
                notes=["未找到 .po 或 locales/*.json / i18n/*.csv 本地化词表"],
            )

        entries = self.collect(root)
        if not entries:
            return ProbeReport(
                engine="Universal i18n Table",
                root=root,
                readable=False,
                writable=False,
                notes=["找到本地化目录，但未提取出有效文本条目"],
            )

        kinds = []
        if po_files:
            kinds.append(f".po ({len(po_files)})")
        if json_files:
            kinds.append(f".json ({len(json_files)})")
        if csv_files:
            kinds.append(f".csv ({len(csv_files)})")

        return ProbeReport(
            engine="Universal i18n Table",
            root=root,
            readable=True,
            writable=True,
            verifiable=False,
            unit_count=len(entries),
            container="folder",
            text_format=", ".join(kinds),
            unicode_engine=True,
            in_place=True,
            notes=[f"检出通用本地化词表 {', '.join(kinds)}，共 {len(entries)} 条可翻译条目。"],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).absolute()
        po_files, json_files, csv_files = self._find_files(root)
        entries: list[Entry] = []

        for path in po_files:
            rel = path.relative_to(root).as_posix()
            try:
                raw = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, msgid, _msgstr in parse_po_entries(raw):
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}:{lineno}",
                        english=msgid,
                        source="game",
                        kind="po",
                        rel_path=rel,
                        json_pointer=str(lineno),
                    )
                )

        for path in json_files:
            rel = path.relative_to(root).as_posix()
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            for ptr, text in _iter_json_i18n(payload):
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}#{ptr}",
                        english=text,
                        source="game",
                        kind="json",
                        rel_path=rel,
                        json_pointer=ptr,
                    )
                )

        for path in csv_files:
            rel = path.relative_to(root).as_posix()
            try:
                raw = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                continue
            rows = list(csv.reader(io.StringIO(raw)))
            for r_idx, row in enumerate(rows):
                if r_idx == 0 and all(re.fullmatch(r"[A-Za-z_0-9-]+", c.strip()) for c in row if c.strip()):
                    continue
                for c_idx, cell in enumerate(row):
                    if c_idx == 0 and re.fullmatch(r"[A-Za-z0-9_.:/-]+", cell.strip()):
                        continue
                    if _is_translatable_value(cell):
                        entries.append(
                            Entry(
                                modid=root.name,
                                key=f"{rel}#{r_idx}:{c_idx}",
                                english=cell,
                                source="game",
                                kind="csv",
                                rel_path=rel,
                                json_pointer=f"{r_idx}:{c_idx}",
                            )
                        )

        return entries

    def apply(
        self,
        root: Path,
        translations: dict[str, str],
        out: Path | None = None,
    ) -> ApplyReport:
        root = Path(root).absolute()
        report = ApplyReport()
        entries = self.collect(root)
        if not entries:
            report.notes.append("未找到可回写的本地化词表条目")
            return report

        by_file: dict[str, list[Entry]] = {}
        for entry in entries:
            by_file.setdefault(entry.rel_path, []).append(entry)

        pending: dict[str, str] = {}
        for rel_path, file_entries in by_file.items():
            full_path = root / rel_path
            kind = file_entries[0].kind

            if kind == "po":
                try:
                    raw = full_path.read_text(encoding="utf-8-sig")
                except OSError as exc:
                    report.conflicts.append(f"{rel_path}: {exc}")
                    continue
                po_map: dict[str, str] = {}
                for entry in file_entries:
                    tr = translations.get(entry.key) or translations.get(entry.english)
                    if tr and tr.strip():
                        po_map[entry.english] = tr
                    else:
                        report.missed += 1
                if po_map:
                    new_text, count = apply_po_translations(raw, po_map)
                    if count > 0:
                        pending[rel_path] = new_text
                        report.applied += count

            elif kind == "json":
                try:
                    payload = json.loads(full_path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError) as exc:
                    report.conflicts.append(f"{rel_path}: {exc}")
                    continue
                changed = False
                for entry in file_entries:
                    tr = translations.get(entry.key) or translations.get(entry.english)
                    if not tr or not tr.strip():
                        report.missed += 1
                        continue
                    try:
                        set_pointer(payload, entry.json_pointer, tr)
                        changed = True
                        report.applied += 1
                    except (KeyError, IndexError, TypeError, ValueError) as exc:
                        report.conflicts.append(f"{entry.key}: {exc}")
                if changed:
                    pending[rel_path] = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

            elif kind == "csv":
                try:
                    raw = full_path.read_text(encoding="utf-8-sig")
                except OSError as exc:
                    report.conflicts.append(f"{rel_path}: {exc}")
                    continue
                rows = list(csv.reader(io.StringIO(raw)))
                changed = False
                for entry in file_entries:
                    tr = translations.get(entry.key) or translations.get(entry.english)
                    if not tr or not tr.strip():
                        report.missed += 1
                        continue
                    r_str, c_str = entry.json_pointer.split(":")
                    r_i, c_i = int(r_str), int(c_str)
                    if 0 <= r_i < len(rows) and 0 <= c_i < len(rows[r_i]):
                        rows[r_i][c_i] = tr
                        changed = True
                        report.applied += 1
                if changed:
                    buf = io.StringIO()
                    writer = csv.writer(buf, lineterminator="\n")
                    writer.writerows(rows)
                    pending[rel_path] = buf.getvalue()

        if not pending:
            report.notes.append("没有命中任何译文，未修改文件")
            return report

        log = UndoLog.open(root, engine=self.name)
        for rel_path, new_text in pending.items():
            log.will_modify(rel_path)
            (root / rel_path).write_text(new_text, encoding="utf-8")
            log.did_write(rel_path)
        log.finish()

        report.out_path = root
        report.in_place = True
        report.undo_path = root / UNDO_NAME
        report.notes.append(f"已原地更新 {len(pending)} 个本地化词表文件（{report.applied} 条），revert 可还原。")
        return report
