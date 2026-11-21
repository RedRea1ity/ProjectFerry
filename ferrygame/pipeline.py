"""通用游戏线总装管线：scan → translate → apply（以及一步到位的 run）。

为什么拆成三步、同时又留一步到位的 `run`：
- **一步到位（`run`）**：碰到小众游戏拖进来就翻，翻完立刻产出副本/补丁，同时在游戏旁边
  留下一份明文 `ferry_tl.json`。
- **分步（`scan` / `translate` / `apply`）**：玩的时候发现哪句台词不顺眼、哪个菜单词超长，
  直接用记事本改 `ferry_tl.json` 里的 `"dst"`，然后跑 `apply` —— 不调模型、不花一分钱 token，
  1 秒钟重新写回游戏。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ferrycore import adapters as core_adapters
from ferrycore.adapters.base import Adapter, ApplyReport, ProbeReport
from ferrycore.models import Entry

from .pc98 import PC98Adapter
from .project import Project
from . project import load as load_project
from .project import merge as merge_project
from .rawbin import RawBinaryAdapter
from .translate import CallFn, TranslateReport, translate_units
from .units import Unit, classify, detect_source_lang

def _game_adapters() -> list[type[Adapter]]:
    seen: list[type[Adapter]] = []
    for cls in (PC98Adapter, *getattr(core_adapters, "REGISTRY", ()), RawBinaryAdapter):
        if cls not in seen:
            seen.append(cls)
    return seen


def registered_adapters() -> list[str]:
    return [cls.name for cls in _game_adapters()]


def get_adapter(name: str, **options) -> Adapter | None:
    for cls in _game_adapters():
        if cls.name == name:
            try:
                return cls(**options)
            except TypeError:
                return cls()
    return None


def detect_adapter(root: Path, **options) -> Adapter | None:
    root = Path(root).resolve()
    for cls in sorted(_game_adapters(), key=lambda c: c.priority):
        try:
            adapter = cls(**options)
        except TypeError:
            adapter = cls()
        try:
            if adapter.probe(root).readable:
                return adapter
        except Exception:
            continue
    return None


def _entries_to_units(entries: list[Entry]) -> list[Unit]:
    units: list[Unit] = []
    for idx, entry in enumerate(entries, start=1):
        kind = entry.kind if entry.kind in ("story", "menu", "name", "font") else classify(entry.english)
        rel = entry.rel_path or (entry.key.split("#", 1)[0] if "#" in entry.key else "")
        units.append(
            Unit(
                key=entry.key,
                src=entry.english,
                kind=kind,
                file=rel,
                order=idx,
            )
        )
    return units


def collect_units(adapter: Adapter, root: Path) -> list[Unit]:
    """优先走适配器的 `scan_units`（保留字节预算和偏移）；没有就从 `collect()` 转。"""
    scan_fn = getattr(adapter, "scan_units", None)
    if callable(scan_fn):
        return list(scan_fn(root))
    return _entries_to_units(adapter.collect(root))


def scan_game(
    root: Path,
    adapter: Adapter | None = None,
    adapter_options: dict[str, Any] | None = None,
    save: bool = True,
) -> tuple[Project, ProbeReport]:
    """扫描游戏并生成/更新 `ferry_tl.json`。已有的译文与人工修改原样保留。"""
    root = Path(root).resolve()
    if adapter is None:
        adapter = detect_adapter(root, **(adapter_options or {}))
    if adapter is None:
        probe = ProbeReport(root=root, notes=["所有已知引擎与二进制盲扫均未发现可提取的剧情/菜单文本"])
        return Project(root=root, game=root.name, notes=list(probe.notes)), probe

    probe = adapter.probe(root)
    if not probe.readable:
        return Project(root=root, game=root.name, adapter=adapter.name, notes=list(probe.notes)), probe

    units = collect_units(adapter, root)
    lang = detect_source_lang([u.src for u in units if u.translatable])
    target_enc = next((u.encoding for u in units if u.encoding), "utf-8")
    fresh = Project(
        root=root,
        game=root.name,
        adapter=adapter.name,
        source_lang=lang,
        target_encoding=target_enc,
        units=units,
        notes=list(probe.notes),
    )
    existing = load_project(root)
    project = merge_project(existing, fresh)
    if save and units:
        project.save()
    return project, probe


def translate_project(
    project: Project,
    settings: dict[str, Any],
    force: bool = False,
    progress: Callable[[int, int], None] | None = None,
    cancel_event=None,
    call: CallFn | None = None,
    cache_dir: Path | None = None,
    save: bool = True,
) -> TranslateReport:
    """翻译 `Project` 里未完成的条目，并把结果落盘到 `ferry_tl.json`。"""
    report = translate_units(
        project.units,
        settings=settings,
        source_lang=project.source_lang or "ja",
        glossary=project.glossary,
        force=force,
        progress=progress,
        cancel_event=cancel_event,
        call=call,
        cache_dir=cache_dir,
    )
    if save:
        project.save()
    return report


def apply_project(
    project: Project,
    adapter: Adapter | None = None,
    out: Path | None = None,
) -> ApplyReport:
    """把 `Project` 里的译文写回游戏（不调模型）。"""
    root = Path(project.root).resolve()
    if adapter is None:
        adapter = get_adapter(project.adapter) if project.adapter else detect_adapter(root)
    if adapter is None:
        return ApplyReport(notes=[f"找不到适配器：{project.adapter or 'auto'}"])

    apply_units_fn = getattr(adapter, "apply_units", None)
    if callable(apply_units_fn):
        return apply_units_fn(root, project.units, out=out)

    flat: dict[str, str] = {}
    for unit in project.units:
        if unit.done and unit.translatable:
            flat[unit.key] = unit.dst
            # 同时提供原文映射兜底（Tyrano / GameMaker CSV 等按原文查表）
            flat.setdefault(unit.src, unit.dst)
    if not flat:
        return ApplyReport(notes=["译文表里还没有已完成的译文"])
    return adapter.apply(root, flat, out=out)


@dataclass
class GameRunResult:
    project: Project
    probe: ProbeReport
    translation: TranslateReport = field(default_factory=TranslateReport)
    applied: ApplyReport = field(default_factory=ApplyReport)

    @property
    def ok(self) -> bool:
        return self.applied.ok

    def summary(self) -> str:
        lines = [
            self.probe.summary(),
            f"译文表：{self.project.summary()}",
        ]
        if self.project.path:
            lines.append(f"双语文件：{self.project.path}（可直接手改后跑 apply 重刷）")
        if self.translation.requested:
            lines.append(f"翻译：{self.translation.summary()}")
        if self.applied.applied:
            mode = "原地写回" if self.applied.in_place else "写出副本"
            lines.append(f"{mode}：{self.applied.applied} 条 → {self.applied.out_path}")
        for c in self.applied.conflicts[:10]:
            lines.append(f"  冲突：{c}")
        return "\n".join(lines)


def run_game(
    root: Path,
    settings: dict[str, Any] | None = None,
    adapter: Adapter | None = None,
    out: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    call: CallFn | None = None,
    cache_dir: Path | None = None,
) -> GameRunResult:
    """一步到位：扫描 → 落盘 `ferry_tl.json` → 翻译缺失项 → 写回游戏。"""
    project, probe = scan_game(root, adapter=adapter, save=not dry_run)
    result = GameRunResult(project=project, probe=probe)
    if not probe.ready or dry_run or settings is None:
        return result

    if adapter is None:
        adapter = get_adapter(project.adapter)
    result.translation = translate_project(
        project,
        settings=settings,
        force=force,
        call=call,
        cache_dir=cache_dir,
        save=True,
    )
    result.applied = apply_project(project, adapter=adapter, out=out)
    return result
