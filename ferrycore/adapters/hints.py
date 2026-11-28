"""认不出引擎时，把现场原样摊开。

批量试游戏时最常见的结局就是「没有适配器认得它」。只回一句「认不出」信息量
为零：既不能让人判断值不值得等，也不能让我知道下一个适配器该先加谁。

所以这里不猜，只做一件事 —— **把目录里能定引擎的东西列出来**：

    scan_hints(root) -> list[str]

只看顶层和一层子目录，绝不递归整棵树（游戏目录动辄几个 GB）。这一层的
信息已经足够把九成引擎归位：nw.pak 就是 nw.js，Game.rgss3a 就是 RPG Maker VX Ace。
"""

from __future__ import annotations

import os
from pathlib import Path

from .container import locate_appended_zip

# (引擎, 命中任意一项即算)。名字一律小写比较；以 "*." 开头表示按后缀匹配。
_MARKERS: list[tuple[str, tuple[str, ...]]] = [
    ("nw.js / Chromium 应用壳（Tyrano、RPG Maker MV 都用它）", ("nw.pak", "icudtl.dat", "package.nw", "nw.dll", "ffmpegsumo.dll")),
    ("Electron", ("app.asar", "electron.exe", "electron.asar")),
    ("Ren'Py", ("renpy.exe", "renpy", "*.rpy", "*.rpa", "*.rpyc")),
    ("Unity", ("unityplayer.dll", "gameassembly.dll", "*.assets", "level0")),
    ("Godot", ("*.pck", "godot.exe", "*.godot")),
    ("Kirikiri（吉里吉里）", ("*.xp3", "krkrz.exe", "tvpwin32.exe")),
    ("WOLF RPG Editor", ("data.wolf", "basicdata", "gamedata")),
    ("RPG Maker XP / VX / VX Ace", ("game.rgssad", "game.rgss2a", "game.rgss3a", "rgss300.dll", "rgss202e.dll", "rgss104e.dll")),
    ("RPG Maker 2000 / 2003", ("rpg_rt.exe", "rpg_rt.ini")),
    ("GameMaker", ("data.win", "gmlive", "gamemaker")),
    ("TyranoScript", ("tyrano",)),
    ("NScripter", ("nscript.dat", "*.nsa", "*.ns2", "*.nbz")),
    ("Siglus / BGI / Artemis", ("*.pck", "*.arc", "*.pfs")),
    ("LiveMaker", ("livemaker.exe", "*.lsb")),
    ("HSP（Hot Soup Processor）", ("hsp3imp.dll", "hsp3.exe", "*.ax", "*.dpm")),
    ("Flash / AIR", ("*.swf", "adobe air", "*.swc")),
    ("Adventure Game Studio (AGS)", ("acsetup.cfg", "*.tra", "ac2game.dta", "speech.vox", "music.vox")),
    ("Galgame 老引擎（SystemNNN / CatSystem）", ("*.pac", "cs2.exe", "system40.exe")),
    ("Leaf Visual Novel System (LVNS / To Heart 等)", ("lvns3scn.pak", "lvns3dat.pak", "lvns3.ex", "lvns3.exe")),
    ("PC 光盘镜像（Alcohol 120% / Nero / CloneCD，请先挂载或解包游戏目录）", ("*.mdf", "*.mds", "*.nrg", "*.ccd")),
    ("世嘉土星 Sega Saturn", ("ip.bin", "*.cue", "*.chd")),
    ("PlayStation 1 / PlayStation 2", ("system.cnf", "psx.exe", "*.cue", "*.chd")),
    ("任天堂 Nintendo 64", ("*.z64", "*.v64", "*.n64")),
    ("PC-98 磁盘镜像", ("*.hdi", "*.fdi", "*.nhd")),
]

# 这些目录名下面通常住着剧本或文本表。
_TEXT_DIR_NAMES = {
    "scenario", "scenarios", "script", "scripts", "text", "texts",
    "data", "dat", "message", "messages", "dialogue", "story", "event", "eve",
}

# 大目录只看到这个条数就够判断了，再数下去是浪费。
_SCAN_LIMIT = 4000


def _suffixes(pool: set[str]) -> set[str]:
    return {name.rsplit(".", 1)[-1] for name in pool if "." in name}


def _match(mark: str, pool: set[str], suffixes: set[str]) -> bool:
    if mark.startswith("*."):
        return mark[2:] in suffixes
    return mark in pool


def scan_hints(root: Path, max_lines: int = 10) -> list[str]:
    """返回几条人能直接看懂的线索。认不出引擎时给用户看，也贴给我看。"""
    root = Path(root)
    if not root.exists():
        return [f"{root} 不存在。"]
    if not root.is_dir():
        hints: list[str] = []
        try:
            size = root.stat().st_size
            size_desc = f"{size / 1048576:.2f} MB" if size >= 1024 * 1024 else f"{size:,} 字节"
        except OSError:
            size_desc = "大小未知"
        tail = ""
        if root.suffix.lower() == ".exe":
            try:
                span = locate_appended_zip(root)
            except OSError:
                span = None
            if span is not None:
                tail = f"，尾部追加了 zip 档案（{span.size / 1048576:.1f} MB）"
        hints.append(f"单文件目标：{root.name}（{size_desc}{tail}）")
        hints.append("当前已精准锁定该文件本身（未扫描同目录里的其他文件），但没有适配器认领它。")
        hints.append("如果它属于多文件游戏目录的一部分，请直接把整个游戏文件夹拖进窗口。")
        return hints

    try:
        top = list(os.scandir(root))
    except OSError as exc:
        return [f"读不了这个目录：{exc}"]

    names = {entry.name.lower() for entry in top}
    nested: set[str] = set()
    budget = _SCAN_LIMIT
    for entry in top:
        if budget <= 0:
            break
        if not entry.is_dir():
            continue
        try:
            for child in os.scandir(entry.path):
                nested.add(child.name.lower())
                budget -= 1
                if budget <= 0:
                    break
        except OSError:
            continue
    pool = names | nested
    suffixes = _suffixes(pool)

    hints: list[str] = []

    # 1) 可执行文件：大小 + 有没有内嵌 zip。这一条最能定性容器。
    exes = []
    for entry in top:
        low = entry.name.lower()
        if not low.endswith(".exe") or low.endswith(".cn.exe"):
            continue
        try:
            exes.append((entry.stat().st_size, entry))
        except OSError:
            continue
    exes.sort(key=lambda item: -item[0])
    for size, entry in exes[:3]:
        tail = ""
        try:
            span = locate_appended_zip(Path(entry.path))
        except OSError:
            span = None
        if span is not None:
            tail = f"，尾部追加了 zip 档案（{span.size / 1048576:.1f} MB）"
        hints.append(f"可执行文件 {entry.name}（{size / 1048576:.1f} MB{tail}）")

    # 2) 引擎标志。命中几个就报几个 —— 撞车本身也是线索。
    engine_hits = [label for label, marks in _MARKERS if any(_match(m, pool, suffixes) for m in marks)]
    if engine_hits:
        hints.append("疑似引擎：" + "、".join(engine_hits))

    # 3) 文本可能住在哪。
    texty: list[str] = []
    for entry in top:
        if not entry.is_dir() or entry.name.lower() not in _TEXT_DIR_NAMES:
            continue
        try:
            count = sum(1 for _ in os.scandir(entry.path))
        except OSError:
            count = -1
        texty.append(f"{entry.name}/（{count} 个文件）" if count >= 0 else f"{entry.name}/")
    if texty:
        hints.append("可能有文本的目录：" + "、".join(texty[:5]))

    dirs = sum(1 for entry in top if entry.is_dir())
    files = len(top) - dirs
    hints.append(f"顶层共 {files} 个文件 / {dirs} 个子目录")

    if not engine_hits:
        hints.append("没有命中任何已知引擎标志：可能是自研引擎，或游戏数据藏在更深的子目录里。")

    return hints[:max_lines]
