"""桌面工作台的导航与汉化产物索引。"""

import tkinter as tk
from pathlib import Path
from tkinter import ttk


RAIL = "#203D38"
FONT = "Microsoft YaHei UI"
PAGES = {"home": "项目工作台", "game": "游戏汉化", "mc": "模组汉化", "engine": "翻译引擎"}


class Workbench(ttk.Frame):
    def __init__(self, parent, navigate, icon, utilities):
        super().__init__(parent)
        rail = tk.Frame(self, bg=RAIL, padx=9, pady=14)
        rail.pack(side="left", fill="y")
        if icon is not None:
            self.icon = icon
            tk.Label(rail, image=icon, bg=RAIL).pack(anchor="w", padx=8)
        tk.Label(rail, text="摆渡计划", font=(FONT, 15, "bold"), bg=RAIL, fg="white").pack(anchor="w", padx=6)
        tk.Label(rail, text="游戏本地化工作台", font=(FONT, 9), bg=RAIL, fg="#B7D4C9").pack(anchor="w", padx=8, pady=(0, 20))
        self.nav = {}

        def button(title, callback):
            return tk.Button(rail, text=title, command=callback, anchor="w", width=11,
                             font=(FONT, 10), bg=RAIL, fg="white", relief="flat", bd=0,
                             activebackground="#396158", activeforeground="white", cursor="hand2",
                             highlightcolor="#E3CE81", highlightbackground=RAIL,
                             highlightthickness=1, padx=12, pady=5)

        for key, title in PAGES.items():
            if key == "engine":
                tk.Frame(rail, bg="#49675F", height=1).pack(fill="x", padx=6, pady=10)
            self.nav[key] = button(title, lambda k=key: navigate(k))
            self.nav[key].pack(fill="x", pady=1)
        for title, callback in reversed(utilities):
            button(title, callback).pack(side="bottom", fill="x")
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
        self.title.set(PAGES[key])
        self.hint.set(hint)
        for name, button in self.nav.items():
            button.configure(bg="#C7E6D9" if name == key else RAIL,
                             fg="#182E2B" if name == key else "white")


class ProjectIndex(ttk.Frame):
    def __init__(self, parent, actions, open_output):
        super().__init__(parent)
        self.open_output = open_output
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
        self.open_button = ttk.Button(header, text="打开产物目录", command=self._open)
        self.open_button.pack(side="right")
        box = tk.Frame(self, bg="white", highlightthickness=1, highlightbackground="#CFDAD6")
        box.pack(fill="both", expand=True)
        self.table = ttk.Treeview(box, columns=("name", "engine", "mode", "date"), show="headings", height=5, selectmode="browse")
        for key, title, width in (("name", "游戏", 210), ("engine", "适配器", 110), ("mode", "写入方式", 96), ("date", "登记日期", 110)):
            self.table.column(key, width=width, minwidth=50)
            self.table.heading(key, text=title, anchor="w")
        scroll = ttk.Scrollbar(box, command=self.table.yview)
        scroll.pack(side="right", fill="y")
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(fill="both", expand=True)
        self.empty = tk.Label(box, text="暂无游戏汉化产物", bg="white", fg="#526761", font=(FONT, 10))
        self.table.bind("<<TreeviewSelect>>", lambda _: self.open_button.state(["!disabled"] if self.table.selection() else ["disabled"]))
        self.table.bind("<Double-1>", lambda e: self._open() if self.table.identify_row(e.y) else None)
        self.table.bind("<Return>", lambda _: self._open())

    def refresh(self, records):
        self.table.delete(*self.table.get_children())
        self.paths = {}
        for rec in records:
            path = Path(rec.play_path)
            item = self.table.insert("", "end", values=(rec.label or path.name, rec.engine or "未记录",
                                     "原地写入" if rec.mode == "in_place" else "汉化副本", rec.created[:10] or "未记录"))
            self.paths[item] = path
        self.open_button.state(["disabled"])
        if self.paths:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.5, anchor="center")

    def _open(self):
        selected = self.table.selection()
        if selected and selected[0] in self.paths:
            self.open_output(self.paths[selected[0]])
