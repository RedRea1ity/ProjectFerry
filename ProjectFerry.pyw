"""摆渡计划 / ProjectFerry: 双击启动的桌面界面。"""
from __future__ import annotations

import os
import queue
import re
import threading
import time
import traceback
import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import mc_ai_translator as core


# 可写文件放 core.app_dir()（打包后=exe 目录）；图标是只读资源，放脚本/解压目录旁。
WHITELIST_FILE = core.app_dir() / "ferry_whitelist.json"
UI_STATE_FILE = core.app_dir() / "ferry_ui.json"
ICON_FILE = Path(__file__).with_name("ferry_icon.png")

APP_NAME = "ProjectFerry"
APP_DISPLAY = "摆渡计划"
APP_VERSION = "1.0.0"
APP_AUTHOR = "红现实"
APP_LICENSE = "MIT License"
APP_SLOGAN = "人无语言则茫然无依，故为摆渡。"
PROJECT_URL = "https://github.com/RedRea1ity/-_-ai_-"

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

# C：筛选档位（顺序即下拉框顺序）。
TABLE_FILTERS = ["全部", "只看有缺口", "只看失败", "只看含人工汉化", "只看疑似硬编码", "只看已完成"]


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

    def __init__(self, parent, text: str = "", command=None, fill: str = "#185FA5", fg: str = "#FFFFFF",
                 font=("Microsoft YaHei UI", 10, "bold"), padx: int = 16, pady: int = 5,
                 radius: int = 8, state: str = "normal", **kwargs):
        try:
            background = ttk.Style(parent).lookup("TFrame", "background") or "#F0F0F0"
        except Exception:
            background = "#F0F0F0"
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
        super().__init__(parent, width=self._width, height=self._height, highlightthickness=0, bd=0, bg=background, **kwargs)
        self.configure(cursor="hand2")
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        disabled = self._state == "disabled"
        base = "#9AA0A6" if disabled else self._fill
        color = _shade(base, 0.88) if (self._hover and not disabled) else base
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
        self._draw()

    def _on_leave(self, _event: object = None) -> None:
        self._hover = False
        self._draw()

    def _on_press(self, _event: object = None) -> None:
        self._pressed = True

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


def make_action_button(parent, text: str, command, color: str, state: str = "normal") -> RoundedButton:
    return RoundedButton(parent, text=text, command=command, fill=color, state=state)


STATUS_SYMBOLS = {
    "complete": "✓", "done": "✓", "partial": "◐", "pending": "○",
    "community": "◈", "reverted": "⊘", "uninstalled": "⊗", "failed": "✗",
}
STATUS_TAGS = {
    "complete": "status_complete", "done": "status_done", "partial": "status_partial",
    "pending": "status_pending", "community": "status_community", "reverted": "status_reverted",
    "uninstalled": "status_uninstalled", "failed": "failed",
}


def mini_bar(percent: int, width: int = 8) -> str:
    percent = max(0, min(100, int(percent)))
    filled = int(round(percent / 100 * width))
    return "█" * filled + "░" * (width - filled)


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


class FerryApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_DISPLAY} / {APP_NAME}")
        self._app_icon = None
        self._set_app_icon()
        self.result_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.scanning = False
        self.translating = False
        self.managing_pack = False
        self._closing = False
        self.updating_glossary = False
        self.cancel_event = threading.Event()
        self.translatable_targets: dict[str, set[str]] = {}
        self.hardcoded_results: list[tuple[str, int, list[str]]] = []
        self.row_targets: dict[str, str] = {}
        self.row_modids: dict[str, str] = {}
        self.uninstalled_by_row: set[str] = set()
        self.coverage_by_row: dict[str, str] = {}
        self.coverage_tip: tk.Toplevel | None = None
        self.coverage_hover_item = ""
        self.scan_cache: dict[str, core.ScanResult] = {}
        self._hardcoded_cache: dict[str, list[tuple[str, int, list[str]]]] = {}
        self.whitelist: set[str] = self._load_whitelist()
        self.config: dict = core.load_config()
        self._last_engine: str = str(self.config.get("engine", "mymemory"))
        self._build_ui()
        self._apply_geometry()
        self._sync_engine_ui()
        self._drain_after = self.after(100, self._drain_queue)
        self._initial_afters = [
            self.after(200, self.refresh_instances),
            self.after(400, self._maybe_show_notice),
            self.after(700, self._security_check),
        ]
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            if "vista" in style.theme_names():
                style.theme_use("vista")
        except Exception:
            pass
        # 表格文字等比放大：行高、正文字号、表头字号一起调大。
        style.configure("Treeview", rowheight=32, font=("Microsoft YaHei UI", 11))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 11, "bold"))

        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="摆渡计划 / ProjectFerry", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(root, text="只补缺口，不覆盖也不改动任何已有的人工译文；改动只发生在资源包，删除即还原。", foreground="#185FA5", wraplength=680).pack(anchor="w", pady=(1, 6))

        self.path_var = tk.StringVar()
        self.instance_paths: list[Path] = []

        instance_box = ttk.LabelFrame(root, text="Minecraft 实例")
        instance_box.pack(fill="x", pady=(0, 6))
        instance_box.columnconfigure(0, weight=1)

        inst_row = ttk.Frame(instance_box)
        inst_row.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 4))
        inst_row.columnconfigure(0, weight=1)

        self.instance_combo = ttk.Combobox(inst_row, state="readonly", width=34, height=8)
        self.instance_combo.grid(row=0, column=0, sticky="w")
        self.instance_combo.bind("<<ComboboxSelected>>", self._instance_selected)

        inst_btns = ttk.Frame(inst_row)
        inst_btns.grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Button(inst_btns, text="刷新实例", command=self.refresh_instances).pack(side="left")
        ttk.Button(inst_btns, text="扫描当前实例", command=self.start_scan).pack(side="left", padx=(6, 0))
        ttk.Button(inst_btns, text="浏览…", command=self.choose_path).pack(side="left", padx=(6, 0))
        ttk.Button(inst_btns, text="用户白名单", command=self.manage_whitelist).pack(side="left", padx=(6, 0))

        # F：下拉框只显示实例名，这里用一行实时显示完整路径，避免选错实例。
        self.instance_path_var = tk.StringVar(value="")
        ttk.Label(instance_box, textvariable=self.instance_path_var, foreground="#666666", wraplength=1180, justify="left").grid(row=1, column=0, sticky="w", padx=10, pady=(0, 6))
        self.path_var.trace_add("write", lambda *_: self.instance_path_var.set(self.path_var.get()))

        # 状态 + 游戏版本/格式合并到一行，减少一条横带。
        status_row = ttk.Frame(root)
        status_row.pack(fill="x", pady=(0, 4))
        self.status_var = tk.StringVar(value="正在准备扫描……")
        ttk.Label(status_row, textvariable=self.status_var, foreground="#185FA5", wraplength=720, justify="left").pack(side="left", fill="x", expand=True)
        self.pack_format_var = tk.StringVar(value="游戏版本待检测")
        ttk.Label(status_row, textvariable=self.pack_format_var, foreground="#666666").pack(side="right", padx=(8, 0))
        ttk.Button(status_row, text="格式…", width=6, command=self._choose_pack_format).pack(side="right", padx=(6, 0))

        toolbar = ttk.Frame(root)
        toolbar.pack(fill="x", pady=(0, 4))
        ttk.Label(toolbar, text="搜索：").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(toolbar, textvariable=self.search_var, width=22)
        self.search_entry.pack(side="left")
        self.search_var.trace_add("write", lambda *_: self._render_rows())
        # G：清除按钮。
        ttk.Button(toolbar, text="×", width=3, command=self._clear_search).pack(side="left", padx=(2, 10))
        ttk.Label(toolbar, text="显示：").pack(side="left")
        self.filter_var = tk.StringVar(value="全部")
        self.filter_combo = ttk.Combobox(toolbar, textvariable=self.filter_var, state="readonly", width=16, values=TABLE_FILTERS)
        self.filter_combo.pack(side="left")
        self.filter_combo.bind("<<ComboboxSelected>>", lambda _event: self._render_rows())
        # D：对当前筛选后可见的行做全选 / 反选。
        ttk.Button(toolbar, text="反选", command=self._invert_selection).pack(side="right")
        ttk.Button(toolbar, text="全选", command=self._select_all_visible).pack(side="right", padx=(0, 6))

        table_frame = ttk.Frame(root)
        self.show_instance_column = False
        self.sort_column = "missing"
        self.sort_desc = True
        self.all_rows: list[dict] = []
        self.tree = ttk.Treeview(table_frame, show="headings", selectmode="extended", height=10)
        self._configure_tree_columns(False)
        self.tree.tag_configure("row_even", background="#F4F7FB")
        self.tree.tag_configure("row_odd", background="#FFFFFF")
        self.tree.tag_configure("status_done", foreground="#137A3F")
        self.tree.tag_configure("status_partial", foreground="#B26A00")
        self.tree.tag_configure("status_pending", foreground="#1A1A1A")
        self.tree.tag_configure("status_community", foreground="#4C5A68")
        self.tree.tag_configure("status_reverted", foreground="#8A6D00")
        self.tree.tag_configure("status_uninstalled", foreground="#9A5A22")
        self.tree.tag_configure("status_complete", foreground="#137A3F")
        self.tree.tag_configure("status_hardcoded", foreground="#8A4F00")
        self.tree.tag_configure("failed", foreground="#C00000")
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
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        self.selection_var = tk.StringVar(value="")
        ttk.Label(table_frame, textvariable=self.selection_var, foreground="#666666", font=("Microsoft YaHei UI", 10)).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # P2-4：进度文字 + 进度条紧贴表格下方。
        self.progress_text_var = tk.StringVar(value="")
        ttk.Label(table_frame, textvariable=self.progress_text_var, foreground="#555555", font=("Microsoft YaHei UI", 10)).grid(row=3, column=0, columnspan=2, sticky="w")
        self.progress = ttk.Progressbar(table_frame, mode="determinate")
        self.progress.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(2, 0))

        # P0-3：翻译按钮从设置区移到表格下方，紧跟「看表格 → 选模组 → 点翻译」动线。
        run_bar = ttk.Frame(table_frame)
        run_bar.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(run_bar, text="翻译操作：", foreground="#555555").pack(side="left")
        self.stop_button = make_action_button(run_bar, "停止", self._stop_translate, "#C0392B", state="disabled")
        self.stop_button.pack(side="right", padx=(8, 0))
        self.translate_all_button = make_action_button(run_bar, "翻译全部待AI", self._translate_all, "#1B7F3B")
        self.translate_all_button.pack(side="right", padx=(8, 0))
        self.translate_selected_button = make_action_button(run_bar, "翻译选中模组", self._translate_selected, "#185FA5")
        self.translate_selected_button.pack(side="right")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        # P1-1：翻译设置默认折叠，只留一行摘要；低频配置不再常驻占高。
        translate_frame = ttk.LabelFrame(root, text="翻译设置")
        settings_header = ttk.Frame(translate_frame)
        settings_header.pack(fill="x", padx=10, pady=(6, 0))
        self.settings_summary_var = tk.StringVar(value="")
        ttk.Label(settings_header, textvariable=self.settings_summary_var, foreground="#555555").pack(side="left")
        self.settings_toggle_button = ttk.Button(settings_header, text="展开", command=self._toggle_settings)
        self.settings_toggle_button.pack(side="right")

        self.grid_frame = ttk.Frame(translate_frame)
        for column in (1, 3, 5):
            self.grid_frame.columnconfigure(column, weight=1)

        ttk.Label(self.grid_frame, text="翻译引擎：").grid(row=0, column=0, sticky="w", pady=3)
        self.category_var = tk.StringVar()
        self.category_combo = ttk.Combobox(self.grid_frame, textvariable=self.category_var, state="readonly", width=15, values=list(FREE_ENGINES.values()) + [CUSTOM_CATEGORY_LABEL])
        self.category_combo.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=3)
        self.category_combo.bind("<<ComboboxSelected>>", self._engine_selected)
        ttk.Label(self.grid_frame, text="兼容接口：").grid(row=0, column=2, sticky="w", padx=(0, 4), pady=3)
        self.provider_var = tk.StringVar()
        self.provider_combo = ttk.Combobox(self.grid_frame, textvariable=self.provider_var, state="readonly", width=18, values=list(CUSTOM_ENGINES.values()))
        self.provider_combo.grid(row=0, column=3, sticky="ew", padx=(0, 10), pady=3)
        self.provider_combo.bind("<<ComboboxSelected>>", self._provider_selected)
        ttk.Label(self.grid_frame, text="预设：").grid(row=0, column=4, sticky="w", padx=(0, 4), pady=3)
        self.profile_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(self.grid_frame, textvariable=self.profile_var, state="readonly")
        self.profile_combo.grid(row=0, column=5, sticky="ew", pady=3)
        self.profile_combo.bind("<<ComboboxSelected>>", self._profile_selected)

        # P0-2：API Key 默认掩码，右侧「显示」按钮切换明文（只改显示，不改存储）。
        ttk.Label(self.grid_frame, text="API Key：").grid(row=1, column=0, sticky="w", pady=3)
        self.api_key_var = tk.StringVar()
        key_frame = ttk.Frame(self.grid_frame)
        key_frame.grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=3)
        key_frame.columnconfigure(0, weight=1)
        self.api_key_entry = ttk.Entry(key_frame, textvariable=self.api_key_var, show="•")
        self.api_key_entry.grid(row=0, column=0, sticky="ew")
        self.api_key_show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(key_frame, text="显示", variable=self.api_key_show_var, command=self._toggle_api_key_visibility).grid(row=0, column=1, padx=(4, 0))
        ttk.Label(self.grid_frame, text="Base URL：").grid(row=1, column=2, sticky="w", padx=(0, 4), pady=3)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(self.grid_frame, textvariable=self.url_var)
        self.url_entry.grid(row=1, column=3, sticky="ew", padx=(0, 10), pady=3)
        ttk.Label(self.grid_frame, text="模型：").grid(row=1, column=4, sticky="w", padx=(0, 4), pady=3)
        self.model_var = tk.StringVar()
        self.model_entry = ttk.Entry(self.grid_frame, textvariable=self.model_var)
        self.model_entry.grid(row=1, column=5, sticky="ew", pady=3)

        actions = ttk.Frame(self.grid_frame)
        actions.grid(row=2, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="保存预设", command=self._save_profile).pack(side="left")
        ttk.Button(actions, text="删除预设", command=self._delete_profile).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="保存设置", command=self._save_settings).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="测试连接", command=self._test_connection).pack(side="left", padx=(6, 0))
        self.glossary_button = ttk.Button(actions, text="更新术语表", command=self._update_glossary)
        self.glossary_button.pack(side="left", padx=(6, 0))
        self.fill_var = tk.BooleanVar(value=bool(self.config.get("fill_community", False)))
        ttk.Checkbutton(actions, text="填补人工汉化缺失", variable=self.fill_var, command=self._refine_changed).pack(side="left", padx=(12, 0))
        # P0-1：并发只在设置弹窗里改，主界面给一个入口按钮，避免两个控件互相覆盖。
        ttk.Button(actions, text="并发 / 模型池…", command=self._show_settings).pack(side="right")

        self.refine_var = tk.BooleanVar(value=bool(self.config.get("refine_community", False)))
        ttk.Checkbutton(self.grid_frame, text="参考人工汉化语料补全缺失 key（不改原译文）", variable=self.refine_var, command=self._refine_changed).grid(row=3, column=0, columnspan=6, sticky="w", pady=(6, 0))
        self.community_var = tk.BooleanVar(value=bool(self.config.get("community_enabled", True)))
        ttk.Checkbutton(self.grid_frame, text="联网查询社区汉化（命中后 AI 只补缺口）", variable=self.community_var).grid(row=4, column=0, columnspan=6, sticky="w")

        self.concurrency_var = tk.IntVar(value=int(self.config.get("concurrency", 1)))
        self.settings_expanded = bool(self.config.get("settings_expanded", False))
        self._apply_settings_visibility()

        bottom = ttk.Frame(root)
        bottom.pack(side="bottom", fill="x", pady=(6, 0))
        bottom.columnconfigure(0, weight=1)
        # 提示与按钮分成两行，避免并排相加把窗口最小宽度顶得很大。
        ttk.Label(bottom, text="提示：Ctrl 多选；双击翻译该模组；右键可删除译文 / 还原英文 / 重翻 / 跳过词条 / 安装人工汉化包。", foreground="#666666", wraplength=520).grid(row=0, column=0, sticky="w")
        btn_bar = ttk.Frame(bottom)
        btn_bar.grid(row=1, column=0, sticky="e", pady=(4, 0))
        ttk.Button(btn_bar, text="打开项目目录", command=self.open_project).pack(side="right")
        ttk.Button(btn_bar, text="关于", command=self._show_about).pack(side="right", padx=(6, 0))
        ttk.Button(btn_bar, text="设置", command=self._show_settings).pack(side="right", padx=(6, 0))
        self.quality_button = ttk.Button(btn_bar, text="质量检查", command=self._show_quality)
        self.quality_button.pack(side="right", padx=(6, 0))
        # P1-5：汉化限制与实例无关，移到此处与质量检查 / 日志 / 设置 / 关于同排。
        self.limits_button = ttk.Button(btn_bar, text="汉化限制", command=self._show_limits, state="disabled")
        self.limits_button.pack(side="right", padx=(6, 0))
        ttk.Button(btn_bar, text="日志", command=self._show_log).pack(side="right", padx=(6, 0))

        # 先预留底部栏和翻译区（从下往上），再让表格占用剩余空间；
        # 高度不足时只压缩表格，翻译按钮、进度条和底部按钮始终可见。
        translate_frame.pack(side="bottom", fill="x", pady=(10, 0))
        table_frame.pack(fill="both", expand=True)

    # ---------- P1-2 / P1-3 / P1-4 / P1-6：表格列、渲染、搜索、筛选、排序 ----------

    def _toggle_api_key_visibility(self) -> None:
        self.api_key_entry.configure(show="" if self.api_key_show_var.get() else "•")

    def _toggle_settings(self) -> None:
        self.settings_expanded = not self.settings_expanded
        self.config["settings_expanded"] = self.settings_expanded
        core.save_config(self.config)
        self._apply_settings_visibility()

    def _apply_settings_visibility(self) -> None:
        if getattr(self, "settings_expanded", False):
            self.grid_frame.pack(fill="x", padx=10, pady=6)
            self.settings_toggle_button.configure(text="收起")
        else:
            self.grid_frame.pack_forget()
            self.settings_toggle_button.configure(text="展开")
        self._update_settings_summary()

    def _update_settings_summary(self) -> None:
        engine = self._current_engine()
        model_field = ENGINE_MODEL_FIELDS.get(engine, "")
        model = self.model_var.get().strip() or str(self.config.get(model_field, "")) or "(默认模型)"
        try:
            concurrency = int(self.concurrency_var.get())
        except Exception:
            concurrency = 1
        self.settings_summary_var.set(f"引擎：{self._engine_label(engine)} · 模型：{model} · 并发 {concurrency}")

    def _configure_tree_columns(self, show_instance: bool) -> None:
        columns = ["modid", "complete", "status", "missing", "ai", "community"]
        headings = {"modid": "模组", "complete": "完成度", "status": "状态", "missing": "缺", "ai": "AI", "community": "人工"}
        anchors = {"modid": "w", "complete": "w", "status": "w", "missing": "center", "ai": "center", "community": "center"}
        widths = {"modid": 320 if not show_instance else 260, "complete": 132, "status": 180, "missing": 64, "ai": 56, "community": 78}
        if show_instance:
            columns.append("instance")
            headings["instance"] = "实例"
            anchors["instance"] = "w"
            widths["instance"] = 150
        self.tree.configure(columns=columns)
        for col in columns:
            self.tree.column(col, width=widths[col], minwidth=40, anchor=anchors[col], stretch=col in ("modid", "status", "instance"))
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

    @staticmethod
    def _mini_bar(percent: float, width: int = 5) -> str:
        filled = max(0, min(width, int(round(percent * width))))
        return "▉" * filled + "░" * (width - filled)

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
            return row["missing"] == 0 and not row["uninstalled"] and not row["hardcoded"]
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
        # A-1：状态列显示完整 label（含硬编码 / 手册待翻 / 上次失败标记），加符号前缀。
        status_text = f"{row['symbol']} {row.get('label') or row['short']}".strip()
        values: list = [row["modid"], row["complete_text"], status_text, row["missing"], row["ai"], row["community"]]
        if self.show_instance_column:
            values.append(row["instance"])
        return tuple(values)

    def _render_rows(self) -> None:
        show_instance = len({r["instance_path"] for r in self.all_rows if not r["placeholder"]}) > 1
        if show_instance != self.show_instance_column:
            self._configure_tree_columns(show_instance)
        query = self.search_var.get().strip().lower()
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

    def _mark_hardcoded_row(self, row: dict) -> None:
        """后台硬编码检测结果回来后，给已显示的行补上标记。"""
        if row.get("hardcoded"):
            return
        row["hardcoded"] = True
        label = row.get("label", "")
        if "硬编码" not in label:
            row["label"] = f"{label}·疑似硬编码文本" if label else "疑似硬编码文本"
        if row["tag"] not in ("failed", "status_uninstalled", "status_reverted"):
            row["symbol"] = "⚠"
            row["short"] = "含硬编码文本" if row["missing"] == 0 else f"含硬编码·缺{row['missing']}"
            row["tag"] = "status_hardcoded"

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

    def _on_ctrl_a(self, _event: object = None) -> str:
        widget = self.focus_get()
        if isinstance(widget, (ttk.Entry, tk.Entry)):
            try:
                widget.select_range(0, "end")
            except tk.TclError:
                pass
            return "break"
        self._select_all_visible()
        return "break"

    def _on_rescan_key(self, _event: object = None) -> str:
        self.start_scan()
        return "break"

    def _on_escape_key(self, _event: object = None) -> str:
        if self.search_var.get():
            self.search_var.set("")
        else:
            selection = self.tree.selection()
            if selection:
                self.tree.selection_remove(*selection)
        return "break"

    # ---------- 扫描结果 -> 行数据 ----------

    def _status_visual(self, st, is_uninstalled: bool, failed_count: int, hardcoded: bool, patch_pending: bool) -> tuple[str, str, str]:
        if failed_count:
            return "✗", f"失败{failed_count}条", "failed"
        if is_uninstalled:
            return "⊗", "已卸载", "status_uninstalled"
        if st.fully_reverted:
            return "⊘", "已还原", "status_reverted"
        # A-2：含硬编码文本时单独成状态，避免掉进「待翻·缺0」这种自相矛盾的显示。
        if hardcoded:
            return "⚠", ("含硬编码文本" if st.missing == 0 else f"含硬编码·缺{st.missing}"), "status_hardcoded"
        if st.fully_translated:
            return "✓", "完成", "status_complete"
        if st.has_community:
            short = "人工·完整" if st.fully_community else f"人工·缺{st.missing}"
            return "◈", short + ("·手册" if patch_pending else ""), "status_community"
        if st.ai_keys > 0 and st.missing > 0:
            return "◐", f"部分·缺{st.missing}", "status_partial"
        return "○", f"待翻·缺{st.missing}", "status_pending"

    def _make_row(self, st, label: str, coverage: str, instance: Path, is_uninstalled: bool, failed_count: int, patch_pending: bool, hardcoded: bool) -> dict:
        # A-1：保证三个关键标记一定出现在主表状态文字里（调用方已拼则跳过）。
        if hardcoded and "硬编码" not in label:
            label = f"{label}·疑似硬编码文本" if label else "疑似硬编码文本"
        if patch_pending and "手册" not in label:
            label = f"{label}·手册待翻" if label else "手册待翻"
        if failed_count and "失败" not in label:
            label = f"{label}·上次失败{failed_count}条" if label else f"上次失败{failed_count}条"
        symbol, short, tag = self._status_visual(st, is_uninstalled, failed_count, hardcoded, patch_pending)
        total = int(st.total_keys)
        percent = 0.0 if is_uninstalled or total <= 0 else (total - int(st.missing)) / total
        percent = max(0.0, min(1.0, percent))
        complete_text = "" if total <= 0 else f"{self._mini_bar(percent)} {int(round(percent * 100))}%"
        return {
            "modid": st.modid,
            "label": label,
            "hardcoded": bool(hardcoded),
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
            "placeholder": False,
        }

    def _placeholder_row(self, instance: Path) -> dict:
        return {
            "modid": PLACEHOLDER_MODID, "label": "", "hardcoded": False, "missing": 0, "ai": 0, "community": 0, "total": 0,
            "percent": 0.0, "complete_text": "", "symbol": "", "short": "无待汉化模组", "tag": "status_pending",
            "coverage": "", "instance": instance.name, "instance_path": str(instance),
            "uninstalled": False, "failed": False, "patch_pending": False, "has_community": False, "placeholder": True,
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
        self._closing = True
        for after_id in [getattr(self, "_drain_after", None), *getattr(self, "_initial_afters", [])]:
            if after_id:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass
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
        image.put("#185FA5", to=(0, 0, size, size))
        image.put("#D6E6F7", to=(29, 8, 32, 44))
        for y in range(10, 42):
            width = int((y - 10) * 22 / 32)
            image.put("#FFFFFF", to=(32, y, 32 + width, y + 1))
        for i in range(10):
            image.put("#FFFFFF", to=(12 + i, 44 + i, 52 - i, 45 + i))
        image.put("#D6E6F7", to=(8, 56, 56, 58))
        return image

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
        # 行高受 DPI 缩放影响，这里实测而不是写死像素值。
        self.update_idletasks()
        base_rows = int(self.tree.cget("height"))
        height_a = self.winfo_reqheight()
        self.tree.configure(height=base_rows + 5)
        self.update_idletasks()
        height_b = self.winfo_reqheight()
        row_px = max(1.0, (height_b - height_a) / 5.0)
        overhead = height_a - base_rows * row_px
        # 默认窗口只占屏幕一部分，不再一开就铺满整屏。
        target_h = min(max_h, 720)
        rows = max(6, min(18, int((target_h - overhead) // row_px)))
        self.tree.configure(height=rows)
        self.update_idletasks()
        req_w = self.winfo_reqwidth()
        req_h = self.winfo_reqheight()
        saved = self._parse_geometry(self._load_ui_state().get("geometry"))
        if saved is not None:
            width, height, sx, sy = saved
            width = min(max(width, req_w), max_w)
            height = min(max(height, req_h), max_h)
            x = work_x + (work_w - width) // 2 if sx is None else min(max(sx, work_x), work_x + work_w - width)
            y = work_y + (work_h - height) // 4 if sy is None else min(max(sy, work_y), work_y + work_h - height)
        else:
            width = min(max(960, req_w + 24), max_w)
            height = min(max(640, req_h), max_h)
            x = work_x + max(0, (work_w - width) // 2)
            y = work_y + max(0, (work_h - height) // 4)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min(max(req_w, 860), max_w), min(540, max_h))
        # 只有连可用区域都装不下时才最大化，避免一开就很大。
        if req_w > max_w or req_h > max_h:
            try:
                self.state("zoomed")
            except Exception:
                pass

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
        config["concurrency"] = int(self.concurrency_var.get())
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
        self.status_var.set(f"预设「{name}」已保存。")

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
        self.status_var.set(f"预设「{name}」已删除。")

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
        self.status_var.set(f"设置已保存（引擎：{self.config.get('engine')}）。")

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
        settings = {**core.DEFAULT_CONFIG, **core._resolve_engine_fields(config, engine, {})}
        settings["timeout"] = 12
        settings["delay"] = 0
        self.status_var.set("正在测试翻译接口连接……")
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
        window = tk.Toplevel(self)
        window.title("运行日志")
        text = tk.Text(window, wrap="none", state="normal")
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
        tree.tag_configure("error", foreground="#C00000")
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
                    self.result_queue.put(("error", f"重翻失败：{exc}"))
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

    def _update_glossary(self) -> None:
        if self.updating_glossary:
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
            self.result_queue.put(("error", f"术语表更新失败：{exc}"))

    def _security_check(self) -> None:
        if self._closing:
            return
        warning = core.config_secret_risk()
        if warning:
            core.LOG.warning("secret-risk: %s", warning.replace("\n", " "))
            messagebox.showwarning("密钥安全提示", warning)

    def _maybe_show_notice(self) -> None:
        if self._closing or self.config.get("notice_dismissed"):
            return
        window = tk.Toplevel(self)
        window.title("汉化能力说明 - 摆渡计划")
        window.transient(self)
        body = ttk.Frame(window, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="汉化能力说明", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        ttk.Label(body, text="摆渡计划通过替换 Minecraft 语言文件（lang key）来汉化。以下内容无法通过资源包覆盖：", foreground="#333333", wraplength=510).pack(anchor="w", pady=(8, 6))
        for line in (
            "• 硬编码在模组代码里的文本（部分模组把物品备注、对话、配置界面文字直接写死在代码中）",
            "• 图片 / 纹理里的文字",
            "• 存在物品 NBT / 数据组件里的描述（lore）",
        ):
            ttk.Label(body, text=line, foreground="#555555", wraplength=510, justify="left").pack(anchor="w", pady=2)
        ttk.Label(body, text="扫描完实例后，底部「汉化限制」按钮会列出疑似无法完全汉化的模组，供你参考。", foreground="#185FA5", wraplength=510).pack(anchor="w", pady=(12, 0))
        dismiss = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="以后不再显示", variable=dismiss).pack(anchor="w", pady=(14, 0))

        def close() -> None:
            if dismiss.get():
                self.config["notice_dismissed"] = True
                core.save_config(self.config)
            window.destroy()

        ttk.Button(body, text="我知道了", command=close).pack(anchor="e", pady=(10, 0))
        self._center_over(window, 560, 300)

    def _show_limits(self) -> None:
        window = tk.Toplevel(self)
        window.title("汉化限制 - 摆渡计划")
        window.transient(self)
        body = ttk.Frame(window, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="疑似无法完全汉化的模组", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        ttk.Label(body, text="这些模组含硬编码文本（不在语言文件里），资源包无法覆盖。列表为启发式检测，可能包含少量技术字符串，仅供参考。", foreground="#555555", wraplength=600).pack(anchor="w", pady=(4, 8))
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
                for sample in samples:
                    text.insert("end", f"    {sample}\n")
                text.insert("end", "\n")
        text.configure(state="disabled")
        ttk.Button(body, text="关闭", command=window.destroy).pack(anchor="e", pady=(8, 0))
        self._center_over(window, 640, 400)

    def _show_about(self) -> None:
        window = tk.Toplevel(self)
        window.title(f"关于 {APP_NAME}")
        window.transient(self)
        body = ttk.Frame(window, padding=18)
        body.pack(fill="both", expand=True)

        header = ttk.Frame(body)
        header.pack(fill="x")
        try:
            icon = self._load_icon_image()
        except tk.TclError:
            icon = None
        if icon is not None:
            window._about_icon = icon  # 保持引用，避免被回收
            ttk.Label(header, image=icon).pack(side="left", padx=(0, 14))
        head_text = ttk.Frame(header)
        head_text.pack(side="left", fill="x", expand=True)
        ttk.Label(head_text, text=f"{APP_DISPLAY} / {APP_NAME}", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(head_text, text=f"版本 {APP_VERSION}", foreground="#666666").pack(anchor="w")
        ttk.Label(head_text, text=APP_SLOGAN, foreground="#185FA5").pack(anchor="w")
        ttk.Label(head_text, text="Where words fail, we ferry.", foreground="#888888").pack(anchor="w")

        ttk.Label(body, text="Minecraft 临时 AI 汉化工具：没有人工/官方汉化时先摆渡过去，检测到人工/官方汉化就自动让位、到岸即离。所有改动只发生在资源包，删除即还原。", foreground="#555555", wraplength=520, justify="left").pack(anchor="w", pady=(12, 6))

        def section(title: str, items: list[str]) -> None:
            ttk.Label(body, text=title, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(8, 2))
            for text in items:
                ttk.Label(body, text="• " + text, foreground="#555555", wraplength=510, justify="left").pack(anchor="w")

        section("功能要点", [
            "资源包式临时汉化：只补缺口，绝不覆盖人工 / 官方 / 社区译文",
            "多模型并发：可接入多个自建 API，按字符量平摊资费",
            "社区汉化基线：命中优先采用，AI 只补剩余缺口",
            "硬编码文本检测、质量检查、按条重翻 / 锁定 / 跳过",
            "1.6.1+ 旧版 .lang 与最新年份版本均支持，资源包格式自动探测",
        ])
        section("数据来源与致谢", [
            "Minecraft 官方语言文件（Mojang）：术语表数据来源",
            "PandaDevOfficial / Minecraft-All-Lang：Minecraft 语言文件镜像",
            "CFPAOrg / Minecraft-Mod-Language-Package：社区汉化基线（遵守其许可证与署名要求）",
            "Minecraft Wiki · Pack_format：资源包格式与版本号参考",
            "Python 标准库：本项目无第三方 Python 依赖",
        ])
        section("翻译服务", [
            "MyMemory、DeepL Free、OpenAI 兼容、Anthropic、Gemini、本地模型（Ollama / LM Studio）。",
        ])

        ttk.Separator(body).pack(fill="x", pady=(12, 8))
        ttk.Label(body, text=f"作者：{APP_AUTHOR}　·　许可证：{APP_LICENSE}", foreground="#555555").pack(anchor="w")
        ttk.Label(body, text=f"项目主页：{PROJECT_URL}", foreground="#185FA5", wraplength=520).pack(anchor="w", pady=(2, 0))
        ttk.Label(body, text="灵感来自《边狱巴士》零协汉化组。", foreground="#888888", wraplength=520).pack(anchor="w", pady=(8, 0))

        ttk.Button(body, text="关闭", command=window.destroy).pack(anchor="e", pady=(14, 0))
        self._center_over(window, 580, 560)

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
        ttk.Label(row, text="（多个批次同时翻译，加速；MyMemory 免费额度有限，建议 2~4）", foreground="#666666").pack(side="left", padx=(8, 0))

        ttk.Label(body, text="多模型并发池（可添加你自建的多个 API，并行翻译不同批次）", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 6))
        ttk.Label(body, text="每行一个模型：引擎 / 模型 / 权重 / 地址。权重越大承担越多批次，用于按字符量平摊各 API 的资费。", foreground="#666666", wraplength=610).pack(anchor="w", pady=(0, 6))
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
            self.status_var.set("设置已保存。")

        ttk.Button(body, text="保存", command=save).pack(anchor="e", pady=(12, 0))
        self._center_over(window, 660, 420)

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
        ttk.Label(frame, text="权重越大承担越多批次（如便宜 / 免费模型给 2~3），按字符量平摊资费。", foreground="#666666", wraplength=470).grid(row=5, column=0, columnspan=2, sticky="w", pady=(0, 4))

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
            self.status_var.set(f"已从预设添加 {added} 个模型到并发池，记得点“保存”。")

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
        self.instance_combo.configure(values=[f"{path.name}  —  {path}" for path in self.instance_paths])
        if self.instance_paths and select is not None:
            select = max(0, min(select, len(self.instance_paths) - 1))
            self.instance_combo.current(select)
        elif not self.instance_paths:
            self.instance_combo.set("未找到实例，请点击浏览")

    def refresh_instances(self) -> None:
        if self._closing:
            return
        self.status_var.set("正在探测本机 Minecraft 实例……")
        self._hardcoded_cache.clear()
        self._set_instance_options([])
        self.instance_combo.set("正在探测实例……")
        threading.Thread(target=self._discover_worker, daemon=True).start()

    def _discover_worker(self) -> None:
        try:
            self.result_queue.put(("instances", core.find_instance_dirs()))
        except Exception as exc:
            self.result_queue.put(("error", f"实例探测失败：{exc}"))

    def _instance_selected(self, _event: object = None) -> None:
        index = self.instance_combo.current()
        if 0 <= index < len(self.instance_paths):
            self.path_var.set(str(self.instance_paths[index]))
            self.start_scan()

    def choose_path(self) -> None:
        path = filedialog.askdirectory(title="选择 Minecraft 实例目录")
        if path:
            selected = Path(path)
            self.path_var.set(str(selected))
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
        if self.scanning or self.managing_pack:
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
        self.scan_cache.clear()
        self.selection_var.set("")
        # 路径在主线程读好再传进工作线程，避免工作线程访问 Tk 变量。
        threading.Thread(target=self._scan_worker, args=(self.fill_var.get(), self.refine_var.get(), self.path_var.get().strip()), daemon=True).start()

    def _scan_worker(self, fill_community: bool, refine_community: bool, manual: str) -> None:
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
            for instance in instances:
                mods = instance / "mods"
                packs = instance / "resourcepacks"
                scan = core.scan_inputs(mods, packs if packs.is_dir() else None)
                output_dir = Path(self.config.get("output_dir") or packs)
                pack_name = self.config.get("pack_name") or core.DEFAULT_CONFIG["pack_name"]
                pack = output_dir / pack_name
                version = core.detect_minecraft_version(instance)
                try:
                    legacy = version is not None and core.pack_format_for_version(version) <= 3
                except ValueError:
                    legacy = False
                if legacy and core.migrate_legacy_pack(pack, core.pack_format_for_version(version)):
                    scan = core.scan_inputs(mods, packs if packs.is_dir() else None)
                core.yield_to_community(scan, pack)
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
                        complete = st.fully_translated and not is_uninstalled and st.modid not in patch_pending and not is_hardcoded
                        coverage = core.translation_coverage_label(st) if complete else ""
                        rows.append(self._make_row(st, label, coverage, instance, is_uninstalled, failed_count, st.modid in patch_pending, is_hardcoded))
                        if st.modid not in uninstalled[str(instance)] and ((st.missing > 0 and (not st.has_community or fill_community or refine_community)) or st.modid in patch_pending):
                            translatable_targets.setdefault(str(instance), set()).add(st.modid)
                else:
                    rows.append(self._placeholder_row(instance))
            # 先把列表发出去（秒出），再做最慢的硬编码检测，完成后单独补标记。
            self.result_queue.put(("rows", (rows, translatable_targets, failed_modids, hardcoded_findings, scan_cache, uninstalled)))
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
            self.result_queue.put(("error", f"扫描失败：{core.redact_secrets(str(exc))}"))

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
        item = selection[0]
        modid = self.row_modids.get(item, "")
        instance_path = self.row_targets.get(item, "")
        if modid == PLACEHOLDER_MODID:
            modid = "（无待汉化模组）"
        self.selection_var.set(f"选中：{modid}    实例路径：{instance_path}")

    def _hide_coverage_tip(self, _event: object = None) -> None:
        self.coverage_hover_item = ""
        if self.coverage_tip is not None:
            self.coverage_tip.destroy()
            self.coverage_tip = None

    def _on_coverage_motion(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if column != "#2" or item not in self.coverage_by_row:
            self._hide_coverage_tip()
            return
        if item == self.coverage_hover_item:
            return
        self._hide_coverage_tip()
        self.coverage_hover_item = item
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        ttk.Label(tip, text=self.coverage_by_row[item], padding=(10, 8), justify="left", relief="solid").pack()
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
        count = self._pending_count(targets)
        if count <= 0:
            messagebox.showinfo("无需翻译", "所选模组没有待翻译的 key。")
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
        menu.add_command(label="强制还原英文（生成覆盖资源包）", command=lambda: self._remove_translation("en"))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

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
                self.result_queue.put(("error", f"重翻失败：{core.redact_secrets(str(exc))}"))
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
                    scan = core.scan_inputs(instance / "mods", packs if packs.is_dir() else None)
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
                        scan = core.scan_inputs(instance / "mods", packs if packs.is_dir() else None)
                    english = core.english_map_for(scan.english, set(modids))
                    for modid in modids:
                        if modid not in english:
                            skipped.append(modid)
                    changed.extend(core.revert_mods(pack, english, settings["pack_format"]))
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
        self._start_translate(targets)

    def _translate_all(self) -> None:
        if self.translating:
            return
        if self.scanning:
            messagebox.showinfo("提示", "正在扫描实例，请扫描完成后再开始翻译。")
            return
        targets = self.translatable_targets
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
        cached = [(self.scan_cache.get(path), modids) for path, modids in targets.items()]
        if all(scan is not None for scan, _ in cached) and not any(
            core.missing_entries(
                scan.english, scan.community, scan.ai, set(self.whitelist),
                fill_community=config["fill_community"], modids=modids,
                reverted=scan.reverted, refine_community=config["refine_community"],
            ) or core.missing_patchouli_entries(scan, modids)
            for scan, modids in cached
        ):
            message = "所选模组没有需要翻译的非空英文 key；已有汉化或 AI 已覆盖全部有效条目。"
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
        self.progress.configure(value=0, maximum=1)
        self.progress_text_var.set("准备翻译……")
        self.status_var.set("开始翻译……")
        self._set_translating_ui(True)
        threading.Thread(target=self._translate_worker, args=(targets, config, self.cancel_event), daemon=True).start()

    def _stop_translate(self) -> None:
        if self.translating:
            self.cancel_event.set()
            self.stop_button.configure(state="disabled")
            self.status_var.set("正在停止…… 当前条目完成后打包已翻译内容。")

    def _set_translating_ui(self, active: bool) -> None:
        state = "disabled" if active else "normal"
        self.translate_all_button.configure(state=state)
        self.translate_selected_button.configure(state=state)
        self.stop_button.configure(state="normal" if active else "disabled")

    def _finish_translate(self, packs: list[str], errors: list[str], cancelled: bool, usage_text: str = "") -> None:
        self.translating = False
        self._set_translating_ui(False)
        self.progress.configure(value=0)
        self.progress_text_var.set("")
        if packs:
            lines = ["已生成：" + "\n".join(packs)]
            if usage_text:
                lines.append("\n各模型用量（按字符量平摊）：\n" + usage_text)
            if errors:
                lines.append(f"\n有 {len(errors)} 个批次失败（相关 key 保留英文，下次运行会自动重试）：")
                lines.append("\n".join("· " + e for e in errors[:10]))
            if cancelled:
                lines.append("\n翻译已停止，这是不完整的包（只含已翻译的部分）。")
            self.status_var.set(f"翻译{'已停止' if cancelled else '完成'}，写入 {len(packs)} 个资源包。")
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
            self.status_var.set("没有需要翻译的 key（可能已由人工汉化覆盖，或已翻译完成）。")
        # 只有真正产生了变化（写包 / 有失败）时才自动重扫，避免“点翻译却只是在重扫”的错觉。
        if packs or errors:
            self.start_scan()

    def _translate_worker(self, targets: dict[str, set[str]], config: dict, cancel_event: threading.Event) -> None:
        try:
            packs_written: list[str] = []
            all_errors: list[str] = []
            usage: dict[str, dict[str, int]] = {}
            for instance_path, modids in targets.items():
                instance = Path(instance_path)
                mods = instance / "mods"
                packs = instance / "resourcepacks"
                scan = self.scan_cache.get(instance_path)
                if scan is None:
                    scan = core.scan_inputs(mods, packs if packs.is_dir() else None)
                settings = core.resolve_translate_settings(config, instance)
                core.yield_to_community(scan, Path(settings["output_dir"]) / settings["pack_name"])
                if settings["engine"] in core.ENGINES_WITH_KEY and not settings["api_key"]:
                    self.result_queue.put(("error", f"{settings['engine']} 引擎需要 API key。请在下方填入并保存。"))
                    return
                entries = core.missing_entries(
                    scan.english, scan.community, scan.ai, set(self.whitelist),
                    fill_community=settings["fill_community"], modids=modids,
                    reverted=scan.reverted,
                    refine_community=settings["refine_community"],
                )
                patch_entries = core.missing_patchouli_entries(scan, modids)
                baseline: dict[str, dict[str, str]] = {}
                if settings.get("community_enabled"):
                    candidate_scan = core.ScanResult(
                        english=[file for file in scan.english if file.modid in modids],
                        community=scan.community, ai=scan.ai, reverted=scan.reverted,
                    )
                    baseline = core.fetch_community_baseline(candidate_scan, config, Path(settings["cache_dir"]), modids)
                    baseline_files = [core.LangFile(modid, "zh_cn", "community-baseline", data) for modid, data in baseline.items()]
                    entries = core.entries_with_baseline(scan, baseline, set(self.whitelist), modids, settings["fill_community"], settings["refine_community"])
                else:
                    baseline_files = []
                entries.extend(patch_entries)
                entries = core.skip_ignored_entries(entries, config)
                if not entries and not baseline:
                    continue

                def progress(done: int, total: int, modid: str) -> None:
                    self.result_queue.put(("progress", (done, total, modid)))

                corpus = core.community_corpus(scan.english, scan.community + baseline_files) if settings["refine_community"] else None
                translations, errors, failed = core.translate_entries(entries, settings, progress=progress, cancel_event=cancel_event, usage=usage, corpus=corpus) if entries else ({}, [], {})
                all_errors.extend(errors)
                core.update_progress(translations, failed)
                if translations or baseline:
                    pack = Path(settings["output_dir"]) / settings["pack_name"]
                    lang_translations = {modid: dict(data) for modid, data in baseline.items()}
                    for modid, data in translations.items():
                        lang_translations.setdefault(modid, {}).update({key: value for key, value in data.items() if not key.startswith("patchouli:")})
                    merged = core.merge_pack_translations(pack, lang_translations, scan.community)
                    pages = core.load_pack_patchouli(pack)
                    pages.update(core.translated_patchouli_files(scan, patch_entries, translations))
                    sources = {modid: {key: "community" for key in data} for modid, data in baseline.items()}
                    core.write_pack(pack, merged, settings["pack_format"], core.load_pack_reverted(pack), patchouli=pages, sources=core.merged_sources(pack, merged, sources))
                    packs_written.append(str(pack))
                if cancel_event.is_set():
                    break
            usage_text = core.format_usage(usage)
            if cancel_event.is_set():
                self.result_queue.put(("cancelled", (packs_written, all_errors, usage_text)))
            else:
                self.result_queue.put(("translated", (packs_written, all_errors, usage_text)))
        except Exception as exc:
            self.result_queue.put(("error", f"翻译失败：{core.redact_secrets(str(exc))}"))

    def _drain_queue(self) -> None:
        if self._closing:
            return
        try:
            kind, payload = self.result_queue.get_nowait()
        except queue.Empty:
            self._drain_after = self.after(100, self._drain_queue)
            return
        if kind == "status":
            # 扫描过程中的进度提示；绝不能在这里重置 scanning，否则可重复触发并发扫描。
            self.status_var.set(str(payload))
        elif kind == "instances":
            self.scanning = False
            self._set_instance_options(list(payload), select=0 if payload else None)
            if self.instance_paths:
                self.path_var.set(str(self.instance_paths[0]))
                self.status_var.set(f"检测到 {len(self.instance_paths)} 个 Minecraft 实例，正在扫描当前实例……")
                self.start_scan()
            else:
                self.status_var.set("没有自动找到 Minecraft 实例。你可以点击“浏览”手动选择。")
        elif kind == "rows":
            self.scanning = False
            rows, translatable_targets, failed_modids, hardcoded, scan_cache, uninstalled = payload
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
            ai_mods = sum(1 for r in rows if not r["placeholder"] and not r["has_community"])
            com_mods = sum(1 for r in rows if not r["placeholder"] and r["has_community"])
            partial = sum(1 for r in rows if not r["placeholder"] and not r["has_community"] and r["ai"] > 0 and r["missing"] > 0)
            pending_community = max(0, sum(len(v) for v in translatable_targets.values()) - ai_mods)
            hint = ""
            if partial:
                self.translate_all_button.configure(text="继续翻译")
                hint = f"；检测到 {partial} 个模组上次未翻完"
            else:
                self.translate_all_button.configure(text="翻译全部待AI")
            if failed_modids:
                hint += f"；{len(failed_modids)} 个模组有失败记录（标红）"
            if hardcoded:
                hint += f"；{len(hardcoded)} 个模组疑似含硬编码文本（见「汉化限制」）"
            if self.refine_var.get():
                summary = f"扫描完成：{ai_mods} 个模组待 AI 翻译，{pending_community} 个模组待人工汉化精加工"
            elif self.fill_var.get():
                summary = f"扫描完成：{ai_mods} 个模组待 AI 翻译，{pending_community} 个模组待补人工汉化缺失"
            else:
                summary = f"扫描完成：{ai_mods} 个模组待 AI 翻译，{com_mods} 个已有人工汉化（自动降级）"
            self.status_var.set(f"{summary}{hint}。")
        elif kind == "empty":
            self.scanning = False
            self.status_var.set(str(payload))
        elif kind == "hardcoded":
            instance_path, namespaces, findings = payload
            namespace_set = set(namespaces)
            for row in self.all_rows:
                if row["instance_path"] == instance_path and row["modid"] in namespace_set:
                    self._mark_hardcoded_row(row)
            if findings:
                self.hardcoded_results = list(self.hardcoded_results or []) + list(findings)
                self.limits_button.configure(state="normal")
            self._render_rows()
        elif kind == "progress":
            done, total, modid = payload
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
            messagebox.showinfo("删除汉化", message + "。\n\n记得在游戏里保持本工具资源包为启用状态。")
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
            messagebox.showinfo("人工汉化包已下载", f"已安装至：\n{pack}\n\n请在 Minecraft 资源包界面启用它；如与其他资源包冲突，请调整资源包顺序。")
            self.start_scan()
        elif kind == "qc_retranslated":
            self.translating = False
            self._set_translating_ui(False)
            count, errors = payload
            messagebox.showinfo("质量检查", f"重翻 {count} 条；失败 {len(errors)} 批。")
            self.start_scan()
        elif kind == "connection_test":
            self.status_var.set(str(payload))
            messagebox.showinfo("测试连接", str(payload))
        elif kind == "glossary_updated":
            self.updating_glossary = False
            self.glossary_button.configure(state="normal")
            self.status_var.set(f"术语表已更新：{payload} 条，下次翻译自动生效。")
            messagebox.showinfo("术语表更新", f"已更新 {payload} 条术语到 user_glossary.json。")
        else:
            self.scanning = False
            self.translating = False
            self.updating_glossary = False
            self.glossary_button.configure(state="normal")
            self._set_translating_ui(False)
            self.progress.configure(value=0)
            self.status_var.set("出错了，详情见弹窗。")
            messagebox.showerror("摆渡计划错误", str(payload))
        self._drain_after = self.after(100, self._drain_queue)


if __name__ == "__main__":
    _enable_high_dpi()
    FerryApp().mainloop()
