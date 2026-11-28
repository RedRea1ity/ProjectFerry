"""欧美与全球叙事引擎适配器 —— Ink (`.ink.json`)、Yarn Spinner (`.yarn`)、Twine (`.html`)、Dialogic (`.dtl`)、AGS (`.trs`)。

在欧美独立游戏、互动小说（Interactive Fiction）与叙事 RPG（如《80 Days》《Dredge》《Night in the Woods》
《Citizen Sleeper》《Slay the Princess》《Roadwarden》等）中，绝大多数文本并不使用日系 `.ks`，而是采用以下五种标准叙事格式：

1. **Inkle's Ink 编译剧本 (`*.ink.json` / 含 `"inkVersion"` 的 JSON)**：
   - 语法树位于 `"root"`，所有展示给玩家的正文节点均以 `"^"` 开头（例如 `"^The heavy oak door creaks open."`）。
   - 回写时严格保留前导 `"^"` 标记与 JSON Pointer 树位置。
2. **Yarn Spinner (`*.yarn`)**：
   - 节点位于 `---` 与 `===` 之间，台词形如 `Speaker: Text #line:id`，选项形如 `-> Choice text #line:id`。
   - 回写时严格保护 `<<if ...>>` 指令块、`->` 缩进、说话人前缀与 `#line:` 语音定位标签。
3. **Twine 互动小说 (`*.html` 含 `<tw-storydata>` / `<tw-passagedata>`)**：
   - 抽取 `<tw-passagedata>` 段落内的正文行；对 `[[显示文本|跳转目标]]` 或 `[[显示文本->跳转目标]]` 仅翻译显示文本，绝不破坏段落跳转 ID。
4. **Godot Dialogic (`*.dtl`)**：
   - 支持 `Character (portrait): Dialogue` 与 `- Choice | [if ...]` 语法。
5. **Adventure Game Studio (`*.trs` 点击冒险翻译源)**：
   - 标准双行对照格式（上行原文、下行译文，跳过 `//` 注释）。
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .rpgmaker import set_pointer
from .undo import UNDO_NAME, UndoLog
from .unity import is_unity_translatable

_YARN_LINE_RE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<arrow>->\s+)?"
    r"(?P<speaker>[A-Za-z0-9_\u3040-\u30ff\u4e00-\u9fff ]{1,24}:\s+)?"
    r"(?P<body>.+?)"
    r"(?P<tags>(?:\s+#[^\s]+)*)\s*$"
)
_DTL_LINE_RE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<prefix>-\s+|[A-Za-z0-9_\u3040-\u30ff\u4e00-\u9fff]+(?:\s*\([^)]*\))?:\s+)?"
    r"(?P<body>[^|\r\n]+?)"
    r"(?P<tail>\s*(?:\|.*)?)$"
)
_TWINE_PASSAGE_RE = re.compile(
    r"(<tw-passagedata\b[^>]*>)(.*?)(</tw-passagedata>)",
    re.DOTALL | re.IGNORECASE,
)


def _iter_ink_strings(node, base: str = ""):
    """遍历 Ink 编译 JSON，抽取所有以 `^` 开头的可翻译台词节点。"""
    if isinstance(node, dict):
        for k, v in node.items():
            token = str(k).replace("~", "~0").replace("/", "~1")
            ptr = f"{base}/{token}"
            if isinstance(v, str):
                if v.startswith("^") and len(v) > 2 and is_unity_translatable(v[1:]):
                    yield ptr, v[1:]
            elif isinstance(v, (dict, list)):
                yield from _iter_ink_strings(v, ptr)
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            ptr = f"{base}/{idx}"
            if isinstance(item, str):
                if item.startswith("^") and len(item) > 2 and is_unity_translatable(item[1:]):
                    yield ptr, item[1:]
            elif isinstance(item, (dict, list)):
                yield from _iter_ink_strings(item, ptr)


def parse_yarn_lines(text: str) -> list[tuple[int, str]]:
    """解析 `.yarn` 剧本中的台词与选项行，返回 `[(lineno, body_text), ...]`。"""
    out: list[tuple[int, str]] = []
    in_body = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if stripped == "---":
            in_body = True
            continue
        if stripped == "===":
            in_body = False
            continue
        if not in_body or not stripped:
            continue
        if stripped.startswith(("//", "<<", "[[")):
            continue
        m = _YARN_LINE_RE.match(raw)
        if not m:
            continue
        body = m.group("body").strip()
        if is_unity_translatable(body):
            out.append((lineno, body))
    return out


def apply_yarn_lines(text: str, mapping: dict[int, str]) -> tuple[str, int]:
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r\n"))
    lines = text.splitlines()
    applied = 0
    for lineno, tr in mapping.items():
        if not (1 <= lineno <= len(lines)):
            continue
        m = _YARN_LINE_RE.match(lines[lineno - 1])
        if not m:
            continue
        indent = m.group("indent") or ""
        arrow = m.group("arrow") or ""
        speaker = m.group("speaker") or ""
        tags = m.group("tags") or ""
        lines[lineno - 1] = f"{indent}{arrow}{speaker}{tr}{tags}"
        applied += 1
    return newline.join(lines) + (newline if trailing else ""), applied


def parse_dtl_lines(text: str) -> list[tuple[int, str]]:
    """解析 Godot Dialogic `.dtl` 剧本。"""
    out: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", "[", "join ", "leave ", "update ", "jump ", "label ", "set ", "if ", "elif ", "else", "VAR ")):
            continue
        m = _DTL_LINE_RE.match(raw)
        if not m:
            continue
        body = m.group("body").strip()
        if is_unity_translatable(body):
            out.append((lineno, body))
    return out


def apply_dtl_lines(text: str, mapping: dict[int, str]) -> tuple[str, int]:
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r\n"))
    lines = text.splitlines()
    applied = 0
    for lineno, tr in mapping.items():
        if not (1 <= lineno <= len(lines)):
            continue
        m = _DTL_LINE_RE.match(lines[lineno - 1])
        if not m:
            continue
        indent = m.group("indent") or ""
        prefix = m.group("prefix") or ""
        tail = m.group("tail") or ""
        lines[lineno - 1] = f"{indent}{prefix}{tr}{tail}"
        applied += 1
    return newline.join(lines) + (newline if trailing else ""), applied


def parse_trs_entries(text: str) -> list[tuple[int, str]]:
    """解析 Adventure Game Studio `.trs` 翻译源文件（原文行 + 空/译文行成对出现）。"""
    lines = text.splitlines()
    out: list[tuple[int, str]] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        if not line or line.startswith("//"):
            i += 1
            continue
        if is_unity_translatable(line):
            out.append((i + 1, lines[i]))
        i += 2
    return out


def apply_trs_entries(text: str, mapping: dict[str, str]) -> tuple[str, int]:
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r\n"))
    lines = text.splitlines()
    applied = 0
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        if not line or line.startswith("//"):
            i += 1
            continue
        tr = mapping.get(str(i + 1)) or mapping.get(lines[i])
        if tr and tr.strip() and i + 1 < n:
            lines[i + 1] = tr
            applied += 1
        i += 2
    return newline.join(lines) + (newline if trailing else ""), applied


def parse_twine_html(raw_html: str) -> list[tuple[int, str]]:
    """从 Twine `<tw-passagedata>` 中抽取纯文本段落行（跳过纯宏与 `[[Target]]` 无显示文本链接）。"""
    out: list[tuple[int, str]] = []
    for p_idx, m in enumerate(_TWINE_PASSAGE_RE.finditer(raw_html)):
        unescaped = html.unescape(m.group(2))
        for l_idx, line in enumerate(unescaped.splitlines()):
            s = line.strip()
            if not s or s.startswith(("<<", "(set:", "(if:", "//")):
                continue
            if re.fullmatch(r"\[\[[^\]|>]+?\]\]", s):
                continue
            if is_unity_translatable(s):
                out.append((p_idx * 10000 + l_idx, s))
    return out


def apply_twine_html(raw_html: str, mapping: dict[str, str]) -> tuple[str, int]:
    applied = 0
    p_counter = -1

    def _repl(m: re.Match) -> str:
        nonlocal applied, p_counter
        p_counter += 1
        p_idx = p_counter
        open_tag, body, close_tag = m.group(1), m.group(2), m.group(3)
        unescaped = html.unescape(body)
        lines = unescaped.splitlines()
        changed = False
        for l_idx, line in enumerate(lines):
            s = line.strip()
            if not s:
                continue
            loc_key = str(p_idx * 10000 + l_idx)
            tr = mapping.get(loc_key) or mapping.get(s)
            if tr and tr.strip() and tr != s:
                indent = line[: len(line) - len(line.lstrip())]
                lines[l_idx] = indent + tr
                applied += 1
                changed = True
        if not changed:
            return m.group(0)
        rejoined = "\n".join(lines)
        return f"{open_tag}{html.escape(rejoined, quote=False)}{close_tag}"

    new_html = _TWINE_PASSAGE_RE.sub(_repl, raw_html)
    return new_html, applied


class NarrativeAdapter(Adapter):
    """欧美及全球独立叙事引擎适配器（Ink / Yarn Spinner / Twine / Dialogic / AGS）。"""

    name = "narrative"
    priority = 29

    def _find_files(self, root: Path) -> dict[str, list[Path]]:
        root = Path(root)
        result: dict[str, list[Path]] = {
            "ink": [],
            "yarn": [],
            "twine": [],
            "dtl": [],
            "trs": [],
        }
        candidates = [root] if root.is_file() else (
            [p for p in sorted(root.rglob("*")) if p.is_file() and ".ferry-backup" not in p.parts]
            if root.is_dir() else []
        )
        for p in candidates:
            low = p.name.lower()
            if low.endswith(".yarn"):
                result["yarn"].append(p)
            elif low.endswith(".dtl"):
                result["dtl"].append(p)
            elif low.endswith(".trs"):
                result["trs"].append(p)
            elif low.endswith(".json"):
                try:
                    with p.open("rb") as fh:
                        head = fh.read(512)
                except OSError:
                    continue
                if b'"inkVersion"' in head:
                    result["ink"].append(p)
            elif low.endswith((".html", ".htm")):
                try:
                    with p.open("rb") as fh:
                        content = fh.read(65536)
                except OSError:
                    continue
                if b"<tw-storydata" in content or b"<tw-passagedata" in content:
                    result["twine"].append(p)
        return result

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        found = self._find_files(root)
        if not any(found.values()):
            return ProbeReport(
                engine=self.name,
                root=root,
                notes=["未找到 Ink (.ink.json)、Yarn Spinner (.yarn)、Twine (.html)、Dialogic (.dtl) 或 AGS (.trs) 叙事脚本"],
            )

        entries = self.collect(root)
        active_kinds = [k for k, v in found.items() if v]
        if not entries:
            return ProbeReport(
                engine=f"Narrative Engine ({', '.join(active_kinds)})",
                root=root,
                readable=False,
                writable=False,
                notes=["检测到叙事脚本文件，但未提取出有效台词"],
            )

        return ProbeReport(
            engine=f"Narrative Engine ({', '.join(active_kinds)})",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container="folder",
            text_format=", ".join(active_kinds),
            unicode_engine=True,
            in_place=True,
            notes=[
                f"检出全球叙事脚本 ({', '.join(active_kinds)})，共 {len(entries)} 条台词/分支选项。",
                "回写时自动保护 Ink '^' 节点标记、Yarn '<<...>>' 指令与 '#line:' 标签、以及 Twine 段落跳转目标。",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        found = self._find_files(root)
        entries: list[Entry] = []

        for path in found["ink"]:
            rel = path.relative_to(base).as_posix()
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            for ptr, text in _iter_ink_strings(payload.get("root", payload), "/root" if "root" in payload else ""):
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}#{ptr}",
                        english=text,
                        source="game",
                        kind="ink",
                        rel_path=rel,
                        json_pointer=ptr,
                    )
                )

        for path in found["yarn"]:
            rel = path.relative_to(base).as_posix()
            try:
                raw = path.read_text(encoding="utf-8-sig")
            except Exception:
                continue
            for lineno, text in parse_yarn_lines(raw):
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}:{lineno}",
                        english=text,
                        source="game",
                        kind="yarn",
                        rel_path=rel,
                        json_pointer=str(lineno),
                    )
                )

        for path in found["dtl"]:
            rel = path.relative_to(base).as_posix()
            try:
                raw = path.read_text(encoding="utf-8-sig")
            except Exception:
                continue
            for lineno, text in parse_dtl_lines(raw):
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}:{lineno}",
                        english=text,
                        source="game",
                        kind="dtl",
                        rel_path=rel,
                        json_pointer=str(lineno),
                    )
                )

        for path in found["trs"]:
            rel = path.relative_to(base).as_posix()
            try:
                raw = path.read_text(encoding="utf-8-sig")
            except Exception:
                continue
            for lineno, text in parse_trs_entries(raw):
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}:{lineno}",
                        english=text,
                        source="game",
                        kind="trs",
                        rel_path=rel,
                        json_pointer=str(lineno),
                    )
                )

        for path in found["twine"]:
            rel = path.relative_to(base).as_posix()
            try:
                raw = path.read_text(encoding="utf-8-sig")
            except Exception:
                continue
            for loc_id, text in parse_twine_html(raw):
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}#{loc_id}",
                        english=text,
                        source="game",
                        kind="twine",
                        rel_path=rel,
                        json_pointer=str(loc_id),
                    )
                )

        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        entries = self.collect(root)
        report = ApplyReport(in_place=True, out_path=base)
        if not entries:
            report.notes.append("未找到可回写的叙事脚本条目")
            return report

        by_file: dict[str, list[Entry]] = {}
        for e in entries:
            by_file.setdefault(e.rel_path, []).append(e)

        pending: dict[str, str] = {}
        for rel, file_entries in by_file.items():
            full_path = base / rel
            kind = file_entries[0].kind
            try:
                raw = full_path.read_text(encoding="utf-8-sig")
            except OSError as exc:
                report.conflicts.append(f"{rel}: {exc}")
                continue

            if kind == "ink":
                try:
                    payload = json.loads(raw)
                except ValueError as exc:
                    report.conflicts.append(f"{rel}: {exc}")
                    continue
                changed = False
                for e in file_entries:
                    tr = translations.get(e.key) or translations.get(e.english)
                    if not tr or not tr.strip():
                        report.missed += 1
                        continue
                    try:
                        set_pointer(payload, e.json_pointer, "^" + tr)
                        changed = True
                        report.applied += 1
                    except Exception as exc:
                        report.conflicts.append(f"{e.key}: {exc}")
                if changed:
                    pending[rel] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

            elif kind in ("yarn", "dtl"):
                line_map: dict[int, str] = {}
                for e in file_entries:
                    tr = translations.get(e.key) or translations.get(e.english)
                    if tr and tr.strip():
                        line_map[int(e.json_pointer)] = tr
                    else:
                        report.missed += 1
                if line_map:
                    fn = apply_yarn_lines if kind == "yarn" else apply_dtl_lines
                    new_text, count = fn(raw, line_map)
                    if count > 0:
                        pending[rel] = new_text
                        report.applied += count

            elif kind in ("trs", "twine"):
                str_map: dict[str, str] = {}
                for e in file_entries:
                    tr = translations.get(e.key) or translations.get(e.english)
                    if tr and tr.strip():
                        str_map[e.json_pointer] = tr
                        str_map[e.english] = tr
                    else:
                        report.missed += 1
                if str_map:
                    fn = apply_trs_entries if kind == "trs" else apply_twine_html
                    new_text, count = fn(raw, str_map)
                    if count > 0:
                        pending[rel] = new_text
                        report.applied += count

        if not pending:
            report.notes.append("未命中任何译文，未修改文件")
            return report

        undo = UndoLog.open(base, engine=self.name)
        for rel, new_text in pending.items():
            undo.will_modify(rel)
            (base / rel).write_text(new_text, encoding="utf-8")
            undo.did_write(rel)
        report.undo_path = undo.finish()
        report.notes.append(f"已原地更新 {len(pending)} 个叙事脚本文件（{report.applied} 条）。")
        return report
