"""通用游戏线命令行（`python -m ferrygame`）。

五个子命令对应完整的「野生小众游戏破壁」流程：

    recon      先侦查（熵值、PE 画字/菜单导入表、替身 DLL 空位、PC-98 镜像、台词密度）
    scan       抽文本，在游戏旁生成明文 `ferry_tl.json`（不花 token）
    translate  翻 `ferry_tl.json` 里待翻的剧情与菜单（带上下文滑窗 + 字节预算硬约束）
    apply      把 `ferry_tl.json` 写回游戏（改了译文表之后跑它，1 秒重刷、零 token）
    run        一步到位（scan → translate → apply，照样保留 `ferry_tl.json`）
    revert     按 `.ferry-undo.json` 一键还原
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ferrycore.adapters.undo import revert as revert_game
from ferrycore.config import load_config
from ferrycore.engine import ENGINES_WITH_KEY, resolve_translate_settings
from ferrycore.fontpatch import patch_game_font
from ferrycore.patchpack import export_patch_pack

from .pipeline import (
    analyze_game,
    apply_project,
    get_adapter,
    registered_adapters,
    run_game,
    scan_game,
    translate_project,
)
from .project import TSV_NAME, export_tsv, import_external_translations
from .project import load as load_project
from .recon import inspect_game


def _reconfigure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def _resolve_settings(root: Path, args: argparse.Namespace) -> dict | None:
    config = load_config()
    overrides = {
        "engine": getattr(args, "engine", None),
        "base_url": getattr(args, "base_url", None),
        "api_key": getattr(args, "api_key", None),
        "model": getattr(args, "model", None),
        "batch_size": getattr(args, "batch_size", None),
        "timeout": getattr(args, "timeout", None),
        "delay": getattr(args, "delay", None),
        "concurrency": getattr(args, "concurrency", None),
        "pack_format": 15,
    }
    settings = resolve_translate_settings(config, root, overrides)
    if settings["engine"] in ENGINES_WITH_KEY and not settings["api_key"]:
        print(
            f"{settings['engine']} 引擎需要 API key：请在 ferry_config.json 配置或用 --api-key 传入。",
            file=sys.stderr,
        )
        return None
    return settings


def cmd_recon(args: argparse.Namespace) -> int:
    root = Path(args.game)
    if not root.exists():
        print(f"路径不存在：{root}", file=sys.stderr)
        return 2
    report = inspect_game(root)
    print(report.summary())
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    root = Path(args.game)
    if not root.exists():
        print(f"路径不存在：{root}", file=sys.stderr)
        return 2
    adapter = None
    if args.adapter:
        adapter = get_adapter(args.adapter)
        if adapter is None:
            print(f"未知适配器：{args.adapter}（可用：{'、'.join(registered_adapters())}）", file=sys.stderr)
            return 2
    project, probe = scan_game(root, adapter=adapter, save=True)
    print(probe.summary())
    if project.units:
        print(f"译文表：{project.summary()}")
        print(f"已写入：{project.path}")
        if project.path:
            tsv_path = project.path.with_name(TSV_NAME)
            if tsv_path.is_file():
                print(f"自翻表格（Excel/记事本直改）：{tsv_path}")
        return 0
    return 1


def cmd_extract(args: argparse.Namespace) -> int:
    """提取文本并导出自翻译双语表（ferry_tl.json + ferry_tl.tsv）。"""
    root = Path(args.game)
    if not root.exists():
        print(f"路径不存在：{root}", file=sys.stderr)
        return 2
    adapter = get_adapter(args.adapter) if getattr(args, "adapter", None) else None
    project, probe = scan_game(root, adapter=adapter, save=True)
    print(probe.summary())
    if not project.units:
        return 1
    out_tsv = Path(args.tsv) if getattr(args, "tsv", None) else None
    tsv_path = export_tsv(project, out_tsv)
    print(f"已提取 {len(project.units)} 条文本（0 Token）：")
    print(f"  · JSON 工程表：{project.path}")
    print(f"  · TSV 自翻表（可用 Excel / 记事本 / ParaTranz 编辑）：{tsv_path}")
    return 0


def cmd_repack(args: argparse.Namespace) -> int:
    """从自翻译文件（.tsv / .csv / .json）回录译文并重打包写回游戏。"""
    root = Path(args.game)
    if not root.exists():
        print(f"路径不存在：{root}", file=sys.stderr)
        return 2
    project = load_project(root)
    if project is None:
        project, probe = scan_game(root, save=True)
        if not probe.readable or not project.units:
            print(probe.summary(), file=sys.stderr)
            return 1
    from_path = Path(args.from_file) if getattr(args, "from_file", None) else None
    imported = import_external_translations(project, from_path, save=True)
    if imported:
        print(f"已从自翻译表回录 {imported} 条新增/修改译文。")
    out = Path(args.out) if getattr(args, "out", None) else None
    report = apply_project(project, out=out)
    if report.applied:
        mode = "原地写回" if report.in_place else "重打包产出副本"
        print(f"{mode}：{report.applied} 条 → {report.out_path}")
    for note in report.notes:
        print(note)
    for conflict in report.conflicts[:20]:
        print(f"  冲突：{conflict}", file=sys.stderr)
    return 0 if report.ok else 1


def cmd_translate(args: argparse.Namespace) -> int:
    root = Path(args.game)
    if not root.exists():
        print(f"路径不存在：{root}", file=sys.stderr)
        return 2
    project = load_project(root)
    if project is None:
        project, probe = scan_game(root, save=True)
        if not probe.readable or not project.units:
            print(probe.summary(), file=sys.stderr)
            return 1
    settings = _resolve_settings(root, args)
    if settings is None:
        return 2
    report = translate_project(project, settings=settings, force=bool(args.force), save=True)
    print(report.summary())
    print(f"译文表：{project.summary()} → {project.path}")
    return 0 if (report.translated > 0 or report.requested == 0) else 1


def cmd_apply(args: argparse.Namespace) -> int:
    root = Path(args.game)
    project = load_project(root)
    if project is None:
        print(f"未找到译文表（请先跑 scan 或 run）：{root}", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else None
    report = apply_project(project, out=out)
    if report.applied:
        mode = "原地写回" if report.in_place else "写出副本"
        print(f"{mode} {report.applied} 条 → {report.out_path}")
    for note in report.notes:
        print(note)
    for conflict in report.conflicts[:20]:
        print(f"  冲突：{conflict}", file=sys.stderr)
    return 0 if report.ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    if getattr(args, "list_adapters", False):
        print("已注册的游戏适配器：" + "、".join(registered_adapters()))
        return 0
    root = Path(args.game)
    if not root.exists():
        print(f"路径不存在：{root}", file=sys.stderr)
        return 2
    adapter = None
    if args.adapter:
        adapter = get_adapter(args.adapter)
        if adapter is None:
            print(f"未知适配器：{args.adapter}（可用：{'、'.join(registered_adapters())}）", file=sys.stderr)
            return 2
    settings = None
    if not args.dry_run:
        settings = _resolve_settings(root, args)
        if settings is None:
            return 2
    out = Path(args.out) if args.out else None
    res = run_game(
        root,
        settings=settings,
        adapter=adapter,
        out=out,
        dry_run=bool(args.dry_run),
        force=bool(args.force),
        analyze=bool(getattr(args, "analyze", False)),
    )
    print(res.summary())
    return 0 if (res.probe.ready if args.dry_run else res.ok) else 1


def cmd_analyze(args: argparse.Namespace) -> int:
    """第 0 步：轻量剧本预分析，自动产出可手改的 ferry_worldbook.json（世界观/人物/术语表）。"""
    root = Path(args.game)
    if not root.exists():
        print(f"路径不存在：{root}", file=sys.stderr)
        return 2
    settings = _resolve_settings(root, args)
    if settings is None:
        return 2
    options = {"aes_key": args.aes_key} if getattr(args, "aes_key", None) else None
    adapter = get_adapter(args.adapter, **(options or {})) if getattr(args, "adapter", None) else None
    try:
        wb, _proj, _probe = analyze_game(
            root,
            settings=settings,
            adapter=adapter,
            adapter_options=options,
            force=bool(getattr(args, "force", False)),
            save=True,
        )
    except Exception as exc:
        print(f"剧本预分析失败：{exc}", file=sys.stderr)
        return 1
    print(f"剧本预分析完成：{wb.summary()}")
    if wb.path:
        print(f"设定集已落盘（可用记事本编辑微调）：{wb.path}")
    for note in wb.notes:
        print(f"  · {note}")
    return 0


def cmd_export_patch(args: argparse.Namespace) -> int:
    """一键导出纯净可分享的汉化补丁 ZIP。"""
    root = Path(args.game)
    if not root.exists():
        print(f"路径不存在：{root}", file=sys.stderr)
        return 2
    out = Path(args.out) if getattr(args, "out", None) else None
    report = export_patch_pack(root, out_zip=out, include_source=not bool(getattr(args, "no_source", False)))
    print(report.summary())
    for note in report.notes:
        print(f"  · {note}")
    return 0 if report.ok else 1


def cmd_fontpatch(args: argparse.Namespace) -> int:
    """一键为高频引擎注入中文字体回退。"""
    root = Path(args.game)
    if not root.exists():
        print(f"路径不存在：{root}", file=sys.stderr)
        return 2
    font_path = Path(args.font) if getattr(args, "font", None) else None
    report = patch_game_font(
        root,
        engine=str(getattr(args, "engine", "auto") or "auto"),
        font_path=font_path,
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    print(report.summary())
    for note in report.notes:
        print(f"  · {note}")
    return 0 if report.ok else 1


def cmd_revert(args: argparse.Namespace) -> int:
    root = Path(args.game)
    base = root.parent if root.is_file() else root
    report = revert_game(base, dry_run=bool(args.dry_run))
    print(report.summary())
    for item in report.failed:
        print(f"  失败：{item}", file=sys.stderr)
    return 0 if report.ok else 1


def _add_engine_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--engine", choices=["mymemory", "deepl", "openai", "anthropic", "gemini", "local"], default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--delay", type=float, default=None)
    parser.add_argument("--concurrency", type=int, default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ferrygame",
        description="摆渡 · 通用小众游戏破壁汉化线（与 MC 模组线独立）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_recon = sub.add_parser("recon", help="字节侦查：熵值、PE 画字/菜单导入、替身 DLL、PC-98 镜像、台词密度")
    p_recon.add_argument("game", nargs="?", default=".")
    p_recon.set_defaults(func=cmd_recon)

    p_scan = sub.add_parser("scan", help="抽取剧情与操作菜单，生成/更新明文 ferry_tl.json 与 ferry_tl.tsv（不花 token）")
    p_scan.add_argument("game", nargs="?", default=".")
    p_scan.add_argument("--adapter", default=None)
    p_scan.set_defaults(func=cmd_scan)

    p_ext = sub.add_parser("extract", help="【测试版功能】仅提取文本并导出自翻双语表（ferry_tl.json + Excel 友好的 ferry_tl.tsv）")
    p_ext.add_argument("game", nargs="?", default=".")
    p_ext.add_argument("--adapter", default=None)
    p_ext.add_argument("--tsv", default=None, help="自定义导出 .tsv 路径")
    p_ext.set_defaults(func=cmd_extract)

    p_rep = sub.add_parser("repack", help="【测试版功能】回录自翻译文件（.tsv/.csv/.json）并重打包写回游戏（零 token）")
    p_rep.add_argument("game", nargs="?", default=".")
    p_rep.add_argument("--from", dest="from_file", default=None, help="外部自翻译文件路径（默认读取游戏目录下的 ferry_tl.tsv / ferry_tl.json）")
    p_rep.add_argument("--out", default=None)
    p_rep.set_defaults(func=cmd_repack)

    p_tr = sub.add_parser("translate", help="翻译 ferry_tl.json 中的待翻条目（带上下文滑窗与字节预算约束）")
    p_tr.add_argument("game", nargs="?", default=".")
    p_tr.add_argument("--force", action="store_true", help="重翻非人工锁定的已翻译条目")
    _add_engine_args(p_tr)
    p_tr.set_defaults(func=cmd_translate)

    p_ap = sub.add_parser("apply", help="把 ferry_tl.json 里的译文写回游戏（手改译文表后跑它，零 token）")
    p_ap.add_argument("game", nargs="?", default=".")
    p_ap.add_argument("--out", default=None)
    p_ap.set_defaults(func=cmd_apply)

    p_an = sub.add_parser("analyze", help="第 0 步：轻量剧本预分析，自动产出可手改的 ferry_worldbook.json（世界观/人设/术语表）")
    p_an.add_argument("game", nargs="?", default=".")
    p_an.add_argument("--adapter", default=None)
    p_an.add_argument("--aes-key", default=None, help="虚幻引擎加密 .pak 的 256 位 AES 密钥")
    p_an.add_argument("--force", action="store_true", help="忽略已有设定集，重新生成")
    _add_engine_args(p_an)
    p_an.set_defaults(func=cmd_analyze)

    p_run = sub.add_parser("run", help="一步到位：scan → translate → apply（同时落盘 ferry_tl.json）")
    p_run.add_argument("game", nargs="?", default=".")
    p_run.add_argument("--list-adapters", action="store_true")
    p_run.add_argument("--adapter", default=None)
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--force", action="store_true")
    p_run.add_argument("--analyze", action="store_true", help="翻译前先执行轻量剧本预分析生成 ferry_worldbook.json")
    p_run.add_argument("--out", default=None)
    _add_engine_args(p_run)
    p_run.set_defaults(func=cmd_run)

    p_ep = sub.add_parser("export-patch", help="导出纯净可分享的汉化补丁 ZIP（含自动安装/还原脚本，不含原版游戏大文件）")
    p_ep.add_argument("game", nargs="?", default=".")
    p_ep.add_argument("--out", default=None)
    p_ep.add_argument("--no-source", action="store_true", help="不附带 ferry_tl.json / ferry_tl.tsv 译文协作文件")
    p_ep.set_defaults(func=cmd_export_patch)

    p_fp = sub.add_parser("fontpatch", help="一键为高频引擎注入中文字体回退（支持撤销）")
    p_fp.add_argument("game", nargs="?", default=".")
    p_fp.add_argument("--engine", default="auto")
    p_fp.add_argument("--font", default=None)
    p_fp.add_argument("--dry-run", action="store_true")
    p_fp.set_defaults(func=cmd_fontpatch)

    p_rev = sub.add_parser("revert", help="按 .ferry-undo.json 一键撤销汉化")
    p_rev.add_argument("game", nargs="?", default=".")
    p_rev.add_argument("--dry-run", action="store_true")
    p_rev.set_defaults(func=cmd_revert)

    return parser


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
