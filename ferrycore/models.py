"""数据结构：语言文件、扫描结果、模组翻译状态。

纯数据 + 几个纯函数式标签，**不依赖包内任何其它模块**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Entry:
    modid: str
    key: str
    english: str
    source: str
    kind: str = "lang"
    book: str = ""
    rel_path: str = ""
    json_pointer: str = ""


@dataclass
class LangFile:
    modid: str
    locale: str
    path: str
    data: dict[str, str]
    is_ai_generated: bool = False


@dataclass
class PatchouliFile:
    modid: str
    book: str
    rel_path: str
    data: dict[str, Any]
    path: str
    locale: str
    is_ai_generated: bool = False


@dataclass
class ScanResult:
    english: list[LangFile] = field(default_factory=list)
    community: list[LangFile] = field(default_factory=list)
    ai: list[LangFile] = field(default_factory=list)
    reverted: dict[str, set[str]] = field(default_factory=dict)
    patchouli_english: list[PatchouliFile] = field(default_factory=list)
    patchouli_community: list[PatchouliFile] = field(default_factory=list)
    patchouli_ai: list[PatchouliFile] = field(default_factory=list)
    community_baseline: dict[str, dict[str, str]] = field(default_factory=dict)
    # 存在于 resourcepacks 目录、但未在 options.txt 中启用的包里的人工汉化。
    # 它们不抵扣缺口（游戏里并未生效），但作为术语语料喂给 AI。
    # 这是「最尊重汉化组」的关键：没启用的译文仍被参考，只是不能冒充已生效。
    reference_only: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class ModTranslationStatus:
    modid: str
    total_keys: int
    community_keys: int
    ai_keys: int
    reverted_keys: int = 0
    uncovered_keys: int | None = None

    @property
    def missing(self) -> int:
        if self.uncovered_keys is not None:
            return self.uncovered_keys
        return max(0, self.total_keys - self.community_keys - self.ai_keys - self.reverted_keys)

    @property
    def has_community(self) -> bool:
        return self.community_keys > 0

    @property
    def fully_community(self) -> bool:
        return self.community_keys >= self.total_keys and self.total_keys > 0

    @property
    def fully_reverted(self) -> bool:
        return self.reverted_keys >= self.total_keys and self.total_keys > 0

    @property
    def fully_translated(self) -> bool:
        return self.total_keys > 0 and self.missing == 0 and self.reverted_keys == 0


def translation_coverage_label(status: ModTranslationStatus) -> str:
    if not status.fully_translated:
        return ""
    total = status.total_keys
    human = status.community_keys
    ai = status.ai_keys
    return (
        f"共 {total} 个 key，已完全汉化\n"
        f"人工汉化：{human} 个（{human / total:.1%}）\n"
        f"AI 补全：{ai} 个（{ai / total:.1%}）"
    )


def mod_status_label(status: ModTranslationStatus, fill_community: bool = False, refine_community: bool = False) -> str:
    if status.fully_reverted:
        return "已还原英文（不翻译）"
    if status.has_community:
        if status.fully_community:
            return "人工汉化·完整（已降级）"
        if status.fully_translated:
            return "人工汉化·已补全"
        if refine_community:
            return f"人工汉化·缺{status.missing}（参考语料补缺）"
        if fill_community:
            return f"人工汉化·缺{status.missing}（补缺）"
        return f"人工汉化·缺{status.missing}（已降级不补）"
    if status.ai_keys == 0 and status.reverted_keys == 0:
        return f"待翻译·缺{status.missing}"
    if status.missing == 0:
        if status.reverted_keys:
            return f"已完成（还原{status.reverted_keys}条英文）"
        return "已完成"
    return f"部分翻译·已翻{status.ai_keys}·缺{status.missing}"
