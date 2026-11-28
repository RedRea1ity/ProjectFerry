"""桌面工作台的导航与汉化产物索引。"""

import tkinter as tk
from pathlib import Path
from tkinter import ttk


RAIL = "#203D38"
FONT = "Microsoft YaHei UI"
PAGES = {"home": "项目工作台", "game": "游戏汉化", "mc": "模组汉化", "settings": "设置"}


class Workbench(ttk.Frame):
    def __init__(self, parent, navigate, icon, utilities):
        super().__init__(parent)
        self._rail_bg = RAIL
        self._rail_active_bg = "#C7E6D9"
        self._rail_active_fg = "#182E2B"
        self._rail_hover = "#396158"
        self._active_key = "home"

        rail = tk.Frame(self, bg=RAIL, padx=9, pady=14)
        rail.pack(side="left", fill="y")
        self._rail = rail
        self._rail_labels: list[tk.Label] = []
        if icon is not None:
            self.icon = icon
            lbl = tk.Label(rail, image=icon, bg=RAIL)
            lbl.pack(anchor="w", padx=8)
            self._rail_labels.append(lbl)
        brand = tk.Label(rail, text="摆渡计划", font=(FONT, 15, "bold"), bg=RAIL, fg="white")
        brand.pack(anchor="w", padx=6)
        self._rail_labels.append(brand)
        sub = tk.Label(rail, text="游戏本地化工作台", font=(FONT, 9), bg=RAIL, fg="#B7D4C9")
        sub.pack(anchor="w", padx=8, pady=(0, 20))
        self._sub_label = sub
        self._rail_labels.append(sub)
        self.nav = {}
        self.util_buttons: dict[str, tk.Button] = {}
        self._rail_sep: tk.Frame | None = None

        def button(title, callback):
            return tk.Button(rail, text=title, command=callback, anchor="w", width=11,
                             font=(FONT, 10), bg=RAIL, fg="white", relief="flat", bd=0,
                             activebackground="#396158", activeforeground="white", cursor="hand2",
                             highlightcolor="#E3CE81", highlightbackground=RAIL,
                             highlightthickness=1, padx=12, pady=5)

        for key, title in PAGES.items():
            if key == "settings":
                self._rail_sep = tk.Frame(rail, bg="#49675F", height=1)
                self._rail_sep.pack(fill="x", padx=6, pady=10)
            self.nav[key] = button(title, lambda k=key: navigate(k))
            self.nav[key].pack(fill="x", pady=1)
        for title, callback in reversed(utilities):
            btn = button(title, callback)
            btn.pack(side="bottom", fill="x")
            self.util_buttons[title] = btn
        main = ttk.Frame(self, padding=(16, 12))
        main.pack(side="left", fill="both", expand=True)
        self.title = tk.StringVar(self)
        self.hint = tk.StringVar(self)
        ttk.Label(main, textvariable=self.title, font=(FONT, 17, "bold")).pack(anchor="w")
        hint = ttk.Label(main, textvariable=self.hint, style="Muted.TLabel")
        hint.pack(fill="x", pady=(2, 9))
        hint.bind("<Configure>", lambda e: hint.configure(wraplength=max(100, e.width)))
        self.body = ttk.Frame(main)
        self.body.pack(fill="both", expand=True)

    def select(self, key, hint):
        key = "settings" if key == "engine" else key
        self._active_key = key
        self.title.set(PAGES[key])
        self.hint.set(hint)
        for name, button in self.nav.items():
            button.configure(
                bg=self._rail_active_bg if name == key else self._rail_bg,
                fg=self._rail_active_fg if name == key else "white",
            )

    def apply_theme(self, palette: dict) -> None:
        self._rail_bg = palette["rail"]
        self._rail_active_bg = palette["rail_active_bg"]
        self._rail_active_fg = palette["rail_active_fg"]
        self._rail_hover = palette["rail_hover"]
        self._rail.configure(bg=self._rail_bg)
        for lbl in self._rail_labels:
            lbl.configure(bg=self._rail_bg)
        self._sub_label.configure(fg=palette["rail_sub"])
        if self._rail_sep is not None:
            self._rail_sep.configure(bg=palette["rail_sep"])
        for name, btn in self.nav.items():
            btn.configure(
                bg=self._rail_active_bg if name == self._active_key else self._rail_bg,
                fg=self._rail_active_fg if name == self._active_key else "white",
                activebackground=self._rail_hover,
                highlightbackground=self._rail_bg,
            )
        for btn in self.util_buttons.values():
            btn.configure(
                bg=self._rail_bg,
                fg="white",
                activebackground=self._rail_hover,
                highlightbackground=self._rail_bg,
            )


class ProjectIndex(ttk.Frame):
    def __init__(self, parent, actions, open_output, on_remove=None, on_clear=None):
        super().__init__(parent)
        self.open_output = open_output
        self.on_remove = on_remove
        self.on_clear = on_clear
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(6, 20))
        for column, (title, detail, commands) in enumerate(actions):
            bar.columnconfigure(column, weight=1, uniform="source")
            group = ttk.Frame(bar)
            group.grid(row=0, column=column, sticky="nsew", padx=(0, 12))
            ttk.Label(group, text=title, font=(FONT, 12, "bold")).pack(anchor="w")
            ttk.Label(group, text=detail, style="Muted.TLabel").pack(anchor="w", pady=(2, 8))
            buttons = ttk.Frame(group)
            buttons.pack(anchor="w")
            for label, command in commands:
                ttk.Button(buttons, text=label, command=command).pack(side="left", padx=(0, 6))
        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 6))
        ttk.Label(header, text="最近的游戏汉化产物", font=(FONT, 11, "bold")).pack(side="left")
        self.clear_button = ttk.Button(header, text="清理历史", command=self._clear)
        self.clear_button.pack(side="right")
        self.remove_button = ttk.Button(header, text="移除选中", command=self._remove)
        self.remove_button.pack(side="right", padx=(0, 6))
        self.open_button = ttk.Button(header, text="打开产物目录", command=self._open)
        self.open_button.pack(side="right", padx=(0, 6))
        box = tk.Frame(self, bg="white", highlightthickness=1, highlightbackground="#CFDAD6")
        box.pack(fill="both", expand=True)
        self._box = box
        self.table = ttk.Treeview(box, columns=("name", "engine", "mode", "date"), show="headings", height=5, selectmode="browse")
        for key, title, width in (("name", "游戏", 210), ("engine", "适配器", 110), ("mode", "写入方式", 96), ("date", "登记日期", 110)):
            self.table.column(key, width=width, minwidth=50)
            self.table.heading(key, text=title, anchor="w")
        scroll = ttk.Scrollbar(box, command=self.table.yview)
        scroll.pack(side="right", fill="y")
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(fill="both", expand=True)
        self.empty = tk.Label(box, text="暂无游戏汉化产物", bg="white", fg="#526761", font=(FONT, 10))
        self.paths: dict[str, Path] = {}
        self.table.bind("<<TreeviewSelect>>", self._on_select)
        self.table.bind("<Double-1>", lambda e: self._open() if self.table.identify_row(e.y) else None)
        self.table.bind("<Return>", lambda _: self._open())
        self.table.bind("<Delete>", lambda _: self._remove())

    def _on_select(self, _event=None):
        state = ["!disabled"] if self.table.selection() else ["disabled"]
        self.open_button.state(state)
        self.remove_button.state(state)

    def refresh(self, records):
        self.table.delete(*self.table.get_children())
        self.paths = {}
        for rec in records:
            path = Path(rec.play_path)
            item = self.table.insert("", "end", values=(rec.label or path.name, rec.engine or "未记录",
                                     "原地写入" if rec.mode == "in_place" else "汉化副本", rec.created[:10] or "未记录"))
            self.paths[item] = path
        self.open_button.state(["disabled"])
        self.remove_button.state(["disabled"])
        self.clear_button.state(["!disabled"] if self.paths else ["disabled"])
        if self.paths:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.5, anchor="center")

    def _open(self):
        selected = self.table.selection()
        if selected and selected[0] in self.paths:
            self.open_output(self.paths[selected[0]])

    def _remove(self):
        selected = self.table.selection()
        if selected and selected[0] in self.paths and callable(self.on_remove):
            self.on_remove(self.paths[selected[0]])

    def _clear(self):
        if self.paths and callable(self.on_clear):
            self.on_clear()

    def apply_theme(self, palette: dict) -> None:
        self._box.configure(bg=palette["card"], highlightbackground=palette["border"])
        self.empty.configure(bg=palette["card"], fg=palette["muted"])
