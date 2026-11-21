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

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ferrycore.base import app_dir, atomic_write_text

from .units import Unit

TL_NAME = "ferry_tl.json"
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
        return target


def default_path(root: Path) -> Path:
    root = Path(root)
    return (root.parent if root.is_file() else root) / TL_NAME


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
    return project


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

