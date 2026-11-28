"""译文表：一个游戏一份 `ferry_tl.json`，人能直接打开改。

小众游戏没有汉化组帮你校对，校对的只有你自己。所以译文不能只活在缓存哈希里，
必须落成一份**明文、按游戏顺序排好、能用记事本改**的文件：

    {
      "format": "ferry-game-tl", "version": 1,
      "game": "八星帝", "adapter": "rawbin", "source_lang": "ja",
      "glossary": {"アリス": "爱丽丝"},
      "units": [
        {"key": "...", "kind": "story", "src": "原文", "dst": "译文", ...},
        ...
      ]
    }

改了 `dst` 再跑 `apply` 就直接写回游戏，不花一个 token。
重新 `scan` 时，原文没变的条目保留你的译文；原文变了的才清空重翻。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ferrycore.base import app_dir, atomic_write_text

from .units import Unit

TL_NAME = "ferry_tl.json"
TSV_NAME = "ferry_tl.tsv"
FORMAT = "ferry-game-tl"
VERSION = 1


@dataclass
class Project:
    root: Path
    game: str = ""
    adapter: str = ""
    source_lang: str = ""
    target_encoding: str = ""
    glossary: dict[str, str] = field(default_factory=dict)
    units: list[Unit] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    path: Path | None = None

    # ---- 统计 ----

    def stats(self) -> dict[str, int]:
        out = {"total": 0, "done": 0, "pending": 0, "story": 0, "menu": 0, "too_long": 0, "manual": 0}
        for unit in self.units:
            if not unit.translatable:
                continue
            out["total"] += 1
            out[unit.kind] = out.get(unit.kind, 0) + 1
            if unit.done:
                out["done"] += 1
            else:
                out["pending"] += 1
            if unit.note.startswith("超长"):
                out["too_long"] += 1
            if unit.origin == "manual":
                out["manual"] += 1
        return out

    def summary(self) -> str:
        s = self.stats()
        line = f"共 {s['total']} 条（剧情 {s['story']} / 菜单 {s['menu']}），已翻 {s['done']}，待翻 {s['pending']}"
        if s["too_long"]:
            line += f"，{s['too_long']} 条超长待人工缩短"
        return line

    # ---- 读写 ----

    def to_json(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "version": VERSION,
            "game": self.game,
            "root": str(self.root),
            "adapter": self.adapter,
            "source_lang": self.source_lang,
            "target_encoding": self.target_encoding,
            "notes": self.notes,
            "glossary": self.glossary,
            "units": [unit.to_json() for unit in self.units],
        }

    def save(self, path: Path | None = None) -> Path:
        target = Path(path or self.path or default_path(self.root))
        text = json.dumps(self.to_json(), ensure_ascii=False, indent=1) + "\n"
        try:
            atomic_write_text(target, text)
        except OSError:
            # 游戏装在 Program Files 之类写不进的地方：退到摆渡自己的目录
            target = fallback_path(self.root)
            atomic_write_text(target, text)
        self.path = target
        try:
            export_tsv(self, _tsv_sibling(target))
        except OSError:
            pass
        return target


def default_path(root: Path) -> Path:
    root = Path(root)
    if root.is_file():
        return root.parent / f"{root.stem}.{TL_NAME}"
    return root / TL_NAME


def fallback_path(root: Path) -> Path:
    root = Path(root).resolve()
    tag = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:8]
    folder = app_dir() / "game_projects" / f"{root.name}-{tag}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / TL_NAME


def find(root: Path) -> Path | None:
    for candidate in (default_path(root), fallback_path(root)):
        if candidate.is_file():
            return candidate
    return None


def _tsv_sibling(json_path: Path) -> Path:
    name = json_path.name
    if name.endswith(f".{TL_NAME}"):
        prefix = name[: -len(TL_NAME)]
        return json_path.with_name(f"{prefix}{TSV_NAME}")
    return json_path.with_name(TSV_NAME)


def load(root: Path) -> Project | None:
    path = find(root)
    if path is None:
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError(f"{path} 不是摆渡的译文表")
    if int(data.get("version") or 0) != VERSION:
        raise ValueError(f"{path} 的版本是 {data.get('version')}，当前只认 {VERSION}")
    project = Project(
        root=Path(root),
        game=str(data.get("game", "")),
        adapter=str(data.get("adapter", "")),
        source_lang=str(data.get("source_lang", "")),
        target_encoding=str(data.get("target_encoding", "")),
        glossary={str(k): str(v) for k, v in (data.get("glossary") or {}).items() if str(k).strip()},
        units=[Unit.from_json(u) for u in data.get("units") or [] if isinstance(u, dict)],
        notes=[str(n) for n in data.get("notes") or []],
        path=path,
    )
    tsv_sibling = _tsv_sibling(path)
    if tsv_sibling.is_file():
        try:
            import_external_translations(project, tsv_sibling, save=False)
        except Exception:
            pass
    return project


def _esc_cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace("\t", "\\t")


def _unesc_cell(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "t":
                out.append("\t")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def export_tsv(project: Project, path: Path | None = None) -> Path:
    """将项目的待翻/已翻条目导出为 Excel / 记事本 / ParaTranz 友好的 UTF-8-BOM `.tsv` 双语表。"""
    base_json = project.path or default_path(project.root)
    target = Path(path) if path is not None else _tsv_sibling(base_json)
    lines = ["key\tkind\tmax_bytes\tsrc\tdst\tnote"]
    for u in project.units:
        if not u.translatable:
            continue
        mb = str(u.max_bytes) if u.max_bytes else ""
        lines.append(
            f"{_esc_cell(u.key)}\t{u.kind}\t{mb}\t{_esc_cell(u.src)}\t{_esc_cell(u.dst)}\t{_esc_cell(u.note)}"
        )
    content = "\n".join(lines) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    # 带 UTF-8 BOM (\xef\xbb\xbf)，Windows 下双击用 Excel 打开不会乱码
    target.write_bytes(content.encode("utf-8-sig"))
    return target


def import_external_translations(
    project: Project,
    path: Path | None = None,
    save: bool = True,
) -> int:
    """从外部自翻译文件（`.tsv` / `.csv` / `.json` / ParaTranz 词表）回录译文到 `Project`。

    返回成功更新/回录的条数。
    """
    base_json = project.path or default_path(project.root)
    src_file = Path(path) if path is not None else _tsv_sibling(base_json)
    if not src_file.is_file():
        return 0

    raw_text = src_file.read_text(encoding="utf-8-sig", errors="replace")
    by_key: dict[str, str] = {}
    by_src: dict[str, str] = {}

    suf = src_file.suffix.lower()
    if suf == ".json":
        payload = json.loads(raw_text)
        if isinstance(payload, dict):
            if isinstance(payload.get("units"), list):
                for item in payload["units"]:
                    if isinstance(item, dict) and item.get("dst"):
                        k = str(item.get("key") or "")
                        s = str(item.get("src") or "")
                        d = str(item.get("dst") or "")
                        if k:
                            by_key[k] = d
                        if s:
                            by_src[s] = d
            else:
                for k, v in payload.items():
                    if isinstance(v, str) and v.strip():
                        by_key[str(k)] = v
                        by_src[str(k)] = v
        elif isinstance(payload, list):
            # 兼容 ParaTranz JSON: [{"key": "...", "original": "...", "translation": "..."}]
            for item in payload:
                if not isinstance(item, dict):
                    continue
                d = str(item.get("translation") or item.get("dst") or "").strip()
                if not d:
                    continue
                k = str(item.get("key") or "")
                s = str(item.get("original") or item.get("src") or "")
                if k:
                    by_key[k] = d
                if s:
                    by_src[s] = d
    elif suf == ".csv":
        rows = list(csv.reader(io.StringIO(raw_text)))
        for idx, row in enumerate(rows):
            if idx == 0 and row and row[0].strip().lower() in ("key", "id", "src", "original"):
                continue
            if len(row) >= 5:
                k, s, d = row[0].strip(), row[3], row[4]
                if d.strip():
                    by_key[k] = d
                    by_src[s] = d
            elif len(row) >= 2:
                s, d = row[0], row[1]
                if d.strip():
                    by_key[s] = d
                    by_src[s] = d
    else:
        # 默认按 .tsv 解析
        for idx, line in enumerate(raw_text.splitlines()):
            if not line.strip():
                continue
            parts = line.split("\t")
            if idx == 0 and parts[0].strip().lower() in ("key", "id", "src"):
                continue
            if len(parts) >= 5:
                k = _unesc_cell(parts[0])
                s = _unesc_cell(parts[3])
                d = _unesc_cell(parts[4])
                if d.strip():
                    by_key[k] = d
                    by_src[s] = d
            elif len(parts) >= 2:
                s = _unesc_cell(parts[0])
                d = _unesc_cell(parts[1])
                if d.strip():
                    by_key[s] = d
                    by_src[s] = d

    updated = 0
    for unit in project.units:
        if not unit.translatable:
            continue
        new_dst = by_key.get(unit.key)
        if new_dst is None:
            new_dst = by_src.get(unit.src)
        if new_dst is not None and new_dst.strip():
            if unit.dst != new_dst:
                unit.dst = new_dst
                unit.origin = "manual"
                updated += 1
            elif not unit.origin:
                unit.origin = "manual"

    if updated > 0 and save:
        project.save()
    return updated


def merge(old: Project | None, fresh: Project) -> Project:
    """新扫出来的条目 + 旧表里的译文。

    只认「同一个 key 且原文没变」：原文变了说明游戏更新过，旧译文已经对不上了。
    术语表整份沿用 —— 那是人填的，扫描不该碰。
    """
    if old is None:
        return fresh
    by_key = {unit.key: unit for unit in old.units}
    kept = 0
    for unit in fresh.units:
        prev = by_key.get(unit.key)
        if prev is None or prev.src != unit.src:
            continue
        unit.dst, unit.origin, unit.note = prev.dst, prev.origin, prev.note
        if prev.speaker and not unit.speaker:
            unit.speaker = prev.speaker
        kept += 1
    fresh.glossary = {**old.glossary, **fresh.glossary}
    fresh.path = old.path
    if not fresh.source_lang:
        fresh.source_lang = old.source_lang
    if kept:
        fresh.notes.append(f"沿用旧译文表里 {kept} 条译文")
    return fresh

