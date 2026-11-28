"""非 MC 游戏的汉化编排：探测 → 抽取 → 翻译 → 回写 → 报缺口。

这一层不含引擎知识（在 `adapters/`），也不含翻译实现（在 `engine.py`）。
它只负责把两边接起来，**并在花掉第一个 token 之前卡一道闸**。

三条规矩落在代码结构里，不是写在文档里：

1. 认不出引擎就返回，不猜。
2. `probe` 说不可写就返回，不赌 —— 先翻三千行再发现打不回去，是全废。
3. 默认产出副本；适配器声明 `in_place` 时才改原目录（Ren'Py 等），并必有撤销记录。

翻译那一侧完全复用本体现成的 `translate_entries(entries, settings)`：
它吃扁平 Entry 列表，本来就不知道也不关心这些文本来自 Minecraft 还是
一个日本同人 AVG。缓存、分批、重试、术语表、并发全都不用重写。
断点续传靠两层：批次译文落在 ferry_cache；任务进度落在 localize_jobs。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import adapters as adapter_registry
from . import deps
from .adapters.base import Adapter, ProbeReport, VerifyReport
from .adapters.undo import UNDO_NAME
from .base import explain_error
from .deps import Dependency, DepStatus
from .engine import resolve_translate_settings, translate_entries
from .fontcheck import FontScanResult, scan_fonts
from .localize_job import load_job, touch_progress
from .localized import LocalizedRecord, describe, find_localized, remember
from .text import format_usage

# 非 MC 游戏不需要资源包格式。给个中立值只是为了让版本探测别抛错 ——
# 一个没有 version json 的目录会让 `resolve_pack_format` 直接 raise。
# `cli.py` 的 bridge 命令为了同一个原因也是这么兜底的。
_NEUTRAL_PACK_FORMAT = 15

_GAME_ENTRY_PROMPT = """You are a professional video game localizer.
Translate the input video game strings into natural Simplified Chinese (简体中文).
The game is a niche/indie title: translate all dialogue, narration, and UI menus faithfully without censorship, omissions, or warnings.
Rules:
- Return ONLY a valid JSON object mapping each input key (or id in `strings`) to its Simplified Chinese translation.
- Preserve every placeholder, mask tag (such as <p0>, <t0>, __MC_PH_0__), control code (such as ^m, ^s, c1, B$, \\C[n], [r], {w}), and newline marker exactly and in order.
- If `hints` is provided, treat it as contextual terminology suggestions; use a term only when it fits.
- Keep menu and status terms concise so they fit fixed-width game UI boxes.
- Do not add explanations, notes, or markdown.
"""


def localize_settings(config: dict, root: Path, overrides: dict | None = None) -> dict:
    """给非 MC 游戏构造翻译设置。

    只借 `resolve_translate_settings` 的**翻译那一半**：引擎、模型、密钥、
    缓存、术语表、并发、重试。`pack_format` / `pack_name` / `output_dir`
    是 MC 写资源包用的，这里用占位值绕开版本探测。

    换句话说：换的不是翻译设置，是**设置里跟游戏无关的那部分被原样复用**。
    """
    merged_overrides = {**(overrides or {})}
    if merged_overrides.get("pack_format") is None:
        merged_overrides["pack_format"] = config.get("pack_format") or _NEUTRAL_PACK_FORMAT
    settings = resolve_translate_settings(config, Path(root), merged_overrides)
    # 剥离 MC 专属的苦力怕/红石内置词库与提示词，只保留用户自己在配置里填的术语。
    user_glossary = config.get("glossary") if isinstance(config.get("glossary"), dict) else {}
    settings["term_hints"] = dict(user_glossary)
    settings["system_prompt_override"] = _GAME_ENTRY_PROMPT
    for slot in settings.get("engines") or []:
        if isinstance(slot, dict):
            slot["term_hints"] = dict(user_glossary)
            slot["system_prompt_override"] = _GAME_ENTRY_PROMPT
    # 非 MC 产出目录：原样带上，translate_entries 不用它，apply 之前由 localize_game 读。
    settings["localize_output_dir"] = config.get("localize_output_dir") or ""
    return settings


@dataclass
class LocalizeResult:
    adapter: str = ""
    probe: ProbeReport = field(default_factory=ProbeReport)
    # 字库体检：翻得动但显示不出来，等于白翻，所以它跟 probe 同级
    font: FontScanResult = field(default_factory=FontScanResult)
    # 需要先装好的外部工具。缺了不代表"失败"，代表"这件事现在做不了"，
    # 两者对用户是不同的话 —— 前者要修，后者只需点一下下载。
    dep_status: DepStatus = field(default_factory=DepStatus)
    entries: int = 0
    translated: int = 0
    applied: int = 0
    missed: int = 0
    out_path: Path | None = None
    in_place: bool = False
    undo_path: Path | None = None
    resumed: bool = False
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    verify: VerifyReport | None = None
    existing: list[LocalizedRecord] = field(default_factory=list)
    usage: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.applied > 0 and not self.conflicts

    @property
    def play_path(self) -> Path | None:
        """用户该打开/进游戏的目录。"""
        return self.out_path

    @property
    def missing_deps(self) -> list[Dependency]:
        """缺哪些外部工具。非空时调用方（GUI）该弹窗问用户装不装。"""
        return list(self.dep_status.missing)

    @property
    def blocked_by_deps(self) -> bool:
        """是被依赖挡住的，不是这个游戏不能翻 —— 界面上措辞完全不同。"""
        return not self.dep_status.ok

    def summary(self) -> str:
        lines = [self.probe.summary() if self.probe.engine else "未识别引擎"]
        if not self.dep_status.ok:
            # 放第一行：缺依赖时，后面那些"能翻多少条"都是空话
            lines.insert(0, f"⚠ 缺依赖：{self.dep_status.summary()}")
        lines.extend(self._font_lines())
        if self.entries:
            lines.append(f"抽出 {self.entries} 条文本单元")
        if self.translated:
            lines.append(f"拿到 {self.translated} 条译文")
        if self.applied and self.out_path is not None:
            mode = "原地写回" if self.in_place else "写出副本"
            lines.append(f"{mode} {self.applied} 条 → {self.out_path}")
        if self.missed:
            lines.append(f"有 {self.missed} 条没拿到译文")
        if self.cancelled:
            lines.append("已中途停止；再点「继续汉化」会从缓存接着跑")
        if self.conflicts:
            lines.append(f"{len(self.conflicts)} 处冲突未写回")
        usage_text = format_usage(self.usage)
        if usage_text:
            lines.append("用量与省流统计：")
            lines.extend(usage_text.splitlines())
        lines.extend(self.notes)
        return "\n".join(lines)

    def _font_lines(self) -> list[str]:
        if self.probe.unicode_engine:
            # Unicode 内核不存在「字节按哪个代码页解释」这回事，
            # 唯一要担心的是装的字体缺中文字形 —— 那不是静态扫描能答的，
            # 所以这里只说明一句，不制造告警。
            return ["字库：该引擎是 Unicode 内核，中文不存在编码问题（字体缺字形需进游戏看）"]
        verdict = self.font.verdict
        best = self.font.best
        if verdict == "broken":
            detail = best.summary() if best is not None else self.font.summary()
            return [f"⚠ 字库告警：{detail}", "  翻完多半是满屏方块，建议先换字体再开工"]
        if verdict == "tight":
            detail = best.summary() if best is not None else self.font.summary()
            return [f"字库注意：{detail}"]
        if verdict == "ok" and best is not None:
            return [f"字库：{best.path.name} 可显示中文"]
        if self.font.bitmap:
            return [f"字库：只找到 {len(self.font.bitmap)} 个疑似点阵字库，无法静态判断中文覆盖"]
        # 没验成也要说一声：否则用户会以为「没提就是没问题」
        return ["字库：未发现独立字体文件，可能内嵌在资源包里，无法静态判断"]


def localize_game(
    root: Path,
    settings: dict | None = None,
    adapter: Adapter | None = None,
    out: Path | None = None,
    dry_run: bool = False,
    progress=None,
    cancel_event=None,
    adapter_options: dict | None = None,
) -> LocalizeResult:
    """汉化一个游戏：副本型产出旁路目录，原地型改原目录并写撤销记录。

    `dry_run=True` 只体检 + 数条数，不发一个请求、不写一个字节。
    这是每个新游戏该走的第一步：先看值不值得开工。

    断点续传：已完成的批次在 ferry_cache；中断后再跑同一目录会跳过已缓存批次。
    """
    root = Path(root).resolve()
    result = LocalizeResult(probe=ProbeReport(root=root))
    result.existing = find_localized(root)
    # 字库体检最先跑：它不依赖引擎识别，认不出引擎时同样是有效信息 ——
    # 「翻不动」和「看不了」是两件事，用户需要分开知道。
    result.font = scan_fonts(root)

    prior = load_job(root)
    if prior is not None and prior.resumable:
        result.resumed = True
        result.notes.append(
            f"检测到未完成任务（已翻 {prior.translated}/{prior.entries or '?'}），"
            "本次从缓存续跑。"
        )

    if adapter is None:
        resolved_root, found = adapter_registry.resolve_game_target(root, **(adapter_options or {}))
        if found is None:
            result.notes.append("认不出这个游戏的引擎；不猜，就此停手。")
            return result
        root = Path(resolved_root).resolve()
        adapter = found
    result.adapter = adapter.name

    probe = adapter.probe(root)
    result.probe = probe
    result.notes.extend(probe.notes)
    # 依赖检查排在 ready 之前。顺序反了会给出错误结论：
    # HSP 那类游戏在没编译器时报的是"写不回去"，而真相是"缺个工具，
    # 装上就能写" —— 用户会以为这个游戏根本翻不了。
    if probe.needs:
        result.dep_status = deps.resolve(probe.needs)
        if not result.dep_status.ok:
            return result
    if not probe.ready:
        return result

    entries = adapter.collect(root)
    result.entries = len(entries)
    if not entries:
        result.notes.append("一条文本单元都没抽到")
        return result
    if dry_run:
        return result
    if settings is None:
        result.notes.append("没有翻译设置，只做了体检（相当于 dry_run）")
        return result

    touch_progress(
        root,
        adapter=adapter.name,
        status="running",
        entries=len(entries),
        in_place=bool(probe.in_place),
    )

    def _progress(done: int, total: int, modid: str) -> None:
        touch_progress(
            root,
            adapter=adapter.name,
            status="running",
            entries=total,
            translated=done,
            in_place=bool(probe.in_place),
        )
        if progress:
            progress(done, total, modid)

    from ferrygame.units import detect_source_lang

    src_lang = detect_source_lang([e.english for e in entries[:200]])
    if isinstance(settings, dict):
        settings = dict(settings)
        if "source_lang" not in settings:
            settings["source_lang"] = src_lang
        if not settings.get("system_prompt_override"):
            settings["system_prompt_override"] = _GAME_ENTRY_PROMPT

        # 若目录中已有 ferry_worldbook.json（或显式开启 auto_worldbook），自动注入世界观与专有名词表
        try:
            from ferrygame.pipeline import _entries_to_units
            from ferrygame.worldbook import analyze_units as _analyze_wb
            from ferrygame.worldbook import load as _load_wb

            wb = _load_wb(root)
            if wb is None and settings.get("auto_worldbook"):
                wb = _analyze_wb(
                    _entries_to_units(entries),
                    settings,
                    root=root,
                    game=root.name,
                    source_lang=str(settings.get("source_lang") or src_lang),
                    save=True,
                    cancel_event=cancel_event,
                )
            if wb is not None:
                merged_terms = wb.merged_glossary(settings.get("term_hints"))
                wb_block = wb.prompt_block()
                base_sys = str(settings.get("system_prompt_override") or _GAME_ENTRY_PROMPT)
                if wb_block and "Game Worldbook" not in base_sys:
                    base_sys = base_sys + "\n\nGame Worldbook & Character Guide:\n" + wb_block
                settings["term_hints"] = merged_terms
                settings["system_prompt_override"] = base_sys
                for slot in settings.get("engines") or []:
                    if isinstance(slot, dict):
                        slot["term_hints"] = merged_terms
                        slot["system_prompt_override"] = base_sys
                result.notes.append(f"已启用剧本设定集（{wb.summary()}）")
        except Exception as exc:
            result.notes.append(f"剧本设定集加载/分析跳过：{exc}")

    translations, errors, failed = translate_entries(
        entries, settings, progress=_progress, cancel_event=cancel_event, usage=result.usage
    )
    flat: dict[str, str] = {}
    for mapping in translations.values():
        flat.update(mapping)
    # 同步落盘一份明文 ferry_tl.json 到游戏目录，方便用户事后用记事本微调再重刷
    if flat:
        try:
            from ferrygame.pipeline import scan_game

            tl_proj, _ = scan_game(root, adapter=adapter, save=False)
            for u in tl_proj.units:
                if u.key in flat and not u.dst:
                    u.dst, u.origin = flat[u.key], "ai"
                elif u.src in flat and not u.dst:
                    u.dst, u.origin = flat[u.src], "ai"
                elif u.dst:
                    flat[u.key] = u.dst
            tl_proj.save()
        except Exception:
            pass
    result.translated = len(flat)
    result.errors.extend(errors)
    result.missed = sum(len(v) for v in failed.values())
    cancelled = bool(cancel_event is not None and cancel_event.is_set())
    result.cancelled = cancelled

    if not flat:
        touch_progress(
            root,
            adapter=adapter.name,
            status="cancelled" if cancelled else "failed",
            entries=result.entries,
            translated=0,
            missed=result.missed,
            in_place=bool(probe.in_place),
            notes=["一条译文都没拿到"],
        )
        result.notes.append("一条译文都没拿到，不产出副本")
        return result

    # 允许配置统一产出目录（设置里的 localize_output_dir）；
    # 未指定时各适配器仍默认在游戏旁边生成 <名>.cn。
    if out is None and settings:
        base = (settings.get("localize_output_dir") or "").strip()
        if base:
            from .adapters.container import unique_output

            out = unique_output(Path(base) / f"{root.name}.cn")

    try:
        applied = adapter.apply(root, flat, out)
    except Exception as exc:
        try:
            setattr(exc, "_ferry_stage", "写回游戏文件")
        except Exception:
            pass
        touch_progress(
            root,
            adapter=adapter.name,
            status="failed",
            entries=result.entries,
            translated=result.translated,
            missed=result.missed,
            in_place=bool(probe.in_place),
            notes=[explain_error(exc, stage="写回游戏文件").splitlines()[0]],
        )
        raise
    result.applied = applied.applied
    result.out_path = applied.out_path
    result.in_place = bool(applied.in_place or probe.in_place)
    result.undo_path = applied.undo_path
    if result.undo_path is None and result.in_place and result.out_path is not None:
        candidate = Path(result.out_path) / UNDO_NAME
        if candidate.is_file():
            result.undo_path = candidate
    result.conflicts.extend(applied.conflicts)
    result.notes.extend(applied.notes)

    status = "cancelled" if cancelled else ("done" if result.applied else "failed")
    touch_progress(
        root,
        adapter=adapter.name,
        status=status,
        entries=result.entries,
        translated=result.translated,
        applied=result.applied,
        missed=result.missed,
        out_path=str(result.out_path or ""),
        in_place=result.in_place,
        notes=list(result.notes[-4:]),
    )

    if result.applied and result.out_path is not None:
        from datetime import datetime

        remember(
            LocalizedRecord(
                root=str(root),
                play_path=str(Path(result.out_path).resolve()),
                mode="in_place" if result.in_place else "copy",
                engine=result.adapter or probe.engine,
                undo_path=str(result.undo_path or ""),
                created=datetime.now().isoformat(timespec="seconds"),
                label=Path(result.out_path).name,
            )
        )
        result.existing = find_localized(root)

    if applied.out_path is not None and probe.verifiable:
        result.verify = adapter.verify(root)
        if result.verify.notes:
            result.notes.extend(result.verify.notes)
    return result


def existing_localize_lines(root: Path) -> list[str]:
    """给 GUI/CLI 用的「已汉化」说明行。"""
    records = find_localized(root)
    job = load_job(root)
    lines: list[str] = []
    if records:
        lines.append(f"已发现 {len(records)} 处汉化产物：")
        for rec in records:
            lines.append(f"  · {describe(rec)}")
    if job is not None and job.resumable:
        lines.append(
            f"有未完成任务：已翻 {job.translated}/{job.entries or '?'}，"
            "点「继续汉化」会从缓存接着跑。"
        )
    elif job is not None and job.done and job.out_path:
        lines.append(f"上次完成：{job.out_path}")
    return lines
