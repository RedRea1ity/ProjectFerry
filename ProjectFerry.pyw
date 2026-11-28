"""摆渡计划 / ProjectFerry: 双击启动的桌面界面。"""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
import traceback
import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import mc_ai_translator as core
from ferrycore.win_dnd import classify_dropped_path, enable_window_file_drop, tk_root_base
from ferrycore.workbench import ProjectIndex, Workbench


# 可写文件放 core.app_dir()（打包后=exe 目录）；图标是只读资源，放脚本/解压目录旁。
WHITELIST_FILE = core.app_dir() / "ferry_whitelist.json"
UI_STATE_FILE = core.app_dir() / "ferry_ui.json"
ICON_FILE = Path(__file__).with_name("ferry_icon.png")

APP_NAME = "ProjectFerry"
APP_DISPLAY = "摆渡计划"
APP_VERSION = "2.0.0-alpha.6.1"
APP_AUTHOR = "红现实"
APP_LICENSE = "MIT License"
APP_SLOGAN = "人无语言则茫然无依，故为摆渡。"
PROJECT_URL = "https://github.com/RedRea1ity/ProjectFerry"

ENGINE_KEY_FIELDS = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "gemini": "gemini_api_key",
    "local": "local_api_key",
    "deepl": "deepl_api_key",
}
ENGINE_URL_FIELDS = {
    "openai": "openai_base_url",
    "anthropic": "anthropic_base_url",
    "gemini": "gemini_base_url",
    "local": "local_base_url",
}
ENGINE_MODEL_FIELDS = {
    "openai": "openai_model",
    "anthropic": "anthropic_model",
    "gemini": "gemini_model",
    "local": "local_model",
}
FREE_ENGINES = {
    "mymemory": "MyMemory（免费）",
    "deepl": "DeepL Free（免费）",
}
CUSTOM_ENGINES = {
    "openai": "OpenAI 兼容",
    "anthropic": "Anthropic 兼容",
    "gemini": "Gemini 兼容",
    "local": "本地模型（Ollama/LM Studio）",
}
CUSTOM_CATEGORY_LABEL = "自定义接口"
FREE_LABEL_TO_ENGINE = {v: k for k, v in FREE_ENGINES.items()}
CUSTOM_LABEL_TO_ENGINE = {v: k for k, v in CUSTOM_ENGINES.items()}

PLACEHOLDER_MODID = "—— 无待汉化模组 ——"

# —— 视觉基调：渡口水色（日间）/ 夜渡深青（夜间）。只让主操作带颜色，其余安静。 ——
THEMES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#F2F5F4",
        "card": "#FFFFFF",
        "border": "#D8DEE6",
        "ink": "#182E2B",
        "body_ink": "#4E5964",
        "muted": "#526761",
        "faint": "#9AA5B1",
        "accent": "#096B61",
        "green": "#1E7A3C",
        "amber": "#9A6A00",
        "red": "#C2372E",
        "select_bg": "#C7E6D9",
        "row_even": "#F6F8FB",
        "row_odd": "#FFFFFF",
        "field_bg": "#FFFFFF",
        "btn_bg": "#E7ECEB",
        "btn_hover": "#D9E2E0",
        "trough": "#E2E7EE",
        "rail": "#203D38",
        "rail_active_bg": "#C7E6D9",
        "rail_active_fg": "#182E2B",
        "rail_sub": "#B7D4C9",
        "rail_sep": "#49675F",
        "rail_hover": "#396158",
    },
    "dark": {
        "bg": "#141A19",
        "card": "#1C2423",
        "border": "#2D3C3A",
        "ink": "#E6EFEC",
        "body_ink": "#C0CFCB",
        "muted": "#8FA6A0",
        "faint": "#627872",
        "accent": "#26A69A",
        "green": "#38B26C",
        "amber": "#E0A43B",
        "red": "#E05A50",
        "select_bg": "#244B44",
        "row_even": "#18201F",
        "row_odd": "#1C2423",
        "field_bg": "#151C1B",
        "btn_bg": "#253230",
        "btn_hover": "#30413E",
        "trough": "#232E2C",
        "rail": "#0F1716",
        "rail_active_bg": "#244B44",
        "rail_active_fg": "#E6EFEC",
        "rail_sub": "#85A39B",
        "rail_sep": "#253633",
        "rail_hover": "#1D2E2B",
    },
}

BG = THEMES["light"]["bg"]
CARD = THEMES["light"]["card"]
BORDER = THEMES["light"]["border"]
INK = THEMES["light"]["ink"]
BODY_INK = THEMES["light"]["body_ink"]
MUTED = THEMES["light"]["muted"]
FAINT = THEMES["light"]["faint"]
ACCENT = THEMES["light"]["accent"]
GREEN = THEMES["light"]["green"]
AMBER = THEMES["light"]["amber"]
RED = THEMES["light"]["red"]
SELECT_BG = THEMES["light"]["select_bg"]
FONT_FAMILY = "Microsoft YaHei UI"
FONT_TITLE = (FONT_FAMILY, 12, "bold")
FONT_BODY = (FONT_FAMILY, 10)
FONT_BOLD = (FONT_FAMILY, 10, "bold")
FONT_SMALL = (FONT_FAMILY, 9)
FONT_SMALL_BOLD = (FONT_FAMILY, 9, "bold")

# C：筛选档位（顺序即下拉框顺序）。
TABLE_FILTERS = ["全部", "只看有缺口", "只看失败", "只看含人工汉化", "只看疑似硬编码", "只看已完成"]
SEARCH_PLACEHOLDER = "搜索模组名或状态…"


def _shade(color: str, factor: float) -> str:
    try:
        color = color.lstrip("#")
        r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
        return "#%02x%02x%02x" % (
            max(0, min(255, int(r * factor))),
            max(0, min(255, int(g * factor))),
            max(0, min(255, int(b * factor))),
        )
    except Exception:
        return color


class RoundedButton(tk.Canvas):
    """圆角高亮按钮：外观与原生按钮一致，支持文字 / 状态切换。"""

    def __init__(self, parent, text: str = "", command=None, fill: str = ACCENT, fg: str = "#FFFFFF",
                 font=FONT_BOLD, padx: int = 18, pady: int = 6,
                 radius: int = 7, state: str = "normal", **kwargs):
        bg = kwargs.pop("bg", None)
        if bg is None:
            try:
                bg = parent.cget("background")
            except Exception:
                bg = None
        if not bg:
            try:
                bg = ttk.Style(parent).lookup("TFrame", "background") or BG
            except Exception:
                bg = BG
        self._text = text
        self._command = command
        self._fill = fill
        self._fg = fg
        self._state = state
        self._font = font
        self._radius = radius
        self._hover = False
        self._pressed = False
        import tkinter.font as tkfont
        metrics = tkfont.Font(font=font)
        text_w = metrics.measure(text) if text else 0
        text_h = metrics.metrics("linespace") if text else 0
        self._width = int(kwargs.pop("width", 0) or (text_w + 2 * padx))
        self._height = int(kwargs.pop("height", 0) or (text_h + 2 * pady))
        super().__init__(parent, width=self._width, height=self._height, highlightthickness=0, bd=0, bg=bg, **kwargs)
        self.configure(cursor="hand2", takefocus=1, highlightthickness=1, highlightcolor=ACCENT, highlightbackground=bg)
        self.bind("<Return>", self._on_key)
        self.bind("<space>", self._on_key)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        disabled = self._state == "disabled"
        base = "#AEB6BF" if disabled else self._fill
        color = _shade(base, 0.9) if (self._hover and not disabled) else base
        fg = "#EDEDED" if disabled else self._fg
        w, h, r = self._width, self._height, self._radius
        points = [
            r, 1, w - r, 1, w - 1, 1, w - 1, r,
            w - 1, h - r, w - 1, h - 1, w - r, h - 1, r, h - 1,
            1, h - 1, 1, h - r, 1, r, 1, 1,
        ]
        self.create_polygon(points, smooth=True, splinesteps=16, fill=color, outline=color)
        self.create_text(w / 2, h / 2, text=self._text, fill=fg, font=self._font)

    def _on_enter(self, _event: object = None) -> None:
        self._hover = True

    def set_surface_bg(self, bg: str, highlight: str | None = None) -> None:
        self.configure(bg=bg, highlightbackground=bg)
        if highlight:
            self.configure(highlightcolor=highlight)
        self._draw()
        self._draw()

    def _on_leave(self, _event: object = None) -> None:
        self._hover = False
        self._draw()

    def _on_press(self, _event: object = None) -> None:
        self._pressed = True

    def _on_key(self, _event: object = None) -> str:
        if self._state != "disabled" and self._command:
            self._command()
        return "break"

    def _on_release(self, event: object) -> None:
        self._pressed = False
        inside = 0 <= getattr(event, "x", 0) <= self._width and 0 <= getattr(event, "y", 0) <= self._height
        if self._state != "disabled" and inside and self._command:
            self._command()

    def configure(self, cnf=None, **kw):
        handled = False
        if "text" in kw:
            self._text = str(kw.pop("text"))
            handled = True
        if "state" in kw:
            self._state = str(kw.pop("state"))
            handled = True
        if "bg" in kw or "fill" in kw:
            self._fill = str(kw.pop("bg", kw.pop("fill", self._fill)))
            handled = True
        if "fg" in kw:
            self._fg = str(kw.pop("fg"))
            handled = True
        if isinstance(cnf, dict):
            for key, value in list(cnf.items()):
                if key in ("text", "state", "bg", "fill", "fg"):
                    kw[key] = value
                    cnf = {k: v for k, v in cnf.items() if k != key}
                    handled = True
        result = super().configure(cnf, **kw) if (cnf or kw) else None
        if handled:
            self._draw()
        return result

    config = configure

    def cget(self, key: str):
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        if key in ("bg", "fill"):
            return self._fill
        if key == "fg":
            return self._fg
        return super().cget(key)


def make_action_button(parent, text: str, command, color: str, state: str = "normal", bg: str | None = None) -> RoundedButton:
    return RoundedButton(parent, text=text, command=command, fill=color, state=state, bg=bg)


STATUS_SYMBOLS = {
    "complete": "✓", "done": "✓", "partial": "◐", "pending": "○",
    "community": "◈", "reverted": "⊘", "uninstalled": "⊗", "failed": "✗",
}
STATUS_TAGS = {
    "complete": "status_complete", "done": "status_done", "partial": "status_partial",
    "pending": "status_pending", "community": "status_community", "reverted": "status_reverted",
    "uninstalled": "status_uninstalled", "failed": "failed",
}


def _enable_high_dpi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def get_work_area(root: tk.Misc) -> tuple[int, int, int, int]:
    """Return (x, y, width, height) of the usable desktop area (excluding the taskbar)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            SPI_GETWORKAREA = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


class FerryApp(tk_root_base(tk)):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_DISPLAY} · 游戏本地化工作台")
        self._app_icon = None
        self._set_app_icon()
        self.result_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.scanning = False
        self.translating = False
        self.managing_pack = False
        self._closing = False
        self.updating_glossary = False
        self.looking_up = False
        self.fetching_literalbridge = False
        self.checking_update = False
        self.cancel_event = threading.Event()
        # 非 MC 游戏（外挂模组）那一窗口的状态；窗口没开时 _loc_win 为 None。
        self._loc_win: tk.Toplevel | None = None
        self._loc_root: Path | None = None
        self._loc_probe = None
        self._loc_out_dir: Path | None = None
        self._loc_play_path: Path | None = None
        self._localizing = False
        self._loc_cancel = threading.Event()
        self._tx_phase: tuple[int, int, str] | None = None
        self._tx_stats: dict | None = None
        self._progress_value = 0.0
        self._progress_cap = 1.0
        self._progress_tick_after: str | None = None
        self.translatable_targets: dict[str, set[str]] = {}
        self.hardcoded_results: list[tuple[str, int, list[str]]] = []
        self.row_targets: dict[str, str] = {}
        self.row_modids: dict[str, str] = {}
        self.uninstalled_by_row: set[str] = set()
        self.coverage_by_row: dict[str, str] = {}
        self.status_tip_by_row: dict[str, str] = {}
        self.coverage_tip: tk.Toplevel | None = None
        self.coverage_hover_item = ""
        self.scan_cache: dict[str, core.ScanResult] = {}
        self.yield_reports: dict[str, core.YieldReport] = {}
        self._hardcoded_cache: dict[str, list[tuple[str, int, list[str]]]] = {}
        self.whitelist: set[str] = self._load_whitelist()
        self.config: dict = core.load_config()
        self.no_translate: set[str] = set(self.config.get("no_translate_mods") or [])
        self._nbt_candidates: dict[str, dict[str, list[str]]] = {}
        self._last_nbt_count = 0
        self._last_engine: str = str(self.config.get("engine", "mymemory"))
        self._theme_mode, self._theme = core.resolve_theme_mode(self.config.get("theme", "system"))
        self._card_frames: list[tk.Frame] = []
        self._themed_buttons: list[tuple[RoundedButton, str]] = []
        self._dnd_hook = None
        self._build_ui()
        self._apply_theme(self._theme_mode, save=False)
        self._apply_geometry()
        self._sync_engine_ui()
        self.bind("<FocusIn>", self._on_root_focus_in, add="+")
        self._drain_after = self.after(100, self._drain_queue)
        self._initial_afters = [
            self.after(80, self._setup_file_drop),
            self.after(700, self._security_check),
            self.after(5000, self._maybe_auto_update_check),
        ]
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_file_drop(self) -> None:
        if self._closing or self._dnd_hook is not None:
            return
        self._dnd_hook = enable_window_file_drop(self, self._on_files_dropped)

    def _on_files_dropped(self, paths: list[Path]) -> None:
        """处理直接拖进主窗口的游戏目录、程序、镜像或 Minecraft 实例。"""
        if not paths or self._closing:
            return
        if self.translating or self._localizing or self.scanning:
            msg = "正在执行当前任务，请等结束后再拖入新游戏。"
            if self._active_mode == "game" and self._loc_alive():
                self._loc_status_var.set(msg)
            else:
                self.status_var.set(msg)
            return
        kind = "invalid"
        target = Path(paths[0])
        for item in paths:
            kind, target = classify_dropped_path(Path(item))
            if kind != "invalid":
                break
        if kind == "invalid":
            return
        if kind == "mc":
            self._switch_mode("mc")
            self.path_var.set(str(target))
            self.config["last_instance"] = str(target)
            try:
                core.save_config(self.config)
            except OSError:
                pass
            if target in self.instance_paths:
                self._set_instance_options(self.instance_paths, select=self.instance_paths.index(target))
            else:
                self.instance_paths.append(target)
                self._set_instance_options(self.instance_paths, select=len(self.instance_paths) - 1)
            self.start_scan()
        else:
            self._switch_mode("game")
            self._loc_path_var.set(str(target))
            if hasattr(self, "_loc_path_entry"):
                try:
                    self._loc_path_entry.xview_moveto(1.0)
                except tk.TclError:
                    pass
            self._loc_start_probe()

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            if "clam" in style.theme_names():
                style.theme_use("clam")
        except Exception:
            pass
        # 统一观感：灰底画布 + 白卡片，字体全走同一族；颜色只给主操作和状态。
        style.configure("TFrame", background=BG)
        style.configure("TButton", font=FONT_BODY, padding=(8, 4))
        style.configure("TNotebook.Tab", font=FONT_SMALL_BOLD, padding=(12, 5))
        style.configure("TLabel", background=BG, foreground=INK, font=FONT_BODY)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=FONT_SMALL)
        style.configure("Card.TFrame", background=CARD)
        style.configure("Card.TLabel", background=CARD, foreground=INK, font=FONT_BODY)
        style.configure("Card.Muted.TLabel", background=CARD, foreground=MUTED, font=FONT_SMALL)
        style.configure("Card.TCheckbutton", background=CARD, foreground=INK, font=FONT_BODY)
        style.configure("TCheckbutton", background=BG, foreground=INK, font=FONT_BODY)
        style.configure("Treeview", rowheight=30, font=FONT_BODY, background=CARD, fieldbackground=CARD, borderwidth=0)
        style.configure("Treeview.Heading", font=FONT_SMALL_BOLD, background=BG, foreground=BODY_INK, relief="flat", padding=(2, 6))
        style.map("Treeview.Heading", background=[("active", BG)])
        style.map("Treeview", background=[("selected", SELECT_BG)], foreground=[("selected", INK)])
        style.configure("Horizontal.TProgressbar", troughcolor="#E2E7EE", background=ACCENT, thickness=6, borderwidth=0)

        self.shell = Workbench(self, self._switch_mode, self._icon_image(32), (
            ("使用说明", self._show_help), ("关于摆渡", self._show_about)))
        self.shell.pack(fill="both", expand=True)
        self._mode_hint_var = self.shell.hint
        host = self.shell.body
        self._active_mode = self._load_ui_state().get("workbench_page", "home")
        self._mc_started = False
        self._loc_win = self
        self._home_page = ProjectIndex(host, (
            ("游戏汉化", "游戏目录、磁盘镜像和独立程序（可直接拖进窗口）", (
                ("选择目录…", lambda: self._open_from_home("game")),
                ("镜像或程序…", lambda: self._open_from_home("file")))),
            ("模组汉化", "Minecraft 实例与整合包（可直接拖进窗口）", (
                ("选择实例…", lambda: self._open_from_home("mc")),)),
        ), self._open_record, on_remove=self._remove_record, on_clear=self._clear_records)
        self._game_page = ttk.Frame(host)
        self._settings_page = ttk.Frame(host)
        self._engine_page = self._settings_page
        self._mc_tabs = ttk.Notebook(host)
        self._mc_page = ttk.Frame(self._mc_tabs, padding=(8, 6))
        self._mc_tools = ttk.Frame(self._mc_tabs, padding=12)
        self._mc_tabs.add(self._mc_page, text="实例与翻译清单")
        self._mc_tabs.add(self._mc_tools, text="资源与桥接")
        self._pages = {"home": self._home_page, "game": self._game_page, "mc": self._mc_tabs, "settings": self._settings_page}
        self._build_mc_page()
        self._build_game_page(self._game_page)
        self._build_settings_page()
        self._switch_mode(self._active_mode, persist=False)

    def _palette(self) -> dict[str, str]:
        return THEMES.get(self._theme, THEMES["light"])

    def _toggle_theme(self) -> None:
        self._apply_theme("dark" if self._theme == "light" else "light", save=True)

    def _on_theme_mode_selected(self) -> None:
        self._apply_theme(self.theme_mode_var.get(), save=True)

    def _on_root_focus_in(self, event: tk.Event) -> None:
        if event.widget is self:
            self._refresh_system_theme()

    def _refresh_system_theme(self) -> None:
        if self._theme_mode != "system" or self._closing:
            return
        _, effective = core.resolve_theme_mode("system")
        if effective != self._theme:
            self._apply_theme("system", save=False)

    def _apply_theme(self, theme_name: str, *, save: bool = False) -> None:
        mode, effective = core.resolve_theme_mode(theme_name)
        self._theme_mode = mode
        self._theme = effective
        p = self._palette()
        style = ttk.Style(self)
        self.configure(bg=p["bg"])
        style.configure("TFrame", background=p["bg"])
        style.configure("TLabel", background=p["bg"], foreground=p["ink"], font=FONT_BODY)
        style.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"], font=FONT_SMALL)
        style.configure("Card.TFrame", background=p["card"])
        style.configure("Card.TLabel", background=p["card"], foreground=p["ink"], font=FONT_BODY)
        style.configure("Card.Muted.TLabel", background=p["card"], foreground=p["muted"], font=FONT_SMALL)
        style.configure("Card.TCheckbutton", background=p["card"], foreground=p["ink"], font=FONT_BODY)
        style.configure("TCheckbutton", background=p["bg"], foreground=p["ink"], font=FONT_BODY)
        style.map("TCheckbutton", background=[("active", p["bg"])], foreground=[("active", p["ink"])])
        style.configure("TRadiobutton", background=p["bg"], foreground=p["ink"], font=FONT_BODY)
        style.map("TRadiobutton", background=[("active", p["bg"])], foreground=[("active", p["ink"])])
        style.configure("Card.TRadiobutton", background=p["card"], foreground=p["ink"], font=FONT_BODY)
        style.map("Card.TRadiobutton", background=[("active", p["card"])], foreground=[("active", p["ink"])])
        style.configure("TButton", font=FONT_BODY, padding=(8, 4), background=p["btn_bg"], foreground=p["ink"], bordercolor=p["border"])
        style.map("TButton", background=[("active", p["btn_hover"]), ("disabled", p["bg"])], foreground=[("disabled", p["faint"])])
        style.configure("TEntry", fieldbackground=p["field_bg"], foreground=p["ink"], insertcolor=p["ink"], bordercolor=p["border"])
        style.configure("TCombobox", fieldbackground=p["field_bg"], background=p["btn_bg"], foreground=p["ink"], arrowcolor=p["ink"], bordercolor=p["border"])
        style.map("TCombobox", fieldbackground=[("readonly", p["field_bg"])], foreground=[("readonly", p["ink"])])
        style.configure("TSpinbox", fieldbackground=p["field_bg"], background=p["btn_bg"], foreground=p["ink"], arrowcolor=p["ink"])
        style.configure("TNotebook", background=p["bg"], bordercolor=p["border"])
        style.configure("TNotebook.Tab", font=FONT_SMALL_BOLD, padding=(12, 5), background=p["btn_bg"], foreground=p["body_ink"])
        style.map("TNotebook.Tab", background=[("selected", p["card"])], foreground=[("selected", p["ink"])])
        style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"])
        style.configure("TLabelframe.Label", background=p["bg"], foreground=p["ink"], font=FONT_SMALL_BOLD)
        style.configure("Treeview", rowheight=30, font=FONT_BODY, background=p["card"], fieldbackground=p["card"], foreground=p["ink"], borderwidth=0)
        style.configure("Treeview.Heading", font=FONT_SMALL_BOLD, background=p["bg"], foreground=p["body_ink"], relief="flat", padding=(2, 6))
        style.map("Treeview.Heading", background=[("active", p["bg"])])
        style.map("Treeview", background=[("selected", p["select_bg"])], foreground=[("selected", p["ink"])])
        style.configure("Horizontal.TProgressbar", troughcolor=p["trough"], background=p["accent"], thickness=6, borderwidth=0)
        style.configure("Vertical.TScrollbar", background=p["btn_bg"], troughcolor=p["bg"], bordercolor=p["border"], arrowcolor=p["muted"])
        style.configure("Horizontal.TScrollbar", background=p["btn_bg"], troughcolor=p["bg"], bordercolor=p["border"], arrowcolor=p["muted"])

        # 更新卡片容器与圆角按钮底色
        for frame in self._card_frames:
            try:
                frame.configure(bg=p["card"], highlightbackground=p["border"])
            except tk.TclError:
                pass
        for btn, surface in self._themed_buttons:
            try:
                btn.set_surface_bg(p["card"] if surface == "card" else p["bg"], highlight=p["accent"])
            except tk.TclError:
                pass
        if hasattr(self, "_settings_canvas"):
            try:
                self._settings_canvas.configure(bg=p["bg"])
            except tk.TclError:
                pass
        if hasattr(self, "_engine_feedback_label"):
            try:
                self._engine_feedback_label.configure(foreground=p["accent"])
            except tk.TclError:
                pass

        # 同步设置页外观控件状态
        if hasattr(self, "theme_mode_var"):
            self.theme_mode_var.set(self._theme_mode)
        if hasattr(self, "theme_status_var"):
            effective_label = "夜间模式" if self._theme == "dark" else "日间模式"
            suffix = "（跟随系统）" if self._theme_mode == "system" else ""
            self.theme_status_var.set(f"当前生效：{effective_label}{suffix}")

        # 更新 MC 表格与空状态标签
        if hasattr(self, "tree"):
            self.tree.tag_configure("row_even", background=p["row_even"])
            self.tree.tag_configure("row_odd", background=p["row_odd"])
            self.tree.tag_configure("status_done", foreground=p["green"])
            self.tree.tag_configure("status_partial", foreground=p["amber"])
            self.tree.tag_configure("status_pending", foreground=p["body_ink"])
            self.tree.tag_configure("status_complete", foreground=p["green"])
            self.tree.tag_configure("status_excluded", foreground=p["faint"])
            self.tree.tag_configure("failed", foreground=p["red"])
        if hasattr(self, "empty_state"):
            self.empty_state.configure(bg=p["card"], fg=p["muted"])

        # 更新游戏页报告文本框
        if hasattr(self, "_loc_report"):
            self._loc_report.configure(bg=p["card"], fg=p["ink"], insertbackground=p["ink"])
            self._loc_report.tag_configure("head", foreground=p["ink"])
            self._loc_report.tag_configure("ok", foreground=p["green"])
            self._loc_report.tag_configure("warn", foreground=p["amber"])
            self._loc_report.tag_configure("bad", foreground=p["red"])
            self._loc_report.tag_configure("dim", foreground=p["muted"])

        # 更新侧边栏与首页产物表
        if hasattr(self, "shell"):
            self.shell.apply_theme(p)
        if hasattr(self, "_home_page"):
            self._home_page.apply_theme(p)

        if hasattr(self, "status_label"):
            self._tone_status()

        if save:
            self.config["theme"] = self._theme_mode
            try:
                core.save_config(self.config)
            except OSError:
                pass

    def _build_mc_page(self) -> None:

        self.path_var = tk.StringVar()
        self.instance_paths: list[Path] = []

        # 第一步：选实例。收进一张白卡片，让「扫描」成为这一屏唯一的主按钮。
        ttk.Label(self._mc_page, text="实例", font=FONT_SMALL_BOLD, foreground=MUTED).pack(anchor="w", pady=(0, 3))
        instance_card = tk.Frame(self._mc_page, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        self._card_frames.append(instance_card)
        instance_card.pack(fill="x", pady=(0, 8))
        instance_box = ttk.Frame(instance_card, style="Card.TFrame", padding=10)
        instance_box.pack(fill="both", expand=True)
        instance_box.columnconfigure(0, weight=1)

        inst_row = ttk.Frame(instance_box, style="Card.TFrame")
        inst_row.grid(row=0, column=0, sticky="ew")
        inst_row.columnconfigure(0, weight=1)
        self.instance_combo = ttk.Combobox(inst_row, state="readonly", height=8, font=FONT_BODY)
        self.instance_combo.grid(row=0, column=0, sticky="ew")
        self.instance_combo.bind("<<ComboboxSelected>>", self._instance_selected)
        self.scan_button = make_action_button(inst_row, "扫描", self.start_scan, ACCENT, bg=CARD)
        self._themed_buttons.append((self.scan_button, "card"))
        self.scan_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Button(inst_row, text="浏览…", command=self.choose_path).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(inst_row, text="刷新", command=self.refresh_instances).grid(row=0, column=3, padx=(6, 0))

        # 下拉框只显示实例名，下面一行实时显示完整路径，避免选错实例。
        path_row = ttk.Frame(instance_box, style="Card.TFrame")
        path_row.grid(row=1, column=0, sticky="ew", pady=(7, 0))
        path_row.columnconfigure(0, weight=1)
        self.instance_path_var = tk.StringVar(value="")
        ttk.Label(path_row, textvariable=self.instance_path_var, style="Card.Muted.TLabel", justify="left").grid(row=0, column=0, sticky="w")
        ttk.Button(path_row, text="用户白名单", command=self.manage_whitelist).grid(row=0, column=1)
        self.path_var.trace_add("write", lambda *_: self.instance_path_var.set(self.path_var.get()))

        # 状态行：一件事一行。左边说进展，右边给游戏版本；颜色随内容变化（忙=蓝，成=绿，坏=红）。
        status_row = ttk.Frame(self._mc_page)
        status_row.pack(fill="x", pady=(0, 6))
        self.status_var = tk.StringVar(value="正在探测实例……")
        self.status_label = ttk.Label(status_row, textvariable=self.status_var, foreground=ACCENT, wraplength=600, justify="left", font=FONT_BODY)
        self.status_label.pack(side="left", fill="x", expand=True)
        self.pack_format_var = tk.StringVar(value="")
        ttk.Label(status_row, textvariable=self.pack_format_var, foreground=MUTED, font=FONT_SMALL).pack(side="right", padx=(8, 0))
        ttk.Button(status_row, text="格式…", width=6, command=self._choose_pack_format).pack(side="right", padx=(6, 0))
        self.status_var.trace_add("write", self._tone_status)

        toolbar = ttk.Frame(self._mc_page)
        toolbar.pack(fill="x", pady=(0, 5))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(toolbar, textvariable=self.search_var, width=30, font=FONT_BODY, foreground=FAINT)
        self.search_entry.pack(side="left")
        self.search_entry.insert(0, SEARCH_PLACEHOLDER)
        self.search_entry.bind("<FocusIn>", self._search_focus_in)
        self.search_entry.bind("<FocusOut>", self._search_focus_out)
        self.search_var.trace_add("write", lambda *_: self._render_rows())
        ttk.Button(toolbar, text="×", width=3, command=self._clear_search).pack(side="left", padx=(3, 12))
        ttk.Label(toolbar, text="筛选", font=FONT_SMALL, foreground=MUTED).pack(side="left")
        self.filter_var = tk.StringVar(value="全部")
        self.filter_combo = ttk.Combobox(toolbar, textvariable=self.filter_var, state="readonly", width=15, values=TABLE_FILTERS, font=FONT_BODY)
        self.filter_combo.pack(side="left", padx=(6, 0))
        self.filter_combo.bind("<<ComboboxSelected>>", lambda _event: self._render_rows())
        # D：对当前筛选后可见的行做全选 / 反选。
        ttk.Button(toolbar, text="反选", command=self._invert_selection).pack(side="right")
        ttk.Button(toolbar, text="全选", command=self._select_all_visible).pack(side="right", padx=(0, 6))

        table_card = tk.Frame(self._mc_page, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        self._card_frames.append(table_card)
        self.show_instance_column = False
        self.sort_column = "missing"
        self.sort_desc = True
        self.all_rows: list[dict] = []
        self.tree = ttk.Treeview(table_card, show="headings", selectmode="extended", height=10)
        self._configure_tree_columns(False)
        self.tree.tag_configure("row_even", background="#F6F8FB")
        self.tree.tag_configure("row_odd", background=CARD)
        self.tree.tag_configure("status_done", foreground=GREEN)
        self.tree.tag_configure("status_partial", foreground=AMBER)
        self.tree.tag_configure("status_pending", foreground=BODY_INK)
        self.tree.tag_configure("status_community", foreground="#5B6B7B")
        self.tree.tag_configure("status_reverted", foreground="#8A6D00")
        self.tree.tag_configure("status_uninstalled", foreground="#9A5A22")
        self.tree.tag_configure("status_excluded", foreground=FAINT)
        self.tree.tag_configure("status_complete", foreground=GREEN)
        self.tree.tag_configure("status_hardcoded", foreground="#8A4F00")
        self.tree.tag_configure("failed", foreground=RED)
        self.tree.bind("<Double-1>", self._on_tree_double)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Button-3>", self._on_tree_right_click)
        self.tree.bind("<Motion>", self._on_coverage_motion)
        self.tree.bind("<Leave>", self._hide_coverage_tip)
        self.tree.bind("<MouseWheel>", self._hide_coverage_tip)
        # E：主窗口快捷键。
        self.bind("<Control-a>", self._on_ctrl_a)
        self.bind("<Control-A>", self._on_ctrl_a)
        self.bind("<F5>", self._on_rescan_key)
        self.bind("<Escape>", self._on_escape_key)
        try:
            self.bind("<Command-a>", self._on_ctrl_a)
        except tk.TclError:
            pass
        yscroll = ttk.Scrollbar(table_card, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table_card, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        # 空状态：表格没内容时给出下一步动作，而不是一片空白。
        self.empty_state = tk.Label(table_card, text="", bg=CARD, fg=MUTED, font=FONT_BODY, justify="center")
        self.empty_state.grid(row=0, column=0, sticky="nsew")
        self.empty_state.grid_remove()
        # 表格随窗口拉伸：没有 weight 时 Treeview 只保持请求宽度，
        # 右侧会留白、竖滚动条悬在半路、最大化后压不满。
        table_card.rowconfigure(0, weight=1)
        table_card.columnconfigure(0, weight=1)

        # 翻译按钮 + 进度：紧贴表格下方；状态符号说明交给「使用说明」，不再铺一排小字。
        run_zone = ttk.Frame(self._mc_page)
        run_bar = ttk.Frame(run_zone)
        run_bar.pack(fill="x")
        self.selection_var = tk.StringVar(value="")
        ttk.Label(run_bar, textvariable=self.selection_var, foreground=MUTED, font=FONT_SMALL).pack(side="left")
        self.stop_button = make_action_button(run_bar, "停止", self._stop_translate, RED, state="disabled")
        self.stop_button.pack(side="right")
        self.translate_all_button = make_action_button(run_bar, "翻译全部待AI", self._translate_all, GREEN)
        self.translate_all_button.pack(side="right", padx=(8, 10))
        self.translate_selected_button = make_action_button(run_bar, "翻译选中模组", self._translate_selected, ACCENT)
        self.translate_selected_button.pack(side="right")
        self._themed_buttons.extend([(self.stop_button, "bg"), (self.translate_all_button, "bg"), (self.translate_selected_button, "bg")])
        progress_row = ttk.Frame(run_zone)
        progress_row.pack(fill="x", pady=(7, 0))
        self.progress_text_var = tk.StringVar(value="")
        ttk.Label(progress_row, textvariable=self.progress_text_var, foreground=MUTED, font=FONT_SMALL).pack(side="right")
        self.progress = ttk.Progressbar(progress_row, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        run_zone.pack(side="bottom", fill="x", pady=(6, 0))
        table_card.pack(fill="both", expand=True)

        ttk.Label(self._mc_tools, text="模组翻译策略", font=FONT_TITLE).pack(anchor="w", pady=(0, 6))
        self._mc_settings_flags = ttk.Frame(self._mc_tools)
        self._mc_settings_flags.pack(fill="x")

        self.hardcoded_var = tk.BooleanVar(value=bool(self.config.get("translate_hardcoded", True)))
        ttk.Checkbutton(self._mc_settings_flags, text="翻译硬编码文本", variable=self.hardcoded_var, command=self._save_mc_policy).pack(anchor="w", pady=2)
        self.auto_export_var = tk.BooleanVar(value=bool(self.config.get("auto_export_bridge", True)))
        ttk.Checkbutton(self._mc_settings_flags, text="译完自动导出桥映射", variable=self.auto_export_var, command=self._auto_export_changed).pack(anchor="w", pady=2)
        self.auto_update_var = tk.BooleanVar(value=bool(self.config.get("auto_update_literalbridge", True)))
        ttk.Checkbutton(self._mc_settings_flags, text="自动升级 LiteralBridge", variable=self.auto_update_var, command=self._auto_update_changed).pack(anchor="w", pady=2)
        self.fill_var = tk.BooleanVar(value=bool(self.config.get("fill_community", False)))
        self.refine_var = tk.BooleanVar(value=bool(self.config.get("refine_community", False)))
        self.community_var = tk.BooleanVar(value=bool(self.config.get("community_enabled", True)))
        for text, var in (("补全人工汉化缺失", self.fill_var), ("参考人工译文补缺", self.refine_var),
                          ("联网查社区汉化", self.community_var)):
            ttk.Checkbutton(self._mc_tools, text=text, variable=var, command=self._save_mc_policy).pack(anchor="w", pady=2)
        ttk.Separator(self._mc_tools).pack(fill="x", pady=12)
        tools = ttk.Frame(self._mc_tools)
        tools.pack(anchor="w")
        for index, (attr, title, command) in enumerate((
            ("lookup_button", "查询在线汉化", self.start_online_lookup), ("quality_button", "质量检查", self._show_quality),
            ("limits_button", "汉化限制", self._show_limits), ("bridge_button", "导出桥接映射", self.export_bridge_mapping),
            ("collect_button", "导入游戏采集", self.import_collected), ("glossary_button", "更新模组术语表", self._update_glossary))):
            button = ttk.Button(tools, text=title, command=command)
            button.grid(row=index // 3, column=index % 3, sticky="ew", padx=(0, 8), pady=4)
            setattr(self, attr, button)
        self.limits_button.state(["disabled"])
        ttk.Label(self._mc_tools, textvariable=self.status_var, wraplength=600, style="Muted.TLabel").pack(anchor="w", pady=10)

    def _build_settings_page(self) -> None:
        host = self._settings_page
        canvas = tk.Canvas(host, bg=self._palette()["bg"], highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(host, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self._settings_canvas = canvas

        content = ttk.Frame(canvas, padding=(0, 2, 10, 12))
        win_id = canvas.create_window((0, 0), window=content, anchor="nw")

        def _sync_scroll(_event: object = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_width(event: tk.Event) -> None:
            canvas.itemconfigure(win_id, width=max(320, event.width))

        def _on_wheel(event: tk.Event) -> None:
            if self._active_mode != "settings":
                return
            bbox = canvas.bbox("all")
            if not bbox or bbox[3] <= canvas.winfo_height():
                return
            delta = int(-event.delta / 120) if event.delta else 0
            if delta:
                canvas.yview_scroll(delta, "units")

        content.bind("<Configure>", _sync_scroll)
        canvas.bind("<Configure>", _sync_width)
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        # ── 1. 翻译引擎 ──
        engine_group = ttk.LabelFrame(content, text="翻译引擎", padding=(14, 10))
        engine_group.pack(fill="x", pady=(0, 12))

        self.settings_summary_var = tk.StringVar()
        self.engine_feedback = tk.StringVar()
        ttk.Label(engine_group, textvariable=self.settings_summary_var, font=FONT_BOLD).pack(anchor="w", pady=(0, 10))
        self.grid_frame = ttk.Frame(engine_group)
        self.grid_frame.pack(fill="x")
        self.grid_frame.columnconfigure(1, weight=1)

        ttk.Label(self.grid_frame, text="翻译引擎").grid(row=0, column=0, sticky="w", padx=(0, 14), pady=4)
        self.category_var = tk.StringVar()
        self.category_combo = ttk.Combobox(self.grid_frame, textvariable=self.category_var, state="readonly", width=15, values=list(FREE_ENGINES.values()) + [CUSTOM_CATEGORY_LABEL], font=FONT_BODY)
        self.category_combo.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=3)
        self.category_combo.bind("<<ComboboxSelected>>", self._engine_selected)
        ttk.Label(self.grid_frame, text="兼容接口").grid(row=1, column=0, sticky="w", pady=4)
        self.provider_var = tk.StringVar()
        self.provider_combo = ttk.Combobox(self.grid_frame, textvariable=self.provider_var, state="readonly", width=18, values=list(CUSTOM_ENGINES.values()), font=FONT_BODY)
        self.provider_combo.grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=4)
        self.provider_combo.bind("<<ComboboxSelected>>", self._provider_selected)
        ttk.Label(self.grid_frame, text="接口预设").grid(row=2, column=0, sticky="w", pady=4)
        self.profile_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(self.grid_frame, textvariable=self.profile_var, state="readonly", font=FONT_BODY)
        self.profile_combo.grid(row=2, column=1, sticky="ew", padx=(0, 10), pady=4)
        self.profile_combo.bind("<<ComboboxSelected>>", self._profile_selected)

        # P0-2：API Key 默认掩码，右侧「显示」按钮切换明文（只改显示，不改存储）。
        # 存储本身在 Windows 上走 DPAPI 加密（见 ferrycore/secrets.py），不是明文躺着。
        ttk.Label(self.grid_frame, text="密钥").grid(row=3, column=0, sticky="w", pady=(10, 4))
        self.api_key_var = tk.StringVar()
        key_frame = ttk.Frame(self.grid_frame)
        key_frame.grid(row=3, column=1, sticky="ew", padx=(0, 10), pady=(10, 4))
        key_frame.columnconfigure(0, weight=1)
        self.api_key_entry = ttk.Entry(key_frame, textvariable=self.api_key_var, show="•", font=FONT_BODY)
        self.api_key_entry.grid(row=0, column=0, sticky="ew")
        self.api_key_show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(key_frame, text="显示", variable=self.api_key_show_var, command=self._toggle_api_key_visibility).grid(row=0, column=1, padx=(4, 0))
        ttk.Label(self.grid_frame, text="接口地址").grid(row=4, column=0, sticky="w", pady=4)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(self.grid_frame, textvariable=self.url_var, font=FONT_BODY)
        self.url_entry.grid(row=4, column=1, sticky="ew", padx=(0, 10), pady=4)
        ttk.Label(self.grid_frame, text="模型").grid(row=5, column=0, sticky="w", pady=4)
        self.model_var = tk.StringVar()
        self.model_entry = ttk.Entry(self.grid_frame, textvariable=self.model_var, font=FONT_BODY)
        self.model_entry.grid(row=5, column=1, sticky="ew", padx=(0, 10), pady=4)

        ttk.Label(self.grid_frame, text="深度思考").grid(row=6, column=0, sticky="w", pady=(8, 2))
        self.disable_thinking_var = tk.BooleanVar(value=bool(self.config.get("disable_thinking", False)))
        ttk.Checkbutton(
            self.grid_frame,
            text="关闭深度思考（省流提速）",
            variable=self.disable_thinking_var,
            command=self._update_settings_summary,
        ).grid(row=6, column=1, sticky="w", padx=(0, 10), pady=(8, 2))
        ttk.Label(
            self.grid_frame,
            text="对 DeepSeek / Qwen3 / GLM / 豆包 / Gemini 2.5 / OpenAI 推理系列生效；关闭可大幅省输出 Token 并提速，但文学剧情润色可能略降，建议自行对比。",
            style="Muted.TLabel",
            wraplength=540,
        ).grid(row=7, column=1, sticky="w", padx=(0, 10), pady=(0, 6))

        actions = ttk.Frame(self.grid_frame)
        actions.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="保存预设", command=self._save_profile).pack(side="left")
        ttk.Button(actions, text="删除预设", command=self._delete_profile).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="保存设置", command=self._save_settings).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="测试连接", command=self._test_connection).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="并发与模型池…", command=self._show_settings).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Batch API（5折异步）…", command=self._show_batch_api_dialog).pack(side="left", padx=(6, 0))
        self.concurrency_var = tk.IntVar(value=int(self.config.get("concurrency", 1)))
        self._engine_feedback_label = ttk.Label(engine_group, textvariable=self.engine_feedback, foreground=self._palette()["accent"], wraplength=600)
        self._engine_feedback_label.pack(anchor="w", pady=(8, 0))

        # ── 2. 外观与主题 ──
        theme_group = ttk.LabelFrame(content, text="外观与主题", padding=(14, 10))
        theme_group.pack(fill="x", pady=(0, 12))
        theme_row = ttk.Frame(theme_group)
        theme_row.pack(fill="x")
        self.theme_mode_var = tk.StringVar(value=self._theme_mode)
        self.theme_status_var = tk.StringVar()
        for value, label in (("system", "跟随系统"), ("light", "日间模式"), ("dark", "夜间模式")):
            ttk.Radiobutton(
                theme_row,
                text=label,
                value=value,
                variable=self.theme_mode_var,
                command=self._on_theme_mode_selected,
            ).pack(side="left", padx=(0, 18))
        ttk.Label(theme_row, textvariable=self.theme_status_var, style="Muted.TLabel").pack(side="right")

        # ── 3. 日志与诊断 ──
        diag_group = ttk.LabelFrame(content, text="日志与诊断", padding=(14, 10))
        diag_group.pack(fill="x")
        diag_row = ttk.Frame(diag_group)
        diag_row.pack(fill="x")
        ttk.Button(diag_row, text="运行日志", command=self._show_log).pack(side="left")
        ttk.Button(diag_row, text="导出诊断包", command=self._export_diagnostics).pack(side="left", padx=(8, 0))
        ttk.Label(
            diag_row,
            text="查看实时运行日志，或一键导出脱敏诊断包用于排障（不含 API 密钥）。",
            style="Muted.TLabel",
        ).pack(side="left", padx=(14, 0))

    _build_engine_page = _build_settings_page

    def _tone_status(self, *_args: object) -> None:
        """状态文字按内容着色：失败=红、完成=绿、没有/待确认=琥珀、进行中=渡船蓝。"""
        p = self._palette()
        text = self.status_var.get()
        if any(word in text for word in ("失败", "错误", "出错")):
            color = p["red"]
        elif any(word in text for word in ("完成", "已保存", "已删除", "已更新", "已安装", "已生成", "已重载")):
            color = p["green"]
        elif any(word in text for word in ("没有", "待确认", "无需", "未找到")):
            color = p["amber"]
        elif text.startswith("正在"):
            color = p["accent"]
        else:
            color = p["ink"]
        self.status_label.configure(foreground=color)

    def _show_empty(self, text: str | None) -> None:
        if text:
            self.empty_state.configure(text=text)
            self.empty_state.grid()
        else:
            self.empty_state.grid_remove()

    # ---------- P1-2 / P1-3 / P1-4 / P1-6：表格列、渲染、搜索、筛选、排序 ----------

    def _toggle_api_key_visibility(self) -> None:
        self.api_key_entry.configure(show="" if self.api_key_show_var.get() else "•")

    def _save_mc_policy(self) -> None:
        for key, var in (("fill_community", self.fill_var), ("refine_community", self.refine_var),
                         ("community_enabled", self.community_var), ("translate_hardcoded", self.hardcoded_var)):
            self.config[key] = bool(var.get())
        core.save_config(self.config)
        self._refine_changed()

    def _update_settings_summary(self) -> None:
        engine = self._current_engine()
        model_field = ENGINE_MODEL_FIELDS.get(engine, "")
        model = self.model_var.get().strip() or str(self.config.get(model_field, "")) or "默认模型"
        try:
            concurrency = int(self.concurrency_var.get())
        except Exception:
            concurrency = 1
        suffix = "，已关思考" if getattr(self, "disable_thinking_var", None) and self.disable_thinking_var.get() else ""
        self.settings_summary_var.set(f"引擎 {self._engine_label(engine)}，模型 {model}，并发 {concurrency}{suffix}")

    def _configure_tree_columns(self, show_instance: bool) -> None:
        columns = ["modid", "complete", "status", "missing", "ai", "community"]
        headings = {"modid": "模组", "complete": "完成度", "status": "状态", "missing": "缺", "ai": "AI", "community": "人工"}
        anchors = {"modid": "w", "complete": "center", "status": "w", "missing": "center", "ai": "center", "community": "center"}
        widths = {"modid": 240 if not show_instance else 220, "complete": 84, "status": 176, "missing": 56, "ai": 52, "community": 64}
        if show_instance:
            columns.append("instance")
            headings["instance"] = "实例"
            anchors["instance"] = "w"
            widths["instance"] = 130
        self.tree.configure(columns=columns)
        for col in columns:
            self.tree.column(col, width=widths[col], minwidth=40, anchor=anchors[col], stretch=col == "modid")
            self.tree.heading(col, anchor=anchors[col])
        self._headings = {col: headings[col] for col in columns}
        self.show_instance_column = show_instance
        self._refresh_headings()

    def _refresh_headings(self) -> None:
        # 只刷新表头文字与排序箭头，不重置用户手动拖动过的列宽。
        for col, base in getattr(self, "_headings", {}).items():
            self.tree.heading(col, text=self._heading_text(col, base), command=lambda c=col: self._on_heading_click(c))

    def _heading_text(self, column: str, base: str) -> str:
        if column == self.sort_column:
            return f"{base} {'▼' if self.sort_desc else '▲'}"
        return base

    def _on_heading_click(self, column: str) -> None:
        if column == self.sort_column:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_column = column
            self.sort_desc = column in ("missing", "ai", "community", "complete")
        self._refresh_headings()
        self._render_rows()

    def _row_matches(self, row: dict, query: str) -> bool:
        # B：同时匹配 modid 与状态 label（含「硬编码 / 失败 / 手册」等标记文字）。
        if not query:
            return True
        return query in f"{row['modid']} {row.get('label', '')}".lower()

    def _filter_match(self, row: dict, chosen: str) -> bool:
        if chosen == "只看有缺口":
            return row["missing"] > 0
        if chosen == "只看失败":
            return row["failed"]
        if chosen == "只看含人工汉化":
            return row["has_community"]
        if chosen == "只看疑似硬编码":
            return row["hardcoded"]
        if chosen == "只看已完成":
            # 「完成」= 语言文件没缺口 + 硬编码也没剩没翻的（检出过但全翻完，仍算完成）。
            return row["missing"] == 0 and not row["uninstalled"] and not row.get("hardcoded_pending")
        return True

    def _row_visible(self, row: dict, query: str, chosen: str) -> bool:
        if row["placeholder"]:
            return chosen == "全部"
        return self._row_matches(row, query) and self._filter_match(row, chosen)

    def _sort_key(self, row: dict):
        column = self.sort_column
        if column in ("missing", "ai", "community"):
            return row.get(column, 0)
        if column == "complete":
            return row.get("percent", 0.0)
        if column == "status":
            return (row.get("label") or row.get("short") or "").lower()
        if column == "instance":
            return (row.get("instance") or "").lower()
        return (row.get("modid") or "").lower()

    def _row_values(self, row: dict) -> tuple:
        # 状态列只放短状态（符号 + 摘要），保证任何窗口宽度都放得下；
        # 完整标记（硬编码 / 手册 / 失败 / 降级说明）悬停「状态」列可看全文。
        status_text = f"{row['symbol']} {row['short']}{'·手册' if row['patch_pending'] else ''}".strip()
        values: list = [row["modid"], row["complete_text"], status_text, row["missing"], row["ai"], row["community"]]
        if self.show_instance_column:
            values.append(row["instance"])
        return tuple(values)

    def _search_query(self) -> str:
        text = self.search_var.get()
        return "" if text == SEARCH_PLACEHOLDER else text.strip().lower()

    def _search_focus_in(self, _event: object = None) -> None:
        if self.search_var.get() == SEARCH_PLACEHOLDER:
            self.search_entry.icursor(tk.END)
            self.search_var.set("")
            self.search_entry.configure(foreground=INK)

    def _search_focus_out(self, _event: object = None) -> None:
        if not self.search_var.get():
            self.search_entry.configure(foreground=FAINT)
            self.search_var.set(SEARCH_PLACEHOLDER)

    def _render_rows(self) -> None:
        show_instance = len({r["instance_path"] for r in self.all_rows if not r["placeholder"]}) > 1
        if show_instance != self.show_instance_column:
            self._configure_tree_columns(show_instance)
        query = self._search_query()
        chosen = self.filter_var.get()
        visible = [r for r in self.all_rows if self._row_visible(r, query, chosen)]
        visible.sort(key=self._sort_key, reverse=self.sort_desc)
        visible = [r for r in visible if not r["placeholder"]] + [r for r in visible if r["placeholder"]]
        self._hide_coverage_tip()
        self.tree.delete(*self.tree.get_children())
        self.row_targets.clear()
        self.row_modids.clear()
        self.uninstalled_by_row.clear()
        self.coverage_by_row.clear()
        self.status_tip_by_row.clear()
        for index, row in enumerate(visible):
            tags = ["row_even" if index % 2 else "row_odd"]
            if row["tag"]:
                tags.append(row["tag"])
            if row["failed"]:
                tags.append("failed")
            item = self.tree.insert("", "end", values=self._row_values(row), tags=tuple(tags))
            self.row_targets[item] = row["instance_path"]
            self.row_modids[item] = row["modid"]
            if row["uninstalled"]:
                self.uninstalled_by_row.add(item)
            if row["coverage"]:
                self.coverage_by_row[item] = row["coverage"]
            if not row["placeholder"] and row.get("label"):
                self.status_tip_by_row[item] = f"{row['symbol']} {row['label']}"
        # 空状态指引：没扫过 → 教第一步；扫过但筛完为空 → 提示清条件。
        if not self.all_rows:
            self._show_empty("正在扫描实例……" if self.scanning else "选一个 Minecraft 实例，点「扫描」\n会列出缺中文的模组；已有人工汉化的会自动让位")
        elif not visible:
            self._show_empty("没有匹配的模组\n试试清空搜索，或换一个筛选条件")
        else:
            self._show_empty(None)

    def _mark_hardcoded_row(self, row: dict, pending: int = 0) -> None:
        """后台硬编码检测结果回来后，给已显示的行补上标记。

        pending = 这个 modid 还有几条硬编码没翻（0 表示全翻完了）。
        语言文件全通、硬编码也翻完 → 状态该是「已 AI 汉化完成 / 已 AI 补完」，
        不该再挂 ⚠；只有还剩没翻的才挂。
        """
        if row.get("hardcoded") or row.get("excluded"):
            return
        row["hardcoded"] = True
        row["hardcoded_pending"] = max(0, int(pending or 0))
        label = row.get("label", "")
        if "硬编码" not in label:
            row["label"] = f"{label}·疑似硬编码文本" if label else "疑似硬编码文本"
        if row["tag"] in ("failed", "status_uninstalled", "status_reverted", "status_excluded"):
            return
        st = row.get("_status")
        if st is None:
            return
        row["symbol"], row["short"], row["tag"] = self._status_visual(
            st, bool(row["uninstalled"]), 0, bool(row["patch_pending"]), bool(row["excluded"]), row["hardcoded_pending"]
        )

    # ---------- D / E / G：选择、快捷键、清除 ----------

    def _select_all_visible(self) -> None:
        items = self.tree.get_children()
        if items:
            self.tree.selection_set(items)

    def _invert_selection(self) -> None:
        selected = set(self.tree.selection())
        self.tree.selection_set([item for item in self.tree.get_children() if item not in selected])

    def _clear_search(self) -> None:
        self.search_var.set("")
        if self.focus_get() is not self.search_entry:
            self._search_focus_out()

    def _on_ctrl_a(self, _event: object = None) -> str:
        widget = self.focus_get()
        if isinstance(widget, (ttk.Entry, tk.Entry)):
            try:
                widget.select_range(0, "end")
            except tk.TclError:
                pass
            return "break"
        if self._active_mode != "mc" or self._mc_tabs.select() != str(self._mc_page):
            return
        self._select_all_visible()
        return "break"

    def _on_rescan_key(self, _event: object = None) -> str:
        if self._active_mode == "mc":
            self.start_scan()
        elif self._active_mode == "game" and self._loc_probe_button.cget("state") != "disabled":
            self._loc_start_probe()
        return "break"

    def _on_escape_key(self, _event: object = None) -> str:
        if self._active_mode != "mc":
            return
        if self._search_query():
            self._clear_search()
        else:
            selection = self.tree.selection()
            if selection:
                self.tree.selection_remove(*selection)
        return "break"

    # ---------- 扫描结果 -> 行数据 ----------

    def _status_visual(self, st, is_uninstalled: bool, failed_count: int, patch_pending: bool, is_excluded: bool = False, hardcoded_pending: int = 0) -> tuple[str, str, str]:
        if failed_count:
            return "✗", f"失败{failed_count}条", "failed"
        if is_uninstalled:
            return "⊗", "已卸载", "status_uninstalled"
        if st.fully_reverted:
            return "⊘", "已还原", "status_reverted"
        if is_excluded:
            return "∅", "已排除（不翻译）", "status_excluded"
        # 硬编码文本（走桥的那部分）还有没翻完的 → 挂 ⚠。只看「还差几条」，
        # 不看「检出过没有」：全翻完了就不该再挂个 ⚠ 吓人。
        if hardcoded_pending:
            tail = f"·缺{st.missing}" if st.missing > 0 else ""
            return "⚠", f"含硬编码·待翻{hardcoded_pending}{tail}", "status_hardcoded"
        # 语言文件全通、硬编码也翻完 —— 这才叫「完成」。措辞要分清楚是谁翻的：
        # 人工打底、AI 只补缺口的，说「已 AI 补完」；AI 从零翻完的，才说「已 AI 汉化完成」。
        # 摆渡的定位是「临时的、到岸即离」，功劳该留给汉化组，别把人工的活算成自己的。
        if st.fully_translated:
            if st.community_keys > 0 and st.ai_keys > 0:
                return "✓", "已 AI 补完", "status_complete"
            if st.community_keys > 0:
                # 全是人工汉化，摆渡压根没插手 —— 不蹭功劳。
                return "◈", "人工·完整（已让位）", "status_community"
            return "✓", "已 AI 汉化完成", "status_complete"
        if st.has_community:
            short = "人工·完整" if st.fully_community else f"人工·缺{st.missing}"
            return "◈", short + ("·手册" if patch_pending else ""), "status_community"
        if st.ai_keys > 0 and st.missing > 0:
            return "◐", f"部分·缺{st.missing}", "status_partial"
        return "○", f"待翻·缺{st.missing}", "status_pending"

    def _make_row(self, st, label: str, coverage: str, instance: Path, is_uninstalled: bool, failed_count: int, patch_pending: bool, hardcoded: bool, is_excluded: bool = False, hardcoded_pending: int = 0) -> dict:
        # A-1：保证三个关键标记一定出现在主表状态文字里（调用方已拼则跳过）。
        if is_excluded and "已排除" not in label:
            label = f"{label}·已排除" if label else "已排除"
        if hardcoded and "硬编码" not in label:
            label = f"{label}·疑似硬编码文本" if label else "疑似硬编码文本"
        if patch_pending and "手册" not in label:
            label = f"{label}·手册待翻" if label else "手册待翻"
        if failed_count and "失败" not in label:
            label = f"{label}·上次失败{failed_count}条" if label else f"上次失败{failed_count}条"
        symbol, short, tag = self._status_visual(st, is_uninstalled, failed_count, patch_pending, is_excluded, hardcoded_pending)
        total = int(st.total_keys)
        percent = 0.0 if is_uninstalled or total <= 0 else (total - int(st.missing)) / total
        percent = max(0.0, min(1.0, percent))
        complete_text = "" if total <= 0 else f"{int(round(percent * 100))}%"
        return {
            "modid": st.modid,
            "label": label,
            "hardcoded": bool(hardcoded),
            "hardcoded_pending": max(0, int(hardcoded_pending or 0)),
            "_status": st,
            "missing": int(st.missing),
            "ai": int(st.ai_keys),
            "community": int(st.community_keys),
            "total": total,
            "percent": percent,
            "complete_text": complete_text,
            "symbol": symbol,
            "short": short,
            "tag": tag,
            "coverage": coverage,
            "instance": instance.name,
            "instance_path": str(instance),
            "uninstalled": bool(is_uninstalled),
            "failed": bool(failed_count),
            "patch_pending": bool(patch_pending),
            "has_community": bool(st.has_community),
            "excluded": bool(is_excluded),
            "placeholder": False,
        }

    def _placeholder_row(self, instance: Path) -> dict:
        return {
            "modid": PLACEHOLDER_MODID, "label": "", "hardcoded": False, "hardcoded_pending": 0, "missing": 0, "ai": 0, "community": 0, "total": 0,
            "percent": 0.0, "complete_text": "", "symbol": "", "short": "无待汉化模组", "tag": "status_pending", "_status": None,
            "coverage": "", "instance": instance.name, "instance_path": str(instance),
            "uninstalled": False, "failed": False, "patch_pending": False, "has_community": False, "excluded": False, "placeholder": True,
        }

    def _pending_count(self, targets: dict[str, set[str]]) -> int:
        config = self._snapshot_config()
        total = 0
        for path, modids in targets.items():
            scan = self.scan_cache.get(path)
            if scan is None:
                continue
            entries = core.missing_entries(
                scan.english, scan.community, scan.ai, set(self.whitelist),
                fill_community=config["fill_community"], modids=modids,
                reverted=scan.reverted, refine_community=config["refine_community"],
            )
            entries.extend(core.missing_patchouli_entries(scan, modids))
            if config.get("translate_hardcoded", True):
                pack = Path(config.get("output_dir") or Path(path) / "resourcepacks") / (config.get("pack_name") or core.DEFAULT_CONFIG["pack_name"])
                entries.extend(core.missing_hardcoded_entries(
                    core.hardcoded_strings_by_modid(scan, self._hardcoded_cache.get(path) or [], modids),
                    core.load_hardcoded_translations(pack), modids))
            entries = core.skip_ignored_entries(entries, config)
            total += len(entries)
        return total

    @staticmethod
    def _parse_geometry(text: object) -> tuple[int, int, int | None, int | None] | None:
        if not isinstance(text, str):
            return None
        match = re.fullmatch(r"(\d+)x(\d+)(?:\+(-?\d+)\+(-?\d+))?", text.strip())
        if not match:
            return None
        width, height = int(match.group(1)), int(match.group(2))
        if width < 400 or height < 300:
            return None
        x = int(match.group(3)) if match.group(3) is not None else None
        y = int(match.group(4)) if match.group(4) is not None else None
        return width, height, x, y

    def _load_ui_state(self) -> dict:
        try:
            if UI_STATE_FILE.is_file():
                data = json.loads(UI_STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError):
            pass
        return {}

    def _save_ui_state(self) -> None:
        try:
            state = self._load_ui_state()
            if self.state() == "normal":
                state["geometry"] = self.geometry()
            UI_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (OSError, tk.TclError):
            pass

    def _on_close(self) -> None:
        self._close_localize()
        if self.translating:
            self.cancel_event.set()
        self._closing = True
        for after_id in [getattr(self, "_drain_after", None), *getattr(self, "_initial_afters", [])]:
            if after_id:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass
        if getattr(self, "_dnd_hook", None) is not None:
            self._dnd_hook.uninstall()
            self._dnd_hook = None
        self._save_ui_state()
        self.destroy()

    def _load_icon_image(self) -> tk.PhotoImage | None:
        if ICON_FILE.is_file():
            try:
                return tk.PhotoImage(file=str(ICON_FILE))
            except tk.TclError:
                pass
        # 没有图标文件时用代码画一个，保持零第三方依赖。
        size = 64
        image = tk.PhotoImage(width=size, height=size)
        image.put(ACCENT, to=(0, 0, size, size))
        image.put("#D6E6F7", to=(29, 8, 32, 44))
        for y in range(10, 42):
            width = int((y - 10) * 22 / 32)
            image.put("#FFFFFF", to=(32, y, 32 + width, y + 1))
        for i in range(10):
            image.put("#FFFFFF", to=(12 + i, 44 + i, 52 - i, 45 + i))
        image.put("#D6E6F7", to=(8, 56, 56, 58))
        return image

    def _icon_image(self, px: int) -> tk.PhotoImage | None:
        """按目标像素取整倍缩小的图标；PNG 原图可能很大，直接塞进弹窗会把它撑爆。"""
        try:
            icon = self._load_icon_image()
        except tk.TclError:
            return None
        if icon is None:
            return None
        try:
            factor = max(1, icon.width() // px)
            if factor > 1:
                icon = icon.subsample(factor)
        except tk.TclError:
            return None
        return icon

    def _set_app_icon(self) -> None:
        try:
            icon = self._load_icon_image()
        except tk.TclError:
            return
        if icon is None:
            return
        self._app_icon = icon
        try:
            self.iconphoto(True, icon)
            self.wm_iconname(APP_NAME)
        except tk.TclError:
            pass

    def _apply_geometry(self) -> None:
        work_x, work_y, work_w, work_h = get_work_area(self)
        max_w = max(640, work_w - 24)
        max_h = max(480, work_h - 24)
        req_w, req_h = 960, 600

        saved = self._parse_geometry(self._load_ui_state().get("geometry"))
        if saved is not None:
            width, height, sx, sy = saved
            # geometry() 不受 minsize 约束，恢复旧窗口时也要兜底到可用最小宽。
            width = min(max(width, 880, req_w), max_w)
            height = min(max(height, req_h), max_h)
            x = work_x + (work_w - width) // 2 if sx is None else min(max(sx, work_x), work_x + work_w - width)
            y = work_y + (work_h - height) // 4 if sy is None else min(max(sy, work_y), work_y + work_h - height)
        else:
            width = min(max(1100, req_w), max_w)
            height = min(max(700, req_h), max_h)
            x = work_x + max(0, (work_w - width) // 2)
            y = work_y + max(0, (work_h - height) // 4)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min(req_w, max_w), min(req_h, max_h))


    def _center_over(self, window: tk.Toplevel, min_w: int = 0, min_h: int = 0) -> None:
        window.update_idletasks()
        work_x, work_y, work_w, work_h = get_work_area(self)
        width = min(max(min_w, window.winfo_reqwidth()), max(320, work_w - 40))
        height = min(max(min_h, window.winfo_reqheight()), max(240, work_h - 40))
        x = work_x + max(0, (work_w - width) // 2)
        y = work_y + max(0, (work_h - height) // 3)
        window.geometry(f"{width}x{height}+{x}+{y}")

    def _load_whitelist(self) -> set[str]:
        try:
            if WHITELIST_FILE.exists():
                data = json.loads(WHITELIST_FILE.read_text(encoding="utf-8"))
                return {str(x).strip().lower().replace("-", "_") for x in data if str(x).strip()}
        except Exception:
            pass
        return set()

    def _save_whitelist(self) -> None:
        WHITELIST_FILE.write_text(json.dumps(sorted(self.whitelist), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _current_engine(self) -> str:
        category = self.category_var.get()
        if category == CUSTOM_CATEGORY_LABEL:
            return CUSTOM_LABEL_TO_ENGINE.get(self.provider_var.get(), "openai")
        return FREE_LABEL_TO_ENGINE.get(category, "mymemory")

    def _commit_engine(self, engine: str) -> None:
        key_field = ENGINE_KEY_FIELDS.get(engine)
        if key_field:
            self.config[key_field] = self.api_key_var.get().strip()
        if engine in ENGINE_URL_FIELDS:
            self.config[ENGINE_URL_FIELDS[engine]] = self.url_var.get().strip()
            self.config[ENGINE_MODEL_FIELDS[engine]] = self.model_var.get().strip()

    def _snapshot_config(self) -> dict:
        engine = self._current_engine()
        config = dict(self.config)
        config["engine"] = engine
        key_field = ENGINE_KEY_FIELDS.get(engine)
        if key_field:
            config[key_field] = self.api_key_var.get().strip()
        if engine in ENGINE_URL_FIELDS:
            config[ENGINE_URL_FIELDS[engine]] = self.url_var.get().strip()
            config[ENGINE_MODEL_FIELDS[engine]] = self.model_var.get().strip()
        config["fill_community"] = bool(self.fill_var.get())
        config["refine_community"] = bool(self.refine_var.get())
        config["community_enabled"] = bool(self.community_var.get())
        config["translate_hardcoded"] = bool(self.hardcoded_var.get())
        config["auto_export_bridge"] = bool(self.auto_export_var.get())
        config["auto_update_literalbridge"] = bool(self.auto_update_var.get())
        config["concurrency"] = int(self.concurrency_var.get())
        config["disable_thinking"] = bool(self.disable_thinking_var.get())
        config["theme"] = self._theme_mode
        return config

    def _choose_pack_format(self, instance: Path | None = None) -> bool:
        if instance is None:
            value = self.path_var.get().strip()
            if not value:
                messagebox.showinfo("游戏版本", "请先选择 Minecraft 实例。")
                return False
            instance = Path(value)
        versions = [".".join(map(str, version)) for version, _ in core.PACK_FORMATS]
        window = tk.Toplevel(self)
        window.title("选择 Minecraft 版本 / 资源包格式")
        window.transient(self)
        window.grab_set()
        body = ttk.Frame(window, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="未检测到游戏版本。请选择实际版本，或直接填写 pack_format：").pack(anchor="w")
        choice = tk.StringVar(value="")
        ttk.Combobox(body, textvariable=choice, values=versions, state="readonly", width=24).pack(fill="x", pady=(12, 6))
        value = tk.StringVar(value=str(self.config.get("pack_format") or ""))
        ttk.Entry(body, textvariable=value).pack(fill="x")
        result = [False]

        def save() -> None:
            try:
                raw = value.get().strip()
                if not raw and not choice.get():
                    raise ValueError("缺少版本")
                selected = core.clean_pack_format(raw) if raw else core.pack_format_for_version(choice.get())
                if float(selected) <= 0:
                    raise ValueError("格式非正数")
            except ValueError:
                messagebox.showerror("格式无效", "请选择版本或输入正数 pack_format（1.21.9+ 为小数，如 97.1）。", parent=window)
                return
            self.config["pack_format"] = selected
            core.save_config(self.config)
            self.pack_format_var.set(f"手动指定 → pack_format {selected}")
            result[0] = True
            window.destroy()

        ttk.Button(body, text="确定", command=save).pack(anchor="e", pady=(12, 0))
        self._center_over(window, 460, 210)
        self.wait_window(window)
        return result[0]

    def _ensure_pack_format(self, instance: Path, config: dict) -> bool:
        try:
            fmt = core.resolve_pack_format(config, instance)
        except ValueError:
            if not self._choose_pack_format(instance):
                return False
            config["pack_format"] = self.config["pack_format"]
            fmt = core.clean_pack_format(config["pack_format"])
        version = core.detect_minecraft_version(instance)
        self.pack_format_var.set(f"检测到 {version} → pack_format {fmt}" if version else f"游戏版本未确认 → pack_format {fmt}（手动/已有包）")
        return True

    def _sync_engine_ui(self) -> None:
        engine = self.config.get("engine", "mymemory")
        if engine in FREE_ENGINES:
            self.category_var.set(FREE_ENGINES[engine])
        else:
            self.category_var.set(CUSTOM_CATEGORY_LABEL)
            self.provider_var.set(CUSTOM_ENGINES.get(engine, "OpenAI 兼容"))
        self._refresh_profiles()
        self._update_input_state()
        self._update_settings_summary()

    def _refresh_profiles(self) -> None:
        profiles = self.config.get("custom_profiles", {})
        self.profile_combo.configure(values=list(profiles.keys()))
        if not profiles:
            self.profile_var.set("")

    def _profile_selected(self, _event: object = None) -> None:
        name = self.profile_var.get()
        profile = self.config.get("custom_profiles", {}).get(name)
        if not profile:
            return
        provider = profile.get("provider", "openai")
        if provider in CUSTOM_ENGINES:
            self.provider_var.set(CUSTOM_ENGINES[provider])
        self.category_var.set(CUSTOM_CATEGORY_LABEL)
        self.api_key_var.set(profile.get("api_key", ""))
        self.url_var.set(profile.get("base_url", ""))
        self.model_var.set(profile.get("model", ""))
        self._last_engine = self._current_engine()
        self.api_key_entry.configure(state="normal")
        self.url_entry.configure(state="normal")
        self.model_entry.configure(state="normal")
        self.provider_combo.configure(state="readonly")
        self.profile_combo.configure(state="readonly")
        self._update_settings_summary()

    def _save_profile(self) -> None:
        name = simpledialog.askstring("保存预设", "给这个自定义接口起个名字：", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        provider = CUSTOM_LABEL_TO_ENGINE.get(self.provider_var.get(), "openai")
        profiles = dict(self.config.get("custom_profiles", {}))
        profiles[name] = {
            "provider": provider,
            "api_key": self.api_key_var.get().strip(),
            "base_url": self.url_var.get().strip(),
            "model": self.model_var.get().strip(),
        }
        self.config["custom_profiles"] = profiles
        core.save_config(self.config)
        self._refresh_profiles()
        self.profile_var.set(name)
        self.engine_feedback.set(f"预设「{name}」已保存。")

    def _delete_profile(self) -> None:
        name = self.profile_var.get().strip()
        profiles = dict(self.config.get("custom_profiles", {}))
        if not name or name not in profiles:
            messagebox.showinfo("删除预设", "请先在「预设」下拉框里选中要删除的预设。")
            return
        if not messagebox.askyesno("删除预设", f"确定删除预设「{name}」吗？此操作不可撤销。"):
            return
        del profiles[name]
        self.config["custom_profiles"] = profiles
        core.save_config(self.config)
        self.profile_var.set("")
        self._refresh_profiles()
        self.engine_feedback.set(f"预设「{name}」已删除。")

    def _engine_selected(self, _event: object = None) -> None:
        self._commit_engine(self._last_engine)
        self._last_engine = self._current_engine()
        self._update_input_state()
        self._update_settings_summary()

    def _provider_selected(self, _event: object = None) -> None:
        self._commit_engine(self._last_engine)
        self._last_engine = self._current_engine()
        self._update_input_state()
        self._update_settings_summary()

    def _update_input_state(self) -> None:
        engine = self._current_engine()
        key_field = ENGINE_KEY_FIELDS.get(engine)
        if key_field:
            self.api_key_entry.configure(state="normal")
            self.api_key_var.set(str(self.config.get(key_field, "")))
        else:
            self.api_key_entry.configure(state="disabled")
            self.api_key_var.set("")
        if engine in ENGINE_URL_FIELDS:
            self.url_entry.configure(state="normal")
            self.url_var.set(str(self.config.get(ENGINE_URL_FIELDS[engine], "")))
            self.model_entry.configure(state="normal")
            self.model_var.set(str(self.config.get(ENGINE_MODEL_FIELDS[engine], "")))
        else:
            self.url_entry.configure(state="disabled")
            self.url_var.set("")
            self.model_entry.configure(state="disabled")
            self.model_var.set("")
        if engine in CUSTOM_ENGINES:
            self.provider_combo.configure(state="readonly")
            self.profile_combo.configure(state="readonly")
        else:
            self.provider_combo.configure(state="disabled")
            self.profile_combo.configure(state="disabled")

    def _save_settings(self) -> None:
        self.config = self._snapshot_config()
        core.save_config(self.config)
        self._update_settings_summary()
        self.engine_feedback.set(f"设置已保存（引擎：{self.config.get('engine')}）。")

    def _confirm_community_lookup(self, config: dict) -> bool:
        if not config.get("community_enabled") or config.get("community_notice_accepted"):
            return True
        if not messagebox.askokcancel("社区汉化查询", "翻译前会按模组 ID 联网查询 CFPA 社区汉化，命中的 key 将优先使用社区译文；查询结果缓存 7 天。网络失败时仅使用 AI。是否启用？"):
            self.community_var.set(False)
            config["community_enabled"] = False
            return True
        config["community_notice_accepted"] = True
        self.config["community_notice_accepted"] = True
        core.save_config(self.config)
        return True

    def _test_connection(self) -> None:
        if self.translating:
            return
        config = self._snapshot_config()
        engine = self._current_engine()
        settings = {**core.DEFAULT_CONFIG, **config, **core._resolve_engine_fields(config, engine, {})}
        settings["timeout"] = 12
        settings["delay"] = 0
        self.engine_feedback.set("正在测试翻译接口连接……")
        def work() -> None:
            started = time.monotonic()
            try:
                if engine in core.ENGINES_WITH_KEY and not settings["api_key"]:
                    raise ValueError("缺少 API Key，请在翻译设置中填写。")
                answer = core.translate_batch(engine, {"test.connection": "Hello"}, settings)
                if "test.connection" not in answer:
                    raise ValueError("接口返回成功，但缺少测试条目。请检查模型或接口兼容性。")
                self.result_queue.put(("connection_test", f"连接成功：{settings['model']}，延迟 {time.monotonic() - started:.1f} 秒。"))
            except Exception as exc:
                self.result_queue.put(("connection_test", f"连接失败：{core.redact_secrets(str(exc))}"))
        threading.Thread(target=work, daemon=True).start()

    def _show_log(self) -> None:
        p = self._palette()
        window = tk.Toplevel(self)
        window.title("运行日志")
        window.configure(bg=p["bg"])
        text = tk.Text(window, wrap="none", state="normal", bg=p["card"], fg=p["ink"], insertbackground=p["ink"])
        text.pack(fill="both", expand=True, padx=12, pady=(12, 0))
        def refresh() -> None:
            text.configure(state="normal")
            text.delete("1.0", "end")
            if core.LOG_FILE.is_file():
                text.insert("end", core.redact_secrets(core.LOG_FILE.read_text(encoding="utf-8", errors="replace")[-50000:]))
            text.configure(state="disabled")
        refresh()
        buttons = ttk.Frame(window)
        buttons.pack(fill="x", padx=12, pady=12)
        ttk.Button(buttons, text="刷新", command=refresh).pack(side="left")
        ttk.Button(buttons, text="打开日志目录", command=lambda: os.startfile(str(core.LOG_FILE.parent))).pack(side="left", padx=8)
        ttk.Button(buttons, text="清空", command=lambda: (core.LOG_FILE.write_text("", encoding="utf-8"), refresh())).pack(side="left")
        self._center_over(window, 720, 430)

    # 切换时只隐藏页面，进行中的任务和控件内容保持原样。

    def _switch_mode(self, mode: str, persist: bool = True) -> None:
        if mode == "engine":
            mode = "settings"
        mode = mode if isinstance(mode, str) and mode in self._pages else "home"
        self._active_mode = mode
        for name, page in self._pages.items():
            if name != mode:
                page.pack_forget()
        self._pages[mode].pack(fill="both", expand=True)
        hints = {"home": "打开项目", "mc": "Minecraft 实例与整合包",
                 "game": "已注册适配器：" + " / ".join(core.engine_adapters.registered()),
                 "settings": "翻译引擎、外观主题与日志诊断"}
        self.shell.select(mode, hints[mode])
        if mode == "home":
            self._home_page.refresh(core.load_localize_history())
        if mode == "settings":
            self._refresh_system_theme()
        if mode == "mc" and not self._mc_started:
            self._mc_started = True
            self.refresh_instances()
        if persist:
            try:
                state = self._load_ui_state()
                state["workbench_page"] = mode
                UI_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError:
                pass

    def _open_from_home(self, kind: str) -> None:
        if kind == "mc":
            self._mc_started = True
            self._switch_mode("mc")
            self._mc_tabs.select(self._mc_page)
            if self.scanning or self.translating or self.managing_pack:
                return
            self.choose_path()
            if not self.instance_paths and not self.path_var.get():
                self.refresh_instances()
        else:
            self._switch_mode("game")
            if not self._localizing:
                (self._loc_choose_file if kind == "file" else self._loc_choose)()

    def _open_record(self, path: Path) -> None:
        if not path.exists():
            messagebox.showinfo("产物目录不存在", f"这份产物可能已移动或删除：\n{path}", parent=self)
            return
        os.startfile(str(path if path.is_dir() else path.parent))

    def _remove_record(self, path: Path) -> None:
        core.remove_localize_history(path)
        self._home_page.refresh(core.load_localize_history())

    def _clear_records(self) -> None:
        records = core.load_localize_history()
        if not records:
            messagebox.showinfo("清理历史", "产物历史已经是空的。", parent=self)
            return
        if not messagebox.askyesno(
            "清理产物历史",
            f"当前共有 {len(records)} 条历史记录。\n"
            "只清空软件里的历史登记，不会删除磁盘上的任何游戏或汉化副本。\n\n"
            "确认清空全部产物历史？",
            parent=self,
        ):
            return
        core.clear_localize_history()
        self._home_page.refresh([])
        if self._loc_alive():
            self._loc_status_var.set("已清空产物历史")

    def _loc_alive(self) -> bool:
        try:
            return not self._closing and bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _show_localize(self) -> None:
        self._switch_mode("game")

    def _build_game_page(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent,
            text="项目位置",
            font=FONT_SMALL_BOLD, foreground=MUTED,
        ).pack(anchor="w", pady=(0, 3))

        target_card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        self._card_frames.append(target_card)
        target_card.pack(fill="x", pady=(0, 8))
        box = ttk.Frame(target_card, style="Card.TFrame", padding=10)
        box.pack(fill="both", expand=True)

        pick = ttk.Frame(box, style="Card.TFrame")
        pick.pack(fill="x")
        self._loc_path_var = tk.StringVar(value=str(self._load_ui_state().get("localize_dir") or ""))
        entry = ttk.Entry(pick, textvariable=self._loc_path_var, font=FONT_BODY)
        self._loc_path_entry = entry
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _e: self._loc_start_probe())
        ttk.Button(pick, text="选游戏目录…", command=self._loc_choose).pack(side="left", padx=(6, 0))
        ttk.Button(pick, text="选择镜像或程序…", command=self._loc_choose_file).pack(side="left", padx=(6, 0))
        self._loc_path_var.trace_add("write", self._loc_path_changed)

        out_row = ttk.Frame(box, style="Card.TFrame")
        out_row.pack(fill="x", pady=(6, 0))
        ttk.Label(out_row, text="副本输出目录（可选）", style="Card.Muted.TLabel").pack(side="left")
        self._loc_out_var = tk.StringVar(value=str(self.config.get("localize_output_dir") or ""))
        out_entry = ttk.Entry(out_row, textvariable=self._loc_out_var, font=FONT_BODY)
        out_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))
        ttk.Button(out_row, text="浏览…", command=self._loc_choose_output).pack(side="left", padx=(6, 0))

        opt_row = ttk.Frame(box, style="Card.TFrame")
        opt_row.pack(fill="x", pady=(6, 0))
        ttk.Label(opt_row, text="UE AES 密钥（可选）", style="Card.Muted.TLabel").pack(side="left")
        self._loc_aes_var = tk.StringVar(value="")
        aes_entry = ttk.Entry(opt_row, textvariable=self._loc_aes_var, font=FONT_BODY, width=26)
        aes_entry.pack(side="left", fill="x", expand=True, padx=(8, 10))
        aes_entry.bind("<Return>", lambda _e: self._loc_start_probe())
        self._loc_worldbook_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt_row,
            text="翻译前自动预分析设定集",
            variable=self._loc_worldbook_var,
        ).pack(side="left")

        tools = ttk.Frame(parent)
        tools.pack(fill="x", pady=(0, 6))
        self._loc_probe_button = make_action_button(tools, "检测游戏", self._loc_start_probe, ACCENT)
        self._loc_probe_button.pack(side="left")
        self._loc_recon_button = ttk.Button(tools, text="字节侦查", command=self._loc_start_recon)
        self._loc_recon_button.pack(side="left", padx=(6, 0))
        self._loc_analyze_button = ttk.Button(tools, text="剧本预分析", command=self._loc_start_analyze)
        self._loc_analyze_button.pack(side="left", padx=(6, 0))
        self._loc_run_button = make_action_button(tools, "开始汉化", self._loc_start, GREEN, state="disabled")
        self._loc_run_button.pack(side="left", padx=(8, 0))
        self._loc_stop_button = make_action_button(tools, "停止", self._loc_stop, RED, state="disabled")
        self._loc_stop_button.pack(side="right")
        self._themed_buttons.extend([(self._loc_probe_button, "bg"), (self._loc_run_button, "bg"), (self._loc_stop_button, "bg")])
        self._loc_status_var = tk.StringVar(value="等待检测")
        ttk.Label(parent, textvariable=self._loc_status_var, foreground=MUTED, font=FONT_SMALL, wraplength=620).pack(anchor="w")

        self._loc_progress = ttk.Progressbar(parent, mode="determinate")
        self._loc_progress.pack(fill="x", pady=(0, 6))

        report_card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        self._card_frames.append(report_card)
        self._loc_report = tk.Text(
            report_card, height=11, wrap="word", bg=CARD, fg=INK, font=FONT_BODY,
            relief="flat", padx=12, pady=10,
            highlightthickness=0, spacing1=1, spacing3=3,
        )
        loc_scroll = ttk.Scrollbar(report_card, orient="vertical", command=self._loc_report.yview)
        self._loc_report.configure(yscrollcommand=loc_scroll.set)
        self._loc_report.pack(side="left", fill="both", expand=True)
        loc_scroll.pack(side="right", fill="y")
        for tag, color, font in (
            ("head", INK, FONT_BOLD),
            ("ok", GREEN, FONT_BODY),
            ("warn", AMBER, FONT_BODY),
            ("bad", RED, FONT_BODY),
            ("dim", MUTED, FONT_SMALL),
        ):
            self._loc_report.tag_configure(tag, foreground=color, font=font)
        self._loc_report.configure(state="disabled")

        foot = ttk.Frame(parent)
        foot.pack(side="bottom", fill="x", pady=(6, 0))
        report_card.pack(fill="both", expand=True)
        foot_row1 = ttk.Frame(foot)
        foot_row1.pack(fill="x")
        self._loc_edit_tl_button = ttk.Button(foot_row1, text="① 提取文本(自翻)", command=self._loc_open_tl_file)
        self._loc_edit_tl_button.pack(side="left")
        self._loc_apply_button = ttk.Button(foot_row1, text="② 自翻回录重打包", command=self._loc_apply_only)
        self._loc_apply_button.pack(side="left", padx=(6, 6))
        self._loc_import_tl_button = ttk.Button(foot_row1, text="导入外部译文…", command=self._loc_import_external_tl)
        self._loc_import_tl_button.pack(side="left", padx=(0, 6))
        self._loc_fontpatch_button = ttk.Button(foot_row1, text="修复中文字体", command=self._loc_fontpatch)
        self._loc_fontpatch_button.pack(side="left", padx=(0, 6))
        self._loc_export_patch_button = ttk.Button(foot_row1, text="导出补丁包", command=self._loc_export_patch)
        self._loc_export_patch_button.pack(side="left")

        foot_row2 = ttk.Frame(foot)
        foot_row2.pack(fill="x", pady=(4, 0))
        self._loc_open_button = ttk.Button(foot_row2, text="打开游戏目录", command=self._loc_open_output, state="disabled")
        self._loc_open_button.pack(side="left")
        self._loc_revert_button = ttk.Button(foot_row2, text="撤销汉化", command=self._loc_revert_game)
        self._loc_revert_button.pack(side="left", padx=(6, 0))
        self._loc_history_button = ttk.Button(foot_row2, text="最近记录", command=self._loc_show_history)
        self._loc_history_button.pack(side="left", padx=(6, 0))
        ttk.Button(foot_row2, text="清理历史", command=self._clear_records).pack(side="left", padx=(6, 0))

        self._loc_play_path: Path | None = None
        self._loc_write([
            ("head", "检测报告"),
            ("dim", "尚未检测游戏。支持直接把游戏文件夹、.exe 或镜像拖进本窗口，松手即自动识别并体检。"),
        ])

    def _close_localize(self) -> None:
        if self._localizing:
            self._loc_cancel.set()
        self._localizing = False

    def _loc_choose(self) -> None:
        path = filedialog.askdirectory(title="选择游戏目录（exe 或镜像所在的那一层）", parent=self)
        if path:
            self._loc_path_var.set(path)
            self._loc_start_probe()

    def _loc_choose_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择游戏镜像 / 安装包（.apk/.ipa/.vpk/.iso/.cso/.nds/.jar/.sis/.cod/.bar/.swf/.hdi/.dir）或单 EXE 游戏",
            filetypes=[
                ("全平台游戏镜像 / 安装包 / 可执行文件", "*.chd *.cue *.bin *.iso *.mdf *.mds *.cso *.nds *.z64 *.v64 *.n64 *.tra *.apk *.ipa *.vpk *.jar *.sis *.sisx *.cod *.bar *.swf *.hdi *.fdi *.nhd *.dir *.dxr *.cst *.ald *.love *.xp3 *.pck *.pfs *.pak *.locres *.exe *.com *.dat"),
                ("主机与掌机镜像 (.chd/.cue/.bin/.iso/.mdf/.cso/.nds/.z64/.v64/.n64/.vpk)", "*.chd *.cue *.bin *.iso *.mdf *.mds *.cso *.nds *.z64 *.v64 *.n64 *.vpk"),
                ("移动端 / 功能机 / 复古与 AGS (.apk/.ipa/.jar/.sis/.swf/.hdi/.tra/.pak)", "*.apk *.ipa *.jar *.sis *.sisx *.cod *.bar *.swf *.dir *.dxr *.cst *.ald *.hdi *.fdi *.nhd *.tra *.pak"),
                ("所有文件", "*.*"),
            ],
            parent=self,
        )
        if path:
            p = Path(path)
            self._loc_path_var.set(str(p))
            if hasattr(self, "_loc_path_entry"):
                try:
                    self._loc_path_entry.xview_moveto(1.0)
                except tk.TclError:
                    pass
            self._loc_start_probe()

    def _loc_adapter_options(self) -> dict[str, object]:
        opts: dict[str, object] = {}
        raw_aes = (self._loc_aes_var.get() if hasattr(self, "_loc_aes_var") else "").strip()
        if raw_aes:
            opts["aes_key"] = raw_aes
        return opts

    def _loc_start_analyze(self) -> None:
        if not self._loc_alive() or self._localizing:
            return
        raw = self._loc_path_var.get().strip().strip('"')
        if not raw or not Path(raw).exists():
            messagebox.showinfo("剧本预分析", "请先选择一个有效的游戏目录或文件。", parent=self)
            return
        root = Path(raw)
        from ferrygame.worldbook import find as find_wb

        existing = find_wb(root)
        force = False
        if existing is not None:
            choice = messagebox.askyesnocancel(
                "剧本设定集已存在",
                f"检测到该游戏已有设定集：\n{existing}\n\n"
                "· 点「是」：直接打开手改\n"
                "· 点「否」：重新调用大模型预分析并覆盖\n"
                "· 点「取消」：返回",
                parent=self,
            )
            if choice is None:
                return
            if choice is True:
                try:
                    os.startfile(str(existing))
                except OSError as exc:
                    messagebox.showerror("打开设定集", core.explain_error(exc, stage="打开设定集"), parent=self)
                return
            force = True

        config = self._snapshot_config()
        try:
            settings = core.localize_settings(config, root)
        except (ValueError, OSError) as exc:
            messagebox.showerror("剧本预分析", core.explain_error(exc, stage="准备分析配置"), parent=self)
            return
        if settings["engine"] in core.ENGINES_WITH_KEY and not settings.get("api_key"):
            messagebox.showinfo(
                "缺少 API Key",
                f"当前引擎「{settings['engine']}」需要 API Key，请先在「设置」页配置。",
                parent=self,
            )
            return

        opts = self._loc_adapter_options()
        self._localizing = True
        self._loc_cancel.clear()
        self._loc_set_busy(True)
        self._loc_status_var.set("正在通读抽样剧本，生成世界观与专有名词表……")
        self._loc_write([("dim", "正在执行第 0 步：轻量剧本预分析（通常只需 1 次模型调用）……")])
        threading.Thread(
            target=self._loc_analyze_worker,
            args=(root, settings, opts, force),
            daemon=True,
        ).start()

    def _loc_analyze_worker(self, root: Path, settings: dict, opts: dict, force: bool) -> None:
        try:
            from ferrygame.pipeline import analyze_game

            wb, _proj, _probe = analyze_game(
                root,
                settings=settings,
                adapter_options=opts,
                force=force,
                save=True,
                cancel_event=self._loc_cancel,
            )
            self.result_queue.put(("loc_analyze_done", wb))
        except Exception as exc:
            core.LOG.exception("localize: analyze failed")
            self.result_queue.put(("loc_error", ("analyze", core.explain_error(exc, stage="剧本预分析"))))

    def _loc_fontpatch(self) -> None:
        raw = self._loc_path_var.get().strip().strip('"')
        if not raw or not Path(raw).exists():
            messagebox.showinfo("修复中文字体", "请先选择一个游戏目录。", parent=self)
            return
        root = Path(raw)
        if root.is_file():
            root = root.parent
        engine = "auto"
        if self._loc_probe is not None and getattr(self._loc_probe, "engine", ""):
            low = self._loc_probe.engine.lower()
            if "ren'py" in low or "renpy" in low:
                engine = "renpy"
            elif "ace" in low or "vx" in low or "xp" in low:
                engine = "rpgmaker_ace"
            elif "rpg maker" in low:
                engine = "rpgmaker"
            elif "kirikiri" in low:
                engine = "kirikiri"
            elif "godot" in low:
                engine = "godot"
        font_choice = filedialog.askopenfilename(
            title="选择开源/本机中文字体（直接点取消则自动使用摆渡 fonts/ 或系统黑体/等线）",
            filetypes=[("TrueType / OpenType 字体", "*.ttf *.otf"), ("所有文件", "*.*")],
            parent=self,
        )
        font_path = Path(font_choice) if font_choice else None
        try:
            rep = core.patch_game_font(root, engine=engine, font_path=font_path)
        except Exception as exc:
            messagebox.showerror("修复中文字体", core.explain_error(exc, stage="注入中文字体"), parent=self)
            return
        tag = "ok" if rep.ok else "warn"
        lines: list[tuple[str, str]] = [(tag, rep.summary())]
        for note in rep.notes:
            lines.append(("dim", f"· {note}"))
        if rep.ok:
            lines.append(("dim", "已纳入 .ferry-undo.json，随时可点「撤销汉化」还原，也会自动收入「导出补丁包」。"))
        self._loc_write(lines)
        self._loc_status_var.set("中文字体回退已注入" if rep.ok else "未写入字体文件")

    def _loc_export_patch(self) -> None:
        raw = self._loc_path_var.get().strip().strip('"')
        if not raw or not Path(raw).exists():
            messagebox.showinfo("导出纯净补丁包", "请先选择一个已汉化或已提取文本的游戏目录。", parent=self)
            return
        root = Path(raw)
        base = root.parent if root.is_file() else root
        out_path = filedialog.asksaveasfilename(
            title="导出可分享的纯净汉化补丁包（不含原版游戏大文件）",
            initialdir=str(base.parent),
            initialfile=f"{base.name}_汉化补丁.zip",
            defaultextension=".zip",
            filetypes=[("ZIP 补丁压缩包", "*.zip")],
            parent=self,
        )
        if not out_path:
            return
        try:
            rep = core.export_patch_pack(base, out_zip=Path(out_path), include_source=True)
        except Exception as exc:
            messagebox.showerror("导出补丁包", core.explain_error(exc, stage="导出汉化补丁包"), parent=self)
            return
        tag = "ok" if rep.ok else "warn"
        lines: list[tuple[str, str]] = [(tag, rep.summary())]
        for note in rep.notes:
            lines.append(("dim", f"· {note}"))
        for sk in rep.skipped[:10]:
            lines.append(("warn", f"跳过：{sk}"))
        self._loc_write(lines)
        self._loc_status_var.set("已导出纯净补丁包" if rep.ok else "未导出补丁包")
        if rep.ok and rep.out_zip is not None:
            if messagebox.askyesno("导出成功", f"{rep.summary()}\n\n是否打开补丁包所在文件夹？", parent=self):
                try:
                    os.startfile(str(rep.out_zip.parent))
                except OSError:
                    pass

    def _loc_offer_missing_deps(self, root: Path, needs: list[str]) -> None:
        if not needs:
            return
        status = core.resolve_dependencies(needs)
        if not status.missing:
            return
        dep = status.missing[0]
        if not messagebox.askyesno("缺少外部汉化工具", dep.prompt(), parent=self):
            return
        self._loc_set_busy(True)
        self._loc_status_var.set(f"正在下载安装 {dep.name}……")
        self._loc_write([("dim", f"正在下载并安装 {dep.name} 到 {dep.target}，请稍候……")])
        threading.Thread(
            target=self._loc_dep_install_worker,
            args=(root, dep),
            daemon=True,
        ).start()

    def _loc_dep_install_worker(self, root: Path, dep) -> None:
        def _prog(done: int, total: int) -> None:
            if total > 0:
                self.result_queue.put(("loc_dep_progress", (dep.name, done, total)))

        ok, message = core.install_dependency(dep, progress=_prog)
        self.result_queue.put(("loc_dep_done", (root, ok, message)))

    def _loc_start_recon(self) -> None:
        raw = self._loc_path_var.get().strip().strip('"')
        if not raw or not Path(raw).exists():
            messagebox.showinfo("字节侦查", "请先选择有效的游戏目录或文件。", parent=self)
            return
        from ferrygame.recon import inspect_game

        root = Path(raw)
        report = inspect_game(root)
        lines: list[tuple[str, str]] = []
        for line in report.summary().splitlines():
            tag = "head" if (line.startswith("===") or line.startswith("[")) else ("ok" if "最佳替身" in line or "剧情" in line else "dim")
            lines.append((tag, line))
        self._loc_write(lines)
        self._loc_status_var.set("字节侦查完成")

    _SINGLE_FILE_GAME_EXTS = {
        ".chd", ".cue", ".bin", ".iso", ".mdf", ".mds", ".cso", ".nds", ".z64", ".v64", ".n64",
        ".vpk", ".apk", ".ipa", ".swf", ".hdi", ".fdi", ".nhd", ".tra", ".pak",
    }

    def _loc_progress_busy(self, active: bool, *, fill_done: bool = False) -> None:
        """统一驱动游戏汉化页的进度条（体检、提取文本、AI 汉化共用同一条进度条）。"""
        if not self._loc_alive():
            return
        if active:
            self._loc_progress.configure(mode="indeterminate", maximum=100, value=0)
            self._loc_progress.start(14)
        else:
            self._loc_progress.stop()
            self._loc_progress.configure(mode="determinate", maximum=100, value=100 if fill_done else 0)

    def _loc_open_tl_file(self) -> None:
        if not self._loc_alive() or self._localizing:
            return
        raw = self._loc_path_var.get().strip().strip('"')
        if not raw or not Path(raw).exists():
            messagebox.showinfo("提取文本（自翻导出）", "请先选择一个游戏目录或文件。", parent=self)
            return
        root = Path(raw)
        self._localizing = True
        self._loc_set_busy(True)
        self._loc_progress_busy(True)
        self._loc_status_var.set("正在解包并提取游戏文本……")
        self._loc_write([
            ("head", "正在提取文本（0 Token）……"),
            ("dim", f"目标：{root}"),
            ("dim", "大体积镜像（如 .chd / .iso / .pak）首次解包可能需要数秒，请稍候。"),
        ])
        opts = self._loc_adapter_options()
        threading.Thread(target=self._loc_extract_worker, args=(root, opts), daemon=True).start()

    def _loc_extract_worker(self, root: Path, opts: dict | None = None) -> None:
        try:
            from ferrygame.pipeline import scan_game
            from ferrygame.project import export_tsv, load as load_tl

            adapter_opts = dict(opts or {})
            root, detected = core.engine_adapters.resolve_game_target(root, **adapter_opts)
            proj = load_tl(root)
            need_rescan = (
                proj is None
                or not proj.units
                or (proj.game and proj.game != root.name)
                or (detected is not None and proj.adapter and proj.adapter != detected.name)
            )
            probe_summary = ""
            if need_rescan:
                proj, probe = scan_game(root, adapter=detected, save=True)
                probe_summary = probe.summary()
                if proj.path is None or not proj.units:
                    self.result_queue.put(("loc_extract_done", (None, None, None, probe_summary)))
                    return
            tsv_path = export_tsv(proj)
            out_dir = tsv_path.parent if tsv_path.is_file() else (proj.path.parent if proj.path else root)
            self.result_queue.put(("loc_extract_done", (proj, tsv_path, out_dir, probe_summary)))
        except Exception as exc:
            core.LOG.exception("localize: extract_worker failed")
            self.result_queue.put(("loc_extract_error", core.explain_error(exc, stage="提取游戏文本")))

    def _loc_import_external_tl(self) -> None:
        raw = self._loc_path_var.get().strip().strip('"')
        if not raw or not Path(raw).exists():
            messagebox.showinfo("导入外部译文", "请先选择游戏目录。", parent=self)
            return
        ext_file = filedialog.askopenfilename(
            title="选择自翻译完成的词表文件（.tsv / .csv / .json / ParaTranz）",
            filetypes=[
                ("自翻译双语词表 (.tsv / .csv / .json)", "*.tsv *.csv *.json"),
                ("所有文件", "*.*"),
            ],
            parent=self,
        )
        if not ext_file:
            return
        from ferrygame.pipeline import scan_game
        from ferrygame.project import import_external_translations, load as load_tl

        root = Path(raw)
        proj = load_tl(root)
        if proj is None:
            proj, _ = scan_game(root, save=True)
        count = import_external_translations(proj, Path(ext_file), save=True)
        self._loc_status_var.set(f"已从 {Path(ext_file).name} 回录 {count} 条译文")
        if messagebox.askyesno("回录完成", f"已成功从 {Path(ext_file).name} 回录 {count} 条译文。\n是否立即执行重打包写回游戏？", parent=self):
            self._loc_apply_only()

    def _loc_apply_only(self) -> None:
        raw = self._loc_path_var.get().strip().strip('"')
        if not raw or not Path(raw).exists():
            messagebox.showinfo("写回译文表", "请先选择游戏目录。", parent=self)
            return
        from ferrygame.pipeline import apply_project
        from ferrygame.project import load as load_tl

        root = Path(raw)
        proj = load_tl(root)
        if proj is None:
            messagebox.showinfo("写回译文表", "该目录下还没有译文表，请先检测游戏或点击「编辑译文表」。", parent=self)
            return
        out_str = (self._loc_out_var.get() or "").strip()
        out = Path(out_str) if out_str else None
        try:
            ap = apply_project(proj, out=out)
        except Exception as exc:
            core.LOG.exception("localize: apply_only failed")
            messagebox.showerror("写回译文表", core.explain_error(exc, stage="写回游戏文件"), parent=self)
            return
        lines: list[tuple[str, str]] = [
            ("head", f"写回完成（0 Token）：成功 {ap.applied} 条"),
            ("ok", f"产出位置：{ap.out_path}"),
        ]
        for note in ap.notes:
            lines.append(("ok", f"· {note}"))
        for c in ap.conflicts[:15]:
            lines.append(("bad", f"冲突：{c}"))
        self._loc_write(lines)
        if ap.out_path:
            self._loc_play_path = Path(ap.out_path)
            self._loc_out_dir = self._loc_play_path if self._loc_play_path.is_dir() else self._loc_play_path.parent
            self._loc_open_button.configure(state="normal")
        self._loc_status_var.set(f"已按 ferry_tl.json 重刷 {ap.applied} 条")

    def _loc_revert_game(self) -> None:
        raw = self._loc_path_var.get().strip().strip('"')
        if not raw or not Path(raw).exists():
            messagebox.showinfo("撤销还原", "请先选择游戏目录。", parent=self)
            return
        root = Path(raw)
        base = root.parent if root.is_file() else root
        if not messagebox.askyesno("一键撤销还原", f"确定按 .ferry-undo.json 还原该游戏目录吗？\n{base}", parent=self):
            return
        rev = core.engine_adapters.revert_game(base)
        tag = "ok" if rev.ok else "warn"
        lines: list[tuple[str, str]] = [(tag, f"撤销结果：{rev.summary()}")]
        for f in rev.failed:
            lines.append(("bad", f"失败：{f}"))
        self._loc_write(lines)
        self._loc_status_var.set(rev.summary())

    def _loc_choose_output(self) -> None:
        if not self._loc_alive():
            return
        path = filedialog.askdirectory(title="选择汉化副本产出目录", parent=self._loc_win)
        if path:
            self._loc_out_var.set(path)
            self._loc_save_output_dir()

    def _loc_save_output_dir(self) -> None:
        value = (self._loc_out_var.get() if hasattr(self, "_loc_out_var") else "").strip()
        self.config["localize_output_dir"] = value
        try:
            core.save_config(self.config)
        except OSError as exc:
            core.LOG.debug("save localize_output_dir failed: %s", type(exc).__name__)

    def _loc_path_changed(self, *_args: object) -> None:
        # 换了目录，上一次的体检结论就作废 —— 宁可多点一次，也不能拿旧结论开新游戏。
        self._loc_probe = None
        self._loc_refresh_run_state()

    def _remember_localize_dir(self, root: Path) -> None:
        try:
            state = self._load_ui_state()
            state["localize_dir"] = str(root)
            UI_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (OSError, tk.TclError) as exc:
            core.LOG.debug("remember localize dir failed: %s", type(exc).__name__)

    def _loc_refresh_run_state(self) -> None:
        if not self._loc_alive():
            return
        ready = bool(self._loc_probe is not None and self._loc_probe.ready)
        self._loc_run_button.configure(state="normal" if ready else "disabled")
        label = "开始汉化"
        root = self._loc_root
        if ready and root is not None:
            try:
                job = core.load_localize_job(root)
            except Exception:
                job = None
            if job is not None and getattr(job, "resumable", False):
                label = "继续汉化"
            elif job is not None and getattr(job, "done", False):
                label = "再次汉化"
        try:
            self._loc_run_button.configure(text=label)
        except tk.TclError:
            pass

    def _loc_set_busy(self, busy: bool) -> None:
        if not self._loc_alive():
            return
        self._loc_probe_button.configure(state="disabled" if busy else "normal")
        self._loc_stop_button.configure(state="normal" if busy else "disabled")
        if hasattr(self, "_loc_edit_tl_button"):
            self._loc_edit_tl_button.configure(state="disabled" if busy else "normal")
        if busy:
            self._loc_run_button.configure(state="disabled")
        else:
            self._loc_refresh_run_state()

    def _loc_write(self, lines: list[tuple[str, str]]) -> None:
        if not self._loc_alive():
            return
        widget = self._loc_report
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for tag, text in lines:
            widget.insert("end", text + "\n", tag)
        widget.configure(state="disabled")

    def _loc_start_probe(self) -> None:
        if not self._loc_alive() or self._localizing:
            return
        raw = self._loc_path_var.get().strip().strip('"')
        if not raw:
            messagebox.showinfo("体检", "先选一个游戏目录或文件。", parent=self._loc_win)
            return
        root = Path(raw)
        if not root.exists():
            messagebox.showinfo("体检", f"这个路径不存在：\n{raw}", parent=self._loc_win)
            return
        self._loc_root = root
        self._loc_probe = None
        self._loc_out_dir = None
        self._loc_play_path = None
        self._remember_localize_dir(root)
        if self._loc_alive():
            self._loc_open_button.configure(state="disabled")
            self._loc_progress_busy(True)
            if root.is_file():
                self._loc_status_var.set(f"正在体检文件：{root.name}……")
                self._loc_write([("dim", f"正在精准识别文件「{root.name}」，不扫描同目录其他文件，稍等。")])
            else:
                self._loc_status_var.set("正在体检……")
                self._loc_write([("dim", "正在看这个游戏的目录结构，稍等。")])
        self._loc_set_busy(True)
        opts = self._loc_adapter_options()
        threading.Thread(target=self._loc_probe_worker, args=(root, opts), daemon=True).start()

    def _loc_probe_worker(self, root: Path, opts: dict | None = None) -> None:
        try:
            adapter_opts = dict(opts or {})
            root, adapter = core.engine_adapters.resolve_game_target(root, **adapter_opts)
            existing_lines = core.existing_localize_lines(root)
            try:
                from ferrygame.worldbook import load as load_wb

                wb = load_wb(root)
                if wb is not None:
                    existing_lines = list(existing_lines) + [f"剧本设定集：{wb.summary()}（{wb.path}）"]
            except Exception:
                pass
            if adapter is None:
                hints = core.scan_hints(root)
                self.result_queue.put(("loc_probe", (root, None, "", hints, existing_lines)))
                return
            report = adapter.probe(root)
            try:
                from ferrygame.pipeline import scan_game

                tl_proj, _ = scan_game(root, adapter=adapter, adapter_options=adapter_opts, save=True)
                if tl_proj.units and tl_proj.path:
                    existing_lines = list(existing_lines) + [
                        f"双语译文表：{tl_proj.summary()}",
                        f"  · 文件位置：{tl_proj.path}",
                    ]
            except Exception:
                pass
            self.result_queue.put(("loc_probe", (root, report, adapter.name, [], existing_lines)))
        except Exception as exc:
            core.LOG.exception("localize: probe failed")
            self.result_queue.put(("loc_error", ("probe", core.explain_error(exc, stage="游戏体检"))))

    def _loc_probe_lines(self, report, adapter_name: str, existing_lines: list[str] | None = None) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = [("head", f"引擎：{report.engine}（适配器 {adapter_name}）")]
        target_root = report.root or self._loc_root
        if target_root is not None:
            tp = Path(target_root)
            kind_label = "目标文件" if tp.is_file() else "目标目录"
            lines.append(("dim", f"{kind_label}：{tp.name}（{tp}）"))
        if report.container or report.text_format:
            lines.append(("dim", f"容器：{report.container or '未标注'}　｜　文本格式：{report.text_format or '未标注'}"))
        lines.append(("", f"待翻：{report.unit_count} 条"))
        if report.writable:
            if getattr(report, "in_place", False):
                lines.append(("warn", "可写：是 —— 原地改写游戏目录（有撤销记录），不会另建「.cn」副本"))
                lines.append(("ok", f"汉化后请直接打开：{report.root or self._loc_root}"))
            else:
                lines.append(("ok", "可写：是 —— 只会产出副本，原游戏一个字节都不动"))
        else:
            lines.append(("bad", "可写：否 —— 翻了也放不回去，这就该停手"))
        if report.verifiable:
            lines.append(("ok", "自动验证：可以（能自己起游戏、读画面确认真的是中文）"))
        else:
            lines.append(("dim", "自动验证：这个引擎还没有验证通道，只能你自己进游戏看"))
        if report.images_with_text:
            lines.append(("warn", f"图片文字：{report.images_with_text} 处 —— 文本层永远碰不到，得重画图"))
        for note in report.notes:
            lines.append(("dim", note))
        if existing_lines:
            lines.append(("", ""))
            lines.append(("head", "已汉化 / 续传"))
            for tip in existing_lines:
                tag = "ok" if tip.startswith("已发现") or tip.startswith("上次") else "warn"
                if tip.startswith("  ·"):
                    tag = "ok"
                lines.append((tag, tip))
            play = self._loc_first_play_path(self._loc_root)
            if play is not None:
                self._loc_play_path = play
                self._loc_out_dir = play if play.is_dir() else play.parent
                if self._loc_alive():
                    self._loc_open_button.configure(
                        state="normal",
                        text="打开游戏目录" if getattr(report, "in_place", False) else "打开产出文件夹",
                    )
        lines.append(("", ""))
        if report.ready:
            if getattr(report, "in_place", False):
                lines.append(("ok", "结论：可以开工。翻完直接进原目录玩游戏；停止后再点「继续汉化」会从缓存续跑。"))
            else:
                lines.append(("ok", "结论：可以开工。翻完会产出副本，原版留着；停止后再点「继续汉化」会从缓存续跑。"))
        elif not report.readable:
            lines.append(("bad", "结论：认不出文本在哪，先别开工。"))
        elif not report.writable:
            lines.append(("bad", "结论：写不回去，先别开工 —— 这才是真正会白干的那一步。"))
        else:
            lines.append(("bad", "结论：没找到可翻译的文本单元。"))
        return lines

    def _loc_first_play_path(self, root: Path | None) -> Path | None:
        if root is None:
            return None
        try:
            records = core.find_localized(root)
        except Exception:
            return None
        for rec in records:
            path = Path(rec.play_path)
            if path.exists():
                return path
        return None

    def _loc_hint_lines(self, root: Path, hints: list[str]) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = [
            ("bad", "没有适配器认得这个游戏"),
            ("dim", "摆渡不是不能翻它，是还不知道它的文本住在哪。下面是把目录摊开看到的线索："),
            ("", ""),
        ]
        for hint in hints:
            lines.append(("", f"· {hint}"))
        lines.append(("", ""))
        lines.append(("dim", f"目录：{root}"))
        lines.append(("dim", "把这段发给我，就能判断该先加哪个引擎的适配器。"))
        return lines

    def _loc_start(self) -> None:
        if not self._loc_alive() or self._localizing:
            return
        probe = self._loc_probe
        root = self._loc_root
        if probe is None or root is None or not probe.ready:
            messagebox.showinfo("开始汉化", "先体检，确认「可写」和条数之后再开工。", parent=self._loc_win)
            return
        self._loc_save_output_dir()
        config = self._snapshot_config()
        try:
            settings = core.localize_settings(config, root)
        except (ValueError, OSError) as exc:
            messagebox.showerror("开始汉化", core.explain_error(exc, stage="准备汉化配置"), parent=self._loc_win)
            return
        if hasattr(self, "_loc_worldbook_var") and self._loc_worldbook_var.get():
            settings["auto_worldbook"] = True
        adapter_opts = self._loc_adapter_options()
        if settings["engine"] in core.ENGINES_WITH_KEY and not settings.get("api_key"):
            messagebox.showinfo(
                "缺少 API Key",
                f"现在的引擎是「{settings['engine']}」，需要 API Key。\n\n"
                "请回主界面展开「翻译设置」填好密钥并保存，再回来点开始。",
                parent=self._loc_win,
            )
            return
        self._localizing = True
        self._loc_cancel.clear()
        self._loc_set_busy(True)
        core.LOG.info("localize: start root=%s engine=%s entries=%s", root, settings.get("engine"), probe.unit_count)
        if self._loc_alive():
            self._loc_status_var.set("正在翻译……")
            self._loc_progress.configure(maximum=max(1, probe.unit_count), value=0)
            self._loc_open_button.configure(state="disabled")
        threading.Thread(target=self._loc_worker, args=(root, settings, adapter_opts), daemon=True).start()

    def _loc_stop(self) -> None:
        if not self._localizing:
            return
        self._loc_cancel.set()
        if self._loc_alive():
            self._loc_status_var.set("已请求停止，等当前批次返回后收尾……")

    def _loc_worker(self, root: Path, settings: dict, adapter_opts: dict | None = None) -> None:
        def progress(done: int, total: int, modid: str) -> None:
            self.result_queue.put(("loc_progress", (done, total, modid)))

        try:
            result = core.localize_game(
                root,
                settings=settings,
                progress=progress,
                cancel_event=self._loc_cancel,
                adapter_options=adapter_opts,
            )
            core.LOG.info("localize: done entries=%s translated=%s applied=%s out=%s",
                          result.entries, result.translated, result.applied, result.out_path)
            self.result_queue.put(("loc_done", result))
        except Exception as exc:
            core.LOG.exception("localize: worker failed")
            stage = str(getattr(exc, "_ferry_stage", "") or "汉化游戏")
            self.result_queue.put(("loc_error", ("run", core.explain_error(exc, stage=stage))))

    def _loc_show_result(self, result) -> None:
        lines: list[tuple[str, str]] = []
        in_place = bool(getattr(result, "in_place", False))
        cancelled = bool(getattr(result, "cancelled", False))
        if result.applied and result.out_path:
            play = Path(result.out_path)
            lines.append(("ok", f"{'中途停止，已写回' if cancelled else '完成'}：{result.applied} 条"))
            if in_place:
                lines.append(("ok", f"汉化地址（原地）：{play}"))
                lines.append(("dim", "没有「.cn」副本。直接进这个目录启动游戏。撤销靠 .ferry-undo.json。"))
            else:
                lines.append(("", f"副本：{play}"))
                lines.append(("dim", "原游戏没有改动。双击这个副本就是中文版。"))
            self._loc_play_path = play
            self._loc_out_dir = play if play.is_dir() else play.parent
            if self._loc_alive():
                self._loc_open_button.configure(
                    state="normal",
                    text="打开游戏目录" if in_place else "打开产出文件夹",
                )
            self._loc_status_var.set(
                f"{'已暂停 · 可继续' if cancelled else '完成'} · {result.applied} 条"
            )
        elif result.translated and not result.applied:
            lines.append(("warn", f"拿到 {result.translated} 条译文，但一条都没能写回去"))
            self._loc_status_var.set("写回失败")
        elif cancelled and result.translated:
            lines.append(("warn", f"已停止：缓存了 {result.translated} 条译文，尚未写回"))
            lines.append(("dim", "再点「继续汉化」会跳过已缓存批次，接着翻剩余的。"))
            self._loc_status_var.set("已暂停 · 可继续")
        elif result.entries:
            lines.append(("warn", "抽到了文本，但没有产出"))
            self._loc_status_var.set("未产出")
        else:
            lines.append(("warn", "没有产出"))
            self._loc_status_var.set("未产出")
        lines.append(("", ""))
        for line in result.summary().split("\n"):
            if line.strip():
                lines.append(("dim", line))
        if result.missed:
            lines.append(("warn", f"{result.missed} 条没拿到译文；再跑一次会只补缺的。"))
        for conflict in result.conflicts[:6]:
            lines.append(("bad", f"冲突：{conflict}"))
        if len(result.conflicts) > 6:
            lines.append(("bad", f"……另有 {len(result.conflicts) - 6} 处冲突，见运行日志。"))
        for error in result.errors[:6]:
            lines.append(("bad", error))
        existing = getattr(result, "existing", None) or []
        if existing:
            lines.append(("", ""))
            lines.append(("head", "可进游戏的地址"))
            for rec in existing:
                lines.append(("ok", core.describe_localized(rec)))
        lines.append(("", ""))
        if result.applied:
            if in_place:
                lines.append(("dim", "下一步：打开上面的目录，启动游戏看一眼。文本层翻不到的东西不在这次范围内。"))
            else:
                lines.append(("dim", "下一步：双击副本进游戏看一眼。文本层翻不到的东西（图片按钮、特殊字体）不在这次范围内。"))
        elif cancelled:
            lines.append(("dim", "下次点「继续汉化」即可断点续传，不会把已缓存的批次重翻一遍。"))
        self._loc_write(lines)
        self._loc_refresh_run_state()

    def _loc_open_output(self) -> None:
        target = self._loc_play_path or self._loc_out_dir
        if target is None:
            return
        path = Path(target)
        if path.is_file():
            path = path.parent
        if path.is_dir():
            os.startfile(str(path))

    def _loc_show_history(self) -> None:
        if not self._loc_alive():
            return
        try:
            records = core.load_localize_history()
        except Exception as exc:
            messagebox.showerror("最近汉化", core.redact_secrets(str(exc)), parent=self._loc_win)
            return
        if not records:
            messagebox.showinfo("最近汉化", "还没有登记过汉化产物。\n完成一次汉化后会出现在这里。", parent=self._loc_win)
            return
        lines: list[tuple[str, str]] = [("head", "最近汉化记录")]
        first_play: Path | None = None
        for rec in records[:20]:
            lines.append(("ok", core.describe_localized(rec)))
            if rec.root:
                lines.append(("dim", f"  原目录：{rec.root}"))
            play = Path(rec.play_path)
            if first_play is None and play.exists():
                first_play = play
        lines.append(("", ""))
        lines.append(("dim", "点「打开游戏目录」会打开列表里第一条仍存在的路径。"))
        self._loc_write(lines)
        if first_play is not None:
            self._loc_play_path = first_play
            self._loc_out_dir = first_play if first_play.is_dir() else first_play.parent
            self._loc_open_button.configure(state="normal", text="打开游戏目录")

    def _export_diagnostics(self) -> None:
        """一键诊断：把排障信息收成一个文件，让用户能整份发给作者。

        刻意做成「点一下就完事」——不弹保存对话框、不让选路径。会点这个按钮的人
        本来就是遇到了问题；每多一步选择，就多一批人放弃反馈。

        文件里**不含密钥的值**（配置一栏只写「设没设、是明文还是密文」），日志走脱敏，
        所以这一份可以直接丢到公开的地方。
        """
        instance_path = self.path_var.get().strip()
        instance = Path(instance_path) if instance_path and Path(instance_path).is_dir() else None

        summary: dict[str, object] = {}
        scan = self.scan_cache.get(instance_path) if instance_path else None
        if scan is not None:
            summary["检出模组数"] = len({f.modid for f in scan.english})
            summary["英文语言文件"] = len(scan.english)
            summary["已有中文（社区/官方）"] = len(scan.community)
            summary["摆渡自己生成的包"] = len(scan.ai)

        try:
            target = core.write_diagnostic_report(
                app_version=APP_VERSION,
                instance=instance,
                scan_summary=summary or None,
            )
        except OSError as exc:
            messagebox.showerror("一键诊断", f"写诊断文件失败：{exc}")
            return

        self.status_var.set(f"诊断包已生成：{target.name}")
        if messagebox.askyesno(
            "一键诊断",
            f"诊断包已生成：\n{target}\n\n"
            "里面没有你的 API Key（配置一栏只记录「设没设、是不是密文」）。\n"
            "遇到问题时把这个文件整份发出去就行 —— 粘贴或当附件都可以。\n\n"
            "要现在打开它所在的文件夹吗？",
        ):
            try:
                os.startfile(str(target.parent))
            except (OSError, AttributeError):
                messagebox.showinfo("一键诊断", f"文件在：\n{target}")

    def _show_quality(self) -> None:
        instance_path = self.path_var.get().strip()
        if not instance_path or instance_path not in self.scan_cache:
            messagebox.showinfo("质量检查", "请先扫描一个 Minecraft 实例。")
            return
        instance = Path(instance_path)
        settings = core.resolve_translate_settings(self._snapshot_config(), instance)
        pack = Path(settings["output_dir"]) / settings["pack_name"]
        scan = self.scan_cache[instance_path]
        window = tk.Toplevel(self)
        window.title("质量检查 · 摆渡包")
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        columns = ("modid", "key", "source", "target", "issue", "severity")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
        tree.tag_configure("error", foreground=RED)
        tree.tag_configure("warning", foreground="#9A5A22")
        for name, title, width in zip(columns, ("模组", "key", "原文", "译文", "问题", "等级"), (115, 190, 180, 180, 125, 60)):
            tree.heading(name, text=title)
            tree.column(name, width=width, minwidth=60)
        tree.pack(fill="both", expand=True)
        def refresh() -> None:
            tree.delete(*tree.get_children())
            for issue in core.quality_issues(scan, pack):
                tree.insert("", "end", values=issue, tags=("error" if issue[-1] == "错误" else "warning",))
        refresh()
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        def selected() -> dict[str, set[str]]:
            result: dict[str, set[str]] = {}
            for item in tree.selection():
                modid, key = tree.item(item, "values")[:2]
                result.setdefault(modid, set()).add(key)
            return result
        def delete() -> None:
            if selected() and messagebox.askyesno("删除摆渡译文", "只删除选中的摆渡包 key，下次翻译可重新生成。", parent=window):
                core.remove_pack_keys(pack, selected())
                refresh()
                self.start_scan()
        def lock() -> None:
            for item in tree.selection():
                modid, key, _, target = tree.item(item, "values")[:4]
                core.lock_translation(modid, key, target)
            refresh()
        def skip() -> None:
            keys = selected()
            if not keys:
                return
            config = self._snapshot_config()
            ignored = dict(config.get("skip_keys") or {})
            for modid, selected_keys in keys.items():
                ignored[modid] = sorted(set(ignored.get(modid, [])) | selected_keys)
            config["skip_keys"] = ignored
            self.config = config
            core.save_config(config)
            self.status_var.set(f"已将 {sum(map(len, keys.values()))} 个 key 加入不翻译列表。")
        def retranslate() -> None:
            keys = selected()
            if not keys:
                return
            if self.translating or self.scanning:
                messagebox.showinfo("质量检查", "请等待当前操作结束。", parent=window)
                return
            self.translating = True
            self._set_translating_ui(True)
            def work() -> None:
                try:
                    count, errors = core.retranslate_pack_keys(scan, pack, keys, settings)
                    self.result_queue.put(("qc_retranslated", (count, errors)))
                except Exception as exc:
                    self.result_queue.put(("error", ("translate", f"重翻失败：{core.redact_secrets(str(exc))}")))
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(buttons, text="删除选中", command=delete).pack(side="left")
        ttk.Button(buttons, text="重翻选中", command=retranslate).pack(side="left", padx=6)
        ttk.Button(buttons, text="锁定译文", command=lock).pack(side="left", padx=6)
        ttk.Button(buttons, text="此 key 不翻译", command=skip).pack(side="left", padx=6)
        self._center_over(window, 920, 470)

    def _refine_changed(self) -> None:
        if self._closing:
            return
        if self.scanning:
            self.after(250, self._refine_changed)
        elif not self.translating and self.path_var.get():
            self.start_scan()

    def _auto_export_changed(self) -> None:
        """「译完自动导出桥映射」开关：只影响翻译收尾，不必触发重扫。"""
        if self._closing:
            return
        self.config["auto_export_bridge"] = bool(self.auto_export_var.get())
        core.save_config(self.config)

    def _auto_update_changed(self) -> None:
        """「自动升级 LiteralBridge」开关：同上，只存配置。"""
        if self._closing:
            return
        self.config["auto_update_literalbridge"] = bool(self.auto_update_var.get())
        core.save_config(self.config)

    def _update_glossary(self) -> None:
        if self.updating_glossary or self.translating:
            return
        self.updating_glossary = True
        self.glossary_button.configure(state="disabled")
        self.status_var.set("正在联网更新术语表（可能需要十几秒）……")
        threading.Thread(target=self._glossary_worker, daemon=True).start()

    def _glossary_worker(self) -> None:
        try:
            glossary = core.fetch_glossary(core.DEFAULT_GLOSSARY_EN_URL, core.DEFAULT_GLOSSARY_ZH_URL)
            core.USER_GLOSSARY_FILE.write_text(json.dumps(glossary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.result_queue.put(("glossary_updated", len(glossary)))
        except Exception as exc:
            self.result_queue.put(("error", ("glossary", f"术语表更新失败：{core.redact_secrets(str(exc))}")))

    def _security_check(self) -> None:
        if self._closing:
            return
        warning = core.config_secret_risk()
        if warning:
            core.LOG.warning("secret-risk: %s", warning.replace("\n", " "))
            messagebox.showwarning("密钥安全提示", warning)



    def _show_help(self) -> None:
        window = tk.Toplevel(self)
        window.title("使用说明 - 摆渡计划")
        window.transient(self)
        body = ttk.Frame(window, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="使用说明", font=FONT_TITLE).pack(anchor="w")
        ttk.Button(body, text="关闭", command=window.destroy).pack(side="bottom", anchor="e", pady=(8, 0))
        reading = tk.Text(body, width=62, height=20, bg=CARD, fg=BODY_INK, wrap="word", font=FONT_BODY,
                          relief="flat", padx=12, pady=10, spacing3=4)
        scroll = ttk.Scrollbar(body, command=reading.yview)
        reading.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        reading.pack(fill="both", expand=True)
        reading.tag_configure("section", foreground=ACCENT, font=FONT_BOLD, spacing1=10)

        def section(title: str, lines: list[str]) -> None:
            reading.insert("end", title + "\n", "section")
            reading.insert("end", "\n".join(lines) + "\n")

        section("游戏汉化", [
            "直接把游戏文件夹、.exe 或镜像拖进窗口即可自动体检（0 费用）。",
            "检测通过后点击开始汉化；也可提取双语表手动校对后 0 Token 写回。",
            "完成后可在首页打开或清理产物历史记录（清理历史不会删游戏文件）。",
        ])
        section("模组汉化", [
            "1. 拖入或选择 Minecraft 实例并扫描，已有人工汉化的模组会自动让位。",
            "2. 双击行单翻、Ctrl/Shift 多选、右键可删除/重载/还原/排除模组，F5 重扫。",
            "3. 生成资源包进游戏启用即可；硬编码文本可勾「翻译硬编码文本」并导出桥映射。",
        ])
        section("状态符号", [
            "✓ 已完成　◈ 人工完整　◐ 部分翻译　○ 待翻译",
            "⚠ 含硬编码　⊗ AI 已卸载　⊘ 已还原英文　∅ 已排除　✗ 上次失败",
        ])
        section("省流与 Batch API", [
            "内置预过滤、去重、条目级缓存与前缀缓存；「设置」页可开关深度思考、切换日夜主题或使用国内/国际 5 折 Batch API。",
        ])
        section("密钥安全", [
            "API Key 仅保存在本机 ferry_config.json（Windows 上经 DPAPI 加密落盘），不上传到任何第三方。",
        ])
        reading.configure(state="disabled")
        self._center_over(window, 580, 500)

    def export_bridge_mapping(self) -> None:
        """按钮入口：导出 LiteralBridge 规则 + 摆渡桥外部映射表，并弹窗说明。"""
        if self.scanning:
            messagebox.showinfo("提示", "正在扫描，请稍候再导出。")
            return
        result = self._write_bridge_mapping(self.path_var.get().strip(), notify=True)
        if result is None:
            return
        if result.get("empty"):
            self.status_var.set("没有可导出的映射：先完成一次翻译（或有社区/人工汉化）再试。")
            return
        if result.get("error"):
            messagebox.showerror("导出桥接映射", result["error"])
            return
        self.status_var.set(
            f"桥接规则已导出：{result['groups']} 个模组组、{result['rules']} 条，详见弹窗。"
        )

    def _write_bridge_mapping(self, instance_value: str, *, notify: bool) -> dict | None:
        """把扫描 / 翻译结果写成桥能读的两份文件，返回结果字典。

        两个调用方：
        · 手动点「导出桥接映射」→ notify=True，弹窗说明写到了哪；
        · 翻译结束后自动导出 → notify=False，只更新状态栏，不打断流程。

        自动导出是「进游戏之前、全在摆渡里扫描 → 翻译 → 一步到位」的最后一环：
        译完即写进游戏目录，摆渡桥热重载立刻生效，不必再手点一次。
        """
        if not instance_value:
            return None
        scan = self.scan_cache.get(instance_value)
        if scan is None:
            if notify:
                messagebox.showinfo("提示", "请先扫描实例，再导出桥接映射。")
            return None
        try:
            settings = core.resolve_translate_settings(self._snapshot_config(), Path(instance_value))
            pack = Path(settings["output_dir"]) / settings["pack_name"]
            ai_by_modid = core.load_pack_translations(pack)
        except (OSError, ValueError):
            pack = None
            ai_by_modid = {}
        hardcoded = core.load_hardcoded_translations(pack) if pack else None
        groups = core.build_bridge_groups(scan.english, scan.community, ai_by_modid, hardcoded=hardcoded)
        if not groups:
            return {"empty": True}
        mapping = core.build_bridge_mapping(scan.english, scan.community, ai_by_modid, hardcoded=hardcoded)
        rules_doc = core.build_literalbridge_export(groups)
        bridge_config_dir = Path(instance_value) / "config" / "literalbridge"
        try:
            bridge_config_dir.mkdir(parents=True, exist_ok=True)
            # 根节点只能是组（LiteralBridge 按组解析，混入元数据键会让整个文件被拒载）
            (bridge_config_dir / "ferry.json").write_text(json.dumps(rules_doc, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            return {"error": f"写入规则文件失败：{exc}"}
        # 自研摆渡桥读的是**游戏目录**下的外部映射表（ferrybridge-mappings.json），
        # 不是 config/ferrybridge/translations.json —— 那是早期版本留下的位置，
        # 现在的桥根本不读那里，写进去等于没写（这正是「导出后依旧全是英文」的原因之一）。
        try:
            fb_target = core.write_ferrybridge_external(Path(instance_value), mapping)
        except OSError as exc:
            return {"error": f"写入摆渡桥映射失败：{exc}"}
        mods_dir = Path(instance_value) / "mods"
        installed = core.installed_literalbridge(mods_dir)
        has_bridge = installed is not None
        total_rules = sum(len(group.get("rules", [])) for group in rules_doc.values())
        # 没装 → 联网装一份；已装且开了自动升级 → 联网查一次有没有新版，有就换。
        # 两者都是网络操作，只能丢后台；结果走 result_queue 的 "literalbridge" 消息。
        bridge_note = ""
        if not has_bridge:
            bridge_note = self._start_literalbridge_fetch(instance_value, notify=notify, upgrade=False)
        elif self.auto_update_var.get():
            bridge_note = self._start_literalbridge_fetch(instance_value, notify=notify, upgrade=True)
        if notify:
            summary = (
                f"LiteralBridge 规则（{len(rules_doc)} 个模组组、{total_rules} 条）：\n"
                f"{bridge_config_dir / 'ferry.json'}\n"
                f"摆渡桥映射（{len(mapping)} 条）：\n"
                f"{fb_target}\n"
                + (f"（实例 mods 里已装 LiteralBridge {installed['version'] or '未知版本'}）\n" if has_bridge else "")
                + "\n进游戏生效方式：\n"
                "· LiteralBridge（聊天 / 实体名 / 动作栏）：检测到规则变化自动热重载；\n"
                "· 摆渡桥（物品 tooltip / 聊天）：读上面那份 ferrybridge-mappings.json，\n"
                "  每秒自动热重载，无需重启游戏；按 F9 可查看实时状态，并导出一份"
                "诊断包（游戏目录里的 ferrybridge-diagnose.txt，出问题时把它发出来）。\n"
                "并确认资源包列表里摆渡包处于启用状态。"
            )
            if bridge_note:
                summary += f"\n\n{bridge_note}"
            messagebox.showinfo("桥接映射已导出", summary)
        return {
            "target": str(fb_target),
            "ferry_json": str(bridge_config_dir / "ferry.json"),
            "groups": len(rules_doc),
            "rules": total_rules,
            "mapping": len(mapping),
            "has_bridge": has_bridge,
            "bridge_note": bridge_note,
        }

    def _start_literalbridge_fetch(self, instance_value: str, *, notify: bool, upgrade: bool = False) -> str:
        """取 / 升级 LiteralBridge，返回一句给用户看的说明。

        规则（2026-10-12 定）：**网络通就直接从 GitHub Release 下载**匹配的 jar 装进 mods/；
        只有「网络不通」或「没有支持该加载器 + MC 版本的本体」时，才回落到打开项目页。
        upgrade=True 表示实例里已经有桥了，这次只是来查更新 —— 失败不弹窗也不开浏览器。
        下载是网络操作，必须丢后台线程，否则会卡住界面。
        """
        if self.fetching_literalbridge:
            return "正在获取 LiteralBridge，请稍候……"
        self.fetching_literalbridge = True
        threading.Thread(
            target=self._literalbridge_worker,
            args=(instance_value, notify, upgrade),
            daemon=True,
        ).start()
        if upgrade:
            return "已开始检查 LiteralBridge 更新……"
        return "实例 mods 里没有 LiteralBridge，已开始从 GitHub 获取……"

    def _literalbridge_worker(self, instance_value: str, notify: bool, upgrade: bool) -> None:
        try:
            instance = Path(instance_value)
            loader = core.detect_mod_loader(instance)
            version = core.detect_minecraft_version(instance) or ""
            result = core.download_literalbridge(
                instance / "mods", loader=loader, mc_version=version, upgrade=upgrade
            )
            result.update({"notify": notify, "upgrade": upgrade, "loader": loader, "version": version})
        except Exception as exc:  # noqa: BLE001 —— 后台线程不能把异常抛回主循环
            result = {
                "ok": False,
                "reason": "error",
                "page": core.LITERALBRIDGE_PAGE,
                "detail": core.redact_secrets(str(exc)),
                "notify": notify,
                "upgrade": upgrade,
            }
        self.result_queue.put(("literalbridge", result))

    def _after_literalbridge(self, payload: dict) -> None:
        """取桥结果落地：装好了 / 升级了就报喜，没装成且本来也没有 → 回落到打开项目页。"""
        self.fetching_literalbridge = False
        notify = bool(payload.get("notify"))
        # upgrade=True：实例里本来就有桥，这次只是来查更新。
        # 这种情况失败只是「没升成」，不该弹窗、抢焦点或开浏览器。
        upgrade = bool(payload.get("upgrade"))
        page = str(payload.get("page") or core.LITERALBRIDGE_PAGE)
        action = str(payload.get("action") or "")
        if payload.get("ok"):
            version = payload.get("version")
            if action == "up_to_date":
                self.status_var.set(f"LiteralBridge 已是最新（{version}），无需更新。")
                return
            if action == "upgraded":
                was = payload.get("from_version") or "旧版"
                backup = payload.get("backup") or ""
                self.status_var.set(
                    f"LiteralBridge 已从 {was} 升级到 {version}，重启游戏后生效。"
                )
                if notify:
                    messagebox.showinfo(
                        "LiteralBridge 已升级",
                        f"LiteralBridge：{was} → {version}\n"
                        f"新版：{payload.get('path')}\n"
                        + (f"旧版已改名备份：{backup}\n（想回退就把文件名里的 .bak-… 去掉）\n" if backup else "")
                        + "\n模组本体需要【重启游戏】才会加载（映射表能热重载，模组不行）。",
                    )
                return
            self.status_var.set(
                f"已下载 LiteralBridge {version} 到实例 mods，重启游戏后生效。"
            )
            if notify:
                messagebox.showinfo(
                    "LiteralBridge 已装好",
                    f"LiteralBridge {payload.get('version')} 已下载到：\n{payload.get('path')}\n\n"
                    "它是模组本体，需要【重启游戏】才会加载（资源包能热重载，模组不行）。\n"
                    "重进游戏后，摆渡导出的规则文件才会被它读到。",
                )
            return
        reason = str(payload.get("reason") or "")
        if reason == "already_installed":
            return
        tried = payload.get("tried") or []
        if reason == "no_match":
            loader = payload.get("loader") or "未知加载器"
            version = payload.get("version") or "未知版本"
            head = (
                f"没有支持「{loader} + Minecraft {version}」的 LiteralBridge。\n"
                "（它是硬编码文本的显示层桥，官方 Release 目前只有 Forge / MC 1.20.1–1.20.x 版。）\n"
                f"已试过的构件：{'、'.join(tried) if tried else '无'}"
            )
        elif reason == "network":
            head = f"联网获取 LiteralBridge 失败：{payload.get('detail') or '网络不可达'}"
        elif reason == "no_release":
            head = "GitHub 上没有带 jar 附件的 LiteralBridge Release。"
        elif reason == "backup_failed":
            head = (
                "旧版 LiteralBridge 改名备份失败，为避免两份同 modId 同时加载崩游戏，这次没有升级。\n"
                "多半是文件被占用（游戏还开着？），关掉游戏重试即可。"
            )
        else:
            head = f"获取 LiteralBridge 时出错：{payload.get('detail') or '未知原因'}"
        if upgrade:
            # 本来就有桥，查更新失败只是「没升成」，安静说一声就行。
            self.status_var.set(f"LiteralBridge 更新检查失败：{head.splitlines()[0]}")
            return
        self.status_var.set("没能自动获取 LiteralBridge。")
        # 只在用户主动点「导出桥接映射」时才弹窗 / 开浏览器 ——
        # 译完自动导出那条路径是静默的，别突然弹窗或抢焦点。
        if notify:
            messagebox.showinfo("需要手动安装 LiteralBridge", f"{head}\n\n已为你打开项目页。")
            try:
                os.startfile(page)
            except OSError:
                pass

    def import_collected(self) -> None:
        """读游戏里采集的未命中文本 → 批量翻译 → 写摆渡桥外部映射表。

        这是「大批量多模组翻译」的入口：玩家正常玩一圈，摆渡桥把「出现过、
        但映射表里没有译文」的英文记进 ferrybridge-collected.txt；这里把它们
        一次性翻译出来，写回游戏目录，桥每秒比对 mtime 热重载 —— 不用重启游戏。
        """
        if self.scanning or self.translating:
            messagebox.showinfo("提示", "有任务正在进行，请稍候。")
            return
        instance_value = self.path_var.get().strip()
        if not instance_value:
            messagebox.showinfo("提示", "请先选择游戏实例。")
            return
        instance = Path(instance_value)
        collected_path = instance / core.COLLECTED_NAME
        buckets = core.parse_collected_file(collected_path)
        if not buckets:
            messagebox.showinfo(
                "没有采集数据",
                f"没找到可用的采集清单：\n{collected_path}\n\n"
                "先去游戏里正常玩一圈（悬停物品、看聊天栏），\n"
                "摆渡桥会把没翻到的英文记下来，再回来点这个按钮。",
            )
            return
        total = sum(len(items) for items in buckets.values())
        detail = "、".join(f"{name} {len(items)} 条" for name, items in sorted(buckets.items()))
        if not messagebox.askyesno(
            "导入游戏采集",
            f"采集到 {total} 条未翻译文本（{detail}）。\n\n"
            "会用当前翻译引擎把它们翻成中文，写进摆渡桥的外部映射表；\n"
            "游戏内每秒自动热重载，无需重启。继续？",
        ):
            return
        self.translating = True
        self.cancel_event.clear()
        self._set_translating_ui(True)
        self.status_var.set(f"正在翻译采集到的 {total} 条文本……")
        threading.Thread(target=self._collected_worker, args=(instance, buckets), daemon=True).start()

    def _collected_worker(self, instance: Path, buckets: dict) -> None:
        """后台线程：翻译采集文本并写出映射表，结果经队列回到 UI 线程。"""
        try:
            settings = core.resolve_translate_settings(self._snapshot_config(), instance)
            entries = core.collected_entries(buckets)
            translations, errors, _failed = core.translate_entries(
                entries,
                settings,
                cancel_event=self.cancel_event,
                progress=lambda done, total, _modid: self.result_queue.put(
                    ("status", f"翻译采集文本中…… {done}/{total}")
                ),
            )
            pairs = {
                english: chinese
                for english, chinese in (translations.get(core.COLLECTED_MODID) or {}).items()
                if chinese and str(chinese).strip()
            }
            if not pairs:
                self.result_queue.put(("collected", {
                    "error": "没有翻译出任何条目。请检查翻译引擎配置（API key / 网络）。",
                }))
                return
            target = core.write_ferrybridge_external(instance, pairs)
            self.result_queue.put(("collected", {
                "target": str(target),
                "count": len(pairs),
                "errors": len(errors),
            }))
        except Exception as exc:
            self.result_queue.put(("collected", {"error": core.redact_secrets(str(exc))}))

    def _after_collected(self, payload: dict) -> None:
        """UI 线程侧：收尾翻译采集的结果。"""
        self.translating = False
        self._set_translating_ui(False)
        error = payload.get("error")
        if error:
            self.status_var.set("翻译采集文本失败。")
            messagebox.showerror("导入游戏采集", error)
            return
        count = int(payload.get("count", 0))
        message = (
            f"已写出 {count} 条摆渡桥映射：\n{payload.get('target', '')}\n\n"
            "游戏内每秒自动热重载，无需重启；按 F9 可查看实时状态并导出诊断包"
            "（游戏目录里的 ferrybridge-diagnose.txt）。"
        )
        if payload.get("errors"):
            message += f"\n\n另有 {payload['errors']} 个批次失败，相关条目保留英文（下次重跑会自动重试）。"
        messagebox.showinfo("导入游戏采集", message)
        self.status_var.set(f"采集文本已翻译并写出 {count} 条，游戏内热重载生效。")

    def start_online_lookup(self) -> None:
        """选中若干模组，逐个查询源探测网上有没有现成汉化。"""
        if self.looking_up or self.scanning or self.translating:
            return
        modids = sorted({modid for mods in self._selected_targets().values() for modid in mods})
        if not modids:
            self.status_var.set("先在表格里选中要查询的模组，再点「查询在线汉化」。")
            return
        sites = [site for site in (self.config.get("lookup_sites") or []) if isinstance(site, str) and "{modid}" in site]
        self.looking_up = True
        self.lookup_button.configure(state="disabled")
        self.status_var.set(f"正在查询 {len(modids)} 个模组的在线汉化（{len(sites)} 个源）……")
        threading.Thread(target=self._lookup_worker, args=(sites, modids), daemon=True).start()

    def _lookup_worker(self, sites: list[str], modids: list[str]) -> None:
        try:
            cache_dir = core.app_dir() / "ferry_cache"

            def pack_progress(done: int, total: int) -> None:
                self.result_queue.put(("pack_progress", (done, total)))

            pack = core.ensure_community_pack(cache_dir, allow_fetch=True, progress=pack_progress)
            results = core.query_online_translations(
                sites, modids, pack_path=pack,
                progress=lambda done, total: self.result_queue.put(("status", f"在线汉化查询中…… {done}/{total}")),
            )
            self.result_queue.put(("lookup_done", results))
        except Exception as exc:
            self.result_queue.put(("error", ("lookup", f"在线汉化查询失败：{core.redact_secrets(str(exc))}")))

    def _show_lookup_results(self, results: list[dict[str, str]]) -> None:
        found = sum(1 for item in results if item["status"] == "found")
        miss = sum(1 for item in results if item["status"] == "miss")
        error = sum(1 for item in results if item["status"] == "error")
        window = tk.Toplevel(self)
        window.title("在线汉化查询结果 - 摆渡计划")
        window.transient(self)
        body = ttk.Frame(window, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="在线汉化查询结果", font=FONT_TITLE).pack(anchor="w")
        ttk.Label(
            body,
            text=f"共 {len(results)} 项：{found} 找到 · {miss} 未找到 · {error} 失败。选中行后可打开链接查看。",
            font=FONT_SMALL, foreground=MUTED,
        ).pack(anchor="w", pady=(2, 6))
        frame = ttk.Frame(body)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=("modid", "site", "status"), show="headings", height=14)
        for column, text, width, anchor in (("modid", "模组", 200, "w"), ("site", "查询源", 320, "w"), ("status", "结果", 90, "center")):
            tree.column(column, width=width, anchor=anchor)
            tree.heading(column, text=text)
        tree.tag_configure("found", foreground=GREEN)
        tree.tag_configure("miss", foreground=MUTED)
        tree.tag_configure("error", foreground=RED)
        status_text = {"found": "✓ 找到", "miss": "未找到", "error": "失败"}
        urls: dict[str, str] = {}
        for item in sorted(results, key=lambda r: (r["modid"], r["site"])):
            node = tree.insert("", "end", values=(item["modid"], item["site"], status_text.get(item["status"], item["status"])), tags=(item["status"],))
            urls[node] = item["url"]
        tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)

        def open_link() -> None:
            for node in tree.selection():
                url = urls.get(node)
                if url:
                    try:
                        os.startfile(url)
                    except OSError:
                        pass

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))
        make_action_button(btns, "打开链接", open_link, ACCENT).pack(side="right")
        ttk.Button(btns, text="编辑查询源…", command=self._show_lookup_sites_editor).pack(side="left")
        ttk.Button(btns, text="关闭", command=window.destroy).pack(side="right", padx=(0, 8))
        self._center_over(window, 660, 460)

    def _show_lookup_sites_editor(self) -> None:
        window = tk.Toplevel(self)
        window.title("查询汉化网站 - 摆渡计划")
        window.transient(self)
        body = ttk.Frame(window, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="每行一个查询源网址，用 {modid} 代替模组 ID。", font=FONT_BODY).pack(anchor="w")
        ttk.Label(
            body,
            text="查询时把 {modid} 替换成模组 ID 再访问；能打开且内容非空即视为「找到汉化」。CFPA 社区整合包是内置来源，无需在此填写。",
            font=FONT_SMALL, foreground=MUTED, wraplength=520, justify="left",
        ).pack(anchor="w", pady=(2, 6))
        text = tk.Text(body, height=8, wrap="none")
        text.pack(fill="both", expand=True)
        text.insert("1.0", "\n".join(self.config.get("lookup_sites") or []))

        def save() -> None:
            lines = [line.strip() for line in text.get("1.0", "end").splitlines() if line.strip()]
            bad = [line for line in lines if "{modid}" not in line]
            if bad:
                messagebox.showwarning("查询源", "这几行缺少 {modid}，请修正后再保存：\n" + "\n".join(bad[:3]))
                return
            self.config["lookup_sites"] = lines
            core.save_config(self.config)
            self.status_var.set(f"查询源已保存：{len(lines)} 个，下次查询生效。")
            window.destroy()

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))
        make_action_button(btns, "保存", save, ACCENT).pack(side="right")
        ttk.Button(btns, text="取消", command=window.destroy).pack(side="right", padx=(0, 8))
        self._center_over(window, 580, 340)

    def _maybe_auto_update_check(self) -> None:
        if self._closing or not self.config.get("update_auto_check", True):
            return
        if time.time() - float(self.config.get("update_last_check") or 0) < 7 * 86400:
            return
        self._start_update_check(manual=False)

    def _start_update_check(self, manual: bool) -> None:
        if self.checking_update:
            return
        self.checking_update = True
        self.config["update_last_check"] = time.time()
        try:
            core.save_config(self.config)
        except OSError:
            pass
        if manual:
            self.status_var.set("正在检查更新……")
        threading.Thread(
            target=lambda: self.result_queue.put(("update_check", (core.latest_github_release(APP_VERSION), manual))),
            daemon=True,
        ).start()

    def _show_update_offer(self, info: dict[str, str]) -> None:
        window = tk.Toplevel(self)
        window.title(f"发现新版本 v{info['version']} - 摆渡计划")
        window.transient(self)
        body = ttk.Frame(window, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"发现新版本 v{info['version']}（当前 v{APP_VERSION}）", font=FONT_TITLE).pack(anchor="w")
        notes = tk.Text(body, height=10, wrap="word")
        notes.pack(fill="both", expand=True, pady=(8, 0))
        notes.insert("1.0", info["notes"] or "（无更新说明）")
        notes.configure(state="disabled")
        auto = tk.BooleanVar(value=bool(self.config.get("update_auto_check", True)))
        ttk.Checkbutton(body, text="启动时自动检查更新（每周最多一次）", variable=auto).pack(anchor="w", pady=(8, 0))

        def save_toggle() -> None:
            self.config["update_auto_check"] = auto.get()
            core.save_config(self.config)

        def open_page() -> None:
            save_toggle()
            try:
                os.startfile(info["url"])
            except OSError:
                pass
            window.destroy()

        def auto_update() -> None:
            save_toggle()
            if not info["asset_url"]:
                open_page()
                return
            if not getattr(sys, "frozen", False):
                messagebox.showinfo("自动更新", "以脚本方式运行时不做自动替换，请到发布页下载新版本。")
                open_page()
                return
            window.destroy()
            self._download_update(info)

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        make_action_button(btns, "自动更新", auto_update, ACCENT).pack(side="right")
        ttk.Button(btns, text="打开发布页", command=open_page).pack(side="right", padx=(6, 8))
        ttk.Button(btns, text="以后再说", command=lambda: (save_toggle(), window.destroy())).pack(side="right")
        self._center_over(window, 560, 420)

    def _download_update(self, info: dict[str, str]) -> None:
        exe = Path(sys.executable)
        dest = exe.with_name("ProjectFerry.update.exe")
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        self.progress.configure(value=0, maximum=max(1, int(info.get("asset_size") or 1)))
        self.status_var.set(f"正在下载 v{info['version']}……")

        def worker() -> None:
            try:
                core.download_release_asset(
                    info["asset_url"], dest,
                    progress=lambda done, total: self.result_queue.put(("update_progress", (done, total))),
                )
                self.result_queue.put(("update_ready", (dest, info["version"])))
            except Exception as exc:
                self.result_queue.put(("update_failed", core.redact_secrets(str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_update_swap(self, payload: tuple[Path, str]) -> None:
        dest, version = payload
        self.progress.configure(value=0)
        if not getattr(sys, "frozen", False) or not dest.is_file() or dest.stat().st_size < 1048576:
            dest.unlink(missing_ok=True)
            self.status_var.set("下载的更新文件无效，请到发布页手动下载。")
            return
        exe = Path(sys.executable)
        try:
            old = exe.with_name(exe.stem + ".old.exe")
            if old.exists():
                old.unlink()
            os.replace(exe, old)
            try:
                os.replace(dest, exe)
            except OSError:
                os.replace(old, exe)
                raise
        except OSError as exc:
            self.status_var.set(f"自动更新失败：{exc}。可到发布页手动下载。")
            return
        self.status_var.set(f"已更新到 v{version}，重启程序后生效。")
        if messagebox.askyesno("更新完成", f"已更新到 v{version}。现在重启摆渡计划吗？"):
            try:
                os.startfile(str(exe))
            except OSError:
                pass
            self._on_close()

    def _show_limits(self) -> None:
        window = tk.Toplevel(self)
        window.title("汉化限制 - 摆渡计划")
        window.transient(self)
        body = ttk.Frame(window, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="疑似无法完全汉化的模组", font=FONT_TITLE).pack(anchor="w")
        ttk.Label(body, text="以下模组含硬编码文本（不在语言文件里，资源包无法覆盖），启发式检测仅供参考。", foreground=BODY_INK, wraplength=880).pack(anchor="w", pady=(4, 8))
        btn_row = ttk.Frame(body)
        btn_row.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(btn_row, text="关闭", command=window.destroy).pack(side="right")
        text = tk.Text(body, wrap="word", height=18)
        text.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        scrollbar.pack(side="right", fill="y")
        text.configure(yscrollcommand=scrollbar.set)
        if not self.hardcoded_results:
            text.insert("end", "本次扫描未检测到疑似硬编码文本的模组。\n")
        else:
            for name, count, samples in self.hardcoded_results:
                text.insert("end", f"{name}  ——  疑似 {count} 条\n")
                for sample in samples[:3]:
                    text.insert("end", f"    {sample}\n")
                text.insert("end", "\n")
        text.configure(state="disabled")
        self._center_over(window, 920, 430)

    def _show_about(self) -> None:
        window = tk.Toplevel(self)
        window.title(f"关于 {APP_NAME}")
        window.transient(self)
        window.minsize(360, 300)
        shell = ttk.Frame(window, padding=12)
        shell.pack(fill="both", expand=True)
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(0, weight=1)
        viewport = ttk.Frame(shell)
        viewport.grid(row=0, column=0, sticky="nsew")
        canvas = tk.Canvas(viewport, highlightthickness=0, bd=0, bg=BG)
        scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(canvas, padding=(8, 8, 16, 12))
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")

        header = ttk.Frame(body)
        header.pack(fill="x")
        icon = self._icon_image(40)
        if icon is not None:
            window._about_icon = icon  # 保持引用，避免被回收
            ttk.Label(header, image=icon).pack(side="left", padx=(0, 14))
        head_text = ttk.Frame(header)
        head_text.pack(side="left", fill="x", expand=True)
        ttk.Label(head_text, text=f"{APP_DISPLAY} / {APP_NAME}", font=FONT_TITLE).pack(anchor="w")
        ttk.Label(head_text, text=f"版本 {APP_VERSION}", foreground=MUTED).pack(anchor="w")
        ttk.Label(head_text, text=APP_SLOGAN, foreground=ACCENT).pack(anchor="w")
        ttk.Label(head_text, text="Where words fail, we ferry.", foreground=FAINT).pack(anchor="w")

        intro = ttk.Label(
            body,
            text="摆渡计划是一套游戏本地化工作台，帮助你检测游戏文本、提取可翻译内容、管理译文并生成可玩的汉化结果。它同时支持通用小众游戏和 Minecraft 模组，两类项目共用翻译引擎与本地项目记录。",
            foreground=BODY_INK,
            wraplength=700,
            justify="left",
        )
        intro.pack(anchor="w", fill="x", pady=(14, 8))

        content = ttk.Frame(body)
        content.pack(fill="both", expand=True)
        sections: list[tuple[ttk.Frame, int, int, list[ttk.Label]]] = []

        def section(parent: ttk.Frame, title: str, items: list[str], row: int, column: int) -> None:
            group = ttk.Frame(parent)
            group.grid(row=row, column=column, sticky="new", padx=(0 if column == 0 else 20, 0), pady=(8, 0))
            ttk.Label(group, text=title, font=FONT_BOLD).pack(anchor="w", pady=(0, 3))
            labels = []
            for text in items:
                label = ttk.Label(group, text="• " + text, foreground=BODY_INK, wraplength=350, justify="left")
                label.pack(anchor="w", fill="x", pady=(0, 2))
                labels.append(label)
            sections.append((group, row, column, labels))

        section(content, "游戏汉化", [
            "支持目录、PC-98 镜像、单 EXE 等入口，并自动识别已注册的游戏引擎。",
            "检测可读文本与写回能力，提供双语译文表校对和零 Token 写回。",
            "支持原地写入、生成汉化副本、撤销恢复，并记录最近的汉化产物。",
        ], 0, 0)
        section(content, "Minecraft 模组", [
            "扫描模组语言文件，人工与社区译文优先，AI 只补缺口。",
            "以低优先级资源包输出，支持硬编码桥接与术语表。",
        ], 0, 1)
        section(content, "翻译引擎", [
            "支持 OpenAI 兼容、Anthropic、Gemini、本地模型及国内/国际 5 折 Batch API。",
            "支持 API 预设、多模型并发池、思考链开关与条目级本地缓存。",
        ], 1, 0)
        section(content, "数据与安全", [
            "配置与译文缓存留在本地，API Key 经 DPAPI 加密落盘，诊断包自动脱敏。",
        ], 1, 1)
        section(content, "能力边界", [
            "图片文字需单独处理；部分硬编码或运行时拼接文本需桥接支持。",
        ], 2, 0)
        section(content, "数据来源与致谢", [
            "Losketch / LiteralBridge（Apache-2.0）",
            "Mojang 官方语言文件 / PandaDevOfficial 镜像",
            "CFPAOrg / Minecraft-Mod-Language-Package（社区汉化基线）",
            "Python 标准库 + tkinterdnd2（拖放支持）",
        ], 2, 1)
        ttk.Separator(body).pack(fill="x", pady=(12, 8))
        full_width_labels = [intro]
        for caption, color, gap in (
            (f"作者：{APP_AUTHOR}　·　许可证：{APP_LICENSE}", BODY_INK, 0),
            (f"项目主页：{PROJECT_URL}", ACCENT, 2),
            ("灵感来自《边狱巴士》零协汉化组。", FAINT, 8),
        ):
            label = ttk.Label(body, text=caption, foreground=color, justify="left")
            label.pack(anchor="w", fill="x", pady=(gap, 0))
            full_width_labels.append(label)

        previous_width = 0
        previous_columns = 0

        def resize_body(event: tk.Event) -> None:
            nonlocal previous_width, previous_columns
            width = event.width
            if width == previous_width:
                return
            previous_width = width
            canvas.itemconfigure(body_window, width=width)
            columns = 1 if width < 680 else 2
            gutter = 20 if columns == 2 else 0
            text_width = max(1, width - 24 - 4)  # body 左右内距和标签边缘
            if columns != previous_columns:
                for col in range(2):
                    active = col < columns
                    content.columnconfigure(col, weight=int(active), uniform="about" if active else "")
                for group, row, col, _ in sections:
                    group.grid_configure(row=row if columns == 2 else row * 2 + col,
                                         column=col if columns == 2 else 0,
                                         padx=(gutter // 2 * col, gutter // 2 * (1 - col)))
                previous_columns = columns
            column_wrap = max(1, (width - 24 - gutter) // columns - 4)
            for _, _, _, labels in sections:
                for label in labels:
                    label.configure(wraplength=column_wrap)
            for label in full_width_labels:
                label.configure(wraplength=text_width)
            head_width = max(1, text_width - (icon.width() + 16 if icon is not None else 0))
            for label in head_text.winfo_children():
                label.configure(wraplength=head_width)

        canvas.bind("<Configure>", resize_body)
        body.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox(body_window)))
        window.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-int(e.delta / 120), "units"))

        btn_row = ttk.Frame(shell)
        btn_row.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        ttk.Button(btn_row, text="检查更新", width=0, command=lambda: self._start_update_check(manual=True)).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="打开项目目录", width=0, command=self.open_project).pack(side="left")
        ttk.Button(btn_row, text="关闭", width=0, command=window.destroy).pack(side="right")
        self._center_over(window, 820, 730)

    def _show_settings(self) -> None:
        window = tk.Toplevel(self)
        window.title("设置 - 摆渡计划")
        window.transient(self)

        body = ttk.Frame(window, padding=16)
        body.pack(fill="both", expand=True)

        row = ttk.Frame(body)
        row.pack(fill="x", pady=(0, 12))
        ttk.Label(row, text="并发线程数：").pack(side="left")
        # P0-1：以主界面唯一的 concurrency_var 为准，而不是从 config 读旧值。
        concurrency_var = tk.IntVar(value=int(self.concurrency_var.get()))
        ttk.Spinbox(row, from_=1, to=16, textvariable=concurrency_var, width=5).pack(side="left")
        ttk.Label(row, text="（多个批次同时翻译，加速；MyMemory 免费额度有限，建议 2~4）", foreground=MUTED).pack(side="left", padx=(8, 0))

        lookup_row = ttk.Frame(body)
        lookup_row.pack(fill="x", pady=(10, 0))
        ttk.Label(lookup_row, text="在线汉化查询源：").pack(side="left")
        ttk.Button(lookup_row, text="编辑查询源…", command=self._show_lookup_sites_editor).pack(side="left", padx=(6, 0))

        ttk.Label(body, text="多模型并发池（可添加你自建的多个 API，并行翻译不同批次）", font=FONT_BOLD).pack(anchor="w", pady=(0, 6))
        ttk.Label(body, text="每行一个模型：引擎 / 模型 / 权重 / 地址。权重越大承担越多批次，用于按字符量平摊各 API 的资费。", foreground=MUTED, wraplength=610).pack(anchor="w", pady=(0, 6))
        pool_frame = ttk.Frame(body)
        pool_frame.pack(fill="both", expand=True)
        listbox = tk.Listbox(pool_frame, height=8)
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(pool_frame, orient="vertical", command=listbox.yview)
        scrollbar.pack(side="right", fill="y")
        listbox.configure(yscrollcommand=scrollbar.set)

        pool = list(self.config.get("model_pool", []) or [])

        def refresh_pool() -> None:
            listbox.delete(0, "end")
            for slot in pool:
                label = f"{slot.get('engine', '?')} | {slot.get('model', '') or '默认'} | 权重{slot.get('weight', 1)} | {slot.get('base_url', '') or '默认地址'}"
                listbox.insert("end", label)

        refresh_pool()

        btn_row = ttk.Frame(body)
        btn_row.pack(fill="x", pady=(8, 0))
        ttk.Button(btn_row, text="添加模型", command=lambda: self._add_pool_slot(window, pool, refresh_pool)).pack(side="left")
        ttk.Button(btn_row, text="从预设添加", command=lambda: self._add_pool_from_profile(window, pool, refresh_pool)).pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="编辑选中", command=lambda: self._edit_pool_slot(listbox, pool, refresh_pool)).pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="删除选中", command=lambda: self._remove_pool_slot(listbox, pool, refresh_pool)).pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="清空", command=lambda: self._clear_pool(listbox, pool, refresh_pool)).pack(side="left", padx=(6, 0))

        def save() -> None:
            self.config["concurrency"] = int(concurrency_var.get())
            self.config["model_pool"] = pool
            core.save_config(self.config)
            self.concurrency_var.set(int(concurrency_var.get()))
            self._update_settings_summary()
            window.destroy()
            self.engine_feedback.set("设置已保存。")

        ttk.Button(body, text="保存", command=save).pack(anchor="e", pady=(12, 0))
        self._center_over(window, 660, 420)

    def _show_batch_api_dialog(self) -> tk.Toplevel:
        """国内/国际 Batch API（5 折异步批量翻译）可视化管理窗口。"""
        window = tk.Toplevel(self)
        window.title("Batch API（5折异步批量翻译） - 摆渡计划")
        window.transient(self)

        body = ttk.Frame(window, padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(
            body,
            text="支持阿里云百炼 / 智谱 BigModel / 硅基流动 / 百度千帆等国内 OpenAI 兼容平台，以及 OpenAI / Anthropic / Gemini 官方 Batch API。",
            foreground=MUTED,
            wraplength=760,
        ).pack(anchor="w", pady=(0, 8))

        form = ttk.LabelFrame(body, text="Batch 平台与接口配置", padding=10)
        form.pack(fill="x", pady=(0, 10))
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        snap = self._snapshot_config()
        current_engine = snap.get("engine") if snap.get("engine") in core.BATCH_PROVIDERS else "openai"
        resolved_cur = core._resolve_engine_fields(snap, current_engine, {})

        preset_labels = ["沿用当前引擎配置"] + [item["label"] for item in core.BATCH_PRESETS.values()]
        label_to_preset_key = {item["label"]: k for k, item in core.BATCH_PRESETS.items()}

        preset_var = tk.StringVar(value="沿用当前引擎配置")
        provider_var = tk.StringVar(value=current_engine)
        url_var = tk.StringVar(value=resolved_cur.get("base_url") or "")
        model_var = tk.StringVar(value=resolved_cur.get("model") or "")
        endpoint_var = tk.StringVar(value=core.resolve_batch_endpoint(current_engine, url_var.get()))
        key_var = tk.StringVar(value=resolved_cur.get("api_key") or "")
        note_var = tk.StringVar(value="先点「生成预览 JSONL（0费用）」可检查待提交批次，确认后再提交云端。")

        ttk.Label(form, text="平台预设：").grid(row=0, column=0, sticky="w", pady=3)
        preset_combo = ttk.Combobox(form, textvariable=preset_var, state="readonly", values=preset_labels, width=30)
        preset_combo.grid(row=0, column=1, sticky="ew", padx=(4, 12), pady=3)

        ttk.Label(form, text="协议类型：").grid(row=0, column=2, sticky="w", pady=3)
        provider_combo = ttk.Combobox(form, textvariable=provider_var, state="readonly", values=["openai", "anthropic", "gemini"], width=14)
        provider_combo.grid(row=0, column=3, sticky="w", padx=(4, 0), pady=3)

        ttk.Label(form, text="接口地址：").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(form, textvariable=url_var).grid(row=1, column=1, sticky="ew", padx=(4, 12), pady=3)

        ttk.Label(form, text="模型名：").grid(row=1, column=2, sticky="w", pady=3)
        ttk.Entry(form, textvariable=model_var).grid(row=1, column=3, sticky="ew", padx=(4, 0), pady=3)

        ttk.Label(form, text="API Key：").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(form, textvariable=key_var, show="•").grid(row=2, column=1, sticky="ew", padx=(4, 12), pady=3)

        ttk.Label(form, text="Batch Endpoint：").grid(row=2, column=2, sticky="w", pady=3)
        ttk.Entry(form, textvariable=endpoint_var).grid(row=2, column=3, sticky="ew", padx=(4, 0), pady=3)

        def on_preset_changed(_evt=None) -> None:
            chosen = preset_var.get()
            pkey = label_to_preset_key.get(chosen)
            if pkey:
                p = core.BATCH_PRESETS[pkey]
                provider_var.set(p["provider"])
                url_var.set(p["base_url"])
                model_var.set(p["model"])
                endpoint_var.set(p["endpoint"])
                note_var.set(p["note"])
            else:
                provider_var.set(current_engine)
                url_var.set(resolved_cur.get("base_url") or "")
                model_var.set(resolved_cur.get("model") or "")
                endpoint_var.set(core.resolve_batch_endpoint(current_engine, url_var.get()))
                note_var.set("当前使用「设置」页中已填好的翻译引擎配置。")

        preset_combo.bind("<<ComboboxSelected>>", on_preset_changed)

        def apply_preset_to_main() -> None:
            prov = provider_var.get().strip() or "openai"
            if prov in CUSTOM_ENGINES:
                self.category_var.set(CUSTOM_CATEGORY_LABEL)
                self.provider_var.set(CUSTOM_ENGINES[prov])
                self._provider_selected()
            if url_var.get().strip():
                self.url_var.set(url_var.get().strip())
            if model_var.get().strip():
                self.model_var.set(model_var.get().strip())
            if key_var.get().strip():
                self.api_key_var.set(key_var.get().strip())
            self._update_settings_summary()
            note_var.set("已填入「设置」页翻译引擎区域（记得在「设置」页点「保存设置」持久化）。")

        ttk.Label(form, textvariable=note_var, foreground=ACCENT, font=FONT_SMALL, wraplength=620).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )
        ttk.Button(form, text="套用到主设置", command=apply_preset_to_main).grid(row=3, column=3, sticky="e", pady=(6, 0))

        # ── 零费用生成预览 JSONL ──
        gen_row = ttk.Frame(body)
        gen_row.pack(fill="x", pady=(0, 8))
        status_var = tk.StringVar(value="就绪：可先生成 0 费用预览 JSONL，或选择下方任务刷新进度 / 取回缓存。")

        def build_batch_settings(root_path: Path, is_game: bool = False) -> dict:
            cfg = self._snapshot_config()
            prov = provider_var.get().strip() or "openai"
            overrides = {
                "engine": prov,
                "base_url": url_var.get().strip(),
                "api_key": key_var.get().strip(),
                "model": model_var.get().strip(),
                "pack_format": 15,
                "disable_thinking": bool(self.disable_thinking_var.get()),
            }
            settings = core.localize_settings(cfg, root_path, overrides) if is_game else core.resolve_translate_settings(cfg, root_path, overrides)
            settings["batch_endpoint"] = core.resolve_batch_endpoint(prov, settings.get("base_url", ""), endpoint_var.get().strip())
            return settings

        # ── 本地任务列表 ──
        list_box = ttk.Frame(body)
        list_box.pack(fill="both", expand=True)
        cols = ("job_id", "provider", "status", "counts", "saved")
        tree = ttk.Treeview(list_box, columns=cols, show="headings", height=8, selectmode="browse")
        for col, heading, width in (
            ("job_id", "任务 ID", 190),
            ("provider", "平台 / 模型", 170),
            ("status", "状态", 90),
            ("counts", "条目 / 批次", 110),
            ("saved", "省流 / Token", 210),
        ):
            tree.heading(col, text=heading, anchor="w")
            tree.column(col, width=width, minwidth=60, stretch=col == "saved")
        scroll = ttk.Scrollbar(list_box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        cache_dir = self.config.get("cache_dir") or None

        def refresh_jobs() -> None:
            tree.delete(*tree.get_children())
            for job in core.list_batch_jobs(cache_dir):
                saved_parts = []
                if job.prefiltered:
                    saved_parts.append(f"预过滤{job.prefiltered}")
                if job.tm_hits:
                    saved_parts.append(f"缓存{job.tm_hits}")
                if job.deduped:
                    saved_parts.append(f"去重{job.deduped}")
                if job.input_tokens or job.output_tokens:
                    saved_parts.append(f"入{job.input_tokens}/出{job.output_tokens}")
                tree.insert(
                    "",
                    "end",
                    iid=job.job_id,
                    values=(
                        job.job_id,
                        f"{job.provider}:{job.model}",
                        job.status,
                        f"{job.entry_count} 条 / {job.request_count} 批",
                        " · ".join(saved_parts) or "—",
                    ),
                )

        def run_async(label: str, fn) -> None:
            status_var.set(f"正在{label}……")

            def worker() -> None:
                try:
                    msg = fn()
                    if window.winfo_exists():
                        window.after(0, lambda: (refresh_jobs(), status_var.set(msg)))
                except Exception as exc:
                    core.LOG.exception("batch_api: %s failed", label)
                    explained = core.explain_error(exc, stage=label)
                    first_line = explained.splitlines()[0]
                    if window.winfo_exists():
                        window.after(0, lambda: (status_var.set(first_line), messagebox.showerror("Batch API", explained, parent=window)))

            threading.Thread(target=worker, daemon=True).start()

        def prepare_mc() -> None:
            raw_path = self.path_var.get().strip()
            if not raw_path or not Path(raw_path).is_dir():
                status_var.set("请先在「模组汉化」页选好 Minecraft 实例目录。")
                return
            instance = Path(raw_path)

            def task() -> str:
                cfg = self._snapshot_config()
                settings = build_batch_settings(instance, is_game=False)
                scan = core.scan_inputs(instance / "mods", instance / "resourcepacks", instance)
                baseline = core.fetch_community_baseline(scan, cfg, Path(settings["cache_dir"]))
                baseline_files = [core.LangFile(m, "zh_cn", "community-baseline", d) for m, d in baseline.items()]
                entries = core.entries_with_baseline(
                    scan, baseline, set(self.whitelist), None, settings["fill_community"], settings["refine_community"]
                )
                entries.extend(core.missing_patchouli_entries(scan))
                entries = core.skip_ignored_entries(entries, cfg)
                if not entries:
                    return "当前实例没有待翻译条目，无需生成 Batch 任务。"
                corpus = core.community_corpus(scan.english, scan.community + baseline_files, scan.reference_only) if settings["refine_community"] else None
                job = core.prepare_batch_job(entries, settings, corpus=corpus, meta={"instance": str(instance)})
                return f"已生成预览 JSONL（0费用）：{job.summary()}"

            run_async("扫描实例并生成 Batch 预览", task)

        def prepare_game() -> None:
            chosen = filedialog.askdirectory(parent=window, title="选择要生成 Batch 预览的游戏目录")
            if not chosen:
                return
            root_path = Path(chosen)

            def task() -> str:
                adapter = core.engine_adapters.detect(root_path)
                if adapter is None:
                    raise ValueError("未能识别该目录的游戏引擎，无法抽取文本。")
                entries = adapter.collect(root_path)
                if not entries:
                    return "未抽取到待翻译文本。"
                settings = build_batch_settings(root_path, is_game=True)
                from ferrygame.units import detect_source_lang
                settings["source_lang"] = detect_source_lang([e.english for e in entries[:200]])
                job = core.prepare_batch_job(entries, settings, meta={"game": str(root_path), "adapter": adapter.name})
                return f"已生成游戏 Batch 预览（0费用）：{job.summary()}"

            run_async("抽取游戏文本并生成 Batch 预览", task)

        def selected_job_id() -> str | None:
            sel = tree.selection()
            if not sel:
                status_var.set("请先在下方列表选中一条 Batch 任务。")
                return None
            return str(sel[0])

        def do_submit() -> None:
            jid = selected_job_id()
            if not jid:
                return
            settings = build_batch_settings(Path("."), is_game=False)
            run_async("提交 Batch 任务到云端", lambda: "已提交：" + core.submit_batch_job(jid, settings).summary())

        def do_poll() -> None:
            jid = selected_job_id()
            if not jid:
                return
            settings = build_batch_settings(Path("."), is_game=False)
            run_async("查询云端 Batch 进度", lambda: core.poll_batch_job(jid, settings).summary())

        def do_collect() -> None:
            jid = selected_job_id()
            if not jid:
                return
            settings = build_batch_settings(Path("."), is_game=False)

            def task() -> str:
                tr, errs, failed, job = core.collect_batch_results(jid, settings)
                ok_n = sum(len(v) for v in tr.values())
                fail_n = sum(len(v) for v in failed.values())
                extra = f"（{len(errs)} 批出错）" if errs else ""
                return f"取回完成：成功 {ok_n} 条（已写入条目缓存，点正常翻译可 0 Token 秒写回），失败 {fail_n} 条{extra}"

            run_async("取回 Batch 译文并写入缓存", task)

        def do_cancel() -> None:
            jid = selected_job_id()
            if not jid:
                return
            settings = build_batch_settings(Path("."), is_game=False)
            run_async("取消云端 Batch 任务", lambda: core.cancel_batch_job(jid, settings).summary())

        def open_jobs_dir() -> None:
            folder = core.batch_jobs_dir(cache_dir)
            try:
                os.startfile(str(folder))
            except OSError:
                status_var.set(f"Batch 任务目录：{folder}")

        ttk.Button(gen_row, text="从当前 MC 实例生成预览 JSONL（0费用）", command=prepare_mc).pack(side="left")
        ttk.Button(gen_row, text="从游戏目录生成预览 JSONL（0费用）", command=prepare_game).pack(side="left", padx=(6, 0))
        ttk.Button(gen_row, text="打开 JSONL 目录", command=open_jobs_dir).pack(side="right")

        op_row = ttk.Frame(body)
        op_row.pack(fill="x", pady=(8, 4))
        ttk.Button(op_row, text="提交到云端", command=do_submit).pack(side="left")
        ttk.Button(op_row, text="刷新进度", command=do_poll).pack(side="left", padx=(6, 0))
        ttk.Button(op_row, text="取回并写入缓存", command=do_collect).pack(side="left", padx=(6, 0))
        ttk.Button(op_row, text="取消任务", command=do_cancel).pack(side="left", padx=(6, 0))
        ttk.Button(op_row, text="刷新列表", command=refresh_jobs).pack(side="left", padx=(6, 0))
        ttk.Button(op_row, text="关闭", command=window.destroy).pack(side="right")

        ttk.Label(body, textvariable=status_var, foreground=ACCENT, wraplength=760).pack(anchor="w", pady=(4, 0))

        refresh_jobs()
        self._center_over(window, 820, 560)
        window._batch_controls = {
            "preset_var": preset_var,
            "provider_var": provider_var,
            "url_var": url_var,
            "model_var": model_var,
            "endpoint_var": endpoint_var,
            "on_preset_changed": on_preset_changed,
        }
        return window

    def _engine_label(self, engine: str) -> str:
        if engine in FREE_ENGINES:
            return FREE_ENGINES[engine]
        return CUSTOM_ENGINES.get(engine, "OpenAI 兼容")

    def _pool_slot_dialog(self, parent: tk.Toplevel, initial: dict | None, on_ok) -> None:
        initial = initial or {}
        win = tk.Toplevel(parent)
        win.title("编辑模型" if initial else "添加模型")
        win.transient(parent)
        win.grab_set()
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="引擎 / 接口：").grid(row=0, column=0, sticky="w", pady=4)
        engine_var = tk.StringVar(value=self._engine_label(str(initial.get("engine", "openai"))))
        ttk.Combobox(frame, textvariable=engine_var, state="readonly", values=list(FREE_ENGINES.values()) + list(CUSTOM_ENGINES.values())).grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(frame, text="API Key：").grid(row=1, column=0, sticky="w", pady=4)
        key_var = tk.StringVar(value=str(initial.get("api_key", "")))
        ttk.Entry(frame, textvariable=key_var).grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(frame, text="Base URL：").grid(row=2, column=0, sticky="w", pady=4)
        url_var = tk.StringVar(value=str(initial.get("base_url", "")))
        ttk.Entry(frame, textvariable=url_var).grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(frame, text="模型：").grid(row=3, column=0, sticky="w", pady=4)
        model_var = tk.StringVar(value=str(initial.get("model", "")))
        ttk.Entry(frame, textvariable=model_var).grid(row=3, column=1, sticky="ew", pady=4)

        ttk.Label(frame, text="权重：").grid(row=4, column=0, sticky="w", pady=4)
        weight_var = tk.DoubleVar(value=float(initial.get("weight", 1) or 1))
        ttk.Spinbox(frame, from_=0.1, to=100, increment=0.1, textvariable=weight_var, width=8).grid(row=4, column=1, sticky="w", pady=4)
        ttk.Label(frame, text="权重越大承担越多批次（如便宜 / 免费模型给 2~3），按字符量平摊资费。", foreground=MUTED, wraplength=470).grid(row=5, column=0, columnspan=2, sticky="w", pady=(0, 4))

        def ok() -> None:
            label = engine_var.get()
            engine = FREE_LABEL_TO_ENGINE.get(label) or CUSTOM_LABEL_TO_ENGINE.get(label, "openai")
            slot: dict = {"engine": engine}
            for field, var in (("api_key", key_var), ("base_url", url_var), ("model", model_var)):
                value = var.get().strip()
                if value:
                    slot[field] = value
            try:
                weight = float(weight_var.get())
            except (tk.TclError, ValueError):
                weight = 1.0
            slot["weight"] = weight if weight > 0 else 1.0
            on_ok(slot)
            win.destroy()

        ttk.Button(frame, text="确定", command=ok).grid(row=6, column=1, sticky="e", pady=(10, 0))
        self._center_over(win, 540, 320)

    def _add_pool_slot(self, parent: tk.Toplevel, pool: list, refresh) -> None:
        def apply_slot(slot: dict) -> None:
            pool.append(slot)
            refresh()
        self._pool_slot_dialog(parent, None, apply_slot)

    def _edit_pool_slot(self, listbox: tk.Listbox, pool: list, refresh) -> None:
        selection = listbox.curselection()
        if not selection:
            messagebox.showinfo("提示", "请先选中要编辑的模型。")
            return
        index = selection[0]

        def apply_slot(slot: dict) -> None:
            pool[index] = slot
            refresh()
        self._pool_slot_dialog(self, dict(pool[index]), apply_slot)

    def _add_pool_from_profile(self, parent: tk.Toplevel, pool: list, refresh) -> None:
        profiles = self.config.get("custom_profiles", {}) or {}
        if not profiles:
            messagebox.showinfo("提示", "还没有保存的自定义接口预设。\n请先在主界面填好 API Key / Base URL / 模型，点“保存预设”。")
            return
        win = tk.Toplevel(parent)
        win.title("从预设添加模型")
        win.transient(parent)
        win.grab_set()
        ttk.Label(win, text="选择要加入并发池的 API 预设（可按住 Ctrl / Shift 多选）：", wraplength=410).pack(anchor="w", padx=14, pady=(14, 8))
        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=14)
        listbox = tk.Listbox(frame, height=10, selectmode="extended")
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=listbox.yview)
        scrollbar.pack(side="right", fill="y")
        listbox.configure(yscrollcommand=scrollbar.set)
        names = list(profiles.keys())
        for name in names:
            listbox.insert("end", name)

        def add_selected() -> None:
            added = 0
            for index in listbox.curselection():
                profile = profiles.get(names[index])
                if not isinstance(profile, dict):
                    continue
                slot: dict = {"engine": profile.get("provider", "openai"), "weight": 1.0}
                for field in ("api_key", "base_url", "model"):
                    if profile.get(field):
                        slot[field] = profile[field]
                pool.append(slot)
                added += 1
            refresh()
            win.destroy()
            self.engine_feedback.set(f"已从预设添加 {added} 个模型到并发池，记得点“保存”。")

        ttk.Button(win, text="加入并发池", command=add_selected).pack(anchor="e", padx=14, pady=14)
        self._center_over(win, 460, 360)

    def _remove_pool_slot(self, listbox: tk.Listbox, pool: list, refresh) -> None:
        selection = listbox.curselection()
        if selection:
            index = selection[0]
            del pool[index]
            refresh()

    def _clear_pool(self, listbox: tk.Listbox, pool: list, refresh) -> None:
        pool.clear()
        refresh()

    def manage_whitelist(self) -> None:
        window = tk.Toplevel(self)
        window.title("用户白名单 - 摆渡计划")
        window.transient(self)
        window.grab_set()
        ttk.Label(window, text="加入白名单的 modid 会跳过默认过滤，参与扫描。", wraplength=390).pack(anchor="w", padx=14, pady=(14, 8))
        frame = ttk.Frame(window)
        frame.pack(fill="both", expand=True, padx=14)
        listbox = tk.Listbox(frame, height=10)
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=listbox.yview)
        scrollbar.pack(side="right", fill="y")
        listbox.configure(yscrollcommand=scrollbar.set)
        for value in sorted(self.whitelist):
            listbox.insert("end", value)
        add_frame = ttk.Frame(window)
        add_frame.pack(fill="x", padx=14, pady=8)
        entry = ttk.Entry(add_frame)
        entry.pack(side="left", fill="x", expand=True)

        def add_item() -> None:
            value = entry.get().strip().lower().replace("-", "_")
            if value and value not in self.whitelist:
                self.whitelist.add(value)
                listbox.insert("end", value)
                entry.delete(0, "end")

        def remove_item() -> None:
            selection = listbox.curselection()
            if selection:
                value = listbox.get(selection[0])
                self.whitelist.discard(value)
                listbox.delete(selection[0])

        ttk.Button(add_frame, text="添加", command=add_item).pack(side="left", padx=(6, 0))
        ttk.Button(window, text="删除选中项", command=remove_item).pack(anchor="w", padx=14)

        def save_and_close() -> None:
            self._save_whitelist()
            window.destroy()
            self.status_var.set(f"白名单已保存：{len(self.whitelist)} 个 modid；请重新扫描。")
            self.start_scan()

        ttk.Button(window, text="保存并重新扫描", command=save_and_close).pack(anchor="e", padx=14, pady=14)
        self._center_over(window, 430, 320)

    def _set_instance_options(self, paths: list[Path], select: int | None = None) -> None:
        self.instance_paths = list(paths)
        names = [path.name for path in self.instance_paths]
        self.instance_combo.configure(values=[name if names.count(name) == 1 else f"{name}（{path.parent}）" for name, path in zip(names, self.instance_paths)])
        if self.instance_paths and select is not None:
            select = max(0, min(select, len(self.instance_paths) - 1))
            self.instance_combo.current(select)
        elif not self.instance_paths:
            self.instance_combo.set("没有找到实例，点「浏览…」手动选择")

    def refresh_instances(self) -> None:
        if self._closing or self.scanning or self.translating or self.managing_pack:
            return
        # 记住当前选的实例：刷新后优先回到它，而不是永远跳回第一个
        self._previous_instance = self.path_var.get().strip() or str(self.config.get("last_instance") or "")
        self.status_var.set("正在探测本机 Minecraft 实例……")
        self._hardcoded_cache.clear()
        self._nbt_candidates.clear()
        self._set_instance_options([])
        self.instance_combo.set("正在探测实例……")
        threading.Thread(target=self._discover_worker, daemon=True).start()

    def _discover_worker(self) -> None:
        try:
            self.result_queue.put(("instances", core.find_instance_dirs()))
        except Exception as exc:
            self.result_queue.put(("error", ("discovery", f"实例探测失败：{core.redact_secrets(str(exc))}")))

    def _instance_selected(self, _event: object = None) -> None:
        index = self.instance_combo.current()
        if 0 <= index < len(self.instance_paths):
            self.path_var.set(str(self.instance_paths[index]))
            self.config["last_instance"] = str(self.instance_paths[index])
            core.save_config(self.config)
            self.start_scan()

    def choose_path(self) -> None:
        path = filedialog.askdirectory(title="选择 Minecraft 实例目录")
        if path:
            selected = Path(path)
            self.path_var.set(str(selected))
            self.config["last_instance"] = str(selected)
            core.save_config(self.config)
            if selected in self.instance_paths:
                index = self.instance_paths.index(selected)
                self._set_instance_options(self.instance_paths, select=index)
            else:
                self.instance_paths.append(selected)
                self._set_instance_options(self.instance_paths, select=len(self.instance_paths) - 1)
            self.start_scan()

    def open_project(self) -> None:
        os.startfile(str(Path(__file__).parent))

    def start_scan(self) -> None:
        if self.scanning or self.managing_pack or self.translating:
            return
        # 没选实例时不能把所有自动发现的实例全扫一遍（大整合包会卡很久），改为刷新并只扫第一个。
        if not self.path_var.get().strip():
            self.refresh_instances()
            return
        self._hide_coverage_tip()
        self.scanning = True
        self.status_var.set("正在扫描实例中的模组语言文件……")
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.row_targets.clear()
        self.row_modids.clear()
        self.uninstalled_by_row.clear()
        self.coverage_by_row.clear()
        self.status_tip_by_row.clear()
        self.scan_cache.clear()
        self.selection_var.set("")
        # 清掉旧列表，让表格空状态显示「正在扫描」，而不是残留上一轮结果。
        self.all_rows = []
        self._render_rows()
        # 路径在主线程读好再传进工作线程，避免工作线程访问 Tk 变量。
        threading.Thread(target=self._scan_worker, args=(self.fill_var.get(), self.refine_var.get(), self.path_var.get().strip(), self.hardcoded_var.get()), daemon=True).start()

    def _scan_worker(self, fill_community: bool, refine_community: bool, manual: str, translate_hardcoded: bool = True) -> None:
        try:
            # 只扫当前实例；未指定时也只取自动发现的第一个，绝不整盘全扫。
            if manual:
                instances = [Path(manual)]
            else:
                instances = core.find_instance_dirs()[:1]
            if not instances:
                self.result_queue.put(("empty", "没有自动找到 Minecraft 实例。你可以点击“浏览”手动选择实例目录。"))
                return
            rows: list[dict] = []
            all_status: list[core.ModTranslationStatus] = []
            translatable_targets: dict[str, set[str]] = {}
            failed_modids: set[str] = set()
            hardcoded_findings: list[tuple[str, int, list[str]]] = []
            pending_detection: list[tuple[str, Path]] = []
            progress_failed = core.load_progress()
            whitelist = set(self.whitelist)
            scan_cache: dict[str, core.ScanResult] = {}
            uninstalled: dict[str, set[str]] = {}
            yield_reports: list[tuple[str, core.YieldReport]] = []
            for instance in instances:
                mods = instance / "mods"
                packs = instance / "resourcepacks"
                scan = core.scan_inputs(mods, packs if packs.is_dir() else None, instance)
                output_dir = Path(self.config.get("output_dir") or packs)
                pack_name = self.config.get("pack_name") or core.DEFAULT_CONFIG["pack_name"]
                pack = output_dir / pack_name
                version = core.detect_minecraft_version(instance)
                try:
                    legacy = version is not None and core.pack_format_for_version(version) <= 3
                except ValueError:
                    legacy = False
                if legacy and core.migrate_legacy_pack(pack, core.pack_format_for_version(version)):
                    scan = core.scan_inputs(mods, packs if packs.is_dir() else None, instance)
                core_report = core.yield_to_community(scan, pack)
                if core_report:
                    yield_reports.append((str(instance), core_report))
                uninstalled[str(instance)] = set(core.load_uninstalled_ai(output_dir / pack_name))
                scan_cache[str(instance)] = scan
                provenance = core.load_pack_sources(output_dir / pack_name)
                community_sources = {modid: {key for key, source in data.items() if source == "community"} for modid, data in provenance.items()}
                statuses = core.build_mod_status(scan.english, scan.community, scan.ai, whitelist, scan.reverted, community_sources)
                # 硬编码检测最慢（大整合包可能十几秒）：结果按实例缓存；没缓存时先出列表、后台再补。
                detected = self._hardcoded_cache.get(str(instance))
                if detected is None:
                    pending_detection.append((str(instance), mods))
                    detected = []
                hardcoded_findings.extend(detected)
                hardcoded_namespaces = core.hardcoded_modids(scan, detected)
                # 硬编码翻完没有？比对工具自己记的 .hardcoded.json —— 全翻完的 modid
                # 才配显示「已 AI 汉化完成」，还有剩的挂 ⚠。检测没跑（缓存空）时先当未知。
                hardcoded_pending = (
                    core.pending_hardcoded_modids(detected, scan, core.load_hardcoded_translations(pack))
                    if detected else {}
                )
                patch_pending = {entry.modid for entry in core.missing_patchouli_entries(scan)}
                shown_mods = {status.modid for status in statuses}
                for modid in sorted(patch_pending - shown_mods):
                    statuses.append(core.ModTranslationStatus(modid, 0, 0, 0))
                all_status.extend(statuses)
                if statuses:
                    for st in statuses:
                        label = core.mod_status_label(st, fill_community, refine_community)
                        if st.modid in hardcoded_namespaces:
                            label += "·疑似硬编码文本"
                        if st.modid in patch_pending:
                            label += "·手册待翻"
                        if st.modid in uninstalled[str(instance)]:
                            label = "AI 汉化已卸载·可右键重载"
                        failed_count = len(progress_failed.get(st.modid, []))
                        if failed_count:
                            label += f"·上次失败{failed_count}条"
                            failed_modids.add(st.modid)
                        is_uninstalled = st.modid in uninstalled[str(instance)]
                        is_hardcoded = st.modid in hardcoded_namespaces
                        is_excluded = st.modid in self.no_translate
                        complete = st.fully_translated and not is_uninstalled and st.modid not in patch_pending and not is_hardcoded
                        coverage = core.translation_coverage_label(st) if complete else ""
                        rows.append(self._make_row(st, label, coverage, instance, is_uninstalled, failed_count, st.modid in patch_pending, is_hardcoded, is_excluded, hardcoded_pending.get(st.modid, 0)))
                        if not is_excluded and st.modid not in uninstalled[str(instance)] and ((st.missing > 0 and (not st.has_community or fill_community or refine_community)) or st.modid in patch_pending or (translate_hardcoded and st.modid in hardcoded_namespaces)):
                            translatable_targets.setdefault(str(instance), set()).add(st.modid)
                else:
                    rows.append(self._placeholder_row(instance))
            # 先把列表发出去（秒出），再做最慢的硬编码检测，完成后单独补标记。
            self.result_queue.put(("rows", (rows, translatable_targets, failed_modids, hardcoded_findings, scan_cache, uninstalled, yield_reports)))
            for instance_path, mods_dir in pending_detection:
                self.result_queue.put(("status", f"后台检测硬编码文本：{Path(instance_path).name}……"))
                try:
                    found = core.detect_hardcoded_texts(mods_dir)
                except Exception:
                    found = []
                self._hardcoded_cache[instance_path] = found
                scan = scan_cache.get(instance_path)
                namespaces = sorted(core.hardcoded_modids(scan, found)) if scan else []
                self.result_queue.put(("hardcoded", (instance_path, namespaces, found)))
        except Exception as exc:
            self.result_queue.put(("error", ("scan", f"扫描失败：{core.redact_secrets(str(exc))}")))

    def _selected_targets(self) -> dict[str, set[str]]:
        targets: dict[str, set[str]] = {}
        for item in self.tree.selection():
            modid = self.row_modids.get(item, "")
            instance_path = self.row_targets.get(item, "")
            if modid and modid != PLACEHOLDER_MODID and instance_path:
                targets.setdefault(instance_path, set()).add(modid)
        return targets

    def _on_tree_select(self, _event: object = None) -> None:
        selection = self.tree.selection()
        if not selection:
            self.selection_var.set("")
            return
        if len(selection) == 1:
            modid = self.row_modids.get(selection[0], "")
            self.selection_var.set(f"已选：{modid}" if modid and modid != PLACEHOLDER_MODID else "")
        else:
            self.selection_var.set(f"已选 {len(selection)} 个模组")

    def _hide_coverage_tip(self, _event: object = None) -> None:
        self.coverage_hover_item = ""
        if self.coverage_tip is not None:
            self.coverage_tip.destroy()
            self.coverage_tip = None

    def _on_coverage_motion(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not item:
            self._hide_coverage_tip()
            return
        if column == "#2":
            text = self.coverage_by_row.get(item, "")
        elif column == "#3":
            text = self.status_tip_by_row.get(item, "")
        else:
            text = ""
        if not text:
            self._hide_coverage_tip()
            return
        if item == self.coverage_hover_item:
            return
        self._hide_coverage_tip()
        self.coverage_hover_item = item
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        ttk.Label(tip, text=text, padding=(10, 8), justify="left", relief="solid").pack()
        tip.update_idletasks()
        work_x, work_y, work_w, work_h = get_work_area(self)
        x = min(event.x_root + 14, work_x + work_w - tip.winfo_reqwidth())
        y = min(event.y_root + 18, work_y + work_h - tip.winfo_reqheight())
        tip.wm_geometry(f"+{max(work_x, x)}+{max(work_y, y)}")
        self.coverage_tip = tip

    def _on_tree_double(self, _event: object = None) -> None:
        # P2-3：双击会直接调用付费接口，先确认并显示待翻条数。
        if self.translating or self.scanning:
            return
        targets = self._selected_targets()
        if not targets:
            return
        excluded = sorted({m for mods in targets.values() for m in mods if m in self.no_translate})
        if excluded:
            self.status_var.set(f"{'、'.join(excluded[:3])} 在不翻译名单里；右键选「移出不翻译名单」可恢复。")
            return
        count = self._pending_count(targets)
        if count <= 0:
            messagebox.showinfo("无需翻译", "所选模组没有待翻译的 key。\n硬编码文本若已翻译过，点「导出桥接映射」更新游戏内规则即可。")
            return
        names = sorted({m for v in targets.values() for m in v})
        label = "、".join(names[:5]) + (" 等" if len(names) > 5 else "")
        if not messagebox.askyesno("确认翻译", f"{label} · {count} 条待翻\n\n将调用翻译接口，可能产生费用。是否继续？"):
            return
        self._start_translate(targets)

    def _on_tree_right_click(self, event: object) -> None:
        row = self.tree.identify_row(getattr(event, "y", 0))
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.tree.focus(row)
            self._on_tree_select()
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="联网安装人工汉化包…", command=self._install_human_pack)
        menu.add_separator()
        menu.add_command(label="删除本工具 AI 汉化（回退人工/官方）", command=lambda: self._remove_translation("ai"))
        selection = self.tree.selection()
        menu.add_command(label="重载已卸载的 AI 汉化", command=self._reload_translation, state="normal" if any(item in self.uninstalled_by_row for item in selection) else "disabled")
        menu.add_command(label="重翻整个模组", command=self._retranslate_mod)
        menu.add_command(label="设置不翻译 key…", command=self._skip_mod_key)
        excluded_here = [self.row_modids.get(item, "") for item in selection if self.row_modids.get(item, "") in self.no_translate]
        if excluded_here:
            menu.add_command(label="移出不翻译名单（恢复翻译）", command=self._remove_no_translate)
        if any(self.row_modids.get(item, "") and self.row_modids.get(item, "") != PLACEHOLDER_MODID and self.row_modids.get(item, "") not in self.no_translate for item in selection):
            menu.add_command(label="加入不翻译名单（不再翻译此模组）", command=self._add_no_translate)
        menu.add_command(label="强制还原英文（生成覆盖资源包）", command=lambda: self._remove_translation("en"))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _add_no_translate(self) -> None:
        if self.scanning or self.translating or self.managing_pack:
            messagebox.showinfo("提示", "请先等待当前扫描 / 翻译结束。")
            return
        added = sorted({m for m in (self.row_modids.get(item, "") for item in self.tree.selection()) if m and m != PLACEHOLDER_MODID and m not in self.no_translate})
        if not added:
            return
        self.no_translate.update(added)
        self.config["no_translate_mods"] = sorted(self.no_translate)
        core.save_config(self.config)
        names = "、".join(added[:5]) + (" 等" if len(added) > 5 else "")
        self.status_var.set(f"已把 {len(added)} 个模组加入不翻译名单（{names}），重新扫描后生效。")
        self.start_scan()

    def _remove_no_translate(self) -> None:
        if self.scanning or self.translating or self.managing_pack:
            messagebox.showinfo("提示", "请先等待当前扫描 / 翻译结束。")
            return
        removed = sorted({m for m in (self.row_modids.get(item, "") for item in self.tree.selection()) if m in self.no_translate})
        if not removed:
            return
        self.no_translate.difference_update(removed)
        self.config["no_translate_mods"] = sorted(self.no_translate)
        core.save_config(self.config)
        names = "、".join(removed[:5]) + (" 等" if len(removed) > 5 else "")
        self.status_var.set(f"已把 {len(removed)} 个模组移出不翻译名单（{names}），重新扫描后生效。")
        self.start_scan()

    def _install_human_pack(self) -> None:
        if self.scanning or self.translating or self.managing_pack:
            messagebox.showinfo("提示", "请等待当前扫描或资源包操作完成。")
            return
        selected = [(self.row_targets[item], self.row_modids[item]) for item in self.tree.selection() if item in self.row_targets and self.row_modids[item] != PLACEHOLDER_MODID]
        if len(selected) != 1:
            messagebox.showinfo("选择模组", "请在模组列表中只选中一个模组，再安装对应的人工汉化包。")
            return
        instance_path, modid = selected[0]
        url = simpledialog.askstring("联网安装人工汉化包", f"粘贴含有 {modid} 中文语言文件的资源包 ZIP 直链（HTTPS）：", parent=self)
        if not url:
            return
        destination = core.human_pack_destination(Path(instance_path), modid)
        if destination.exists() and not messagebox.askyesno("更新人工汉化包", f"本工具为 {modid} 安装的人工汉化包已存在，是否用新下载的版本替换？"):
            return
        self.managing_pack = True
        self.status_var.set(f"正在下载 {modid} 的人工汉化资源包……")
        threading.Thread(target=self._install_human_pack_worker, args=(url.strip(), instance_path, modid), daemon=True).start()

    def _install_human_pack_worker(self, url: str, instance_path: str, modid: str) -> None:
        try:
            pack = core.download_human_pack(url, Path(instance_path), modid)
            self.result_queue.put(("human_installed", (modid, str(pack))))
        except Exception as exc:
            self.result_queue.put(("pack_error", f"安装人工汉化包失败：{exc}"))

    def _remove_translation(self, mode: str) -> None:
        if self.translating or self.scanning or self.managing_pack:
            messagebox.showinfo("提示", "请先等待当前扫描 / 翻译结束。")
            return
        targets = self._selected_targets()
        if not targets:
            messagebox.showinfo("提示", "请先在列表里选中要处理的模组（可按住 Ctrl 多选）。")
            return
        total = sum(len(modids) for modids in targets.values())
        if mode == "ai":
            prompt = (
                f"将从本工具生成的资源包中移除 {total} 个模组的 AI 汉化，"
                "使其回退到模组自带的人工 / 官方汉化（没有则显示英文）。\n\n确定吗？"
            )
        else:
            prompt = (
                f"将为 {total} 个模组生成覆盖资源包，把它们的全部中文键写回英文"
                "（连模组自带的人工 / 官方汉化一起盖掉）。\n\n确定吗？"
            )
        if not messagebox.askyesno("删除汉化", prompt):
            return
        config = self._snapshot_config()
        if mode != "ai":
            for instance_path in targets:
                if not self._ensure_pack_format(Path(instance_path), config):
                    return
        self.config = config
        core.save_config(config)
        self.status_var.set("正在生成覆盖资源包……")
        self.managing_pack = True
        threading.Thread(target=self._remove_translation_worker, args=(mode, targets, config), daemon=True).start()

    def _reload_translation(self) -> None:
        if self.translating or self.scanning or self.managing_pack:
            messagebox.showinfo("提示", "请先等待当前操作完成。")
            return
        targets: dict[str, set[str]] = {}
        for item in self.tree.selection():
            if item in self.uninstalled_by_row:
                targets.setdefault(self.row_targets[item], set()).add(self.row_modids[item])
        if not targets:
            return
        config = self._snapshot_config()
        for instance_path in targets:
            if not self._ensure_pack_format(Path(instance_path), config):
                return
        self.managing_pack = True
        self.status_var.set("正在重载已卸载的 AI 汉化……")
        threading.Thread(target=self._reload_worker, args=(targets, config), daemon=True).start()

    def _retranslate_mod(self) -> None:
        if self.translating or self.scanning or self.managing_pack:
            return
        targets = self._selected_targets()
        if len(targets) != 1 or len(next(iter(targets.values()))) != 1:
            messagebox.showinfo("重翻模组", "请选择一个模组。")
            return
        instance_path, modids = next(iter(targets.items()))
        instance = Path(instance_path)
        config = self._snapshot_config()
        if not self._ensure_pack_format(instance, config):
            return
        settings = core.resolve_translate_settings(config, instance)
        pack = Path(settings["output_dir"]) / settings["pack_name"]
        modid = next(iter(modids))
        keys = set(core.load_pack_translations(pack).get(modid, {}))
        if not keys:
            messagebox.showinfo("重翻模组", "该模组尚无摆渡译文。")
            return
        self.translating = True
        self._set_translating_ui(True)
        def work() -> None:
            try:
                scan = self.scan_cache.get(instance_path)
                count, errors = core.retranslate_pack_keys(scan, pack, {modid: keys}, settings)
                self.result_queue.put(("qc_retranslated", (count, errors)))
            except Exception as exc:
                self.result_queue.put(("error", ("translate", f"重翻失败：{core.redact_secrets(str(exc))}")))
        threading.Thread(target=work, daemon=True).start()

    def _skip_mod_key(self) -> None:
        targets = self._selected_targets()
        if len(targets) != 1 or len(next(iter(targets.values()))) != 1:
            messagebox.showinfo("不翻译 key", "请先选中一个模组。")
            return
        modid = next(iter(next(iter(targets.values()))))
        key = simpledialog.askstring("不翻译 key", f"输入 {modid} 中要跳过的完整 lang key：", parent=self)
        if not key or not key.strip():
            return
        ignored = dict(self.config.get("skip_keys") or {})
        ignored[modid] = sorted(set(ignored.get(modid, [])) | {key.strip()})
        self.config["skip_keys"] = ignored
        core.save_config(self.config)
        self.status_var.set(f"已跳过 {modid}:{key.strip()}，下次翻译生效。")

    def _reload_worker(self, targets: dict[str, set[str]], config: dict) -> None:
        try:
            restored: list[str] = []
            for instance_path, modids in targets.items():
                instance = Path(instance_path)
                settings = core.resolve_translate_settings(config, instance)
                scan = self.scan_cache.get(instance_path)
                if scan is None:
                    packs = instance / "resourcepacks"
                    scan = core.scan_inputs(instance / "mods", packs if packs.is_dir() else None, instance)
                pack = Path(settings["output_dir"]) / settings["pack_name"]
                restored.extend(core.reload_ai_translations(pack, modids, scan, settings["pack_format"]))
            self.result_queue.put(("reloaded", restored))
        except Exception as exc:
            self.result_queue.put(("pack_error", f"重载 AI 汉化失败：{exc}"))

    def _remove_translation_worker(self, mode: str, targets: dict[str, set[str]], config: dict) -> None:
        try:
            changed: list[str] = []
            skipped: list[str] = []
            for instance_path, modids in targets.items():
                instance = Path(instance_path)
                settings = core.resolve_translate_settings(config, instance)
                pack = Path(settings["output_dir"]) / settings["pack_name"]
                if mode == "ai":
                    changed.extend(core.remove_ai_translations(pack, set(modids)))
                else:
                    scan = self.scan_cache.get(instance_path)
                    if scan is None:
                        packs = instance / "resourcepacks"
                        scan = core.scan_inputs(instance / "mods", packs if packs.is_dir() else None, instance)
                    english = core.english_map_for(scan.english, set(modids))
                    ftb_selected = any(modid.lower().replace("-", "_") == "ftbquestlocalizer" for modid in modids)
                    ftb_english = dict(scan.ftb_localizer) if (ftb_selected and scan.ftb_localizer) else None
                    for modid in modids:
                        if modid not in english and not (modid.lower().replace("-", "_") == "ftbquestlocalizer" and ftb_english):
                            skipped.append(modid)
                    changed.extend(core.revert_mods(pack, english, settings["pack_format"], ftb_english=ftb_english))
                core.remove_progress_modids(set(modids))
            self.result_queue.put(("reverted", (mode, changed, skipped)))
        except Exception as exc:
            self.result_queue.put(("pack_error", f"生成覆盖资源包失败：{core.redact_secrets(str(exc))}"))

    def _translate_selected(self) -> None:
        if self.translating:
            return
        if self.scanning:
            messagebox.showinfo("提示", "正在扫描实例，请扫描完成后再开始翻译。")
            return
        targets = self._selected_targets()
        if not targets:
            messagebox.showinfo("提示", "请先在列表里选中要汉化的模组（可按住 Ctrl 多选）。")
            return
        dropped = sorted({m for mods in targets.values() for m in mods if m in self.no_translate})
        if dropped:
            for modids in targets.values():
                modids.difference_update(self.no_translate)
            targets = {path: modids for path, modids in targets.items() if modids}
            if not targets:
                self.status_var.set(f"选中的模组都在不翻译名单里（{'、'.join(dropped[:3])}）；右键可移出。")
                return
            self.status_var.set(f"已跳过不翻译名单里的 {len(dropped)} 个模组。")
        self._start_translate(targets)

    def _translate_all(self) -> None:
        if self.translating:
            return
        if self.scanning:
            messagebox.showinfo("提示", "正在扫描实例，请扫描完成后再开始翻译。")
            return
        # 拷贝一份再改，别污染 translatable_targets 本体。
        targets = {path: set(modids) for path, modids in self.translatable_targets.items()}
        if self.hardcoded_var.get():
            # 硬编码检测是扫描后在后台跑的，「翻译全部」在此刻把只剩硬编码候选的模组补进来。
            for instance_path in list(targets):
                scan = self.scan_cache.get(instance_path)
                if scan is None:
                    continue
                try:
                    settings = core.resolve_translate_settings(self.config, Path(instance_path))
                except ValueError:
                    continue
                sidecar = core.load_hardcoded_translations(Path(settings["output_dir"]) / settings["pack_name"])
                entries = core.missing_hardcoded_entries(
                    core.hardcoded_strings_by_modid(scan, self._hardcoded_cache.get(instance_path) or []),
                    sidecar,
                )
                pending = {entry.modid for entry in entries if entry.modid not in self.no_translate}
                if pending:
                    targets.setdefault(instance_path, set()).update(pending)
        if not targets:
            messagebox.showinfo("提示", "没有待 AI 翻译的模组。")
            return
        self._start_translate(targets)

    def _start_translate(self, targets: dict[str, set[str]]) -> None:
        config = self._snapshot_config()
        if not self._confirm_community_lookup(config):
            return
        for instance_path in targets:
            if not self._ensure_pack_format(Path(instance_path), config):
                return
        if self.managing_pack:
            messagebox.showinfo("提示", "正在处理资源包，请稍候。")
            return
        for instance_path, modids in targets.items():
            try:
                settings = core.resolve_translate_settings(config, Path(instance_path))
            except ValueError as exc:
                messagebox.showerror("翻译引擎配置", str(exc))
                return
            pack = Path(settings["output_dir"]) / settings["pack_name"]
            if modids & core.load_uninstalled_ai(pack).keys():
                messagebox.showinfo("AI 汉化已卸载", "所选模组的 AI 汉化已卸载。请在模组列表中右键选择“重载已卸载的 AI 汉化”。")
                return
        cached = [(path, self.scan_cache.get(path), modids) for path, modids in targets.items()]
        if all(scan is not None for _, scan, _ in cached) and not any(
            core.missing_entries(
                scan.english, scan.community, scan.ai, set(self.whitelist),
                fill_community=config["fill_community"], modids=modids,
                reverted=scan.reverted, refine_community=config["refine_community"],
            ) or core.missing_patchouli_entries(scan, modids)
            or (config.get("translate_hardcoded", True) and core.has_pending_hardcoded(self._hardcoded_cache.get(path) or [], scan, modids))
            for path, scan, modids in cached
        ):
            done_n = sum(len(data) for data in core.load_hardcoded_translations(
                Path(config.get("output_dir") or Path(instance_path) / "resourcepacks")
                / (config.get("pack_name") or core.DEFAULT_CONFIG["pack_name"])).values()) if targets else 0
            message = "所选模组没有需要翻译的非空英文 key；已有汉化或 AI 已覆盖全部有效条目。"
            if done_n:
                message += f" 硬编码文本已翻译过 {done_n} 条——点「导出桥接映射」把最新规则写进游戏即可。"
            self.status_var.set(message)
            messagebox.showinfo("无需翻译", message)
            return
        if config["refine_community"]:
            selected = config.get("model_pool") or [{"engine": config["engine"]}]
            if any(slot.get("engine") not in {"openai", "anthropic", "gemini", "local"} for slot in selected):
                messagebox.showerror("人工汉化精加工", "请使用 OpenAI / Anthropic / Gemini / 本地模型，并从并发池移除 MyMemory / DeepL 后重试。")
                return
        self.config = config
        core.save_config(config)
        self.translating = True
        self.cancel_event = threading.Event()
        self._tx_phase = None
        self._tx_stats = None
        self._progress_value = 0.0
        self._progress_cap = 1.0
        self.progress.configure(value=0, maximum=1)
        self.progress_text_var.set("准备翻译……")
        self.status_var.set("开始翻译……")
        self._set_translating_ui(True)
        self._start_progress_tick()
        threading.Thread(target=self._translate_worker, args=(targets, config, self.cancel_event), daemon=True).start()

    def _stop_translate(self) -> None:
        if self.translating:
            self.cancel_event.set()
            self.stop_button.configure(state="disabled")
            self.status_var.set("正在停止，保留已翻译条目…")

    def _set_translating_ui(self, active: bool) -> None:
        state = "disabled" if active else "normal"
        self.translate_all_button.configure(state=state)
        self.translate_selected_button.configure(state=state)
        self.stop_button.configure(state="normal" if active else "disabled")
        # 桥接相关按钮也一起锁：翻译进行中不该再导出 / 导入，避免两个写入撞车。
        self.bridge_button.configure(state=state)
        self.collect_button.configure(state=state)

    def _finish_translate(self, packs: list[str], errors: list[str], cancelled: bool, usage_text: str = "") -> None:
        self.translating = False
        self._set_translating_ui(False)
        self.progress.configure(value=0)
        self.progress_text_var.set("")
        nbt_count = self._last_nbt_count
        self._last_nbt_count = 0
        # ── 「一步到位」最后一环 ──────────────────────────────────────
        # 译完自动把映射写进游戏目录（摆渡桥读它、每秒热重载），
        # 省掉手动点「导出桥接映射」那一步。失败也只降级成提示，绝不冒充翻译失败。
        auto_note = ""
        exported = None
        if self.auto_export_var.get() and not cancelled and (packs or nbt_count):
            try:
                exported = self._write_bridge_mapping(self.path_var.get().strip(), notify=False)
            except Exception as exc:  # 导出是收尾彩蛋，坏掉不能连累已生成的结果
                exported = {"error": core.redact_secrets(str(exc))}
            if exported and exported.get("mapping"):
                auto_note = f"\n已自动导出桥接映射 {exported['mapping']} 条。"
                if exported.get("bridge_note"):
                    auto_note += f"\n（{exported['bridge_note']}）"
            elif exported and exported.get("error"):
                auto_note = f"\n（自动导出桥接映射失败：{exported['error']}）"
        lines: list[str] = []
        if packs:
            lines.append("已生成：" + "\n".join(packs))
            if usage_text:
                lines.append("\n各模型用量与省流统计：\n" + usage_text)
            if errors:
                lines.append(f"\n有 {len(errors)} 个批次失败（相关 key 保留英文，下次运行会自动重试）：")
                lines.append("\n".join("· " + e for e in errors[:10]))
            if cancelled:
                lines.append("\n翻译已停止，这是不完整的包（只含已翻译的部分）。")
            if nbt_count:
                lines.append(f"\n另翻译了 {nbt_count} 条硬编码文本（桥接显示用）。")
            if auto_note:
                lines.append(auto_note)
            base = f"翻译{'已停止' if cancelled else '完成'}：{len(packs)} 个资源包"
            if nbt_count:
                base += f" + {nbt_count} 条硬编码文本"
            if exported and exported.get("mapping"):
                base += f"，已自动导出桥接映射 {exported['mapping']} 条"
            self.status_var.set(base + "。")
            if messagebox.askyesno("已停止" if cancelled else "完成", "\n".join(lines) + "\n\n是否打开所在目录？"):
                try:
                    os.startfile(str(Path(packs[0]).parent))
                except OSError:
                    pass
        elif errors:
            self.status_var.set(f"翻译失败：{len(errors)} 个批次出错，未生成资源包。")
            messagebox.showwarning("翻译失败", "\n".join(errors[:10]))
        elif cancelled:
            self.status_var.set("已停止，未翻译任何内容，未生成资源包。")
        else:
            self.status_var.set("没有待翻译条目。")
            if nbt_count:
                if exported and exported.get("mapping"):
                    self.status_var.set(f"另翻译并自动导出 {nbt_count} 条硬编码文本的映射，进游戏即生效。")
                else:
                    self.status_var.set(f"另翻译了 {nbt_count} 条硬编码文本；点「导出桥接映射」写进游戏。")
        # 只有真正产生了变化（写包 / 有失败）时才自动重扫，避免“点翻译却只是在重扫”的错觉。
        if packs or errors:
            self.start_scan()

    def _start_progress_tick(self) -> None:
        # 250ms 一跳的轻量循环：批次 API 调用期间没有事件，靠插值让进度条别死在半路。
        if self._progress_tick_after is None:
            self._progress_tick_after = self.after(250, self._progress_tick)

    def _progress_tick(self) -> None:
        self._progress_tick_after = None
        if not self.translating:
            return
        st = self._tx_stats
        if st and st.get("mode") == "translate":
            batches_total = st["batches_total"]
            inflight = st["inflight"]
            if batches_total > 0 and inflight > 0 and st["avg"] > 0:
                # 在飞的批次没有事件可收，按平均批耗时的 90% 匀速推进，封顶在「完成+在飞」。
                rate = inflight / batches_total * 0.25 / (st["avg"] * 0.9)
                self._progress_value = min(self._progress_cap, self._progress_value + rate)
                self.progress.configure(value=self._progress_value)
            self._update_translate_status()
        self._progress_tick_after = self.after(250, self._progress_tick)

    def _progress_exact(self, fraction: float) -> None:
        # 准确进度只进不退；封顶在「已完成 + 在飞」批次，避免插值冲过头。
        if getattr(self, "_tx_stats", None) and self._progress_cap > 0:
            fraction = min(fraction, self._progress_cap)
        self._progress_value = max(self._progress_value if self._tx_stats else 0.0, fraction)
        self.progress.configure(maximum=1, value=self._progress_value)

    def _fmt_seconds(self, seconds: float) -> str:
        seconds = max(1, int(round(seconds)))
        minutes, sec = divmod(seconds, 60)
        if minutes >= 60:
            hours, minutes = divmod(minutes, 60)
            return f"{hours} 小时 {minutes} 分"
        return f"{minutes} 分 {sec} 秒" if minutes else f"{sec} 秒"

    def _update_translate_status(self) -> None:
        st = self._tx_stats
        if not st:
            return
        parts: list[str] = []
        phase = self._tx_phase
        if phase:
            parts.append(f"阶段 {phase[0]}/{phase[1]} · {phase[2]}")
        vocab_done, vocab_total = st["entries_done"], st["entries_total"]
        if vocab_total:
            pct = int(self._progress_value * 100 if st["batches_total"] else 0)
            parts.append(f"已翻 {vocab_done}/{vocab_total} 条（{pct}%）")
        batches_left = st["batches_total"] - st["batches_done"]
        if batches_left > 0 and st["avg"] > 0:
            workers = max(1, min(st["concurrency"] or 1, batches_left))
            parts.append(f"预计还需 {self._fmt_seconds(st['avg'] * batches_left / workers)}")
        if parts:
            self.status_var.set(" · ".join(parts))

    def _translate_worker(self, targets: dict[str, set[str]], config: dict, cancel_event: threading.Event) -> None:
        try:
            packs_written: list[str] = []
            all_errors: list[str] = []
            usage: dict[str, dict[str, int]] = {}
            # 批内进度的共享状态：callback 可能来自并发池线程，读写都要过锁。
            batch_stats = {"lock": threading.Lock(), "started": {}, "completed": 0, "durations": []}
            batch_push = {"time": 0.0}
            translate_started = time.monotonic()
            translation_finished = False
            for instance_path, modids in targets.items():
                instance = Path(instance_path)
                mods = instance / "mods"
                packs = instance / "resourcepacks"
                scan = self.scan_cache.get(instance_path)
                if scan is None:
                    self.result_queue.put(("phase", (1, 3, "扫描输入")))
                    scanned_here = True
                    scan = core.scan_inputs(mods, packs if packs.is_dir() else None, instance)
                else:
                    scanned_here = False
                translate_phase, write_phase = ((2, 3) if scanned_here else (1, 2))
                settings = core.resolve_translate_settings(config, instance)
                pack = Path(settings["output_dir"]) / settings["pack_name"]
                pack_path = Path(settings["output_dir"]) / settings["pack_name"]
                translate_report = core.yield_to_community(scan, pack_path)
                if translate_report:
                    self.result_queue.put(("status", f"检测到新的人工汉化，摆渡{translate_report.summary()}。"))
                if settings["engine"] in core.ENGINES_WITH_KEY and not settings["api_key"]:
                    self.result_queue.put(("error", ("translate", f"{settings['engine']} 引擎需要 API key。请在下方填入并保存。")))
                    return
                entries = core.missing_entries(
                    scan.english, scan.community, scan.ai, set(self.whitelist),
                    fill_community=settings["fill_community"], modids=modids,
                    reverted=scan.reverted,
                    refine_community=settings["refine_community"],
                )
                patch_entries = core.missing_patchouli_entries(scan, modids)
                ftb_entries = core.missing_ftb_entries(scan, pack_path, modids) if settings["pack_format"] > 3 else []
                if not scan.ftb_localizer and (scan.ftb_localizer_installed or any(file.modid.lower().replace("-", "_") in {"ftbquests", "ftb_quests"} for file in scan.english)):
                    if scan.ftb_localizer_installed:
                        self.result_queue.put(("status", "已检测到 FTB Quest Localizer，但还没导出：进游戏运行 /ftblang export 后重新扫描即可。摆渡只读这个导出文件，不碰 SNBT。"))
                    else:
                        self.result_queue.put(("status", "想翻 FTB 任务书：先在 CurseForge / Modrinth 安装 FTB Quest Localizer，进游戏运行 /ftblang export，再回来重新扫描。摆渡不解析、不改写任何 SNBT。"))
                baseline: dict[str, dict[str, str]] = {}
                if settings.get("community_enabled"):
                    candidate_scan = core.ScanResult(
                        english=[file for file in scan.english if file.modid in modids],
                        community=scan.community, ai=scan.ai, reverted=scan.reverted,
                    )
                    # 只用已缓存的整合包：在线下载会卡住翻译线程（进度条看起来就是死的）。
                    # 首次运行还没下完时本回合跳过基线，扫描后的预热线程会补上，下次翻译生效。
                    core.ensure_community_pack(Path(settings["cache_dir"]), allow_fetch=False)
                    baseline = core.fetch_community_baseline(candidate_scan, config, Path(settings["cache_dir"]), modids)
                    baseline_files = [core.LangFile(modid, "zh_cn", "community-baseline", data) for modid, data in baseline.items()]
                    entries = core.entries_with_baseline(scan, baseline, set(self.whitelist), modids, settings["fill_community"], settings["refine_community"])
                else:
                    baseline_files = []
                entries.extend(patch_entries)
                entries.extend(ftb_entries)
                # 硬编码候选（桥接用）：key 是 nbt:<hash>，译文存 sidecar，不写进资源包。
                hardcoded_entries: list[core.Entry] = []
                if config.get("translate_hardcoded", True):
                    existing_hardcoded = core.load_hardcoded_translations(pack)
                    hardcoded_entries = core.missing_hardcoded_entries(
                        core.hardcoded_strings_by_modid(scan, self._hardcoded_cache.get(instance_path) or [], modids),
                        existing_hardcoded, set(modids))
                entries.extend(hardcoded_entries)
                entries = core.skip_ignored_entries(entries, config)
                if not entries and not baseline:
                    continue

                def progress(done: int, total: int, modid: str) -> None:
                    self.result_queue.put(("progress", (done, total, modid)))

                def report_batch(batch_index: int, batch_total: int, entries_total: int, modid: str, in_batch_done: int, in_batch_size: int) -> None:
                    # 批内进度：批开始/批结束必发；批中按「每 2 条或每 0.3 秒先到者」节流。
                    with batch_stats["lock"]:
                        now = time.monotonic()
                        pending = batch_stats["started"]
                        if in_batch_done == 0:
                            pending.setdefault(batch_index, now)
                        elif in_batch_done >= in_batch_size and batch_index in pending:
                            started_at = pending.pop(batch_index)
                            batch_stats["completed"] += 1
                            batch_stats["durations"] = (batch_stats["durations"] + [now - started_at])[-3:]
                        durations = batch_stats["durations"]
                        snapshot = (
                            batch_stats["completed"], batch_total, len(pending),
                            sum(durations) / len(durations) if len(durations) >= 2 else 0.0,
                            modid, in_batch_done, in_batch_size,
                            max(1, int(settings.get("concurrency", 1) or 1)), entries_total,
                        )
                        queue_now = in_batch_done == 0 or in_batch_done >= in_batch_size or in_batch_done % 2 == 0 or now - batch_push["time"] > 0.3
                        if queue_now:
                            batch_push["time"] = now
                    if queue_now:
                        self.result_queue.put(("batch_progress", snapshot))

                corpus = core.community_corpus(scan.english, scan.community + baseline_files, scan.reference_only) if settings["refine_community"] else None
                if entries:
                    self.result_queue.put(("phase", (translate_phase, write_phase, "翻译条目")))
                    translations, errors, failed = core.translate_entries(entries, settings, progress=progress, batch_progress=report_batch, cancel_event=cancel_event, usage=usage, corpus=corpus)
                    # 翻译一结束就主动宣告结果，别让用户靠「停止」按钮才能猜出完成了。
                    self.result_queue.put(("translate_done", (len(entries), time.monotonic() - translate_started, cancel_event.is_set())))
                    translation_finished = not cancel_event.is_set()
                else:
                    translations, errors, failed = {}, [], {}
                all_errors.extend(errors)
                nbt_pairs: dict[str, dict[str, str]] = {}
                if hardcoded_entries:
                    nbt_pairs = core.hardcoded_pairs_from_entries(hardcoded_entries, translations)
                ftb_components = core.translated_ftb_components(scan, ftb_entries, translations, pack) if settings["pack_format"] > 3 else {}
                if translations or baseline or ftb_components:
                    lang_translations = {modid: dict(data) for modid, data in baseline.items()}
                    for modid, data in translations.items():
                        lang_translations.setdefault(modid, {}).update({key: value for key, value in data.items() if not key.startswith(("patchouli:", "nbt:", "ftblocalizer:"))})
                    self.result_queue.put(("phase", (write_phase, write_phase, "写入资源包")))
                    def report_pack_write(done: int, total: int, namespace: str) -> None:
                        # 写 zip 按命名空间上报，避免打包阶段零反馈。
                        self.result_queue.put(("pack_write", (done, total, namespace)))
                    merged = core.merge_pack_translations(pack, lang_translations, scan.community)
                    pages = core.load_pack_patchouli(pack)
                    pages.update(core.translated_patchouli_files(scan, patch_entries, translations))
                    sources = {modid: {key: "community" for key in data} for modid, data in baseline.items()}
                    if ftb_components:
                        # 新译出的结构化条目登记为 AI；已有条目保留原来源，避免把社区来源洗成 AI。
                        old_ftb_sources = core.load_pack_sources(pack).get("ftbquestlocalizer", {})
                        sources.setdefault("ftbquestlocalizer", {}).update({key: old_ftb_sources.get(key, "ai") for key in ftb_components})
                    core.write_pack(pack, merged, settings["pack_format"], core.load_pack_reverted(pack), patchouli=pages, sources=core.merged_sources(pack, merged, sources), pack_progress=report_pack_write, ftb_components=ftb_components, ftb_sources=core.ftb_source_map(scan, ftb_components) or None)
                    packs_written.append(str(pack))
                if nbt_pairs:
                    try:
                        merged_hardcoded = core.load_hardcoded_translations(pack)
                        for nbt_modid, pairs in nbt_pairs.items():
                            merged_hardcoded.setdefault(nbt_modid, {}).update(pairs)
                        core.save_hardcoded_translations(pack, merged_hardcoded)
                        self.result_queue.put(("nbt_translated", sum(len(p) for p in nbt_pairs.values())))
                    except OSError as exc:
                        core.LOG.warning("Could not save bridge mapping: %s", type(exc).__name__)
                        all_errors.append("桥接映射保存失败，已生成的资源包不受影响。")
                try:
                    core.update_progress(translations, failed)
                except OSError as exc:
                    core.LOG.warning("Could not save translation progress: %s", type(exc).__name__)
                    all_errors.append("失败记录保存失败，已生成的资源包不受影响。")
                if cancel_event.is_set():
                    break
            usage_text = core.format_usage(usage)
            # 翻译阶段若已完整跑完，之后才点停止只是打包被打断，包并不是「半截货」。
            self.result_queue.put(("translated" if translation_finished else "cancelled", (packs_written, all_errors, usage_text)))
        except Exception as exc:
            core.LOG.exception("translate: worker failed")
            self.result_queue.put(("error", ("translate", core.explain_error(exc, stage="MC 模组翻译"))))

    def _drain_queue(self) -> None:
        if self._closing:
            return
        try:
            self._drain_queue_once()
        except Exception as exc:
            core.LOG.error("GUI queue handler failed: %s", type(exc).__name__)
            self.status_var.set("处理后台结果时出错，请查看日志。")
        finally:
            if not self._closing:
                self._drain_after = self.after(100, self._drain_queue)

    def _drain_queue_once(self) -> None:
        if self._closing:
            return
        try:
            kind, payload = self.result_queue.get_nowait()
        except queue.Empty:
            return
        if kind == "status":
            # 扫描过程中的进度提示；绝不能在这里重置 scanning，否则可重复触发并发扫描。
            self.status_var.set(str(payload))
        elif kind == "instances":
            if self.scanning or self.translating:
                core.LOG.debug("Ignored stale instance-discovery result while busy")
                return
            previous = getattr(self, "_previous_instance", "")
            select = next((index for index, path in enumerate(self.instance_paths) if str(path) == previous), 0 if self.instance_paths else None)
            self._set_instance_options(list(payload), select=select)
            if self.instance_paths:
                self.path_var.set(str(self.instance_paths[select if select is not None else 0]))
                self.status_var.set(f"检测到 {len(self.instance_paths)} 个 Minecraft 实例，正在扫描当前实例……")
                self.start_scan()
            else:
                self.status_var.set("没有自动找到 Minecraft 实例。你可以点击“浏览”手动选择。")
        elif kind == "collected":
            self._after_collected(payload)
        elif kind == "rows":
            self.scanning = False
            rows, translatable_targets, failed_modids, hardcoded, scan_cache, uninstalled, yield_reports = payload
            self.yield_reports = {instance: report for instance, report in (yield_reports or [])}
            self.translatable_targets = translatable_targets
            self.hardcoded_results = hardcoded
            self.scan_cache = scan_cache
            current = self.path_var.get().strip()
            if current:
                try:
                    settings = core.resolve_translate_settings(self._snapshot_config(), Path(current))
                    issues = core.quality_issues(scan_cache[current], Path(settings["output_dir"]) / settings["pack_name"])
                    self.quality_button.configure(text=f"质量检查（{len(issues)}）" if issues else "质量检查")
                except (KeyError, OSError, ValueError):
                    self.quality_button.configure(text="质量检查")
                try:
                    self._ensure_pack_format(Path(current), self._snapshot_config())
                except (OSError, ValueError) as exc:
                    self.pack_format_var.set(f"游戏版本待确认：{exc}")
            self.limits_button.configure(state="normal" if hardcoded else "disabled")
            self.all_rows = rows
            self._render_rows()
            if self.config.get("community_enabled", True):
                threading.Thread(
                    target=core.ensure_community_pack,
                    args=(core.app_dir() / "ferry_cache",),
                    daemon=True,
                ).start()
            excluded_n = sum(1 for r in rows if r.get("excluded"))
            ai_mods = sum(1 for r in rows if not r["placeholder"] and not r["has_community"] and not r.get("excluded"))
            com_mods = sum(1 for r in rows if not r["placeholder"] and r["has_community"] and not r.get("excluded"))
            partial = sum(1 for r in rows if not r["placeholder"] and not r["has_community"] and r["ai"] > 0 and r["missing"] > 0)
            pending_community = max(0, sum(len(v) for v in translatable_targets.values()) - ai_mods)
            if partial:
                self.translate_all_button.configure(text="继续翻译")
            else:
                self.translate_all_button.configure(text="翻译全部待AI")
            if not ai_mods and not partial:
                summary = f"扫描完成：{com_mods} 个模组都已有人工汉化，不需要 AI 补。"
            elif self.refine_var.get():
                summary = f"扫描完成：{ai_mods} 个模组待 AI 翻译，{pending_community} 个待人工汉化精加工。"
            elif self.fill_var.get():
                summary = f"扫描完成：{ai_mods} 个模组待 AI 翻译，{pending_community} 个待补人工汉化缺失。"
            else:
                summary = f"扫描完成：{ai_mods} 个模组缺中文，{com_mods} 个已有人工汉化（自动让位）。"
            for _instance, report in (yield_reports or []):
                summary += f" 检测到新的人工汉化，摆渡{report.summary()}。"
            if excluded_n:
                summary += f" {excluded_n} 个已手动排除（不翻译），右键可移出。"
            if failed_modids:
                summary += f" {len(failed_modids)} 个模组上次翻译失败（标红），可用「只看失败」筛出来重翻。"
            self.status_var.set(summary)
        elif kind == "empty":
            self.scanning = False
            self.status_var.set(str(payload))
        elif kind == "hardcoded":
            instance_path, namespaces, findings = payload
            namespace_set = set(namespaces)
            # 顺便数一下每个 modid 还有几条硬编码没翻 —— 决定状态是「已完成」还是挂 ⚠。
            scan = self.scan_cache.get(instance_path)
            pending: dict[str, int] = {}
            if scan is not None:
                instance = Path(instance_path)
                pack = Path(self.config.get("output_dir") or instance / "resourcepacks") / (
                    self.config.get("pack_name") or core.DEFAULT_CONFIG["pack_name"]
                )
                pending = core.pending_hardcoded_modids(findings, scan, core.load_hardcoded_translations(pack))
            for row in self.all_rows:
                if row["instance_path"] == instance_path and row["modid"] in namespace_set:
                    self._mark_hardcoded_row(row, pending.get(row["modid"], 0))
            if findings:
                self.hardcoded_results = list(self.hardcoded_results or []) + list(findings)
                self.limits_button.configure(state="normal")
            self._render_rows()
        elif kind == "phase":
            if self.translating:
                self._tx_phase = payload
                st = self._tx_stats
                if st is not None:
                    st["mode"] = "pack_write" if str(payload[2]).startswith("写入") else "translate"
                # 切阶段：进度条归零重填，不跨阶段混合百分比。
                self._progress_value = 0.0
                self._progress_cap = 1.0
                self.progress.configure(maximum=1, value=0)
                self.progress_text_var.set("")
                self._update_translate_status()
        elif kind == "batch_progress":
            if self.translating:
                batches_done, batches_total, inflight, avg, modid, in_done, in_size, concurrency, entries_total = payload
                st = self._tx_stats
                if st is None:
                    st = self._tx_stats = {"batches_done": 0, "batches_total": 0, "inflight": 0, "avg": 0.0, "entries_done": 0, "entries_total": 0, "concurrency": 1, "mode": "translate"}
                st.update(batches_done=batches_done, batches_total=batches_total, inflight=inflight, avg=avg, concurrency=concurrency)
                st["entries_total"] = entries_total or st["entries_total"]
                if batches_total > 0:
                    self._progress_exact((batches_done + in_done / max(1, in_size)) / batches_total)
                    text = f"正在翻译 {modid} · 第 {min(batches_done + 1, batches_total)}/{batches_total} 批 · 本批 {in_done}/{in_size} 条"
                    if concurrency > 1:
                        text += f" · {concurrency} 路并发"
                    if avg > 0:
                        text += f" · 平均每批 {avg:.0f} 秒"
                    self.progress_text_var.set(text)
                self._update_translate_status()
        elif kind == "translate_done":
            if self.translating:
                count, elapsed, cancelled = payload
                st = self._tx_stats
                if st is not None:
                    st["mode"] = "pack_write"
                result = "翻译完成" if not cancelled else "已记录停止请求"
                self.status_var.set(f"{result} · {count} 条 · 耗时 {self._fmt_seconds(elapsed)} —— 正在写入资源包…")
        elif kind == "pack_write":
            if self.translating:
                done, total, namespace = payload
                self.progress.configure(maximum=max(1, total), value=done)
                self.progress_text_var.set(f"写入资源包 · {done}/{total} · {namespace}")
                phase = self._tx_phase
                self.status_var.set((f"阶段 {phase[0]}/{phase[1]} · " if phase else "") + f"写入资源包 · {done}/{total}")
        elif kind == "progress":
            done, total, modid = payload
            st = self._tx_stats
            if st is not None and self.translating:
                st["entries_done"], st["entries_total"] = done, total
                if st["batches_total"] > 0:
                    self._progress_exact(st["batches_done"] / st["batches_total"])
                self.progress_text_var.set(f"正在翻译 {modid} · 已翻 {done}/{total} 条")
                self._update_translate_status()
            else:
                self.progress.configure(maximum=total, value=done)
                self.progress_text_var.set(f"正在翻译 {modid}（{done}/{total} 条）")
                self.status_var.set(f"翻译中：{modid} 已翻 {done}/{total} 条")
        elif kind == "translated":
            packs, errors, usage_text = payload
            self._finish_translate(packs, errors, cancelled=False, usage_text=usage_text)
        elif kind == "cancelled":
            packs, errors, usage_text = payload
            self._finish_translate(packs, errors, cancelled=True, usage_text=usage_text)
        elif kind == "reverted":
            self.managing_pack = False
            mode, changed, skipped = payload
            if mode == "ai":
                message = f"已从资源包移除 {len(changed)} 个模组的 AI 汉化"
            else:
                message = f"已生成 / 更新覆盖资源包，把 {len(changed)} 个模组还原为英文"
            if skipped:
                message += f"；{len(skipped)} 个模组未找到英文源，已跳过"
            self.status_var.set(message + "，正在重新扫描……")
            messagebox.showinfo("删除汉化", message + "。")
            self.start_scan()
        elif kind == "reloaded":
            self.managing_pack = False
            message = f"已重载 {len(payload)} 个模组的 AI 汉化；人工汉化已有的 key 未覆盖。"
            self.status_var.set(message)
            messagebox.showinfo("重载 AI 汉化", message)
            self.start_scan()
        elif kind == "pack_error":
            self.managing_pack = False
            self.status_var.set(str(payload))
            messagebox.showerror("资源包操作失败", str(payload))
        elif kind == "human_installed":
            self.managing_pack = False
            modid, pack = payload
            self.status_var.set(f"{modid} 的人工汉化资源包已安装，正在重新扫描……")
            messagebox.showinfo("人工汉化包已下载", f"已安装至：\n{pack}\n请在游戏资源包列表中启用。")
            self.start_scan()
        elif kind == "qc_retranslated":
            self.translating = False
            self._set_translating_ui(False)
            count, errors = payload
            messagebox.showinfo("质量检查", f"重翻 {count} 条；失败 {len(errors)} 批。")
            self.start_scan()
        elif kind == "connection_test":
            self.engine_feedback.set(str(payload))
            messagebox.showinfo("测试连接", str(payload))
        elif kind == "glossary_updated":
            self.updating_glossary = False
            self.glossary_button.configure(state="normal")
            self.status_var.set(f"术语表已更新：{payload} 条，下次翻译自动生效。")
            messagebox.showinfo("术语表更新", f"已更新 {payload} 条术语到 user_glossary.json。")
        elif kind == "lookup_done":
            self.looking_up = False
            self.lookup_button.configure(state="normal")
            self.progress.configure(value=0)
            self._show_lookup_results(payload)
        elif kind == "literalbridge":
            self._after_literalbridge(payload)
        elif kind == "nbt_translated":
            self._last_nbt_count += int(payload)
        elif kind == "pack_progress":
            done, total = payload
            self.progress.configure(maximum=max(1, total), value=done)
            self.status_var.set(f"下载社区汉化整合包…… {max(1, done // 1048576)}/{max(1, total // 1048576)} MB")
        elif kind == "update_check":
            self.checking_update = False
            info, manual = payload
            if info:
                self._show_update_offer(info)
            elif manual:
                self.status_var.set(f"已是最新版本 {APP_VERSION}。")
        elif kind == "update_progress":
            done, total = payload
            self.progress.configure(maximum=max(1, total), value=done)
            percent = f"（{done * 100 // max(1, total)}%）" if total else ""
            self.status_var.set(f"正在下载更新……{percent}")
        elif kind == "update_ready":
            self._apply_update_swap(payload)
        elif kind == "update_failed":
            self.status_var.set(f"更新下载失败：{payload}。可到发布页手动下载。")
        elif kind == "loc_probe":
            if not self._loc_alive():
                return
            if len(payload) >= 5:
                root, report, adapter_name, hints, existing_lines = payload
            else:
                root, report, adapter_name, hints = payload
                existing_lines = []
            self._loc_progress_busy(False)
            self._loc_set_busy(False)
            if root is not None and (self._loc_root is None or Path(self._loc_root) != Path(root)):
                self._loc_root = Path(root)
                self._loc_path_var.set(str(root))
                if hasattr(self, "_loc_path_entry"):
                    try:
                        self._loc_path_entry.xview_moveto(1.0)
                    except tk.TclError:
                        pass
                self._remember_localize_dir(Path(root))
            target_label = f" · {Path(root).name}" if (root is not None and Path(root).is_file()) else ""
            if report is None:
                self._loc_probe = None
                self._loc_status_var.set(f"认不出这个游戏{target_label}")
                hint_lines = self._loc_hint_lines(root, hints)
                if existing_lines:
                    hint_lines.append(("", ""))
                    hint_lines.append(("head", "已汉化 / 续传"))
                    for tip in existing_lines:
                        hint_lines.append(("ok" if tip.startswith("  ·") or tip.startswith("已发现") else "warn", tip))
                    play = self._loc_first_play_path(root)
                    if play is not None:
                        self._loc_play_path = play
                        self._loc_out_dir = play if play.is_dir() else play.parent
                        self._loc_open_button.configure(state="normal", text="打开游戏目录")
                self._loc_write(hint_lines)
            else:
                self._loc_probe = report
                self._loc_status_var.set(
                    ("体检通过，可以开工" if report.ready else "体检完成，但不建议开工") + target_label
                )
                self._loc_write(self._loc_probe_lines(report, adapter_name, existing_lines))
                if getattr(report, "needs", None):
                    self.after(120, lambda r=root, n=list(report.needs): self._loc_offer_missing_deps(r, n))
            self._loc_refresh_run_state()
        elif kind == "loc_analyze_done":
            self._localizing = False
            if not self._loc_alive():
                return
            self._loc_set_busy(False)
            wb = payload
            lines: list[tuple[str, str]] = [
                ("head", f"剧本预分析完成：{wb.summary()}"),
                ("ok", f"设定集文件：{wb.path}"),
            ]
            if wb.worldview:
                lines.append(("", f"世界观：{wb.worldview}"))
            if wb.style:
                lines.append(("", f"文风建议：{wb.style}"))
            for note in wb.notes:
                lines.append(("dim", f"· {note}"))
            lines.append(("dim", "下次点击「开始汉化」时，摆渡会自动加载这份设定集以统一人物称谓与专有名词。"))
            self._loc_write(lines)
            self._loc_status_var.set("剧本预分析完成")
            if wb.path and messagebox.askyesno("预分析完成", f"{wb.summary()}\n\n是否现在打开 ferry_worldbook.json 查看/微调？", parent=self):
                try:
                    os.startfile(str(wb.path))
                except OSError:
                    pass
        elif kind == "loc_extract_done":
            self._localizing = False
            if not self._loc_alive():
                return
            proj, tsv_path, out_dir, probe_summary = payload
            self._loc_set_busy(False)
            if proj is None or tsv_path is None:
                self._loc_progress_busy(False, fill_done=False)
                self._loc_status_var.set("未提取出可翻译条目")
                messagebox.showinfo("提取文本（自翻导出）", f"尚未提取出可翻译条目：\n{probe_summary}", parent=self)
            else:
                self._loc_progress_busy(False, fill_done=True)
                lines = [
                    ("head", f"文本提取完成（0 Token）：{proj.summary()}"),
                    ("ok", f"· 导出目录：{out_dir}"),
                    ("ok", f"· 自翻表格（TSV，改 dst 列即可）：{tsv_path}"),
                    ("ok", f"· 双语工程表（JSON，改 \"dst\" 字段即可）：{proj.path}"),
                    ("dim", "提示：未自动打开文件；可按上方路径自行用习惯的编辑器/Excel 打开，或点击下方「打开导出目录」。"),
                    ("dim", "编辑保存后，直接点击「② 自翻回录重打包」即可写回游戏并重打包！"),
                ]
                self._loc_write(lines)
                self._loc_out_dir = out_dir
                self._loc_open_button.configure(state="normal", text="打开导出目录")
                self._loc_status_var.set(f"已提取文本 → {tsv_path}")
        elif kind == "loc_extract_error":
            self._localizing = False
            if self._loc_alive():
                self._loc_progress_busy(False, fill_done=False)
                self._loc_set_busy(False)
                self._loc_status_var.set("提取文本失败")
                self._loc_write([("bad", "提取文本失败"), ("", str(payload))])
            messagebox.showerror("提取文本", str(payload), parent=self)
        elif kind == "loc_dep_progress":
            if self._loc_alive():
                dep_name, done, total = payload
                pct = done * 100 // max(1, total)
                self._loc_progress.configure(maximum=max(1, total), value=done)
                self._loc_status_var.set(f"正在下载 {dep_name}…… {pct}%")
        elif kind == "loc_dep_done":
            if not self._loc_alive():
                return
            root, ok, message = payload
            self._loc_set_busy(False)
            self._loc_write([("ok" if ok else "bad", message)])
            self._loc_status_var.set("工具安装完成，正在重新检测…" if ok else "工具安装失败")
            if ok:
                self._loc_start_probe()
            else:
                messagebox.showerror("工具安装失败", message, parent=self)
        elif kind == "loc_progress":
            if not self._loc_alive():
                return
            done, total, modid = payload
            self._loc_progress.configure(maximum=max(1, total), value=done)
            self._loc_status_var.set(f"正在翻译 {modid} · {done}/{total} 条（可停止后续传）")
        elif kind == "loc_done":
            self._localizing = False
            if not self._loc_alive():
                return
            self._loc_set_busy(False)
            self._loc_show_result(payload)
            if self._active_mode == "home":
                self._home_page.refresh(core.load_localize_history())
        elif kind == "loc_error":
            self._localizing = False
            if self._loc_alive():
                self._loc_set_busy(False)
                self._loc_status_var.set("出错了")
                self._loc_write([("bad", "出错了"), ("", str(payload[1]))])
                messagebox.showerror("汉化其他游戏", str(payload[1]), parent=self._loc_win)
            else:
                messagebox.showerror("汉化其他游戏", str(payload[1]))
        elif kind == "error":
            source, message = payload if isinstance(payload, tuple) and len(payload) == 2 else ("unknown", str(payload))
            if source == "translate":
                self.translating = False
                self._last_nbt_count = 0
                self._set_translating_ui(False)
                self.progress.configure(value=0)
            elif source == "glossary":
                self.updating_glossary = False
                self.glossary_button.configure(state="normal")
            elif source == "lookup":
                self.looking_up = False
                self.lookup_button.configure(state="normal")
            elif source == "scan":
                self.scanning = False
            self.status_var.set("出错了，详情见弹窗。")
            messagebox.showerror("摆渡计划错误", core.redact_secrets(str(message)))
        else:
            core.LOG.warning("Unknown GUI queue message kind: %s", kind)


if __name__ == "__main__":
    _enable_high_dpi()
    FerryApp().mainloop()
