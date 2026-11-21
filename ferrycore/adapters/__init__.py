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
from .console import PspAdapter, PsvAdapter, parse_iso9660, parse_param_sfo
from .container import (
    AsarContainer,
    Container,
    Document,
    FolderGame,
    ZipInExe,
    find_asar,
    locate_appended_zip,
    open_container,
    parse_asar,
    rebuild_asar,
    unique_output,
)
from .gamemaker import GameMakerAdapter
from .godot import GodotAdapter
from .hints import scan_hints
from .hsp import HspAdapter
from .i18n_table import I18nTableAdapter
from .kirikiri import KirikiriAdapter
from .mobile import ApkAdapter, IpaAdapter, extract_dex_strings, patch_dex_strings
from .renpy import RenpyAdapter
from .rpgmaker import RpgMakerAdapter
from .rpgmaker_2k import RpgMaker2kAdapter
from .rpgmaker_ace import RpgMakerAceAdapter
from .tyrano import TyranoAdapter
from .undo import UNDO_NAME, RevertReport, UndoLog
from .undo import revert as revert_game
from .unity import UnityAdapter
from ferrygame.pc98 import PC98Adapter
from ferrygame.rawbin import RawBinaryAdapter

# 数字小的先探测。认领越具体的排越前，通用兜底排最后。
REGISTRY: list[type[Adapter]] = [
    ApkAdapter,
    IpaAdapter,
    PsvAdapter,
    PspAdapter,
    PC98Adapter,
    TyranoAdapter,
    KirikiriAdapter,
    RenpyAdapter,
    RpgMaker2kAdapter,
    RpgMakerAceAdapter,  # rgss3a / rvdata2，必须在 MV/MZ 明文适配器之前
    RpgMakerAdapter,
    GodotAdapter,
    UnityAdapter,
    GameMakerAdapter,
    I18nTableAdapter,
    HspAdapter,
    RawBinaryAdapter,
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
    "ApkAdapter",
    "ApplyReport",
    "AsarContainer",
    "Container",
    "Document",
    "FolderGame",
    "GameMakerAdapter",
    "GodotAdapter",
    "HspAdapter",
    "I18nTableAdapter",
    "IpaAdapter",
    "KirikiriAdapter",
    "PC98Adapter",
    "ProbeReport",
    "PspAdapter",
    "PsvAdapter",
    "RawBinaryAdapter",
    "RenpyAdapter",
    "RevertReport",
    "RpgMaker2kAdapter",
    "RpgMakerAceAdapter",
    "RpgMakerAdapter",
    "TyranoAdapter",
    "UNDO_NAME",
    "UndoLog",
    "UnityAdapter",
    "VerifyReport",
    "ZipInExe",
    "detect",
    "extract_dex_strings",
    "find_asar",
    "get",
    "locate_appended_zip",
    "open_container",
    "parse_asar",
    "parse_iso9660",
    "parse_param_sfo",
    "patch_dex_strings",
    "rebuild_asar",
    "registered",
    "revert_game",
    "scan_hints",
    "unique_output",
]
