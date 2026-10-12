"""命令行入口：argparse 与各 command_*。
"""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path

from ferrycore.base import CONFIG_FILE, USER_GLOSSARY_FILE
from ferrycore.bridge import COLLECTED_MODID, COLLECTED_NAME, collected_entries, parse_collected_file, write_ferrybridge_external
from ferrycore.community import community_corpus, fetch_community_baseline
from ferrycore.config import DEFAULT_CONFIG, load_config, public_config, save_config, update_progress
from ferrycore.engine import ENGINES_WITH_KEY, resolve_translate_settings, translate_entries
from ferrycore.glossary import BUILTIN_GLOSSARY, DEFAULT_GLOSSARY_EN_URL, DEFAULT_GLOSSARY_ZH_URL, fetch_glossary, load_user_glossary
from ferrycore.models import LangFile, mod_status_label
from ferrycore.packio import load_pack_reverted, load_pack_sources, load_uninstalled_ai, merge_pack_translations, merged_sources, migrate_legacy_pack, write_pack, yield_to_community
from ferrycore.scan import _promote_disabled_packs, build_mod_status, entries_with_baseline, find_instance_dirs, load_pack_patchouli, missing_patchouli_entries, resolve_instance_path, scan_inputs, skip_ignored_entries, translated_patchouli_files
from ferrycore.text import format_usage
from ferrycore.versions import detect_minecraft_version, pack_format_for_version


def include_modids_from_args(args: argparse.Namespace) -> set[str]:
    return {x.strip() for x in (args.include_modid or []) if x.strip()}


def only_modids_from_args(args: argparse.Namespace) -> set[str]:
    values = set()
    for item in (args.only_modid or []):
        for part in item.split(","):
            part = part.strip()
            if part:
                values.add(part.lower().replace("-", "_"))
    return values


def instance_dirs_from_args(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    instance = resolve_instance_path(args.instance, interactive=args.interactive)
    mods = Path(args.mods) if args.mods else instance / "mods"
    resourcepacks = Path(args.resourcepacks) if args.resourcepacks else instance / "resourcepacks"
    return instance, mods, resourcepacks


def command_detect(args: argparse.Namespace) -> int:
    found = find_instance_dirs()
    if not found:
        print("没有自动找到实例。请用 --instance <实例目录> 手动指定。")
        return 1
    for index, path in enumerate(found, 1):
        print(f"{index}. {path}")
    return 0


def command_scan(args: argparse.Namespace) -> int:
    instance, mods, resourcepacks = instance_dirs_from_args(args)
    scan = scan_inputs(mods, resourcepacks)
    config = load_config()
    output_dir = Path(config.get("output_dir") or instance / "resourcepacks")
    pack_name = config.get("pack_name") or DEFAULT_CONFIG["pack_name"]
    version = detect_minecraft_version(instance)
    if version and pack_format_for_version(version) <= 3 and migrate_legacy_pack(output_dir / pack_name, pack_format_for_version(version)):
        scan = scan_inputs(mods, resourcepacks)
    yield_to_community(scan, output_dir / pack_name)
    include = include_modids_from_args(args)
    provenance = load_pack_sources(output_dir / pack_name)
    community_sources = {modid: {key for key, source in data.items() if source == "community"} for modid, data in provenance.items()}
    statuses = build_mod_status(scan.english, scan.community, scan.ai, include, scan.reverted, community_sources)
    only = only_modids_from_args(args)
    if only:
        statuses = [st for st in statuses if st.modid.lower().replace("-", "_") in only]
    print(f"英文语言文件: {len(scan.english)} 个；社区/官方汉化文件: {len(scan.community)} 个；AI 包文件: {len(scan.ai)} 个")
    ai_mods = sum(1 for st in statuses if not st.has_community)
    com_mods = sum(1 for st in statuses if st.has_community)
    print(f"待 AI 翻译模组: {ai_mods} 个；已有人工汉化（自动降级）: {com_mods} 个")
    for st in statuses:
        print(f"  {st.modid}: 缺 {st.missing} [{mod_status_label(st)}]")
    return 0


def command_bridge_collect(args: argparse.Namespace) -> int:
    """读取游戏内采集的未命中文本，批量翻译成摆渡桥的外部映射表。

    这是「大批量翻译」的入口：桥在游戏里采集（`ferrybridge-collected.txt`）
    → 这里翻译 → 写回游戏目录（`ferrybridge-mappings.json`），
    桥每秒比对 mtime 热重载生效，全程不用重启游戏。
    """
    # 英文区域的 Windows 控制台（cp1252）编不了中文提示：宁可显示成 ? 也不让 CLI 崩溃。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    config = load_config()
    instance, _mods, _resourcepacks = instance_dirs_from_args(args)
    collected = instance / COLLECTED_NAME
    buckets = parse_collected_file(collected)
    if not buckets:
        print(f"没有找到可用的采集清单：{collected}")
        print("先在游戏里正常玩一圈（悬停物品、看聊天栏），桥会把没翻到的英文记下来。")
        return 1

    detail = "、".join(f"{name} {len(items)} 条" for name, items in sorted(buckets.items()))
    entries = collected_entries(buckets)
    print(f"采集清单：{detail}；去重后共 {len(entries)} 条")

    overrides = {
        "engine": args.engine,
        "base_url": args.base_url,
        "api_key": args.api_key,
        "model": args.model,
        "batch_size": args.batch_size,
        "timeout": args.timeout,
        "delay": args.delay,
        "concurrency": args.concurrency,
        # 桥接映射与资源包格式无关：给个兜底值，别让「版本认不出来」挡住翻译。
        "pack_format": args.pack_format if args.pack_format is not None else 15,
    }
    settings = resolve_translate_settings(config, instance, overrides)
    if settings["engine"] in ENGINES_WITH_KEY and not settings["api_key"]:
        print(
            f"{settings['engine']} 引擎需要 API key：请在 ferry_config.json 配置，或用 --api-key 传入。",
            file=sys.stderr,
        )
        return 2

    last = [0]

    def progress(done: int, total: int, _modid: str) -> None:
        if done != last[0]:
            last[0] = done
            print(f"\r翻译进度 {done}/{total}", end="", flush=True)

    translations, errors, _failed = translate_entries(entries, settings, progress=progress)
    print("\r" + " " * 24 + "\r", end="")

    pairs = {
        english: chinese
        for english, chinese in (translations.get(COLLECTED_MODID) or {}).items()
        if chinese and str(chinese).strip()
    }
    if errors:
        print(f"{len(errors)} 个批次失败（相关条目保留英文，下次重跑会自动重试）：", file=sys.stderr)
        for message in errors[:5]:
            print(f"  - {message}", file=sys.stderr)
    if not pairs:
        print("没有翻译出任何条目，未写出映射表。请检查翻译引擎配置（API key / 网络）。", file=sys.stderr)
        return 1

    target = write_ferrybridge_external(instance, pairs)
    print(f"已写出 {len(pairs)} 条 → {target}")
    print("游戏内每秒自动热重载，无需重启；按 F9 可查看桥的实时状态。")
    return 0


def command_translate(args: argparse.Namespace) -> int:
    # 英文区域的 Windows 控制台（cp1252）编不了中文提示：宁可显示成 ? 也不让 CLI 崩溃。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    config = load_config()
    instance, mods, resourcepacks = instance_dirs_from_args(args)
    overrides = {
        "engine": args.engine,
        "base_url": args.base_url,
        "api_key": args.api_key,
        "model": args.model,
        "batch_size": args.batch_size,
        "timeout": args.timeout,
        "delay": args.delay,
        "concurrency": args.concurrency,
        "pack_format": args.pack_format,
        "pack_name": args.pack_name,
        "output_dir": args.output_dir,
        "fill_community": args.fill_community,
        "refine_community": args.refine_community,
    }
    try:
        settings = resolve_translate_settings(config, instance, overrides)
        version_unconfirmed = False
    except ValueError as exc:
        if not args.yes or "未检测到游戏版本" not in str(exc):
            raise
        print("警告：版本未确认，--yes 强制使用 pack_format 15；生成的包可能无法加载！")
        overrides["pack_format"] = 15
        settings = resolve_translate_settings(config, instance, overrides)
        version_unconfirmed = True
    if settings["engine"] in ENGINES_WITH_KEY and not settings["api_key"]:
        print(f"{settings['engine']} 引擎需要 API key：请在 ferry_config.json 配置对应字段，或用 --api-key 传入。", file=sys.stderr)
        return 2
    # 传入 instance 是必需的：scan_inputs 需要读 options.txt 判断哪些资源包
    # 真正启用。未启用包里的译文不抵扣缺口（否则会误报"已补全"而玩家看到英文），
    # 但会进 reference_only 当术语语料。
    # --trust-disabled-packs 让玩家手动推翻这一判断：把未启用的包也算人工汉化。
    scan = scan_inputs(mods, resourcepacks, instance)
    if getattr(args, "trust_disabled_packs", None):
        _promote_disabled_packs(scan)
    if getattr(args, "ignore_disabled_packs", None):
        scan.reference_only.clear()
    if migrate_legacy_pack(Path(settings["output_dir"]) / settings["pack_name"], settings["pack_format"]):
        scan = scan_inputs(mods, resourcepacks, instance)
        if getattr(args, "trust_disabled_packs", None):
            _promote_disabled_packs(scan)
        if getattr(args, "ignore_disabled_packs", None):
            scan.reference_only.clear()
    yield_to_community(scan, Path(settings["output_dir"]) / settings["pack_name"])
    requested = only_modids_from_args(args)
    eligible = {file.modid for file in scan.english if (not requested or file.modid.lower().replace("-", "_") in requested) and file.modid not in load_uninstalled_ai(Path(settings["output_dir"]) / settings["pack_name"])}
    baseline = fetch_community_baseline(scan, config, Path(settings["cache_dir"]), eligible)
    baseline_files = [LangFile(modid, "zh_cn", "community-baseline", data) for modid, data in baseline.items()]
    entries = entries_with_baseline(scan, baseline, include_modids_from_args(args), only_modids_from_args(args), settings["fill_community"], settings["refine_community"])
    uninstalled = load_uninstalled_ai(Path(settings["output_dir"]) / settings["pack_name"])
    entries = [entry for entry in entries if entry.modid not in uninstalled]
    patch_entries = [entry for entry in missing_patchouli_entries(scan, only_modids_from_args(args)) if entry.modid not in uninstalled]
    entries.extend(patch_entries)
    entries = skip_ignored_entries(entries, config)
    if not entries and not baseline:
        print("没有需要翻译的 key（可能已由人工汉化覆盖、AI 汉化已卸载，或没有缺失）。不生成资源包。")
        return 0
    usage: dict[str, dict[str, int]] = {}
    all_translations, errors, failed = translate_entries(entries, settings, usage=usage, corpus=community_corpus(scan.english, scan.community + baseline_files, scan.reference_only) if settings["refine_community"] else None) if entries else ({}, [], {})
    update_progress(all_translations, failed)
    usage_text = format_usage(usage)
    if usage_text:
        print("各模型用量（按字符量平摊）：")
        print(usage_text)
    if not all_translations and not baseline:
        print("没有成功翻译任何 key，不生成资源包。")
        if errors:
            print(f"共 {len(errors)} 个批次失败：", file=sys.stderr)
            for message in errors:
                print(f"  - {message}", file=sys.stderr)
        return 1
    pack = Path(settings["output_dir"]) / settings["pack_name"]
    new_values = {modid: dict(data) for modid, data in baseline.items()}
    for modid, data in all_translations.items():
        new_values.setdefault(modid, {}).update({key: value for key, value in data.items() if not key.startswith("patchouli:")})
    merged = merge_pack_translations(pack, new_values, scan.community)
    pages = load_pack_patchouli(pack)
    pages.update(translated_patchouli_files(scan, patch_entries, all_translations))
    sources = {modid: {key: "community" for key in data} for modid, data in baseline.items()}
    write_pack(pack, merged, settings["pack_format"], load_pack_reverted(pack), version_unconfirmed=version_unconfirmed, patchouli=pages, sources=merged_sources(pack, merged, sources))
    print(f"完成: {pack}")
    print(f"写入翻译: {sum(len(v) for v in all_translations.values())} 条；累计 {sum(len(v) for v in merged.values())} 条；缓存: {settings['cache_dir']}")
    if errors:
        print(f"提示：{len(errors)} 个批次失败，相关 key 保留英文（下次运行会自动重试）。", file=sys.stderr)
        for message in errors:
            print(f"  - {message}", file=sys.stderr)
    return 0


def command_config(args: argparse.Namespace) -> int:
    if args.init:
        if CONFIG_FILE.exists():
            print(f"配置文件已存在: {CONFIG_FILE}")
        else:
            save_config(dict(DEFAULT_CONFIG))
            print(f"已生成默认配置: {CONFIG_FILE}")
        return 0
    config = load_config()
    print(f"配置文件: {CONFIG_FILE} ({'存在' if CONFIG_FILE.exists() else '未创建，使用默认值'})")
    print(json.dumps(public_config(config), ensure_ascii=False, indent=2))
    return 0


def command_glossary(args: argparse.Namespace) -> int:
    if args.update:
        en_url = args.en_url or DEFAULT_GLOSSARY_EN_URL
        zh_url = args.zh_url or DEFAULT_GLOSSARY_ZH_URL
        print("正在从官方语言文件下载并提取术语表……")
        try:
            glossary = fetch_glossary(en_url, zh_url, args.timeout)
        except RuntimeError as exc:
            print(f"术语表更新失败: {exc}", file=sys.stderr)
            return 1
        USER_GLOSSARY_FILE.write_text(json.dumps(glossary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已更新术语表：{len(glossary)} 条 -> {USER_GLOSSARY_FILE}")
        return 0
    user = load_user_glossary()
    print(f"内置术语表: {len(BUILTIN_GLOSSARY)} 条")
    print(f"用户术语表 {USER_GLOSSARY_FILE}: {len(user)} 条（{'存在' if USER_GLOSSARY_FILE.exists() else '未创建'}）")
    if args.show and user:
        for en, zh in sorted(user.items()):
            print(f"  {en} -> {zh}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只基于英文原文生成 Minecraft 临时 AI 汉化资源包")
    sub = parser.add_subparsers(dest="command", required=True)
    detect = sub.add_parser("detect", help="自动寻找常见 Minecraft 实例")
    detect.set_defaults(func=command_detect)
    config_cmd = sub.add_parser("config", help="查看或生成配置文件")
    config_cmd.add_argument("--init", action="store_true", help="生成默认配置文件")
    config_cmd.set_defaults(func=command_config)
    glossary_cmd = sub.add_parser("glossary", help="查看或联网更新官方术语表")
    glossary_cmd.add_argument("--update", action="store_true", help="从官方语言文件下载并提取术语表")
    glossary_cmd.add_argument("--show", action="store_true", help="列出用户术语表条目")
    glossary_cmd.add_argument("--en-url", default=None, help="英文语言文件 URL（默认官方镜像）")
    glossary_cmd.add_argument("--zh-url", default=None, help="中文语言文件 URL（默认官方镜像）")
    glossary_cmd.add_argument("--timeout", type=int, default=180, help="下载超时（秒）")
    glossary_cmd.set_defaults(func=command_glossary)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--instance", help="Minecraft 实例目录；不填则自动检测")
    common.add_argument("--mods", help="自定义 mods 目录；默认使用实例下的 mods")
    common.add_argument("--resourcepacks", help="可选：资源包目录，用于识别已有 zh_cn 与人工汉化")
    common.add_argument("--interactive", action="store_true", help="检测到多个实例时让你选择")
    common.add_argument("--include-modid", action="append", help="强制纳入被默认过滤的 modid，可重复指定")
    common.add_argument("--only-modid", action="append", help="只处理指定 modid（可重复，也可逗号分隔）")
    scan = sub.add_parser("scan", parents=[common], help="只扫描并统计缺失 key 与人工汉化状态")
    scan.set_defaults(func=command_scan)
    translate = sub.add_parser("translate", parents=[common], help="调用翻译引擎并生成资源包")
    translate.add_argument("--engine", choices=["mymemory", "deepl", "openai", "anthropic", "gemini", "local"], default=None, help="翻译引擎；默认 mymemory（免费）")
    translate.add_argument("--base-url", default=None, help="覆盖当前引擎的 base URL")
    translate.add_argument("--api-key", default=None, help="覆盖当前引擎的 API key")
    translate.add_argument("--model", default=None, help="覆盖当前引擎的模型名")
    translate.add_argument("--output-dir", default=None, help="输出目录；默认写入实例 resourcepacks")
    translate.add_argument("--pack-name", default=None)
    translate.add_argument("--batch-size", type=int, default=None)
    translate.add_argument("--timeout", type=int, default=None)
    translate.add_argument("--delay", type=float, default=None)
    translate.add_argument("--concurrency", type=int, default=None, help="并发翻译线程数（多模型加速），默认 1")
    translate.add_argument("--pack-format", type=float, default=None, help="资源包格式；默认按实例版本自动检测（1.21.9+ 为小数，如 97.1）")
    translate.add_argument("--yes", action="store_true", help="版本无法识别时强制按 pack_format 15 生成，并在包内标记版本未确认")
    translate.add_argument("--fill-community", action="store_true", default=None, help="检测到人工汉化时仍用 AI 补缺失 key")
    translate.add_argument("--refine-community", action="store_true", default=None, help="用人工汉化作参考语料，仅补缺失 key，不覆盖已有译文（需支持上下文的模型）")
    translate.add_argument(
        "--trust-disabled-packs",
        action="store_true",
        default=None,
        help="把『在 resourcepacks 目录里但未在 options.txt 中启用』的资源包"
             "也算作人工汉化（抵扣缺口）。默认不启用：未启用的包在游戏里并不生效，"
             "抵扣会让摆渡误报『已补全』而玩家看到英文。",
    )
    translate.add_argument(
        "--ignore-disabled-packs",
        action="store_true",
        default=None,
        help="彻底忽略未启用的资源包，连术语语料也不参考。",
    )
    translate.set_defaults(func=command_translate)
    bridge_collect = sub.add_parser(
        "bridge-collect",
        parents=[common],
        help="读取游戏内采集的未命中文本，翻译成摆渡桥外部映射表（游戏内热重载生效）",
    )
    bridge_collect.add_argument("--engine", choices=["mymemory", "deepl", "openai", "anthropic", "gemini", "local"], default=None, help="翻译引擎；默认 mymemory（免费）")
    bridge_collect.add_argument("--base-url", default=None, help="覆盖当前引擎的 base URL")
    bridge_collect.add_argument("--api-key", default=None, help="覆盖当前引擎的 API key")
    bridge_collect.add_argument("--model", default=None, help="覆盖当前引擎的模型名")
    bridge_collect.add_argument("--batch-size", type=int, default=None)
    bridge_collect.add_argument("--timeout", type=int, default=None)
    bridge_collect.add_argument("--delay", type=float, default=None)
    bridge_collect.add_argument("--concurrency", type=int, default=None, help="并发翻译线程数（多模型加速），默认 1")
    bridge_collect.add_argument("--pack-format", type=float, default=None, help="仅用于满足内部校验；桥接映射与其无关")
    bridge_collect.set_defaults(func=command_bridge_collect)
    return parser
