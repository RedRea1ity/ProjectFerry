"""引擎适配器 —— 摆渡的「外挂模组」。

本体只认识 Minecraft。每多一个引擎，就往这个目录里加一个文件，
**不改 `scan` / `packio` / `engine` / `cli` 的任何逻辑**。

    detect(root)  -> Adapter | None     自动认领
    get(name)     -> Adapter | None     按名字取

新增一个引擎的顺序：
1. 在 `container` 里确认它的数据装在哪儿（散装 / zip-in-exe / 需要打包器）
2. 新建 `<engine>.py`，实现 probe / collect / apply
3. 加进下面的 `REGISTRY`
4. 写测试：至少证明「抽出来的条数」和「写回去以后文件仍能被解析」

`probe` 答不出 `writable` 的引擎，不要加进来 —— 翻了也白翻。
"""

from __future__ import annotations

from pathlib import Path

from .base import Adapter, ApplyReport, ProbeReport, VerifyReport
from .container import Container, Document, FolderGame, ZipInExe, locate_appended_zip, open_container, unique_output
from .hints import scan_hints
from .hsp import HspAdapter
from .renpy import RenpyAdapter
from .rpgmaker import RpgMakerAdapter
from .tyrano import TyranoAdapter
from .undo import UNDO_NAME, RevertReport, UndoLog
from .undo import revert as revert_game

# 数字小的先探测。认领越具体的排越前，通用兜底排最后。
REGISTRY: list[type[Adapter]] = [
    TyranoAdapter,
    RenpyAdapter,
    RpgMakerAdapter,
    HspAdapter,
]


def registered() -> list[str]:
    return [cls.name for cls in REGISTRY]


def _build(cls: type[Adapter], options: dict) -> Adapter:
    """按需把选项喂给构造器；适配器不收这个选项就退回无参构造。"""
    try:
        return cls(**options)
    except TypeError:
        return cls()


def get(name: str, **options) -> Adapter | None:
    for cls in REGISTRY:
        if cls.name == name:
            return _build(cls, options)
    return None


def detect(root: Path, **options) -> Adapter | None:
    """交给第一个认领成功的适配器。认不出就返回 None —— 别猜。"""
    for cls in sorted(REGISTRY, key=lambda c: c.priority):
        adapter = _build(cls, options)
        try:
            if adapter.probe(Path(root)).readable:
                return adapter
        except Exception:
            # 单个适配器炸了不该拖垮整轮探测
            continue
    return None


__all__ = [
    "Adapter",
    "ApplyReport",
    "Container",
    "Document",
    "FolderGame",
    "HspAdapter",
    "ProbeReport",
    "RenpyAdapter",
    "RevertReport",
    "RpgMakerAdapter",
    "TyranoAdapter",
    "UNDO_NAME",
    "UndoLog",
    "VerifyReport",
    "ZipInExe",
    "detect",
    "get",
    "locate_appended_zip",
    "open_container",
    "registered",
    "revert_game",
    "scan_hints",
    "unique_output",
]
