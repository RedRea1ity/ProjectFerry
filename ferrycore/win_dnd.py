"""窗口文件拖放辅助（基于原生 C 扩展 tkinterdnd2 / tkdnd，无 Win32 WndProc 崩溃风险）。

支持把游戏文件夹、可执行程序、镜像或 Minecraft 实例直接拖进主窗口：
- 依赖 `tkinterdnd2` 提供的原生 `tkdnd` OLE 拖放（支持中文路径、空格、括号与多文件）；
- 未安装 `tkinterdnd2` 时静默降级，完全不影响软件正常启动与使用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ferrycore.base import LOG

try:
    from tkinterdnd2 import COPY as _DND_COPY
    from tkinterdnd2 import DND_FILES as _DND_FILES
    from tkinterdnd2 import TkinterDnD as _TkinterDnD

    HAS_TKDND = True
except Exception:
    _DND_COPY = "copy"
    _DND_FILES = None
    _TkinterDnD = None
    HAS_TKDND = False


def tk_root_base(tk_module):
    """返回主窗口基类：有 tkinterdnd2 时用 TkinterDnD.Tk，否则退回 tk.Tk。"""
    if HAS_TKDND and _TkinterDnD is not None:
        return _TkinterDnD.Tk
    return tk_module.Tk


def classify_dropped_path(path: Path) -> tuple[str, Path]:
    """把拖入窗口的路径分流为 Minecraft 实例或非 MC 游戏目标（目录或精确单文件）。

    返回 `(kind, target_path)`，其中 `kind` 为 `"mc"` | `"game"` | `"invalid"`。
    拖入单个非 MC 文件时保留精确文件路径，避免退回父目录后误扫同目录里的其他游戏文件。
    """
    try:
        p = Path(path)
    except TypeError:
        return "invalid", Path(".")
    if not p.exists():
        return "invalid", p
    if p.is_dir():
        mods_dir = p / "mods"
        if mods_dir.is_dir():
            has_mc_markers = (
                (p / "resourcepacks").is_dir()
                or (p / "options.txt").is_file()
                or (p / "versions").is_dir()
                or any(mods_dir.glob("*.jar"))
            )
            if has_mc_markers:
                return "mc", p
        return "game", p
    # 拖入的是单个文件：若它是 MC mods 目录下的 .jar，回到实例根目录；否则精准保留该文件本身。
    if p.suffix.lower() == ".jar" and p.parent.name.lower() == "mods":
        candidate = p.parent.parent
        if (candidate / "mods").is_dir():
            return "mc", candidate
    return "game", p


def parse_drop_paths(widget, raw_data: str) -> list[Path]:
    """把 tkdnd 传入的 Tcl 路径列表（含空格花括号与中文路径）安全拆成 Path 列表。"""
    if not raw_data:
        return []
    try:
        items = widget.tk.splitlist(raw_data)
    except Exception:
        items = [raw_data]
    paths: list[Path] = []
    for item in items:
        text = str(item).strip().strip('"')
        if text:
            paths.append(Path(text))
    return paths


class DropHook:
    """tkdnd 拖放注册句柄，退出时安全解绑。"""

    def __init__(self, widget) -> None:
        self._widget = widget
        self._active = True

    def uninstall(self) -> None:
        if not self._active:
            return
        self._active = False
        try:
            if hasattr(self._widget, "drop_target_unregister"):
                self._widget.drop_target_unregister()
        except Exception:
            pass
        self._widget = None


def enable_window_file_drop(widget, on_drop: Callable[[list[Path]], None]) -> DropHook | None:
    """使用原生 tkdnd 为 Tkinter 窗口注册文件拖放；环境不支持时静默返回 None。"""
    if not HAS_TKDND or _DND_FILES is None or _TkinterDnD is None:
        return None
    try:
        if not hasattr(widget, "TkdndVersion"):
            widget.TkdndVersion = _TkinterDnD._require(widget)
        if not hasattr(widget, "drop_target_register"):
            return None
        widget.drop_target_register(_DND_FILES)

        def _handle_drop(event):
            paths = parse_drop_paths(widget, getattr(event, "data", ""))
            if paths:
                widget.after(0, lambda items=paths: on_drop(items))
            return _DND_COPY

        widget.dnd_bind("<<Drop>>", _handle_drop)
        return DropHook(widget)
    except Exception as exc:
        LOG.debug("tkdnd file drop setup skipped: %s", exc)
        return None
