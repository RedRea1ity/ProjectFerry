"""已汉化游戏登记 / 发现。

副本型（Ace 等）产出 `游戏名.cn`；原地型（Ren'Py 等）改原目录并落
`.ferry-undo.json`。用户常找不到「汉化完去哪了」—— 这一层专门回答：

    find_localized(root)  → 这份游戏相关的汉化产物（含路径、是否原地）
    list_recent()         → UI 记住的最近几条
    remember()            → 汉化成功后写入 ferry_ui 同级的记录文件
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .adapters.undo import UNDO_NAME, load as load_undo
from .base import app_dir

HISTORY_NAME = "ferry_localize_history.json"
_MAX_HISTORY = 40


@dataclass
class LocalizedRecord:
    """一处可玩的汉化结果。"""

    root: str  # 用户选的原游戏目录
    play_path: str  # 真正该打开/进游戏的目录
    mode: str  # in_place | copy
    engine: str = ""
    undo_path: str = ""
    created: str = ""
    label: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "LocalizedRecord":
        return cls(
            root=str(data.get("root") or ""),
            play_path=str(data.get("play_path") or data.get("root") or ""),
            mode=str(data.get("mode") or "copy"),
            engine=str(data.get("engine") or ""),
            undo_path=str(data.get("undo_path") or ""),
            created=str(data.get("created") or ""),
            label=str(data.get("label") or ""),
        )


def _history_path() -> Path:
    return app_dir() / HISTORY_NAME


def load_history() -> list[LocalizedRecord]:
    path = _history_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[LocalizedRecord] = []
    for item in raw:
        if isinstance(item, dict) and item.get("play_path"):
            out.append(LocalizedRecord.from_json(item))
    return out


def save_history(records: list[LocalizedRecord]) -> None:
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [r.to_json() for r in records[:_MAX_HISTORY]]
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def remember(record: LocalizedRecord) -> None:
    """汉化成功后登记；同 play_path 的旧条顶掉。"""
    records = [r for r in load_history() if r.play_path != record.play_path]
    records.insert(0, record)
    save_history(records)


def remove_history(play_path: str | Path) -> bool:
    """从历史登记中移除指定 play_path（只删记录，不动磁盘上的任何游戏文件）。"""
    target = str(play_path).strip().lower()
    if not target:
        return False
    records = load_history()
    kept = [r for r in records if str(r.play_path).strip().lower() != target]
    if len(kept) == len(records):
        return False
    save_history(kept)
    return True


def prune_history() -> int:
    """清理历史里已经不存在于磁盘的失效条目，返回清理条数。"""
    records = load_history()
    kept = [r for r in records if r.play_path and Path(r.play_path).exists()]
    removed = len(records) - len(kept)
    if removed:
        save_history(kept)
    return removed


def clear_history() -> int:
    """清空全部产物历史登记（只清空记录文件，不删除磁盘上的游戏或副本），返回原条数。"""
    records = load_history()
    count = len(records)
    save_history([])
    return count


def _record_from_undo(root: Path) -> LocalizedRecord | None:
    data = load_undo(root)
    if not data:
        return None
    actions = data.get("actions") or []
    mode = "in_place"
    play = root
    # 副本型：undo 在原目录，actions 里有 copy 指向副本
    for action in actions:
        if isinstance(action, dict) and action.get("kind") == "copy":
            cand = Path(str(action.get("path") or ""))
            if cand.exists():
                play = cand
                mode = "copy"
                break
    # 原地型：undo 就在被改的游戏目录
    if mode == "in_place" and not any(
        isinstance(a, dict) and a.get("kind") in ("added", "modified") for a in actions
    ):
        # 只有 copy、没有改文件 —— 仍算 copy
        pass
    label = play.name
    return LocalizedRecord(
        root=str(root.resolve()),
        play_path=str(play.resolve()),
        mode=mode,
        engine=str(data.get("engine") or ""),
        undo_path=str((root / UNDO_NAME).resolve()),
        created=str(data.get("created") or ""),
        label=label,
    )


def find_localized(root: Path) -> list[LocalizedRecord]:
    """围绕用户选的目录，找出已有汉化产物。"""
    root = Path(root).resolve()
    found: list[LocalizedRecord] = []
    seen: set[str] = set()

    def add(rec: LocalizedRecord | None) -> None:
        if rec is None or not rec.play_path:
            return
        key = str(Path(rec.play_path).resolve()).lower()
        if key in seen:
            return
        if not Path(rec.play_path).exists():
            return
        seen.add(key)
        found.append(rec)

    # 1) 目录本身有 undo（原地，或 Ace 原目录上记了 copy）
    add(_record_from_undo(root))

    # 2) 旁路副本：目录型为 游戏名.cn / .cn2 …，单文件型为 文件名.cn.扩展名 / 文件名.cn2.扩展名 …
    parent = root.parent
    if parent.is_dir():
        if root.is_file():
            stem_prefix = f"{root.stem}.cn"
            suffix_low = root.suffix.lower()
            for child in sorted(parent.iterdir()):
                if not child.is_file() or child == root:
                    continue
                if child.suffix.lower() != suffix_low:
                    continue
                if child.stem.startswith(stem_prefix):
                    add(
                        LocalizedRecord(
                            root=str(root),
                            play_path=str(child.resolve()),
                            mode="copy",
                            label=child.name,
                            created="",
                        )
                    )
        else:
            stem = root.name
            for child in sorted(parent.iterdir()):
                if not child.is_dir():
                    continue
                name = child.name
                if name == stem or name.startswith(stem + ".cn"):
                    add(_record_from_undo(child))
                    # 没有 undo 但明显是 .cn 副本
                    if name.startswith(stem + ".cn") and name not in {r.label for r in found}:
                        add(
                            LocalizedRecord(
                                root=str(root),
                                play_path=str(child.resolve()),
                                mode="copy",
                                label=name,
                                created="",
                            )
                        )

    # 3) 历史里同 root 的记录
    root_key = str(root).lower()
    for rec in load_history():
        if str(Path(rec.root).resolve()).lower() == root_key:
            add(rec)

    return found


def describe(record: LocalizedRecord) -> str:
    mode = "原地改写" if record.mode == "in_place" else "副本"
    engine = f"（{record.engine}）" if record.engine else ""
    return f"{mode}{engine}：{record.play_path}"
