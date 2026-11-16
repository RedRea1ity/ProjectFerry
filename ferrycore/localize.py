"""非 MC 游戏的汉化编排：探测 → 抽取 → 翻译 → 回写 → 报缺口。

这一层不含引擎知识（在 `adapters/`），也不含翻译实现（在 `engine.py`）。
它只负责把两边接起来，**并在花掉第一个 token 之前卡一道闸**。

三条规矩落在代码结构里，不是写在文档里：

1. 认不出引擎就返回，不猜。
2. `probe` 说不可写就返回，不赌 —— 先翻三千行再发现打不回去，是全废。
3. 只产出副本，原游戏一个字节都不动。

翻译那一侧完全复用本体现成的 `translate_entries(entries, settings)`：
它吃扁平 Entry 列表，本来就不知道也不关心这些文本来自 Minecraft 还是
一个日本同人 AVG。缓存、分批、重试、术语表、并发全都不用重写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import adapters as adapter_registry
from . import deps
from .adapters.base import Adapter, ProbeReport, VerifyReport
from .deps import Dependency, DepStatus
from .engine import resolve_translate_settings, translate_entries
from .fontcheck import FontScanResult, scan_fonts

# 非 MC 游戏不需要资源包格式。给个中立值只是为了让版本探测别抛错 ——
# 一个没有 version json 的目录会让 `resolve_pack_format` 直接 raise。
# `cli.py` 的 bridge 命令为了同一个原因也是这么兜底的。
_NEUTRAL_PACK_FORMAT = 15


def localize_settings(config: dict, root: Path) -> dict:
    """给非 MC 游戏构造翻译设置。

    只借 `resolve_translate_settings` 的**翻译那一半**：引擎、模型、密钥、
    缓存、术语表、并发、重试。`pack_format` / `pack_name` / `output_dir`
    是 MC 写资源包用的，这里用占位值绕开版本探测。

    换句话说：换的不是翻译设置，是**设置里跟游戏无关的那部分被原样复用**。
    """
    overrides = {"pack_format": config.get("pack_format") or _NEUTRAL_PACK_FORMAT}
    return resolve_translate_settings(config, Path(root), overrides)


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
    errors: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    verify: VerifyReport | None = None

    @property
    def ok(self) -> bool:
        return self.applied > 0 and not self.conflicts

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
        if self.applied:
            lines.append(f"写回 {self.applied} 条 → {self.out_path}")
        if self.missed:
            lines.append(f"有 {self.missed} 条没拿到译文")
        if self.conflicts:
            lines.append(f"{len(self.conflicts)} 处冲突未写回")
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
    """把一个游戏汉化成副本。

    `dry_run=True` 只体检 + 数条数，不发一个请求、不写一个字节。
    这是每个新游戏该走的第一步：先看值不值得开工。
    """
    root = Path(root)
    result = LocalizeResult(probe=ProbeReport(root=root))
    # 字库体检最先跑：它不依赖引擎识别，认不出引擎时同样是有效信息 ——
    # 「翻不动」和「看不了」是两件事，用户需要分开知道。
    result.font = scan_fonts(root)

    if adapter is None:
        found = adapter_registry.detect(root, **(adapter_options or {}))
        if found is None:
            result.notes.append("认不出这个游戏的引擎；不猜，就此停手。")
            return result
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

    translations, errors, failed = translate_entries(
        entries, settings, progress=progress, cancel_event=cancel_event
    )
    flat: dict[str, str] = {}
    for mapping in translations.values():
        flat.update(mapping)
    result.translated = len(flat)
    result.errors.extend(errors)
    result.missed = sum(len(v) for v in failed.values())
    if not flat:
        result.notes.append("一条译文都没拿到，不产出副本")
        return result

    applied = adapter.apply(root, flat, out)
    result.applied = applied.applied
    result.out_path = applied.out_path
    result.conflicts.extend(applied.conflicts)
    result.notes.extend(applied.notes)
    if applied.out_path is not None and probe.verifiable:
        result.verify = adapter.verify(root)
        if result.verify.notes:
            result.notes.extend(result.verify.notes)
    return result
