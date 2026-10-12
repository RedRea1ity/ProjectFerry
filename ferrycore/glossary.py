"""术语表：内置原版名词、官方术语下载、用户自定义术语、按批次挑选术语提示。
"""

from __future__ import annotations

import json
import re

from functools import lru_cache
from pathlib import Path

from ferrycore.base import USER_GLOSSARY_FILE
from ferrycore.models import Entry
from ferrycore.net import _http_get_json


DEFAULT_GLOSSARY_EN_URL = "https://cdn.jsdelivr.net/gh/PandaDevOfficial/Minecraft-All-Lang@main/en_gb.json"


DEFAULT_GLOSSARY_ZH_URL = "https://cdn.jsdelivr.net/gh/PandaDevOfficial/Minecraft-All-Lang@main/zh_cn.json"


# 只从官方语言文件里提取这些前缀的专有名词（实体/附魔/效果/物品），避免把通用 UI 文本塞进术语表。
GLOSSARY_TERM_PREFIXES = ("entity.minecraft.", "enchantment.minecraft.", "effect.minecraft.", "item.minecraft.")


# 内置术语表：原版 Minecraft 官方中文名，保证术语一致（非第三方汉化，来自 Mojang 官方本地化）。
BUILTIN_GLOSSARY = {
    # 生物
    "Creeper": "苦力怕", "Zombie": "僵尸", "Skeleton": "骷髅", "Enderman": "末影人",
    "Shulker": "潜影贝", "Phantom": "幻翼", "Villager": "村民", "Wither": "凋灵",
    "Warden": "监守者", "Blaze": "烈焰人", "Ghast": "恶魂", "Guardian": "守卫者",
    "Slime": "史莱姆", "Drowned": "溺尸", "Pillager": "掠夺者", "Ravager": "劫掠兽",
    "Vindicator": "卫道士", "Evoker": "唤魔者", "Allay": "悦灵", "Axolotl": "美西螈",
    "Sniffer": "嗅探兽", "Hoglin": "疣猪兽", "Piglin": "猪灵", "Strider": "炽足兽",
    "Pufferfish": "河豚", "Silverfish": "蠹虫", "Spider": "蜘蛛", "Spider Jockey": "蜘蛛骑士",
    # 矿物 / 材料
    "Redstone": "红石", "Diamond": "钻石", "Emerald": "绿宝石", "Netherite": "下界合金",
    "Obsidian": "黑曜石", "Lapis Lazuli": "青金石", "Quartz": "石英", "Copper": "铜",
    "Amethyst": "紫水晶", "Glowstone": "荧石", "Netherrack": "下界岩", "End Stone": "末地石",
    # 维度 / 地点
    "Nether": "下界", "Overworld": "主世界", "The End": "末地", "Stronghold": "要塞",
    "Dungeon": "地牢", "Mineshaft": "废弃矿井", "Bastion": "堡垒遗迹",
    # 工具 / 装备
    "Sword": "剑", "Pickaxe": "镐", "Shovel": "锹", "Hoe": "锄",
    "Helmet": "头盔", "Chestplate": "胸甲", "Leggings": "护腿", "Boots": "靴子",
    "Bow": "弓", "Crossbow": "弩", "Arrow": "箭", "Shield": "盾牌", "Trident": "三叉戟",
    "Elytra": "鞘翅", "Fishing Rod": "钓鱼竿", "Shears": "剪刀", "Flint and Steel": "打火石",
    # 物品 / 方块
    "Potion": "药水", "Enchantment": "附魔", "Ender Pearl": "末影珍珠",
    "Crafting Table": "工作台", "Furnace": "熔炉", "Anvil": "铁砧", "Ender Chest": "末影箱",
    "TNT": "TNT", "Bone Meal": "骨粉", "Golden Apple": "金苹果", "Enchanted Golden Apple": "附魔金苹果",
    # 附魔
    "Sharpness": "锋利", "Efficiency": "效率", "Fortune": "时运", "Silk Touch": "精准采集",
    "Unbreaking": "耐久", "Mending": "经验修补", "Fire Aspect": "火焰附加",
}


# 内置保留词：这些词翻译时保持原样（品牌 / 框架名）。
BUILTIN_KEEP = {"Minecraft", "Forge", "NeoForge", "Fabric", "OptiFine", "Java Edition", "Bedrock Edition"}


@lru_cache(maxsize=32)
def _term_hint_pattern(term_names: tuple[str, ...]) -> re.Pattern[str] | None:
    if not term_names:
        return None
    alternatives = "|".join(re.escape(term) for term in term_names)
    return re.compile(r"(?<![A-Za-z0-9])(" + alternatives + r")(?![A-Za-z0-9])", re.IGNORECASE)


def relevant_term_hints(entries: list[Entry], terms: dict[str, str] | None, excluded: set[str] | None = None, limit: int = 18) -> dict[str, str]:
    """Select specific terminology hints occurring in this batch; avoid forcing short polysemous words."""
    if not terms:
        return {}
    excluded_folded = {word.casefold() for word in (excluded or set())}
    text = "\n".join(entry.english for entry in entries)
    candidates: dict[str, tuple[str, str]] = {}
    for source, target in terms.items():
        source = str(source).strip()
        target = str(target).strip()
        if not source or not target or source.casefold() in excluded_folded:
            continue
        # Short single words (Lead, Map, Power, Thing, colors, etc.) are too ambiguous
        # to pin to one Chinese sense without sentence context.
        if " " not in source and len(source) < 7:
            continue
        candidates[source.casefold()] = (source, target)
    ordered = sorted(candidates.values(), key=lambda item: (-len(item[0]), item[0].casefold()))
    pattern = _term_hint_pattern(tuple(source for source, _ in ordered))
    if pattern is None:
        return {}
    matched: dict[str, str] = {}
    for match in pattern.finditer(text):
        key = match.group(1).casefold()
        source, target = candidates[key]
        matched.setdefault(source, target)
        if len(matched) >= limit:
            break
    # Because alternatives are longest-first, a longer phrase suppresses its contained shorter hint.
    selected: dict[str, str] = {}
    for source, target in sorted(matched.items(), key=lambda item: (-len(item[0]), item[0].casefold())):
        if any(re.search(r"(?<![A-Za-z0-9])" + re.escape(longer) + r"(?![A-Za-z0-9])", source, re.IGNORECASE) for longer in selected):
            continue
        selected[source] = target
    return selected


def load_user_glossary(path: Path = USER_GLOSSARY_FILE) -> dict[str, str]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k).strip(): str(v) for k, v in data.items() if str(k).strip() and str(v)}
    except (OSError, ValueError):
        pass
    return {}


def fetch_glossary(en_url: str, zh_url: str, timeout: int = 60) -> dict[str, str]:
    en_data = _http_get_json(en_url, timeout)
    zh_data = _http_get_json(zh_url, timeout)
    if not isinstance(en_data, dict) or not isinstance(zh_data, dict):
        raise RuntimeError("语言文件格式不正确（应为 JSON 对象）")
    glossary: dict[str, str] = {}
    for key, en in en_data.items():
        if not isinstance(key, str) or not key.startswith(GLOSSARY_TERM_PREFIXES):
            continue
        zh = zh_data.get(key)
        if not isinstance(en, str) or not isinstance(zh, str):
            continue
        en = en.strip()
        zh = zh.strip()
        if not en or not zh or len(en) > 50 or "\n" in en or "%" in en or "§" in en:
            continue
        glossary[en] = zh
    return glossary
