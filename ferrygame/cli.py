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

from .pipeline import (
    apply_project,
    get_adapter,
    registered_adapters,
    run_game,
    scan_game,
    translate_project,
)
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
        return 0
    return 1


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
    )
    print(res.summary())
    return 0 if (res.probe.ready if args.dry_run else res.ok) else 1


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

    p_scan = sub.add_parser("scan", help="抽取剧情与操作菜单，生成/更新明文 ferry_tl.json（不花 token）")
    p_scan.add_argument("game", nargs="?", default=".")
    p_scan.add_argument("--adapter", default=None)
    p_scan.set_defaults(func=cmd_scan)

    p_tr = sub.add_parser("translate", help="翻译 ferry_tl.json 中的待翻条目（带上下文滑窗与字节预算约束）")
    p_tr.add_argument("game", nargs="?", default=".")
    p_tr.add_argument("--force", action="store_true", help="重翻非人工锁定的已翻译条目")
    _add_engine_args(p_tr)
    p_tr.set_defaults(func=cmd_translate)

    p_ap = sub.add_parser("apply", help="把 ferry_tl.json 里的译文写回游戏（手改译文表后跑它，零 token）")
    p_ap.add_argument("game", nargs="?", default=".")
    p_ap.add_argument("--out", default=None)
    p_ap.set_defaults(func=cmd_apply)

    p_run = sub.add_parser("run", help="一步到位：scan → translate → apply（同时落盘 ferry_tl.json）")
    p_run.add_argument("game", nargs="?", default=".")
    p_run.add_argument("--list-adapters", action="store_true")
    p_run.add_argument("--adapter", default=None)
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--force", action="store_true")
    p_run.add_argument("--out", default=None)
    _add_engine_args(p_run)
    p_run.set_defaults(func=cmd_run)

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
