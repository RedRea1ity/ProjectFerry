"""LÖVE (Love2D) 与 Lua 脚本引擎适配器（`.love` 归档 / `zip-in-exe` 融合包 / 散装 `main.lua`）。

LÖVE (Love2D) 是全球独立游戏（如《Balatro》《Move or Die》《BYTEPATH》《Mari0》等）极常用的开源 2D 引擎。
它的三种标准分发形态本适配器全部覆盖：
1. **独立 `.love` 归档**（本质是包含 `main.lua` 的 ZIP 包）→ 产出 `<name>.cn.love` 副本；
2. **融合单 EXE (`zip-in-exe`)**（把 `.love` 追加在 `love.exe` 尾部）→ 重新打包 ZIP 并拼回 EXE 尾部产出 `<name>.cn.exe`；
3. **散装目录（含 `main.lua`）** → 原地更新 `.lua` 脚本并记入 `UndoLog`。

解析规则：
- 只抽取 `.lua` 中作为表值、变量赋值或 UI/对话参数的人类可读字符串（支持中日韩与欧美多词句子及标准菜单词）；
- 硬跳过 `require(...)`、`love.graphics.newImage(...)`、`love.audio.newSource(...)`、Shader 代码与资源路径。
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from ferrygame.rawbin import EN_MENU_TERMS, JP_MENU_TERMS

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .container import ZipInExe, locate_appended_zip, unique_output
from .undo import UNDO_NAME, UndoLog
from .unity import is_unity_translatable

_LUA_SKIP_LINE_RE = re.compile(
    r"(?:\brequire\s*\(|\blove\.(?:graphics\.new(?:Image|Font|Shader)|audio\.newSource|filesystem\.|event\.|window\.setMode)\b)",
    re.IGNORECASE,
)
_LUA_STRING_RE = re.compile(r'("(?P<dq>(?:\\.|[^"\\])*)"|\'(?P<sq>(?:\\.|[^\'\\])*)\')')


def _is_lua_translatable(val: str) -> bool:
    s = val.strip()
    if s in EN_MENU_TERMS or s in JP_MENU_TERMS:
        return True
    return is_unity_translatable(s)


def parse_lua_strings(text: str) -> list[tuple[int, int, str]]:
    """从 `.lua` 源码按行抽取可翻译字面量，返回 `[(lineno, match_order, unescaped_text), ...]`。"""
    out: list[tuple[int, int, str]] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if _LUA_SKIP_LINE_RE.search(stripped):
            continue
        # 剥除行尾单行注释（粗略保护，不在引号内时生效）
        code_part = raw_line.split("--", 1)[0] if "--" in raw_line and '"' not in raw_line.split("--", 1)[1] else raw_line
        for order, m in enumerate(_LUA_STRING_RE.finditer(code_part)):
            # 若该字符串紧跟在 `[` 后面且后面是 `]=`，说明是 Lua 表键 `["key"] = ...`，绝不翻键名
            prefix = code_part[: m.start()].rstrip()
            suffix = code_part[m.end() :].lstrip()
            if prefix.endswith("[") and suffix.startswith("]"):
                continue
            body = m.group("dq") if m.group("dq") is not None else m.group("sq")
            if body is None:
                continue
            unescaped = body.replace('\\"', '"').replace("\\'", "'").replace("\\n", "\n")
            if _is_lua_translatable(unescaped):
                out.append((lineno, order, unescaped))
    return out


def apply_lua_strings(text: str, mapping: dict[str, str]) -> tuple[str, int]:
    """将 `{lineno#order 或 原文: 译文}` 写回 `.lua` 源码。"""
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r\n"))
    lines = text.splitlines()
    applied = 0

    for lineno, order, src_val in reversed(parse_lua_strings(text)):
        tr = mapping.get(f"{lineno}#{order}") or mapping.get(src_val)
        if not tr or not tr.strip() or tr == src_val:
            continue
        line = lines[lineno - 1]
        matches = list(_LUA_STRING_RE.finditer(line))
        if order >= len(matches):
            continue
        m = matches[order]
        quote = line[m.start()]
        escaped = (
            tr.replace("\\", "\\\\")
            .replace(quote, "\\" + quote)
            .replace("\r\n", "\\n")
            .replace("\n", "\\n")
        )
        lines[lineno - 1] = line[: m.start() + 1] + escaped + line[m.end() - 1 :]
        applied += 1

    return newline.join(lines) + (newline if trailing else ""), applied


class Love2dAdapter(Adapter):
    """LÖVE (Love2D) `.love` / 融合 `.exe` / 散装 `main.lua` 适配器。"""

    name = "love2d"
    priority = 24

    def _find_love_archive(self, root: Path) -> Path | None:
        root = Path(root)
        if root.is_file() and root.suffix.lower() == ".love" and ".cn" not in root.stem.lower():
            return root
        if root.is_dir():
            for p in sorted(root.glob("*.love")):
                if p.is_file() and ".cn" not in p.stem.lower():
                    return p
        return None

    def _find_fused_exe(self, root: Path) -> ZipInExe | None:
        root = Path(root)
        exes = [root] if (root.is_file() and root.suffix.lower() == ".exe") else (
            sorted(root.glob("*.exe")) if root.is_dir() else []
        )
        for exe in exes:
            if ".cn" in exe.stem.lower():
                continue
            span = locate_appended_zip(exe)
            if span is None:
                continue
            container = ZipInExe(root if root.is_dir() else root.parent, exe, span)
            files = {f.lower() for f in container.list_files((".lua",))}
            if "main.lua" in files:
                return container
        return None

    def _is_unpacked_love(self, root: Path) -> bool:
        root = Path(root)
        return root.is_dir() and (root / "main.lua").is_file()

    def _load_lua_files(self, root: Path) -> tuple[str, Path | ZipInExe | None, dict[str, str]]:
        love_arc = self._find_love_archive(root)
        if love_arc is not None:
            files: dict[str, str] = {}
            try:
                with zipfile.ZipFile(love_arc, "r") as zf:
                    names = {n.lower() for n in zf.namelist()}
                    if "main.lua" not in names:
                        return "none", None, {}
                    for info in zf.infolist():
                        if not info.is_dir() and info.filename.lower().endswith(".lua"):
                            files[info.filename] = zf.read(info.filename).decode("utf-8-sig", errors="replace")
            except (OSError, zipfile.BadZipFile):
                return "none", None, {}
            return "love", love_arc, files

        fused = self._find_fused_exe(root)
        if fused is not None:
            files = {}
            for name in fused.list_files((".lua",)):
                files[name] = fused.read_bytes(name).decode("utf-8-sig", errors="replace")
            return "zip-in-exe", fused, files

        if self._is_unpacked_love(root):
            files = {}
            for p in sorted(root.rglob("*.lua")):
                if p.is_file() and ".ferry-backup" not in p.parts:
                    rel = p.relative_to(root).as_posix()
                    try:
                        files[rel] = p.read_text(encoding="utf-8-sig", errors="replace")
                    except OSError:
                        continue
            return "folder", root, files

        return "none", None, {}

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        mode, _src, files = self._load_lua_files(root)
        if mode == "none":
            return ProbeReport(engine=self.name, root=root, notes=["未找到 .love 归档、LÖVE 融合 exe 或 main.lua"])

        entries = self.collect(root)
        if not entries:
            return ProbeReport(
                engine="LÖVE (Love2D)",
                root=root,
                readable=False,
                writable=False,
                container=mode,
                notes=["检测到 LÖVE (main.lua) 工程，但未在 .lua 脚本中检出可翻译文本"],
            )

        return ProbeReport(
            engine="LÖVE (Love2D / Lua)",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container=mode,
            text_format=".lua",
            unicode_engine=True,
            in_place=(mode == "folder"),
            notes=[
                f"从 {len(files)} 个 Lua 脚本抽出 {len(entries)} 条 UI/剧情文本（容器模式：{mode}）。",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        mode, _src, files = self._load_lua_files(root)
        if mode == "none":
            return []
        entries: list[Entry] = []
        for rel, text in sorted(files.items()):
            for lineno, order, val in parse_lua_strings(text):
                loc = f"{lineno}#{order}"
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}#{loc}",
                        english=val,
                        source="game",
                        kind="lua",
                        rel_path=rel,
                        json_pointer=loc,
                    )
                )
        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        mode, src_obj, files = self._load_lua_files(root)
        entries = self.collect(root)
        report = ApplyReport()
        if not entries or mode == "none":
            report.notes.append("未找到可回写的 LÖVE Lua 条目")
            return report

        by_file: dict[str, list[Entry]] = {}
        for e in entries:
            by_file.setdefault(e.rel_path, []).append(e)

        modified: dict[str, str] = {}
        for rel, file_entries in by_file.items():
            raw_text = files.get(rel, "")
            lua_map: dict[str, str] = {}
            for e in file_entries:
                tr = translations.get(e.key) or translations.get(e.english)
                if tr and tr.strip():
                    lua_map[e.json_pointer] = tr
                    lua_map[e.english] = tr
                else:
                    report.missed += 1
            if lua_map:
                new_text, count = apply_lua_strings(raw_text, lua_map)
                if count > 0:
                    modified[rel] = new_text
                    report.applied += count

        if not modified:
            report.notes.append("未命中任何译文，未修改文件")
            return report

        undo = UndoLog.open(base, engine=self.name)
        if mode == "love" and isinstance(src_obj, Path):
            default_out = src_obj.with_name(f"{src_obj.stem}.cn.love")
            target_out = unique_output(Path(out) if out else default_out)
            with zipfile.ZipFile(src_obj, "r") as zin, zipfile.ZipFile(target_out, "w") as zout:
                for info in zin.infolist():
                    data = modified[info.filename].encode("utf-8") if info.filename in modified else zin.read(info.filename)
                    fresh = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
                    fresh.compress_type = info.compress_type
                    fresh.external_attr = info.external_attr
                    zout.writestr(fresh, data)
            undo.did_copy(target_out)
            report.undo_path = undo.finish()
            report.out_path = target_out
            report.in_place = False
            report.notes.append(f"已生成汉化 LÖVE 归档副本：{target_out.name}。")
            return report

        if mode == "zip-in-exe" and isinstance(src_obj, ZipInExe):
            for rel, new_text in modified.items():
                src_obj.write_text(rel, new_text, bom=False)
            default_out = src_obj.source.with_name(f"{src_obj.source.stem}.cn.exe")
            target_out = unique_output(Path(out) if out else default_out)
            src_obj.commit(target_out)
            undo.did_copy(target_out)
            report.undo_path = undo.finish()
            report.out_path = target_out
            report.in_place = False
            report.notes.append(f"已生成汉化 LÖVE 融合 EXE 副本：{target_out.name}。")
            return report

        # 散装目录原地回写
        for rel, new_text in modified.items():
            undo.will_modify(rel)
            (base / rel).write_text(new_text, encoding="utf-8")
            undo.did_write(rel)
        report.undo_path = undo.finish()
        report.out_path = base
        report.in_place = True
        report.notes.append(f"已原地更新 {len(modified)} 个 Lua 脚本文件。")
        return report
