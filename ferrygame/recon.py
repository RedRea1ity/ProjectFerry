"""未知小众游戏字节侦查台（recon）。

遇到从来没见过的猎奇引擎、单 EXE、怪后缀数据包时，先跑 `recon`：
不瞎猜「这是自定义加密解不了」，而是用字节事实说话：

1. **熵值（0~8.0）**：>7.5 是压缩或强加密；5.0~7.2 是未加密的混合二进制；<5.0 是明文脚本。
2. **PE 画字通道与代理空位**：主程序是 32 位还是 64 位？调用了哪些 GDI 画字 / 菜单 API
   （`TextOutA` / `TabbedTextOutA` / `GetGlyphOutlineA` / `ModifyMenuA`）？
   哪个系统 DLL（`msimg32.dll` / `version.dll`）可以安全替身？
3. **明文台词密度**：哪些文件里能直接榨出 Shift-JIS 剧情句和菜单词？各有多少条？
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ferrycore.adapters import pe
from ferrycore.adapters.hints import scan_hints

from .pc98 import PC98_IMAGE_SUFFIXES, parse_fat_image
from .rawbin import candidate_files, scan_sjis_strings

_GDI_TEXT_APIS = {
    "TextOutA", "TextOutW", "ExtTextOutA", "ExtTextOutW",
    "TabbedTextOutA", "TabbedTextOutW", "DrawTextA", "DrawTextW",
    "DrawTextExA", "DrawTextExW", "GetGlyphOutlineA", "GetGlyphOutlineW",
    "CreateFontA", "CreateFontIndirectA",
}

_MENU_UI_APIS = {
    "ModifyMenuA", "AppendMenuA", "InsertMenuItemA", "SetMenuItemInfoA",
    "SetWindowTextA", "SetDlgItemTextA", "MessageBoxA", "CreateWindowExA",
}


def shannon_entropy(blob: bytes, sample_limit: int = 256 * 1024) -> float:
    """计算字节香农熵（0.0 ~ 8.0）。大文件抽样头尾与中段，秒出结果。"""
    if not blob:
        return 0.0
    if len(blob) > sample_limit:
        third = sample_limit // 3
        mid = len(blob) // 2
        sample = blob[:third] + blob[mid : mid + third] + blob[-third:]
    else:
        sample = blob
    n = len(sample)
    counts = Counter(sample)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def entropy_verdict(ent: float) -> str:
    if ent >= 7.5:
        return "高熵（已压缩或强加密，不能静态直改，需走 DLL 代理或先解包）"
    if ent >= 5.5:
        return "中熵（普通编译二进制或未压缩归档，可尝试原位定长覆盖）"
    return "低熵（明文或稀疏结构，最容易静态提取回写）"


@dataclass
class FileRecon:
    rel_path: str
    size: int
    entropy: float
    story_count: int = 0
    menu_count: int = 0
    sample_lines: list[str] = field(default_factory=list)


@dataclass
class PeRecon:
    exe_name: str
    arch: str
    bits: int
    text_apis: list[str] = field(default_factory=list)
    menu_apis: list[str] = field(default_factory=list)
    safe_proxies: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class ReconReport:
    root: Path
    hints: list[str] = field(default_factory=list)
    pe_info: PeRecon | None = None
    files: list[FileRecon] = field(default_factory=list)
    pc98_files: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"=== 摆渡 · 字节侦查报告：{self.root.name} ==="]
        for h in self.hints:
            lines.append(f"  · {h}")
        if self.pe_info:
            p = self.pe_info
            lines.append(f"\n[主程序 PE 分析] {p.exe_name}（{p.arch} / {p.bits} 位）")
            lines.append("  画字 API：" + (", ".join(p.text_apis) if p.text_apis else "未见直接静态导入 GDI 画字函数"))
            lines.append("  菜单 API：" + (", ".join(p.menu_apis) if p.menu_apis else "未见静态导入菜单函数"))
            if p.safe_proxies:
                name, reason = p.safe_proxies[0]
                lines.append(f"  最佳替身 DLL：{name}（{reason}）")
            else:
                lines.append("  替身 DLL：未找到现成低风险导入，需走通用 IAT 注入")
        if self.pc98_files:
            lines.append(f"\n[PC-98 磁盘镜像] 内含 {len(self.pc98_files)} 个 FAT 文件：{', '.join(self.pc98_files[:12])}")
        if self.files:
            lines.append("\n[二进制文本密度与熵值]")
            for f in self.files[:12]:
                lines.append(
                    f"  · {f.rel_path}（{f.size / 1024:.1f} KB，熵 {f.entropy:.2f} - {entropy_verdict(f.entropy)}）"
                    f" → 剧情 {f.story_count} 条 / 菜单 {f.menu_count} 条"
                )
                for s in f.sample_lines[:2]:
                    lines.append(f"      例：{s[:60]}")
        return "\n".join(lines)


def inspect_game(root: Path) -> ReconReport:
    root = Path(root).resolve()
    base = root.parent if root.is_file() else root
    report = ReconReport(root=root, hints=scan_hints(base))

    exe_path = root if (root.is_file() and root.suffix.lower() == ".exe") else pe.exe_of(base)
    if exe_path is not None:
        table = pe.read_imports(exe_path)
        if table is not None:
            all_funcs = {fn for funcs in table.imports.values() for fn in funcs}
            proxies: list[tuple[str, str]] = []
            for dll_name in pe.proxy_candidates(table):
                ok, reason = pe.proxy_verdict(exe_path, dll_name, table)
                if ok:
                    proxies.append((dll_name, reason))
            report.pe_info = PeRecon(
                exe_name=exe_path.name,
                arch=table.arch,
                bits=table.bits,
                text_apis=sorted(all_funcs & _GDI_TEXT_APIS),
                menu_apis=sorted(all_funcs & _MENU_UI_APIS),
                safe_proxies=proxies,
            )

    # PC-98 镜像检查
    imgs = [root] if (root.is_file() and root.suffix.lower() in PC98_IMAGE_SUFFIXES) else (
        list(base.glob("*.hdi")) + list(base.glob("*.fdi")) + list(base.glob("*.nhd"))
    )
    for img in imgs[:2]:
        try:
            entries = parse_fat_image(img.read_bytes())
            report.pc98_files.extend(f"{img.name}::{e.path}" for e in entries)
        except OSError:
            pass

    for path in candidate_files(root)[:30]:
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        ent = shannon_entropy(blob)
        units = scan_sjis_strings(blob, rel_path=path.relative_to(base).as_posix())
        stories = [u for u in units if u.kind == "story"]
        menus = [u for u in units if u.kind == "menu"]
        if units or ent >= 5.0 or path.suffix.lower() in (".exe", ".dat", ".bin", ".mes"):
            report.files.append(
                FileRecon(
                    rel_path=path.relative_to(base).as_posix(),
                    size=len(blob),
                    entropy=ent,
                    story_count=len(stories),
                    menu_count=len(menus),
                    sample_lines=[u.src for u in (stories[:2] or menus[:2])],
                )
            )
    report.files.sort(key=lambda f: -(f.story_count * 2 + f.menu_count))
    return report
