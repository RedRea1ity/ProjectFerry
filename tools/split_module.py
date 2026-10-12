#!/usr/bin/env python3
"""一次性代码搬迁工具：把 mc_ai_translator.py 里的顶层符号整体搬到 ferrycore/ 子模块。

只做「按 AST 行区间整体搬运」，被搬的代码逐字不变；不做任何语义改写。
搬运前会校验依赖：被搬符号引用到的顶层符号，必须要么一起搬、要么已经在
ferrycore 某个已存在的模块里 —— 否则会产生循环 import，直接拒绝。

用法：
    python tools/split_module.py analyze                 # 打印顶层符号的相互引用
    python tools/split_module.py move <模块名> <符号...>   # 执行搬迁
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "mc_ai_translator.py"
PKG = ROOT / "ferrycore"

MODULE_DOCS = {
    "net": "最底层的 HTTP 小工具：GET / POST JSON、统一的错误文案。\n\n零业务依赖，谁都能 import。",
    "config": "配置与进度：读写 ferry_config.json（API key 经 secrets 落盘加密）、失败进度记录。",
    "versions": "Minecraft 版本 ↔ 资源包格式：本地版本表、远程版本表、客户端 jar 探测、"
                "以及「这个实例该用哪个 pack_format」的仲裁。",
    "glossary": "术语表：内置原版名词、官方术语下载、用户自定义术语、按批次挑选术语提示。",
    "scan": "实例发现与扫描：语言文件 / Patchouli 读取、缺失 key 计算、模组状态构建。",
    "hardcoded": "硬编码文本检测：扒 jar 里的 class 常量池，挑出玩家可见但语言文件覆盖不到的文本。",
    "bridge": "摆渡桥与 LiteralBridge 的映射表构建 / 导出、游戏内采集文件解析。",
    "update": "程序自更新：查 GitHub 最新 Release、下载附件。",
    "community": "社区汉化：CFPA 整合包下载 / 解析、人工汉化包下载、参考语料。",
    "literalbridge": "LiteralBridge 第三方桥模组：探测已装版本、下载、自动升级。",
    "online": "在线汉化查询。",
    "engine": "翻译引擎：各家 API 调用、批次翻译、重试、用量统计。",
    "packio": "资源包读写：生成 / 卸载 / 还原 / 让位，锁文件与质量检查。",
    "cli": "命令行入口：argparse 与各 command_*。",
}


# ---------------------------------------------------------------- 基础解析


def load_tree() -> tuple[str, ast.Module]:
    text = SOURCE.read_text(encoding="utf-8")
    return text, ast.parse(text)


def top_symbols(tree: ast.Module) -> dict[str, ast.stmt]:
    out: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = node
    return out


def symbol_order(tree: ast.Module) -> list[str]:
    return list(top_symbols(tree))


def referenced_names(node: ast.stmt) -> set[str]:
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def referenced_modules(node: ast.stmt) -> set[str]:
    mods: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute):
            cur: ast.expr = sub
            while isinstance(cur, ast.Attribute):
                cur = cur.value
            if isinstance(cur, ast.Name):
                mods.add(cur.id)
    return mods


def scan_ferrycore() -> dict[str, str]:
    """返回 {符号名: 所在 ferrycore 模块名}。"""
    out: dict[str, str] = {}
    for path in sorted(PKG.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in top_symbols(tree):
            out[name] = path.stem
    return out


def parse_imports(text: str, tree: ast.Module) -> dict[str, list[str]]:
    """{本地名: [可直接复制的 import 语句...]}，只收非 ferrycore 的 import。

    一个名字可能对应多条语句（如 urllib.error / urllib.parse / urllib.request），全部保留。
    """
    mapping: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                key = alias.asname or alias.name.split(".")[0]
                mapping.setdefault(key, []).append(
                    "import " + alias.name + (f" as {alias.asname}" if alias.asname else "")
                )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                key = alias.asname or alias.name
                mapping.setdefault(key, []).append(
                    f"from {node.module} import {alias.name}"
                    + (f" as {alias.asname}" if alias.asname else "")
                )
    return mapping


def node_span(node: ast.stmt, lines: list[str]) -> tuple[int, int]:
    """返回 [起始行索引(0基), 结束行索引(0基, 不含)]，并把紧贴其上的注释一起带走。"""
    start = node.lineno
    if getattr(node, "decorator_list", None):
        start = min(start, min(d.lineno for d in node.decorator_list))
    idx = start - 1
    while idx - 1 >= 0 and lines[idx - 1].strip().startswith("#"):
        idx -= 1
    return idx, node.end_lineno


def analyze_symbols() -> None:
    text, tree = load_tree()
    syms = top_symbols(tree)
    for name, node in syms.items():
        deps = sorted(referenced_names(node) & set(syms) - {name})
        mods = sorted(referenced_modules(node) - set(syms))
        print(f"{name}: sites={mods} deps={deps}")


# ---------------------------------------------------------------- 搬迁


def build_import_block(module: str, needed_syms: list[str], nodes: list[ast.stmt],
                       import_map: dict[str, list[str]], mod_map: dict[str, str]) -> str:
    plain: set[str] = set()
    froms: dict[str, set[str]] = {}
    imported = set(import_map)

    sites: set[str] = set()
    for node in nodes:
        sites |= referenced_modules(node)
        sites |= referenced_names(node) & imported
    for name in sites:
        for line in import_map.get(name, []):
            if line.startswith("import "):
                plain.add(line)
            else:
                mod, rest = line[len("from "):].split(" import ", 1)
                froms.setdefault(mod, set()).add(rest)

    # 兜底：需要但源文件没 import 过的 ferrycore 符号（理论上不会走到）。
    for name in needed_syms:
        if name in imported:
            continue
        owner = mod_map.get(name)
        if owner and owner != module:
            froms.setdefault(f"ferrycore.{owner}", set()).add(name)

    out: list[str] = ["from __future__ import annotations", ""]
    for line in sorted(plain):
        out.append(line)
    third = sorted(m for m in froms if not m.startswith("ferrycore"))
    local = sorted(m for m in froms if m.startswith("ferrycore"))
    if third:
        if plain:
            out.append("")
        for mod in third:
            out.append(f"from {mod} import {', '.join(sorted(froms[mod]))}")
    if local:
        out.append("")
        for mod in local:
            out.append(f"from {mod} import {', '.join(sorted(froms[mod]))}")
    out.append("")
    return "\n".join(out)


def normalize_ferrycore_imports(text: str) -> str:
    tree = ast.parse(text)
    nodes = [
        n for n in tree.body
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("ferrycore")
    ]
    if not nodes:
        return text
    first, last = min(n.lineno for n in nodes), max(n.end_lineno for n in nodes)
    entries: list[tuple[str, list[str]]] = []
    for n in nodes:
        names = sorted(a.name + (f" as {a.asname}" if a.asname else "") for a in n.names)
        entries.append((n.module, names))
    entries.sort()
    block = "\n".join(f"from {mod} import {', '.join(names)}" for mod, names in entries)
    lines = text.split("\n")
    return "\n".join(lines[: first - 1]) + "\n" + block + "\n" + "\n".join(lines[last:])


def cmd_move(module: str, symbols: list[str]) -> None:
    if module not in MODULE_DOCS:
        raise SystemExit(f"未登记的模块名：{module}")
    text, tree = load_tree()
    syms = top_symbols(tree)
    unknown = [s for s in symbols if s not in syms]
    if unknown:
        raise SystemExit(f"源文件中没有这些顶层符号：{unknown}")

    mod_map = scan_ferrycore()
    moved = set(symbols)

    # 校验：被搬符号依赖的顶层符号，必须一起搬 或 已在 ferrycore 里。
    blockers: list[str] = []
    for name in symbols:
        deps = referenced_names(syms[name]) & set(syms) - {name}
        for dep in sorted(deps - moved):
            if dep not in mod_map:
                blockers.append(f"{name} -> {dep}")
    if blockers:
        raise SystemExit("依赖未满足（会形成循环 import）：\n  " + "\n  ".join(blockers))

    lines = text.split("\n")
    spans: list[tuple[int, int]] = []
    for name in symbols:
        spans.append(node_span(syms[name], lines))
    spans.sort()

    # 生成新模块正文（按源码中的原始顺序）。
    ordered = sorted(symbols, key=lambda s: syms[s].lineno)
    bodies: list[str] = []
    for name in ordered:
        i0, i1 = node_span(syms[name], lines)
        chunk = "\n".join(lines[i0:i1]).rstrip("\n")
        bodies.append(chunk)
    needed = sorted({n for name in ordered for n in referenced_names(syms[name])} & set(mod_map))
    header = build_import_block(module, needed, [syms[n] for n in ordered], parse_imports(text, tree), mod_map)
    doc = MODULE_DOCS[module]
    content = f'"""{doc}\n"""\n\n' + header + "\n\n" + "\n\n\n".join(bodies) + "\n"
    (PKG / f"{module}.py").write_text(content, encoding="utf-8")

    # 从源文件删除这些区间。
    deleted: set[int] = set()
    for i0, i1 in spans:
        deleted.update(range(i0, i1))
    kept = [line for idx, line in enumerate(lines) if idx not in deleted]
    new_text = "\n".join(kept)

    # 清理连续空行。
    new_text = re.sub(r"\n{4,}", "\n\n\n", new_text)

    # 插入新模块的 import。
    new_tree = ast.parse(new_text)
    fc_nodes = [
        n for n in new_tree.body
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("ferrycore")
    ]
    insert_at = max(n.end_lineno for n in fc_nodes)
    nl = new_text.split("\n")
    stmt = f"from ferrycore.{module} import {', '.join(ordered)}"
    nl.insert(insert_at, stmt)
    new_text = "\n".join(nl)
    new_text = normalize_ferrycore_imports(new_text)
    if not new_text.endswith("\n"):
        new_text += "\n"
    SOURCE.write_text(new_text, encoding="utf-8")

    print(f"[ok] ferrycore/{module}.py  <- {len(ordered)} 个符号")
    print("     " + ", ".join(ordered))


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] == "analyze":
        analyze_symbols()
    elif sys.argv[1] == "move":
        if len(sys.argv) < 4:
            raise SystemExit("用法：move <模块名> <符号...>")
        cmd_move(sys.argv[2], sys.argv[3:])
    else:
        raise SystemExit(f"未知命令：{sys.argv[1]}")
