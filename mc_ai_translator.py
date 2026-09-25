#!/usr/bin/env python3
"""摆渡计划 / ProjectFerry: offline Minecraft mod translation pack generator.

人无语言则茫然无依，故有摆渡。
Where words fail, we ferry.

只读英文源语言文件和已有中文文件，计算缺失 key。识别 AI 生成的资源包与
社区/官方汉化：检测到某模组已有人工汉化时自动降级（默认不为其生成 AI 翻译）。

支持多翻译引擎：MyMemory（免费，默认）、DeepL Free、OpenAI 兼容、
Anthropic 兼容、Gemini 兼容、本地模型（Ollama / LM Studio）。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import heapq
import html
import io
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import os
import re
import shutil
import collections
import struct
import subprocess
import sys
import tempfile
import tomllib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# AI 生成的资源包内会写入这个标记文件，用于下次扫描时把它和社区/官方汉化区分开。
AI_MARKER = "AI_TRANSLATION_NOTICE.txt"
# 资源包内的这个文件记录「已强制还原英文」的键：这些键在 zh_cn.json 里是英文，
# 扫描时不算作已翻译，也不会再被“翻译全部”重新翻译。
REVERT_MARKER = "FERRY_REVERTED.json"
SOURCES_MARKER = "FERRY_SOURCES.json"
COMMUNITY_NOTICE = "COMMUNITY_ATTRIBUTION.txt"

CONFIG_FILE = Path(__file__).resolve().parent / "ferry_config.json"
PROGRESS_FILE = Path(__file__).resolve().parent / "ferry_progress.json"
USER_GLOSSARY_FILE = Path(__file__).resolve().parent / "user_glossary.json"
LOCKS_FILE = Path(__file__).resolve().parent / "ferry_locks.json"

DEFAULT_GLOSSARY_EN_URL = "https://cdn.jsdelivr.net/gh/PandaDevOfficial/Minecraft-All-Lang@main/en_gb.json"
LOG = logging.getLogger(__name__)
LOG_FILE = Path(__file__).resolve().parent / "ferry.log"


def redact_secrets(text: str) -> str:
    text = re.sub(r"(?i)(?:sk-|AIza|key-)[A-Za-z0-9_-]{8,}", "sk-***", text)
    return re.sub(r"(?i)(api[_-]?key|authorization)\s*[:=]\s*\S+", r"\1=sk-***", text)


class _RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secrets(record.getMessage())
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        if record.stack_info:
            record.stack_info = None
        return True


def configure_logging() -> None:
    if LOG.handlers:
        return
    handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=7, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(_RedactSecrets())
    LOG.addHandler(handler)
    LOG.setLevel(logging.DEBUG)


configure_logging()
DEFAULT_GLOSSARY_ZH_URL = "https://cdn.jsdelivr.net/gh/PandaDevOfficial/Minecraft-All-Lang@main/zh_cn.json"

# 只从官方语言文件里提取这些前缀的专有名词（实体/附魔/效果/物品），避免把通用 UI 文本塞进术语表。
GLOSSARY_TERM_PREFIXES = ("entity.minecraft.", "enchantment.minecraft.", "effect.minecraft.", "item.minecraft.")

# 默认排除纯前置、库、优化和辅助类模组，避免把没有面向玩家文本的依赖列出来。
# 这是“默认隐藏”而不是删除：命令行可用 --include-modid 强制纳入。
DEFAULT_EXCLUDED_MODIDS = {
    "architectury", "cloth_config", "curios", "ftb_library", "geckolib", "kotlinforforge",
    "kurolib", "libipn", "playeranimator", "player_animation_lib",
    "yet_another_config_lib_v3", "memoryleakfix", "starlight", "ferritecore", "entityculling",
    "imblocker", "sodium", "embeddium", "modernfix", "smoothboot", "optifine",
}
DEFAULT_EXCLUDED_NAME_HINTS = (
    "memoryleak", "kurolib", "starlight", "architectury", "cloth", "geckolib", "kotlin",
    "ferritecore", "entityculling", "imblocker", "libipn", "player-animation", "modernfix",
)

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

DEFAULT_CONFIG = {
    "engine": "mymemory",
    "fill_community": False,
    "refine_community": False,
    "community_enabled": True,
    "community_base_url": "https://cdn.jsdelivr.net/gh/CFPAOrg/Minecraft-Mod-Language-Package@main/assets",
    "community_dict_url": "https://cdn.jsdelivr.net/gh/CFPATools/i18n-dict@main",
    "community_cache_ttl_days": 7,
    "community_notice_accepted": False,
    "pack_name": "AI_Translation_LowPriority.zip",
    "output_dir": "",
    "cache_dir": "",
    "pack_format": 0,
    "batch_size": 30,
    "timeout": 180,
    "delay": 0.2,
    "concurrency": 1,
    # OpenAI 兼容
    "openai_base_url": "https://api.openai.com/v1",
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
    # Anthropic 兼容
    "anthropic_base_url": "https://api.anthropic.com",
    "anthropic_api_key": "",
    "anthropic_model": "claude-3-5-haiku-latest",
    # Gemini 兼容
    "gemini_base_url": "",
    "gemini_api_key": "",
    "gemini_model": "gemini-1.5-flash",
    # MyMemory
    "mymemory_email": "",
    # DeepL Free
    "deepl_api_key": "",
    "deepl_free": True,
    # 本地模型（Ollama / LM Studio 等，OpenAI 兼容）
    "local_base_url": "http://127.0.0.1:11434/v1",
    "local_api_key": "",
    "local_model": "qwen2.5",
    # 多模型池：多个引擎并行翻译不同批次，提速。空列表 = 只用单个引擎。
    "model_pool": [],
    # 自定义接口预设：名称 -> {provider, api_key, base_url, model}
    "custom_profiles": {},
    # 术语表（en -> zh）与专有名词限制表（保持不翻译）
    "glossary": {},
    "keep_untranslated": [],
    "skip_keys": {},
    # 是否已确认"部分模组无法完全汉化"的启动声明
    "disclaimer_accepted": False,
}

# (版本元组, 资源包格式)，升序排列；取 <= 目标版本的最大一项。
# 数据来源：minecraft.wiki/w/Pack_format 的资源包格式历史（1.21.9 起为小数格式）。
PACK_FORMATS = [
    ((1, 6, 1), 1),
    ((1, 9), 2),
    ((1, 11), 3),
    ((1, 13), 4),
    ((1, 15), 5),
    ((1, 16, 2), 6),
    ((1, 17), 7),
    ((1, 18), 8),
    ((1, 19), 9),
    ((1, 19, 3), 12),
    ((1, 19, 4), 13),
    ((1, 20), 15),
    ((1, 20, 2), 18),
    ((1, 20, 3), 22),
    ((1, 20, 5), 32),
    ((1, 21), 34),
    ((1, 21, 2), 42),
    ((1, 21, 4), 46),
    ((1, 21, 5), 55),
    ((1, 21, 6), 63),
    ((1, 21, 7), 64),
    ((1, 21, 9), 69.0),
    ((1, 21, 11), 75.0),
    ((26, 1), 84.0),
    ((26, 2), 88.0),
    ((26, 3), 97.1),
    ((26, 4), 98.0),
]

# 资源包 1.21.9（25w31a）起 pack.mcmeta 改用 min_format / max_format，
# 参考minecraft.wiki/w/Pack_format。旧格式的最高值是 64（1.21.7–1.21.8）。
MIN_MAX_FORMAT_SINCE = 65.0
# 游戏已改用年份版本号（如 26.2）。把误写的 1.26.3 之类的 "1.NN.x" 归一为年号版本。
YEAR_VERSION_MIN_SECOND = 22

ENGINES_WITH_KEY = {"openai", "anthropic", "gemini", "deepl"}
CLOUD_ENGINES = {"openai", "anthropic", "gemini", "deepl"}


def validate_engine_network(engine: str, base_url: str) -> None:
    if engine not in CLOUD_ENGINES:
        return
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{engine} 云模型必须使用 HTTPS 远程 API；本地地址请把引擎设为 local。")


def is_excluded_mod(modid: str, extra: set[str] | None = None) -> bool:
    normalized = modid.lower().replace("-", "_")
    excluded = DEFAULT_EXCLUDED_MODIDS | (extra or set())
    return normalized in excluded or any(hint in normalized for hint in DEFAULT_EXCLUDED_NAME_HINTS)


PLACEHOLDER_RE = re.compile(
    r"%(?:\d+\$)?[-+#0 ]*(?:\d+)?(?:\.\d+)?[a-zA-Z]"
    r"|\$\{[^}]+\}|\{\d+\}|\{[A-Za-z_][A-Za-z0-9_.-]*\}"
    r"|§[0-9a-fk-or]|\$\([^\s)]*\)",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """You are a careful Minecraft Java Edition localization assistant.
Translate English Minecraft mod strings into Simplified Chinese.
Rules:
- Return ONLY a valid JSON object mapping each input key to its translation.
- Preserve every placeholder, formatting code, newline marker, and punctuation variable exactly.
- Do not translate registry ids, technical identifiers, proper mod names, or values inside placeholders.
- Use concise natural in-game Chinese. Item/block/entity names should be noun phrases.
- Keep the original meaning; do not add explanations, notes, markdown, or extra keys.
"""

REFINE_PROMPT = """Use the provided human English/Chinese pairs as terminology and style references for this mod.
Translate only the untranslated keys in `strings` into natural, consistent Simplified Chinese.
The human reference translations must remain unchanged; they are context only and must never appear as output keys.
Return ONLY a JSON object for the keys in `strings`.
Preserve all placeholders and formatting codes exactly. Do not invent meanings or add keys.
"""


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


def _lenient_json_text(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def load_json_bytes(raw: bytes, source: str) -> dict[str, str]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"无法解析 {source}: {exc}") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            value = json.loads(_lenient_json_text(text))
        except json.JSONDecodeError as exc:
            raise ValueError(f"无法解析 {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"语言文件不是 JSON 对象: {source}")
    return {str(k): str(v) for k, v in value.items() if isinstance(v, (str, int, float, bool))}


def load_lang_bytes(raw: bytes, source: str) -> dict[str, str]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"无法解析 {source}: {exc}") from exc
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            result[key] = value
    return result


LANG_FILE_RE = re.compile(r"assets/([^/]+)/lang/(en_us|zh_cn)\.(json|lang)", re.IGNORECASE)
NESTED_JAR_RE = re.compile(r"META-INF/jars/[^/]+\.jar", re.IGNORECASE)
PATCHOULI_RE = re.compile(r"assets/([^/]+)/patchouli_books/([^/]+)/(en_us|zh_cn)/entries/(.+\.json)", re.IGNORECASE)


def patchouli_key(file: PatchouliFile, pointer: str) -> str:
    return f"patchouli:{file.book}/{file.rel_path}#{pointer}"


def patchouli_fields(data: dict[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    if isinstance(data.get("name"), str) and data["name"].strip():
        result.append(("/name", data["name"]))
    pages = data.get("pages")
    if isinstance(pages, list):
        for i, page in enumerate(pages):
            if isinstance(page, dict):
                for field_name in ("text", "title"):
                    if isinstance(page.get(field_name), str) and page[field_name].strip():
                        result.append((f"/pages/{i}/{field_name}", page[field_name]))
    return result


def missing_patchouli_entries(scan: ScanResult, modids: set[str] | None = None) -> list[Entry]:
    human = {(f.modid, f.book, f.rel_path) for f in scan.patchouli_community}
    ai = {(f.modid, f.book, f.rel_path) for f in scan.patchouli_ai}
    wanted = {name.lower().replace("-", "_") for name in modids} if modids else None
    result: list[Entry] = []
    for file in scan.patchouli_english:
        if wanted is not None and file.modid.lower().replace("-", "_") not in wanted:
            continue
        if (file.modid, file.book, file.rel_path) in human | ai:
            continue
        for pointer, text in patchouli_fields(file.data):
            result.append(Entry(file.modid, patchouli_key(file, pointer), text, file.path, "patchouli", file.book, file.rel_path, pointer))
    return result


def load_pack_patchouli(pack: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not pack.is_file():
        return result
    try:
        with zipfile.ZipFile(pack) as zf:
            for name in zf.namelist():
                match = PATCHOULI_RE.fullmatch(name)
                if match and match.group(3).lower() == "zh_cn":
                    data = json.loads(zf.read(name).decode("utf-8-sig"))
                    if isinstance(data, dict):
                        result[name] = data
    except (OSError, ValueError, zipfile.BadZipFile):
        LOG.warning("Could not load Patchouli entries in %s", pack, exc_info=True)
    return result


def translated_patchouli_files(scan: ScanResult, entries: list[Entry], translated: dict[str, dict[str, str]]) -> dict[str, dict[str, Any]]:
    by_file: dict[tuple[str, str, str], list[Entry]] = {}
    for entry in entries:
        if entry.kind == "patchouli":
            by_file.setdefault((entry.modid, entry.book, entry.rel_path), []).append(entry)
    result: dict[str, dict[str, Any]] = {}
    for file in scan.patchouli_english:
        group = by_file.get((file.modid, file.book, file.rel_path))
        if not group or len(group) != len(patchouli_fields(file.data)) or any(entry.key not in translated.get(file.modid, {}) for entry in group):
            continue
        data = json.loads(json.dumps(file.data, ensure_ascii=False))
        for entry in group:
            parts = entry.json_pointer.split("/")[1:]
            field = data
            for part in parts[:-1]:
                field = field[int(part)] if isinstance(field, list) else field[part]
            field[parts[-1]] = translated[file.modid][entry.key]
        result[f"assets/{file.modid}/patchouli_books/{file.book}/zh_cn/entries/{file.rel_path}"] = data
    return result


def _parse_lang_raw(raw: bytes, ext: str, source: str) -> dict[str, str]:
    return load_json_bytes(raw, source) if ext == "json" else load_lang_bytes(raw, source)


def language_files_in_zip(path: Path) -> list[LangFile]:
    with zipfile.ZipFile(path) as zf:
        return _language_files_in_archive(zf, str(path), depth=0)


def patchouli_files_in_zip(path: Path) -> list[PatchouliFile]:
    result: list[PatchouliFile] = []
    with zipfile.ZipFile(path) as zf:
        is_ai = AI_MARKER in zf.namelist()
        for name in zf.namelist():
            match = PATCHOULI_RE.fullmatch(name)
            if not match:
                continue
            try:
                data = json.loads(zf.read(name).decode("utf-8-sig"))
                if isinstance(data, dict):
                    result.append(PatchouliFile(match.group(1), match.group(2), match.group(4), data, f"{path}!{name}", match.group(3).lower(), is_ai))
            except (OSError, ValueError, zipfile.BadZipFile):
                LOG.warning("Skipping invalid Patchouli JSON %s!%s", path, name, exc_info=True)
    return result


def _language_files_in_archive(zf: zipfile.ZipFile, source: str, depth: int) -> list[LangFile]:
    result: list[LangFile] = []
    try:
        names = zf.namelist()
        is_ai = AI_MARKER in names
        for name in names:
            match = LANG_FILE_RE.fullmatch(name)
            if match:
                modid, locale, ext = match.group(1), match.group(2).lower(), match.group(3).lower()
                try:
                    data = _parse_lang_raw(zf.read(name), ext, f"{source}!{name}")
                except (ValueError, OSError, zipfile.BadZipFile) as exc:
                    LOG.warning("Skipping language file %s!%s: %s", source, name, exc)
                    continue
                result.append(LangFile(modid, locale, f"{source}!{name}", data, is_ai))
            elif NESTED_JAR_RE.fullmatch(name) and depth < 3:
                try:
                    with zipfile.ZipFile(io.BytesIO(zf.read(name))) as nested:
                        result.extend(_language_files_in_archive(nested, f"{source}!{name}", depth + 1))
                except (OSError, zipfile.BadZipFile, ValueError) as exc:
                    LOG.warning("Skipping invalid nested jar %s!%s: %s", source, name, exc)
    except (OSError, zipfile.BadZipFile) as exc:
        LOG.warning("Could not scan archive %s: %s", source, exc)
    return result


def language_files_in_directory(path: Path) -> list[LangFile]:
    result: list[LangFile] = []
    is_ai = (path / AI_MARKER).exists()
    files = sorted(list(path.rglob("*.json")) + list(path.rglob("*.lang")))
    for file in files:
        match = re.search(r"[\\/]assets[\\/]([^\\/]+)[\\/]lang[\\/](en_us|zh_cn)\.(json|lang)$", str(file), re.IGNORECASE)
        if not match:
            continue
        modid, locale, ext = match.group(1), match.group(2).lower(), match.group(3).lower()
        try:
            data = _parse_lang_raw(file.read_bytes(), ext, str(file))
        except ValueError as exc:
            print(f"[跳过] {exc}", file=sys.stderr)
            continue
        result.append(LangFile(modid, locale, str(file), data, is_ai))
    return result


def candidate_instance_roots() -> list[Path]:
    """Return existing common Minecraft instance roots without touching files."""
    home = Path.home()
    candidates = [
        home / "AppData/Roaming/.minecraft",
        home / "AppData/Roaming/PrismLauncher/instances",
        home / "AppData/Roaming/PrismLauncher",
        home / "AppData/Roaming/ATLauncher/instances",
        home / "AppData/Roaming/curseforge/minecraft/Instances",
        home / "AppData/Roaming/.minecraft/versions",
        Path("D:/Minecraft/versions"),
        Path("D:/游戏/.minecraft"),
        Path("D:/Minecraft/.minecraft"),
    ]
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate.is_dir() and candidate not in seen:
            result.append(candidate)
            seen.add(candidate)
    return result


def find_instance_dirs() -> list[Path]:
    """Find directories that look like MC instances by containing mods or resourcepacks."""
    roots = candidate_instance_roots()
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        probes = [root]
        if root.name.lower() in {"instances", "versions"}:
            try:
                probes.extend(x for x in root.iterdir() if x.is_dir())
            except OSError:
                pass
        for path in probes:
            if (path / "mods").is_dir() and path not in seen:
                found.append(path)
                seen.add(path)
    return found


def resolve_instance_path(value: str | None, interactive: bool = False) -> Path:
    if value:
        path = Path(value).expanduser()
        if not path.is_dir():
            raise FileNotFoundError(f"Minecraft 实例目录不存在: {path}")
        return path
    found = find_instance_dirs()
    if len(found) == 1:
        print(f"自动检测到实例: {found[0]}")
        return found[0]
    if interactive and found:
        print("检测到多个 Minecraft 实例:")
        for index, path in enumerate(found, 1):
            print(f"  {index}. {path}")
        answer = input("请输入编号，或直接粘贴实例路径: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(found):
            return found[int(answer) - 1]
        path = Path(answer).expanduser()
        if path.is_dir():
            return path
    if not found:
        raise FileNotFoundError("没有自动找到 Minecraft 实例，请使用 --instance 手动指定实例目录。")
    raise RuntimeError("检测到多个实例，请使用 --instance 指定，或在交互模式下运行。")


def scan_inputs(mods_dir: Path, resourcepacks_dir: Path | None, instance: Path | None = None) -> ScanResult:
    result = ScanResult()
    if not mods_dir.is_dir():
        raise FileNotFoundError(f"模组目录不存在: {mods_dir}")
    for jar in sorted(mods_dir.rglob("*.jar")):
        try:
            files = language_files_in_zip(jar)
            patchouli = patchouli_files_in_zip(jar)
        except (zipfile.BadZipFile, OSError) as exc:
            print(f"[跳过] 无法读取 {jar}: {exc}", file=sys.stderr)
            continue
        result.english.extend(x for x in files if x.locale == "en_us")
        result.community.extend(x for x in files if x.locale == "zh_cn")
        result.patchouli_english.extend(x for x in patchouli if x.locale == "en_us")
        result.patchouli_community.extend(x for x in patchouli if x.locale == "zh_cn")
    kubejs = (instance or mods_dir.parent) / "kubejs" / "assets"
    if kubejs.is_dir():
        for lang in sorted(kubejs.glob("*/lang/*")):
            match = re.fullmatch(r"(en_us|zh_cn)\.(json|lang)", lang.name, re.IGNORECASE)
            if not lang.is_file() or not match:
                continue
            try:
                data = _parse_lang_raw(lang.read_bytes(), match.group(2).lower(), str(lang))
            except (OSError, ValueError) as exc:
                LOG.warning("Skipping KubeJS language file %s: %s", lang, exc)
                continue
            file = LangFile(lang.parent.parent.name, match.group(1).lower(), str(lang), data)
            (result.english if file.locale == "en_us" else result.community).append(file)
    if resourcepacks_dir and resourcepacks_dir.is_dir():
        for pack in sorted(resourcepacks_dir.iterdir()):
            try:
                if pack.is_file() and pack.suffix.lower() in {".zip", ".jar"}:
                    files = language_files_in_zip(pack)
                    patchouli = patchouli_files_in_zip(pack)
                elif pack.is_dir():
                    files = language_files_in_directory(pack)
                    patchouli = []
                    for file in sorted(pack.rglob("*.json")):
                        match = PATCHOULI_RE.fullmatch(file.relative_to(pack).as_posix())
                        if match:
                            try:
                                data = json.loads(file.read_text(encoding="utf-8-sig"))
                                if isinstance(data, dict):
                                    patchouli.append(PatchouliFile(match.group(1), match.group(2), match.group(4), data, str(file), match.group(3).lower(), (pack / AI_MARKER).exists()))
                            except (OSError, ValueError):
                                LOG.warning("Skipping invalid Patchouli JSON %s", file, exc_info=True)
                else:
                    continue
            except (zipfile.BadZipFile, OSError) as exc:
                print(f"[跳过] 无法读取资源包 {pack}: {exc}", file=sys.stderr)
                continue
            for file in files:
                if file.locale != "zh_cn":
                    continue
                (result.ai if file.is_ai_generated else result.community).append(file)
            for modid, keys in load_revert_marker(pack).items():
                if keys:
                    result.reverted.setdefault(modid, set()).update(keys)
            result.patchouli_community.extend(x for x in patchouli if x.locale == "zh_cn" and not x.is_ai_generated)
            result.patchouli_ai.extend(x for x in patchouli if x.locale == "zh_cn" and x.is_ai_generated)
    return result


def load_revert_marker(pack: Path) -> dict[str, set[str]]:
    """Read the FERRY_REVERTED.json marker from a resource pack (zip/jar or folder)."""
    raw: str | None = None
    try:
        if pack.is_file() and pack.suffix.lower() in {".zip", ".jar"}:
            with zipfile.ZipFile(pack) as zf:
                if REVERT_MARKER in zf.namelist():
                    raw = zf.read(REVERT_MARKER).decode("utf-8-sig", "replace")
        elif pack.is_dir() and (pack / REVERT_MARKER).exists():
            raw = (pack / REVERT_MARKER).read_text(encoding="utf-8-sig", errors="replace")
    except (zipfile.BadZipFile, OSError, ValueError):
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, set[str]] = {}
    for modid, keys in data.items():
        if isinstance(keys, list):
            result[str(modid)] = {str(k) for k in keys}
    return result


def _modid_keys(files: list[LangFile]) -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {}
    for file in files:
        keys.setdefault(file.modid, set()).update(file.data)
    return keys


def build_mod_status(english: list[LangFile], community: list[LangFile], ai: list[LangFile], include_modids: set[str] | None = None, reverted: dict[str, set[str]] | None = None, community_sources: dict[str, set[str]] | None = None) -> list[ModTranslationStatus]:
    include = {x.lower().replace("-", "_") for x in (include_modids or set())}
    en_keys: dict[str, set[str]] = {}
    for file in english:
        if is_excluded_mod(file.modid) and file.modid.lower().replace("-", "_") not in include:
            continue
        en_keys.setdefault(file.modid, set()).update(key for key, value in file.data.items() if value.strip())
    com_keys = _modid_keys(community)
    ai_keys = _modid_keys(ai)
    community_sources = community_sources or {}
    revert_keys = _normalize_reverted(reverted)
    result: list[ModTranslationStatus] = []
    for modid in sorted(en_keys):
        keys = en_keys[modid]
        total = len(keys)
        reverted_set = keys & revert_keys.get(modid, set())
        community_set = (keys & (com_keys.get(modid, set()) | community_sources.get(modid, set()))) - reverted_set
        ai_set = (keys & ai_keys.get(modid, set())) - reverted_set - community_set
        uncovered = len(keys - community_set - ai_set - reverted_set)
        result.append(ModTranslationStatus(modid, total, len(community_set), len(ai_set), len(reverted_set), uncovered))
    return result


def missing_entries(english: list[LangFile], community: list[LangFile], ai: list[LangFile], include_modids: set[str] | None = None, fill_community: bool = False, modids: set[str] | None = None, reverted: dict[str, set[str]] | None = None, refine_community: bool = False) -> list[Entry]:
    include = {x.lower().replace("-", "_") for x in (include_modids or set())}
    modid_filter = {x.lower().replace("-", "_") for x in (modids or set())} if modids else None
    com_keys = _modid_keys(community)
    ai_keys = _modid_keys(ai)
    revert_keys = _normalize_reverted(reverted)
    result: list[Entry] = []
    seen: set[tuple[str, str]] = set()
    for file in english:
        normalized = file.modid.lower().replace("-", "_")
        if is_excluded_mod(file.modid) and normalized not in include:
            continue
        if modid_filter is not None and normalized not in modid_filter:
            continue
        com = com_keys.get(file.modid, set())
        ai = ai_keys.get(file.modid, set())
        reverted_set = revert_keys.get(file.modid, set())
        has_community = bool(com)
        for key, value in file.data.items():
            identity = (file.modid, key)
            if identity in seen or not value.strip():
                continue
            seen.add(identity)
            if key in reverted_set:
                continue
            if has_community and not fill_community and not refine_community:
                continue
            if key in com or key in ai:
                continue
            result.append(Entry(file.modid, key, value, file.path))
    return result


def skip_ignored_entries(entries: list[Entry], config: dict[str, Any]) -> list[Entry]:
    ignored = config.get("skip_keys") or {}
    if not isinstance(ignored, dict):
        return entries
    return [entry for entry in entries if entry.key not in ignored.get(entry.modid, [])]


def entries_with_baseline(scan: ScanResult, baseline: dict[str, dict[str, str]], include: set[str] | None = None, selected: set[str] | None = None, fill_community: bool = False, refine_community: bool = False) -> list[Entry]:
    files = scan.community + [LangFile(modid, "zh_cn", "community-baseline", data) for modid, data in baseline.items()]
    entries = missing_entries(scan.english, files, scan.ai, include, fill_community, selected, scan.reverted, refine_community)
    if not (fill_community or refine_community) and baseline:
        for entry in missing_entries(scan.english, files, scan.ai, include, True, set(baseline) & selected if selected else set(baseline), scan.reverted):
            if entry.modid in baseline and entry not in entries:
                entries.append(entry)
    return entries


def fetch_community_baseline(scan: ScanResult, config: dict[str, Any], cache_dir: Path, modids: set[str] | None = None) -> dict[str, dict[str, str]]:
    """Fetch optional community keys, excluding anything already present in the instance."""
    if not config.get("community_enabled", True):
        return {}
    base = str(config.get("community_base_url") or DEFAULT_CONFIG["community_base_url"]).rstrip("/")
    if urllib.parse.urlparse(base).scheme != "https":
        LOG.warning("Community base URL is not HTTPS; skipping")
        return {}
    cache = cache_dir / "community"
    cache.mkdir(parents=True, exist_ok=True)
    current = _modid_keys(scan.community)
    ai = _modid_keys(scan.ai)
    english: dict[str, dict[str, str]] = {}
    for file in scan.english:
        if modids is None or file.modid in modids:
            english.setdefault(file.modid, {}).update(file.data)
    result: dict[str, dict[str, str]] = {}
    for modid, original in english.items():
        if not re.fullmatch(r"[a-z0-9_.-]+", modid, re.IGNORECASE):
            continue
        path = cache / f"{modid}.json"
        raw: dict[str, str] = {}
        try:
            fresh = path.is_file() and time.time() - path.stat().st_mtime < float(config.get("community_cache_ttl_days", 7)) * 86400
            if fresh:
                raw = load_json_bytes(path.read_bytes(), str(path))
            else:
                url = f"{base}/{urllib.parse.quote(modid)}/lang/zh_cn.json"
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ProjectFerry/1.0"}), timeout=8) as response:
                    content = response.read(4 * 1024 * 1024 + 1)
                    if len(content) > 4 * 1024 * 1024:
                        raise ValueError("community language file too large")
                    raw = load_json_bytes(content, url)
                path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        except (OSError, ValueError, urllib.error.URLError) as exc:
            LOG.warning("Community baseline unavailable for %s: %s", modid, exc)
            continue
        filtered = {key: value for key, value in raw.items() if original.get(key, "").strip() and value.strip() and key not in current.get(modid, set()) and key not in ai.get(modid, set()) and key not in scan.reverted.get(modid, set())}
        if filtered:
            result[modid] = filtered
    scan.community_baseline = result
    return result


def community_corpus(english: list[LangFile], community: list[LangFile]) -> dict[str, dict[str, tuple[str, str]]]:
    english_values: dict[str, dict[str, str]] = {}
    for file in english:
        english_values.setdefault(file.modid, {}).update(file.data)
    corpus: dict[str, dict[str, tuple[str, str]]] = {}
    for file in community:
        for key, chinese in file.data.items():
            original = english_values.get(file.modid, {}).get(key)
            if original and chinese.strip():
                corpus.setdefault(file.modid, {})[key] = (original, chinese)
    return corpus


def reference_examples(batch: list[Entry], corpus: dict[str, tuple[str, str]], limit: int = 12) -> list[dict[str, str]]:
    prefixes = {entry.key.rsplit(".", 1)[0] for entry in batch}
    ranked = sorted(corpus, key=lambda key: (0 if key.rsplit(".", 1)[0] in prefixes else 1, key))
    return [{"key": key, "en": corpus[key][0], "zh": corpus[key][1]} for key in ranked[:limit]]


def _normalize_reverted(reverted: dict[str, set[str]] | None) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for modid, keys in (reverted or {}).items():
        result[str(modid)] = {str(k) for k in (keys or set())}
    return result


def english_map_for(english: list[LangFile], modids: set[str]) -> dict[str, dict[str, str]]:
    """Collect the full en_us key set (id -> 原英文) for the given modids."""
    wanted = {x.lower().replace("-", "_") for x in (modids or set())}
    result: dict[str, dict[str, str]] = {}
    for file in english:
        if file.modid.lower().replace("-", "_") not in wanted:
            continue
        result.setdefault(file.modid, {}).update(file.data)
    return result


# 判断 .class 里的字符串常量是否像“用户可见文本”，用于检测硬编码文本。
_HARDCODED_TECH_START = (
    "could not", "cannot", "can not", "can't", "failed", "invalid", "unknown",
    "missing", "no ", "not ", "unsupported", "error", "illegal", "unexpected",
    "trying to", "unable", "already", "warning", "incorrect", "duplicate",
    "uninitialized", "use of", "versions from", "application aborted",
)
_HARDCODED_TECH_ANY = (
    "config file", "packet", "message id", "message type", "stacktrace", "null",
    "class bytes", "deserializ", "thread", "mixin", "reflect", "logger", "handler",
    "parameter", "argument", "not exist", "does not exist", "not found", "not support",
    "not allowed", "not present", "must be", "must have", "is null", "registry",
    "bytecode", "modifier", "wrapper", "predicate", "instance", "lambda", "buffer",
    "can only", "have to", "need to be", "supposed", "read it backwards",
    "shader", "uniform", "lexer", "parser", "retry", "logging", "backing up",
    "unloaded", "accessor", "vertex", "zlib", "json", "nbt",
    "serializ", "initializ", "registered", "dependency", "namespace", "resource location",
)


def _class_utf8_constants(data: bytes) -> list[str]:
    out: list[str] = []
    if len(data) < 10 or data[:4] != b"\xca\xfe\xba\xbe":
        return out
    count = struct.unpack(">H", data[8:10])[0]
    i, n, index = 10, len(data), 1
    while index < count and i < n:
        tag = data[i]
        i += 1
        if tag == 1:
            length = struct.unpack(">H", data[i:i + 2])[0]
            i += 2
            raw = data[i:i + length]
            i += length
            try:
                out.append(raw.decode("utf-8"))
            except UnicodeDecodeError:
                pass
        elif tag in (7, 8, 16, 19, 20):
            i += 2
        elif tag == 15:
            i += 3
        elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
            i += 4
        elif tag in (5, 6):
            i += 8
            index += 1
        else:
            break
        index += 1
    return out


def _looks_like_ui_text(text: str) -> bool:
    if len(text) < 14 or len(text) > 160 or " " not in text:
        return False
    if not (text[0].isupper() or text[0].islower() or text[0] in "\"'"):
        return False
    low = text.lower()
    if any(low.startswith(word) for word in _HARDCODED_TECH_START):
        return False
    if any(word in low for word in _HARDCODED_TECH_ANY):
        return False
    if any(ch in text for ch in "/\\;()[]{}=<>@#%&*"):
        return False
    if re.search(r"[a-z][A-Z]", text):
        return False
    printable = sum(ch.isalpha() or ch.isspace() or ch in ".,!?'-" for ch in text)
    if printable < len(text) * 0.92:
        return False
    if len([w for w in text.split() if w]) < 3:
        return False
    return True


def detect_hardcoded_texts(mods_dir: Path, threshold: int = 5, sample_limit: int = 3) -> list[tuple[str, int, list[str]]]:
    """扫描 mods 目录，找出疑似含硬编码文本（无法通过资源包汉化）的模组。

    返回 [(jar 名, 疑似条数, 样例)]，按条数降序。
    """
    results: list[tuple[str, int, list[str]]] = []
    if not mods_dir.is_dir():
        return results
    for jar in sorted(mods_dir.rglob("*.jar")):
        if is_excluded_mod(jar.stem.lower()):
            continue
        try:
            files = language_files_in_zip(jar)
        except (zipfile.BadZipFile, OSError):
            continue
        modids = {file.modid for file in files}
        if modids and all(is_excluded_mod(modid) for modid in modids):
            continue
        lang_values = [str(value) for file in files if file.locale == "en_us" for value in file.data.values()]
        known = " ".join(lang_values).lower()
        constants: list[str] = []
        try:
            with zipfile.ZipFile(jar) as zf:
                for name in zf.namelist():
                    if not name.endswith(".class"):
                        continue
                    try:
                        constants.extend(_class_utf8_constants(zf.read(name)))
                    except (OSError, struct.error, zipfile.BadZipFile):
                        continue
        except (zipfile.BadZipFile, OSError):
            continue
        hardcoded = [text for text in dict.fromkeys(constants) if _looks_like_ui_text(text) and text.lower() not in known]
        if len(hardcoded) >= threshold:
            samples = sorted(hardcoded, key=len, reverse=True)[:sample_limit]
            results.append((jar.name, len(hardcoded), samples))
    results.sort(key=lambda item: -item[1])
    return results


def hardcoded_modids(scan: ScanResult, findings: list[tuple[str, int, list[str]]]) -> set[str]:
    """Associate heuristic class-string findings with language namespaces in the same jar."""
    flagged_jars = {name for name, _, _ in findings}
    return {
        file.modid
        for file in scan.english
        if Path(file.path.split("!", 1)[0]).name in flagged_jars
    }


def placeholder_tokens(value: str) -> list[str]:
    return PLACEHOLDER_RE.findall(value)


def mask_placeholders(value: str) -> tuple[str, dict[str, str]]:
    replacements: dict[str, str] = {}
    index = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal index
        token = f"__MC_PH_{index}__"
        replacements[token] = match.group(0)
        index += 1
        return token

    return PLACEHOLDER_RE.sub(replace, value), replacements


def mask_terms(value: str, glossary: dict[str, str], keep: set[str]) -> tuple[str, dict[str, str]]:
    items: list[tuple[str, str]] = []
    for word in keep:
        if word and len(word) >= 2:
            items.append((word, word))
    for en, zh in glossary.items():
        if en:
            items.append((en, zh))
    if not items:
        return value, {}
    items.sort(key=lambda x: len(x[0]), reverse=True)
    pattern = re.compile(
        r"(?<![A-Za-z0-9])(" + "|".join(re.escape(en) for en, _ in items) + r")(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    replacements: dict[str, str] = {}
    index = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal index
        token = f"__MC_TERM_{index}__"
        matched = match.group(0)
        target = matched
        for en, out in items:
            if en.lower() == matched.lower():
                target = out
                break
        replacements[token] = target
        index += 1
        return token

    return pattern.sub(replace, value), replacements


def mask_all(value: str, glossary: dict[str, str], keep: set[str]) -> tuple[str, dict[str, str]]:
    masked, replacements = mask_placeholders(value)
    masked, term_map = mask_terms(masked, glossary, keep)
    replacements.update(term_map)
    return masked, replacements


def modid_keep_words(modid: str) -> set[str]:
    words: set[str] = set()
    title = modid.replace("_", " ").replace("-", " ").strip()
    if len(title) >= 4:
        if modid.isupper():
            words.add(modid)
        else:
            words.add(title.title())
    return words


def restore_and_validate(source: str, translated: str, glossary: dict[str, str], keep: set[str]) -> str | None:
    _, source_map = mask_all(source, glossary, keep)
    expected = list(source_map)
    if any(token not in translated for token in expected):
        return None
    restored = translated
    for token, original in source_map.items():
        restored = restored.replace(token, original)
    if placeholder_tokens(source) != placeholder_tokens(restored):
        return None
    return restored


def group_entries(entries: list[Entry], batch_size: int) -> list[list[Entry]]:
    groups: list[list[Entry]] = []
    current: list[Entry] = []
    current_modid: str | None = None
    for entry in entries:
        if current and (entry.modid != current_modid or len(current) >= batch_size):
            groups.append(current)
            current = []
        current.append(entry)
        current_modid = entry.modid
    if current:
        groups.append(current)
    return groups


def cache_key(entries: list[Entry], engine: str, model: str, glossary: dict[str, str] | None = None, keep: set[str] | None = None, references: list[dict[str, str]] | None = None) -> str:
    context = ""
    if glossary or keep:
        context = json.dumps({"g": sorted((glossary or {}).items()), "k": sorted(keep or set())}, ensure_ascii=False, sort_keys=True)
    payload = json.dumps([(e.modid, e.key, e.english, e.kind, e.book, e.rel_path, e.json_pointer) for e in entries], ensure_ascii=False, sort_keys=True)
    reference_context = json.dumps(references, ensure_ascii=False, sort_keys=True) if references is not None else ""
    return hashlib.sha256((engine + "\n" + model + "\n" + context + "\n" + payload + "\n" + reference_context).encode("utf-8")).hexdigest()


def _model_input(masked: dict[str, str], settings: dict[str, Any]) -> str:
    if settings.get("refine_community"):
        return json.dumps({"strings": masked, "references": settings.get("reference_examples", [])}, ensure_ascii=False)
    return json.dumps(masked, ensure_ascii=False)


def _system_prompt(settings: dict[str, Any]) -> str:
    return SYSTEM_PROMPT + ("\n" + REFINE_PROMPT if settings.get("refine_community") else "")


def _parse_ai_json(content: Any) -> dict[str, str]:
    if isinstance(content, dict):
        return {str(k): str(v) for k, v in content.items()}
    if not isinstance(content, str):
        raise RuntimeError("AI 返回内容不是文本")
    stripped = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AI 返回的不是合法 JSON: {stripped[:200]}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("AI 返回的不是 JSON 对象")
    return {str(k): str(v) for k, v in value.items()}


def _http_error_detail(code: int) -> str:
    if code == 400:
        return "请求参数错误（400）"
    if code == 401:
        return "API key 无效或未授权（401）"
    if code == 403:
        return "无权限/访问被拒（403），可能是区域限制或账号问题"
    if code == 404:
        return "接口地址或模型名不存在（404），请检查 Base URL 和模型名"
    if code == 429:
        return "请求过于频繁，已被限流（429），稍后重试"
    if code == 456:
        return "DeepL 免费版配额已用完（456）"
    if code >= 500:
        return f"服务器内部错误（{code}），请稍后重试"
    return f"HTTP 错误（{code}）"


def _http_post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = redact_secrets(exc.read().decode("utf-8", "replace")[:300])
        raise RuntimeError(f"{_http_error_detail(exc.code)}：{detail}") from exc
    except TimeoutError as exc:
        raise RuntimeError("请求超时") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise RuntimeError("请求超时") from exc
        raise RuntimeError(f"网络连接失败：{redact_secrets(str(reason))}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("服务器返回的不是合法 JSON") from exc


def _translate_openai(masked: dict[str, str], settings: dict[str, Any]) -> dict[str, str]:
    payload = {
        "model": settings["model"],
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": _system_prompt(settings)},
            {"role": "user", "content": _model_input(masked, settings)},
        ],
        "response_format": {"type": "json_object"},
    }
    url = settings["base_url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    body = _http_post_json(url, payload, {"Content-Type": "application/json", "Authorization": f"Bearer {settings['api_key']}"}, settings["timeout"])
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("OpenAI 返回格式不正确") from exc
    return _parse_ai_json(content)


def _translate_anthropic(masked: dict[str, str], settings: dict[str, Any]) -> dict[str, str]:
    payload = {
        "model": settings["model"],
        "max_tokens": 4096,
        "temperature": 0.2,
        "system": _system_prompt(settings),
        "messages": [{"role": "user", "content": _model_input(masked, settings)}],
    }
    url = settings["base_url"].rstrip("/") + "/v1/messages"
    body = _http_post_json(url, payload, {
        "Content-Type": "application/json",
        "x-api-key": settings["api_key"],
        "anthropic-version": "2023-06-01",
    }, settings["timeout"])
    try:
        parts = body["content"]
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Anthropic 返回格式不正确") from exc
    return _parse_ai_json(text)


def _translate_gemini(masked: dict[str, str], settings: dict[str, Any]) -> dict[str, str]:
    payload = {
        "systemInstruction": {"parts": [{"text": _system_prompt(settings)}]},
        "contents": [{"parts": [{"text": _model_input(masked, settings)}]}],
        "generationConfig": {"temperature": 0.2},
    }
    base = settings["base_url"].rstrip("/")
    url = f"{base}/models/{settings['model']}:generateContent"
    body = _http_post_json(url, payload, {"Content-Type": "application/json", "x-goog-api-key": settings["api_key"]}, settings["timeout"])
    try:
        text = body["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Gemini 返回格式不正确") from exc
    return _parse_ai_json(text)


def _translate_mymemory(masked: dict[str, str], settings: dict[str, Any], cancel_event=None) -> dict[str, str]:
    email = settings.get("mymemory_email", "")
    result: dict[str, str] = {}
    for key, text in masked.items():
        if cancel_event is not None and cancel_event.is_set():
            break
        if len(text) > 450:
            print(f"  [跳过] MyMemory 文本超长: {key} ({len(text)} 字符)", file=sys.stderr)
            continue
        params = urllib.parse.urlencode({"q": text, "langpair": "en|zh-CN"})
        if email:
            params += "&" + urllib.parse.urlencode({"de": email})
        url = f"https://api.mymemory.translated.net/get?{params}"
        request = urllib.request.Request(url, headers={"User-Agent": "ProjectFerry/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=settings["timeout"]) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"  [跳过] MyMemory 请求失败: {key}: {exc}", file=sys.stderr)
            continue
        translated = html.unescape(body.get("responseData", {}).get("translatedText", "")).strip()
        if not translated or translated.upper().startswith("MYMEMORY WARNING") or "QUERY LENGTH LIMIT" in translated.upper():
            print(f"  [跳过] MyMemory 限额/失败: {key}", file=sys.stderr)
            continue
        result[key] = translated
    if not result and masked:
        raise RuntimeError("MyMemory 未返回任何翻译（可能免费配额用完或网络异常）")
    return result


def _translate_deepl(masked: dict[str, str], settings: dict[str, Any]) -> dict[str, str]:
    keys = list(masked.keys())
    texts = list(masked.values())
    form = urllib.parse.urlencode(
        [("text", t) for t in texts] + [("source_lang", "EN"), ("target_lang", "ZH")]
    ).encode("utf-8")
    url = settings["base_url"].rstrip("/") + "/translate"
    request = urllib.request.Request(url, data=form, headers={"Authorization": f"DeepL-Auth-Key {settings['api_key']}"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=settings["timeout"]) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = redact_secrets(exc.read().decode("utf-8", "replace")[:300])
        raise RuntimeError(f"{_http_error_detail(exc.code)}：{detail}") from exc
    except TimeoutError as exc:
        raise RuntimeError("DeepL 请求超时") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise RuntimeError("DeepL 请求超时") from exc
        raise RuntimeError(f"DeepL 网络连接失败：{reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("DeepL 返回的不是合法 JSON") from exc
    translations = body.get("translations", [])
    result: dict[str, str] = {}
    for key, item in zip(keys, translations):
        if isinstance(item, dict):
            result[key] = item.get("text", "")
    return result


def translate_batch(engine: str, masked: dict[str, str], settings: dict[str, Any], cancel_event=None) -> dict[str, str]:
    validate_engine_network(engine, settings.get("base_url", ""))
    if engine == "openai":
        return _translate_openai(masked, settings)
    if engine == "local":
        return _translate_openai(masked, settings)
    if engine == "anthropic":
        return _translate_anthropic(masked, settings)
    if engine == "gemini":
        return _translate_gemini(masked, settings)
    if engine == "mymemory":
        return _translate_mymemory(masked, settings, cancel_event)
    if engine == "deepl":
        return _translate_deepl(masked, settings)
    raise RuntimeError(f"未知翻译引擎: {engine}")


def _translate_batch_with_retry(engine: str, masked: dict[str, str], settings: dict[str, Any], retries: int = 2, cancel_event=None) -> dict[str, str]:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return translate_batch(engine, masked, settings, cancel_event)
        except RuntimeError as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def write_pack(output: Path, translations: dict[str, dict[str, str]], pack_format: int | float, reverted: dict[str, set[str]] | None = None, version_unconfirmed: bool = False, patchouli: dict[str, dict[str, Any]] | None = None, sources: dict[str, dict[str, str]] | None = None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    clean_reverted = {modid: sorted(keys) for modid, keys in _normalize_reverted(reverted).items() if keys}
    number = clean_pack_format(pack_format)
    pack_meta: dict[str, Any] = {"description": "摆渡计划 · AI 临时汉化（低优先级，占位稿）"}
    if uses_min_max_format(float(pack_format)):
        # 1.21.9 / 25w31a 起改用 min_format + max_format，取值可为整数或 [major, minor]。
        major, minor = split_pack_format(pack_format)
        value: Any = [major, minor] if minor else major
        pack_meta["min_format"] = value
        pack_meta["max_format"] = value
    else:
        pack_meta["pack_format"] = number
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pack.mcmeta", json.dumps({"pack": pack_meta}, ensure_ascii=False, indent=2))
        zf.writestr(AI_MARKER, "摆渡计划 / ProjectFerry —— 人无语言则茫然无依，故有摆渡。\n本资源包仅补人工汉化未覆盖的 en_us key；可选模式读取人工译文作为参考语料，不覆盖已有人工译文。\n检测到人工/官方汉化时自动让位（摆渡到岸即离）。\n")
        if version_unconfirmed:
            zf.writestr("README_VERSION_UNCONFIRMED.txt", "版本未确认：pack_format 15 仅为 --yes 强制回退。请核对 Minecraft 版本，可能无法加载本资源包。\n")
        for modid, data in sorted(translations.items()):
            if pack_format <= 3:
                if any("\n" in key or "\r" in key for key in data):
                    raise ValueError(f"{modid} 的语言 key 包含换行，无法写入旧版 .lang")
                content = "\n".join(f"{key}={value.replace(chr(13), '').replace(chr(10), r'\n')}" for key, value in sorted(data.items())) + "\n"
                zf.writestr(f"assets/{modid}/lang/zh_CN.lang", content.encode("utf-8"))
            else:
                zf.writestr(f"assets/{modid}/lang/zh_cn.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        for path, data in sorted((patchouli or {}).items()):
            if not PATCHOULI_RE.fullmatch(path) or "/zh_cn/" not in path.lower():
                raise ValueError(f"Invalid Patchouli output path: {path}")
            zf.writestr(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        if clean_reverted:
            zf.writestr(REVERT_MARKER, json.dumps(clean_reverted, ensure_ascii=False, indent=2) + "\n")
        sources = sources or {modid: {key: "ai" for key in data} for modid, data in translations.items()}
        sources = {modid: {key: source for key, source in entries.items() if key in translations.get(modid, {}) and source in {"ai", "community"}} for modid, entries in sources.items()}
        zf.writestr(SOURCES_MARKER, json.dumps(sources, ensure_ascii=False, indent=2) + "\n")
        if any("community" in entries.values() for entries in sources.values()):
            zf.writestr(COMMUNITY_NOTICE, "部分译文来源：CFPAOrg/Minecraft-Mod-Language-Package 社区汉化项目。原作者保留其版权；请遵守原项目许可证与署名要求。\nhttps://github.com/CFPAOrg/Minecraft-Mod-Language-Package\n")


def load_pack_reverted(output: Path) -> dict[str, set[str]]:
    return load_revert_marker(output)


def load_pack_translations(output: Path) -> dict[str, dict[str, str]]:
    if not output.exists():
        return {}
    result: dict[str, dict[str, str]] = {}
    try:
        with zipfile.ZipFile(output) as zf:
            for name in zf.namelist():
                match = re.fullmatch(r"assets/([^/]+)/lang/zh_cn\.(json|lang)", name, re.IGNORECASE)
                if not match:
                    continue
                modid = match.group(1)
                try:
                    data = _parse_lang_raw(zf.read(name), match.group(2).lower(), f"{output}!{name}")
                    result.setdefault(modid, {}).update(data)
                except ValueError:
                    continue
    except (zipfile.BadZipFile, OSError):
        pass
    return result


def migrate_legacy_pack(pack: Path, pack_format: int) -> bool:
    """Replace an old ProjectFerry JSON language pack with a 1.6–1.12 .lang pack."""
    if pack_format > 3 or not pack.is_file():
        return False
    try:
        with zipfile.ZipFile(pack) as zf:
            names = set(zf.namelist())
            if AI_MARKER not in names or not any(re.fullmatch(r"assets/[^/]+/lang/zh_cn\.json", name, re.IGNORECASE) for name in names):
                return False
            version_unconfirmed = "README_VERSION_UNCONFIRMED.txt" in names
    except (OSError, zipfile.BadZipFile):
        return False
    translations = load_pack_translations(pack)
    sources = load_pack_sources(pack)
    reverted = load_pack_reverted(pack)
    pages = load_pack_patchouli(pack)
    backup = pack.with_name(pack.name + ".pre-lang-backup")
    fd, name = tempfile.mkstemp(dir=pack.parent, suffix=".zip")
    os.close(fd)
    temporary = Path(name)
    try:
        write_pack(temporary, translations, pack_format, reverted, version_unconfirmed, pages, sources)
        if not backup.exists():
            shutil.copy2(pack, backup)
        os.replace(temporary, pack)
    finally:
        temporary.unlink(missing_ok=True)
    LOG.info("Migrated legacy language pack to zh_CN.lang: %s", pack)
    return True


def load_pack_sources(output: Path) -> dict[str, dict[str, str]]:
    if not output.is_file():
        return {}
    try:
        with zipfile.ZipFile(output) as zf:
            raw = json.loads(zf.read(SOURCES_MARKER).decode("utf-8-sig"))
            return {modid: {key: source for key, source in entries.items() if source in {"ai", "community"}} for modid, entries in raw.items() if isinstance(modid, str) and isinstance(entries, dict)}
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, AttributeError):
        return {}


def load_locks(path: Path = LOCKS_FILE) -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {modid: {key: value for key, value in data.items() if isinstance(key, str) and isinstance(value, str)} for modid, data in raw.items() if isinstance(data, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


def lock_translation(modid: str, key: str, value: str, path: Path = LOCKS_FILE) -> None:
    locks = load_locks(path)
    locks.setdefault(modid, {})[key] = value
    path.write_text(json.dumps(locks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def remove_pack_keys(pack: Path, keys: dict[str, set[str]], locks_path: Path = LOCKS_FILE) -> int:
    translations = load_pack_translations(pack)
    locks = load_locks(locks_path)
    sources = load_pack_sources(pack)
    removed = 0
    for modid, selected in keys.items():
        for key in selected:
            if key not in locks.get(modid, {}) and key in translations.get(modid, {}) and sources.get(modid, {}).get(key, "ai") == "ai":
                del translations[modid][key]
                removed += 1
        if not translations.get(modid):
            translations.pop(modid, None)
    if removed:
        pages = load_pack_patchouli(pack)
        if translations or pages:
            write_pack(pack, translations, read_pack_format(pack) or 15, load_pack_reverted(pack), patchouli=pages, sources=load_pack_sources(pack))
        else:
            pack.unlink()
    return removed


def quality_issues(scan: ScanResult, pack: Path, locks_path: Path = LOCKS_FILE) -> list[tuple[str, str, str, str, str, str]]:
    english = english_map_for(scan.english, {file.modid for file in scan.english})
    translations = load_pack_translations(pack)
    sources = load_pack_sources(pack)
    locks = load_locks(locks_path)
    result: list[tuple[str, str, str, str, str, str]] = []
    for modid, data in translations.items():
        for key, target in data.items():
            if key in locks.get(modid, {}) or sources.get(modid, {}).get(key, "ai") != "ai":
                continue
            source = english.get(modid, {}).get(key)
            if source is None:
                result.append((modid, key, "", target, "陈旧 key", "警告"))
                continue
            issue = ""
            severity = "错误"
            if not target.strip():
                issue = "空译文"
            elif source == target:
                issue = "译文等于原文"
            elif sorted(placeholder_tokens(source)) != sorted(placeholder_tokens(target)):
                issue = "占位符丢失或错乱"
            elif re.search(r"§(?![0-9a-fk-or])", target, re.IGNORECASE):
                issue = "格式码错乱"
            elif not re.search(r"[\u3400-\u9fff]", target) and len(source) > 15:
                issue, severity = "疑似残留英文", "警告"
            elif len(target) > len(source) * 3 or len(target) * 5 < len(source):
                issue, severity = "长度异常", "警告"
            if issue:
                result.append((modid, key, source, target, issue, severity))
    return result


def merged_sources(pack: Path, translations: dict[str, dict[str, str]], new_sources: dict[str, dict[str, str]] | None = None) -> dict[str, dict[str, str]]:
    old = load_pack_sources(pack)
    new_sources = new_sources or {}
    return {modid: {key: new_sources.get(modid, {}).get(key, old.get(modid, {}).get(key, "ai")) for key in data} for modid, data in translations.items()}


def retranslate_pack_keys(scan: ScanResult, pack: Path, selected: dict[str, set[str]], settings: dict[str, Any]) -> tuple[int, list[str]]:
    locks = load_locks()
    human = _modid_keys(scan.community)
    sources = load_pack_sources(pack)
    original = english_map_for(scan.english, set(selected))
    entries = [Entry(modid, key, original[modid][key], "QC") for modid, keys in selected.items() for key in keys if key in original.get(modid, {}) and key not in locks.get(modid, {}) and key not in human.get(modid, set()) and sources.get(modid, {}).get(key, "ai") == "ai"]
    if not entries:
        return 0, []
    translated, errors, _ = translate_entries(entries, settings, bypass_cache=True)
    if translated:
        merged = merge_pack_translations(pack, translated, scan.community)
        write_pack(pack, merged, settings["pack_format"], load_pack_reverted(pack), patchouli=load_pack_patchouli(pack), sources=merged_sources(pack, merged))
    return sum(map(len, translated.values())), errors


def human_pack_destination(instance: Path, modid: str) -> Path:
    if not re.fullmatch(r"[a-z0-9_.-]+", modid, flags=re.IGNORECASE) or modid in {".", ".."}:
        raise ValueError("模组 ID 无效，无法生成资源包文件名。")
    return instance / "resourcepacks" / f"ProjectFerry_Human_{modid}.zip"


def download_human_pack(url: str, instance: Path, modid: str) -> Path:
    """Download a direct resource-pack ZIP and install it without changing the AI pack."""
    if urllib.parse.urlparse(url).scheme.lower() != "https":
        raise ValueError("请输入人工汉化资源包的 HTTPS ZIP 直链。")
    destination = human_pack_destination(instance, modid)
    if not (instance / "mods").is_dir():
        raise ValueError(f"实例的 mods 目录不存在：{instance}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ProjectFerry/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            if urllib.parse.urlparse(response.geturl()).scheme.lower() != "https":
                raise ValueError("下载链接跳转到了非 HTTPS 地址。")
            with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".zip.part", delete=False) as handle:
                temporary = Path(handle.name)
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 64 * 1024 * 1024:
                        raise ValueError("资源包超过 64 MB，请检查下载链接。")
                    handle.write(chunk)
        try:
            with zipfile.ZipFile(temporary) as zf:
                files = zf.infolist()
                if len(files) > 5000 or sum(info.file_size for info in files) > 256 * 1024 * 1024:
                    raise ValueError("资源包解压后体积过大。")
                if any(info.filename.startswith("/") or "\\" in info.filename or ".." in info.filename.split("/") for info in files):
                    raise ValueError("资源包中含有不安全的路径。")
                names = {info.filename for info in files}
                if AI_MARKER in names:
                    raise ValueError("下载内容是 AI 汉化包，无法作为人工汉化导入。")
                if "pack.mcmeta" not in names:
                    raise ValueError("压缩包根目录缺少 pack.mcmeta，请使用资源包 ZIP 直链。")
                try:
                    metadata = json.loads(zf.read("pack.mcmeta").decode("utf-8-sig"))
                    if not isinstance(metadata.get("pack"), dict):
                        raise ValueError("pack.mcmeta 缺少 pack 配置。")
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError("资源包的 pack.mcmeta 无效。") from exc
                matches = [info for info in files if (m := LANG_FILE_RE.fullmatch(info.filename)) and m.group(1).lower() == modid.lower() and m.group(2).lower() == "zh_cn"]
                if not matches or not any(any(value.strip() for value in _parse_lang_raw(zf.read(info), info.filename.rsplit(".", 1)[1].lower(), info.filename).values()) for info in matches):
                    raise ValueError(f"资源包里没有 {modid} 的有效 zh_cn 语言文件。")
                if zf.testzip() is not None:
                    raise ValueError("资源包 ZIP 校验失败。")
        except zipfile.BadZipFile as exc:
            raise ValueError("下载内容不是有效的 ZIP 资源包。") from exc
        os.replace(temporary, destination)
        temporary = None
        return destination
    except urllib.error.URLError as exc:
        raise RuntimeError(f"下载人工汉化包失败：{exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def merge_pack_translations(output: Path, new_translations: dict[str, dict[str, str]], community: list[LangFile] | None = None) -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, str]] = {modid: dict(data) for modid, data in load_pack_translations(output).items()}
    for modid, data in new_translations.items():
        if data:
            merged.setdefault(modid, {}).update(data)
    human_keys = _modid_keys(community or [])
    reverted = load_pack_reverted(output)
    locks = load_locks()
    for modid, data in list(merged.items()):
        for key in human_keys.get(modid, set()) - reverted.get(modid, set()) - locks.get(modid, {}).keys():
            data.pop(key, None)
        for key, value in locks.get(modid, {}).items():
            if key in data:
                data[key] = value
        if not data:
            del merged[modid]
    return merged


def yield_to_community(scan: ScanResult, pack: Path) -> bool:
    """Remove this tool's overrides when human translations appear for the same keys."""
    if not pack.is_file():
        return False
    try:
        with zipfile.ZipFile(pack) as zf:
            if AI_MARKER not in zf.namelist():
                return False
    except (zipfile.BadZipFile, OSError):
        return False
    translations = load_pack_translations(pack)
    original_sources = load_pack_sources(pack)
    patchouli = load_pack_patchouli(pack)
    human_pages = {f"assets/{file.modid}/patchouli_books/{file.book}/zh_cn/entries/{file.rel_path}" for file in scan.patchouli_community}
    removed_pages = human_pages & patchouli.keys()
    for page in removed_pages:
        del patchouli[page]
    reverted = load_pack_reverted(pack)
    community_keys = _modid_keys(scan.community)
    removed: dict[str, set[str]] = {}
    locks = load_locks()
    for modid, data in list(translations.items()):
        overlap = (set(data) & community_keys.get(modid, set())) - reverted.get(modid, set()) - locks.get(modid, {}).keys()
        if not overlap:
            continue
        removed[modid] = overlap
        for key in overlap:
            del data[key]
        if not data:
            del translations[modid]
        if modid in reverted:
            reverted[modid].difference_update(overlap)
            if not reverted[modid]:
                del reverted[modid]
    if not removed and not removed_pages:
        return False
    if translations or reverted or patchouli:
        write_pack(pack, translations, read_pack_format(pack) or 15, reverted, patchouli=patchouli, sources=original_sources)
    else:
        pack.unlink()
    for file in scan.ai:
        if file.path.startswith(f"{pack}!"):
            for key in removed.get(file.modid, set()):
                file.data.pop(key, None)
    scan.ai = [file for file in scan.ai if file.data]
    scan.patchouli_ai = [file for file in scan.patchouli_ai if f"assets/{file.modid}/patchouli_books/{file.book}/zh_cn/entries/{file.rel_path}" not in removed_pages]
    for modid, keys in removed.items():
        if modid in scan.reverted:
            scan.reverted[modid].difference_update(keys)
    return True


def read_pack_format(output: Path) -> float | None:
    if not output.exists():
        return None
    try:
        with zipfile.ZipFile(output) as zf:
            if "pack.mcmeta" not in zf.namelist():
                return None
            data = json.loads(zf.read("pack.mcmeta").decode("utf-8-sig", "replace"))
            pack = data.get("pack", {})
            for key in ("pack_format", "min_format", "max_format"):
                value = pack.get(key)
                if isinstance(value, (list, tuple)) and len(value) >= 2:
                    return float(value[0]) + float(value[1]) / 10
                if isinstance(value, (int, float)):
                    return float(value)
            return None
    except (zipfile.BadZipFile, OSError, ValueError, TypeError):
        return None


def uninstalled_ai_path(pack: Path) -> Path:
    return pack.with_name(pack.name + ".uninstalled.json")


def load_uninstalled_ai(pack: Path) -> dict[str, dict[str, str]]:
    path = uninstalled_ai_path(pack)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        modid: {key: value for key, value in entries.items() if isinstance(key, str) and isinstance(value, str)}
        for modid, entries in raw.items()
        if isinstance(modid, str) and isinstance(entries, dict)
    }


def _save_uninstalled_ai(pack: Path, saved: dict[str, dict[str, str]]) -> None:
    path = uninstalled_ai_path(pack)
    if not saved:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def remove_ai_translations(pack: Path, modids: set[str]) -> list[str]:
    """Uninstall AI translations, retaining them beside the pack for a later reload."""
    if not pack.is_file():
        return []
    try:
        with zipfile.ZipFile(pack) as zf:
            if AI_MARKER not in zf.namelist():
                return []
    except (OSError, zipfile.BadZipFile):
        return []
    translations = load_pack_translations(pack)
    sources = load_pack_sources(pack)
    reverted = load_pack_reverted(pack)
    saved = load_uninstalled_ai(pack)
    removed: list[str] = []
    for modid in modids:
        data = translations.get(modid, {})
        ai_data = {key: value for key, value in data.items() if key not in reverted.get(modid, set()) and sources.get(modid, {}).get(key, "ai") == "ai"}
        if not ai_data:
            continue
        saved.setdefault(modid, {}).update(ai_data)
        for key in ai_data:
            del data[key]
        if not data:
            del translations[modid]
        removed.append(modid)
    if removed:
        _save_uninstalled_ai(pack, saved)
        pages = load_pack_patchouli(pack)
        if translations or reverted or pages:
            write_pack(pack, translations, read_pack_format(pack) or 15, reverted, patchouli=pages, sources=sources)
        else:
            pack.unlink()
    return removed


def reload_ai_translations(pack: Path, modids: set[str], scan: ScanResult, pack_format: int) -> list[str]:
    """Restore saved AI keys that still exist in English and lack a human translation."""
    saved = load_uninstalled_ai(pack)
    human_keys = _modid_keys(scan.community)
    english = english_map_for(scan.english, modids)
    reverted = load_pack_reverted(pack)
    restored: dict[str, dict[str, str]] = {}
    for modid in modids:
        if modid not in saved or modid not in english:
            continue
        eligible = {
            key: value for key, value in saved[modid].items()
            if english[modid].get(key, "").strip()
            and key not in human_keys.get(modid, set())
            and key not in reverted.get(modid, set())
        }
        if eligible:
            restored[modid] = eligible
        del saved[modid]
    if restored:
        merged = merge_pack_translations(pack, restored, scan.community)
        write_pack(pack, merged, pack_format, reverted, patchouli=load_pack_patchouli(pack), sources=merged_sources(pack, merged))
    _save_uninstalled_ai(pack, saved)
    return sorted(restored)


def revert_mods(pack: Path, english_map: dict[str, dict[str, str]], pack_format: int) -> list[str]:
    """Write English values as zh_cn overrides so the given mods show English, and mark them reverted."""
    if not english_map:
        return []
    translations = load_pack_translations(pack)
    reverted = load_pack_reverted(pack)
    for modid, data in english_map.items():
        translations[modid] = {key: value for key, value in data.items()}
        reverted[modid] = set(data.keys())
    write_pack(pack, translations, pack_format, reverted, patchouli=load_pack_patchouli(pack), sources=load_pack_sources(pack))
    return list(english_map.keys())


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def normalize_version_tuple(vt: tuple[int, ...]) -> tuple[int, ...]:
    """游戏已用年份版本号（26.2）。兼容用户误写成的 1.26.3 形式。"""
    if len(vt) >= 2 and vt[0] == 1 and vt[1] >= YEAR_VERSION_MIN_SECOND:
        return (vt[1],) + tuple(vt[2:])
    return vt


def uses_min_max_format(pack_format: float) -> bool:
    return float(pack_format) >= MIN_MAX_FORMAT_SINCE


def clean_pack_format(value: Any) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else number


def pack_format_for_version(version: str) -> int | float:
    vt = normalize_version_tuple(_version_tuple(version))
    if not vt:
        raise ValueError(f"无法确认 {version} 的资源包格式，请手动指定 pack_format。")
    if vt < PACK_FORMATS[0][0]:
        raise ValueError(f"无法确认 {version} 的资源包格式，请手动指定 pack_format。")
    chosen = PACK_FORMATS[0][1]
    for ver, fmt in PACK_FORMATS:
        if ver <= vt:
            chosen = fmt
        else:
            break
    return clean_pack_format(chosen)


MC_VERSION_RE = re.compile(r"(?<![\d.])(1\.\d{1,2}(?:\.\d+)?|\d{2}\.\d{1,2}(?:\.\d+)?)(?![\d.])")


def client_jars(instance_path: Path) -> list[Path]:
    """Client jars that may contain the authoritative version.json."""
    candidates: list[Path] = []
    try:
        candidates.extend(sorted(instance_path.glob("*.jar")))
        candidates.extend(sorted(instance_path.glob("versions/*/*.jar")))
        candidates.extend(sorted((instance_path / ".minecraft" / "versions").glob("*/*.jar")))
    except OSError:
        LOG.debug("Could not enumerate client jars in %s", instance_path, exc_info=True)
    return candidates


def client_version_json(instance_path: Path) -> dict[str, Any] | None:
    """Read version.json from the client jar; it carries the exact pack_version."""
    for jar in client_jars(instance_path):
        try:
            with zipfile.ZipFile(jar) as zf:
                if "version.json" not in zf.namelist():
                    continue
                data = json.loads(zf.read("version.json").decode("utf-8-sig"))
                if isinstance(data, dict):
                    LOG.debug("Read version.json from %s", jar)
                    return data
        except (OSError, ValueError, zipfile.BadZipFile):
            LOG.debug("Could not read version.json from %s", jar, exc_info=True)
    return None


def client_pack_format(instance_path: Path) -> int | float | None:
    """Authoritative resource pack format from the client's pack_version (major.minor)."""
    data = client_version_json(instance_path)
    if not data:
        return None
    pack_version = data.get("pack_version")
    if not isinstance(pack_version, dict):
        return None
    try:
        major = int(pack_version["resource_major"])
        minor = int(pack_version.get("resource_minor") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= minor <= 9:
        LOG.warning("Unexpected resource_minor %s; using major only", minor)
        minor = 0
    return clean_pack_format(major + minor / 10)


def split_pack_format(pack_format: int | float) -> tuple[int, int]:
    number = float(pack_format)
    major = int(number)
    minor = int(round((number - major) * 10))
    if not 0 <= minor <= 9:
        major, minor = int(round(number)), 0
    return major, minor


def detect_minecraft_version(instance_path: Path) -> str | None:
    mmc = instance_path / "mmc-pack.json"
    if mmc.exists():
        try:
            data = json.loads(mmc.read_text(encoding="utf-8"))
            for comp in data.get("components", []):
                if comp.get("uid") == "net.minecraft":
                    version = MC_VERSION_RE.search(str(comp.get("version", "")))
                    if version:
                        LOG.debug("Minecraft version %s from %s", version.group(), mmc)
                        return version.group()
        except (OSError, ValueError):
            LOG.debug("Could not read %s", mmc, exc_info=True)

    data = client_version_json(instance_path)
    if data and data.get("id"):
        match = MC_VERSION_RE.search(str(data["id"]))
        if match:
            LOG.debug("Minecraft version %s from client version.json", match.group())
            return match.group()

    for root in (instance_path / "versions", instance_path / ".minecraft" / "versions"):
        if not root.is_dir():
            continue
        found = []
        for version_dir in sorted(root.iterdir()):
            if not version_dir.is_dir():
                continue
            descriptor = version_dir / (version_dir.name + ".json")
            jar = version_dir / (version_dir.name + ".jar")
            if not descriptor.is_file() and not jar.is_file():
                continue
            label = version_dir.name
            if descriptor.is_file():
                try:
                    data = json.loads(descriptor.read_text(encoding="utf-8-sig"))
                    label = str(data.get("id", ""))
                except (OSError, ValueError, AttributeError):
                    LOG.debug("Could not read version descriptor %s", descriptor, exc_info=True)
            match = MC_VERSION_RE.search(label)
            if match:
                found.append(match.group())
        if len(set(found)) == 1:
            LOG.debug("Minecraft version %s from %s", found[0], root)
            return found[0]
        if found:
            LOG.debug("Ambiguous versions in %s: %s", root, sorted(set(found)))

    # Launcher-specific files are not used until their real field format is confirmed.
    for launcher in ("hmclversion.cfg", "PCL.ini"):
        if (instance_path / launcher).is_file():
            LOG.debug("Skipping unverified launcher format: %s", instance_path / launcher)

    votes: collections.Counter[str] = collections.Counter()
    mods = instance_path / "mods"
    if mods.is_dir():
        for jar in sorted(mods.glob("*.jar"))[:50]:
            try:
                with zipfile.ZipFile(jar) as zf:
                    names = set(zf.namelist())
                    if "META-INF/mods.toml" in names:
                        data = tomllib.loads(zf.read("META-INF/mods.toml").decode("utf-8-sig"))
                        for dependencies in data.get("dependencies", {}).values():
                            for dep in dependencies:
                                if dep.get("modId", "").lower() == "minecraft":
                                    matches = set(MC_VERSION_RE.findall(str(dep.get("versionRange", ""))))
                                    if len(matches) == 1:
                                        votes[next(iter(matches))] += 1
                    if "mcmod.info" in names:
                        data = json.loads(zf.read("mcmod.info").decode("utf-8-sig"))
                        for mod in data if isinstance(data, list) else data.get("modList", []):
                            match = MC_VERSION_RE.search(str(mod.get("mcversion", "")))
                            if match:
                                votes[match.group()] += 1
                    if "fabric.mod.json" in names:
                        data = json.loads(zf.read("fabric.mod.json").decode("utf-8-sig"))
                        matches = set(MC_VERSION_RE.findall(str(data.get("depends", {}).get("minecraft", ""))))
                        if len(matches) == 1:
                            votes[next(iter(matches))] += 1
            except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
                LOG.debug("Could not read Minecraft metadata in %s", jar, exc_info=True)
    if votes:
        ranked = votes.most_common()
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            LOG.debug("Minecraft version %s from mod metadata votes %s", ranked[0][0], votes)
            return ranked[0][0]
        LOG.debug("Ambiguous mod metadata versions: %s", votes)

    match = MC_VERSION_RE.search(instance_path.name)
    if match:
        LOG.debug("Minecraft version %s from instance directory", match.group())
        return match.group()
    LOG.debug("No Minecraft version detected for %s", instance_path)
    return None


def detect_pack_format(instance_path: Path) -> int | float:
    # 优先读取客户端 version.json 的 pack_version，权威且含 minor，无需维护版本表。
    exact = client_pack_format(instance_path)
    if exact is not None:
        return exact
    version = detect_minecraft_version(instance_path)
    if version is None:
        raise ValueError("未检测到游戏版本；请在 GUI 选择版本或在命令行指定 --pack-format N。")
    return pack_format_for_version(version)


def resolve_pack_format(config: dict[str, Any], instance: Path, overrides: dict[str, Any] | None = None) -> int | float:
    overrides = overrides or {}
    if overrides.get("pack_format"):
        return clean_pack_format(overrides["pack_format"])
    if config.get("pack_format"):
        return clean_pack_format(config["pack_format"])
    pack = Path(overrides.get("output_dir") or config.get("output_dir") or instance / "resourcepacks") / (overrides.get("pack_name") or config.get("pack_name") or DEFAULT_CONFIG["pack_name"])
    existing = read_pack_format(pack)
    try:
        detected = detect_pack_format(instance)
    except ValueError:
        detected = None
    # 向上兼容：检测到的版本比旧包格式新时升级；反之尊重已有（可能是手动或更高版本）格式。
    if existing is not None and (detected is None or float(existing) >= float(detected)):
        LOG.debug("Reusing pack_format %s from %s (detected %s)", existing, pack, detected)
        return clean_pack_format(existing)
    if detected is not None:
        if existing is not None:
            LOG.info("Upgrading pack_format %s -> %s for %s", existing, detected, pack)
        return detected
    if existing is not None:
        return clean_pack_format(existing)
    raise ValueError("未检测到游戏版本；请在 GUI 选择版本或在命令行指定 --pack-format N。")


def load_config(path: Path = CONFIG_FILE) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                config.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
        except (OSError, ValueError):
            pass
    return config


SECRET_KEY_RE = re.compile(r'(?i)"[^"]*api_key"\s*:\s*"([^"]+)"')


def is_git_ignored(path: Path) -> bool:
    """True when git would ignore this file (uses `git check-ignore`)."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=path.parent,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except (OSError, ValueError):
        return False


def config_secret_risk(config_path: Path = CONFIG_FILE) -> str | None:
    """Warn when the config holds an API key and could be committed to git."""
    if not config_path.is_file():
        return None
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not any(match.strip() for match in SECRET_KEY_RE.findall(text)):
        return None
    project = config_path.resolve().parent
    if not (project / ".git").exists():
        return None
    if is_git_ignored(config_path):
        return None
    return (
        f"检测到 {config_path.name} 内含 API Key，且该文件没有被 .gitignore 忽略。\n"
        "把它提交到 GitHub 会公开泄漏密钥。请保留项目自带的 .gitignore，"
        "或改用不含密钥的 ferry_config.example.json。"
    )


def public_config(config: dict[str, Any]) -> dict[str, Any]:
    def mask(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {k: mask(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [mask(item, key) for item in value]
        if key.lower().endswith("api_key") or key.lower() == "api_key":
            return "sk-***" if value else ""
        return value
    return mask(config)


def save_config(config: dict[str, Any], path: Path = CONFIG_FILE) -> None:
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_progress(path: Path = PROGRESS_FILE) -> dict[str, list[str]]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            failed = data.get("failed", {})
            if isinstance(failed, dict):
                return {str(k): [str(x) for x in v] for k, v in failed.items() if isinstance(v, list)}
    except (OSError, ValueError):
        pass
    return {}


def save_progress(failed: dict[str, list[str]], path: Path = PROGRESS_FILE) -> None:
    path.write_text(json.dumps({"failed": failed}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_user_glossary(path: Path = USER_GLOSSARY_FILE) -> dict[str, str]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k).strip(): str(v) for k, v in data.items() if str(k).strip() and str(v)}
    except (OSError, ValueError):
        pass
    return {}


def _http_get_json(url: str, timeout: int) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "ProjectFerry/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_http_error_detail(exc.code)) from exc
    except TimeoutError as exc:
        raise RuntimeError("下载超时") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise RuntimeError("下载超时") from exc
        raise RuntimeError(f"网络连接失败：{reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("返回的不是合法 JSON") from exc


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


_HARDCODED_LOG_KEYWORDS = {
    "config", "exception", "error", "failed", "loading", "debug", "warning", "logger",
    "registry", "handler", "packet", "mixin", "class", "method", "field", "null",
    "string", "bytes", "layer", "offset", "header", "protocol", "gateway", "http",
    "stack", "trace", "invalid", "unknown", "unsupported", "cannot", "could not",
    "utility", "final", "enabled", "disabled", "shutting", "dump", "task", "report",
    "install", "version", "build", "source", "target", "missing", "request", "response",
    "connection", "server", "already", "provide", "provided", "must", "required",
}
_HARDCODED_CMD_KEYWORDS = {
    "execute", "teleport", "spreadplayers", "stopsound", "scoreboard", "summon",
    "give", "effect", "kill", "title", "say", "tp", "fill", "setblock", "clone",
    "data", "tag", "team", "clear", "particle", "playsound", "@a", "@e", "@p", "@s",
}


def extract_class_strings(data: bytes) -> list[str]:
    if len(data) < 10 or data[:4] != b"\xca\xfe\xba\xbe":
        return []
    idx = 8
    try:
        cp = int.from_bytes(data[idx:idx + 2], "big")
    except IndexError:
        return []
    idx += 2
    strings: list[str] = []
    i = 1
    try:
        while i < cp and idx < len(data):
            tag = data[idx]
            idx += 1
            if tag == 1:
                length = int.from_bytes(data[idx:idx + 2], "big")
                idx += 2
                strings.append(data[idx:idx + length].decode("utf-8", "replace"))
                idx += length
            elif tag in (7, 8, 16, 19, 20):
                idx += 2
            elif tag == 15:
                idx += 3
            elif tag in (3, 4):
                idx += 4
            elif tag in (5, 6):
                idx += 8
                i += 1
            elif tag in (9, 10, 11, 12, 17, 18):
                idx += 4
            else:
                break
            i += 1
    except (IndexError, ValueError):
        pass
    return strings


def looks_like_hardcoded_text(text: str) -> bool:
    if len(text) < 4 or " " not in text:
        return False
    if not re.search(r"[a-zA-Z]", text):
        return False
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return False
    if any(x in text for x in ("net.", "com.", "org.", "java/", "()", "(L", "Ljava", ".java", ".class", "mcreator")):
        return False
    if "{}" in text or "%s" in text or "\x01" in text or "\\n" in text:
        return False
    low = text.lower()
    if any(k in low for k in _HARDCODED_LOG_KEYWORDS):
        return False
    if any(k in low for k in _HARDCODED_CMD_KEYWORDS):
        return False
    return True


def detect_hardcoded_mods(mods_dir: Path) -> dict[str, int]:
    """扫描 mods 目录，返回 {modid: 疑似硬编码可见文本数量}。

    硬编码文本（如直接写在代码里的物品 tooltip、聊天消息）不经过语言文件，
    资源包无法覆盖，因此这类模组无法做到完全汉化。
    """
    result: dict[str, int] = {}
    if not mods_dir.is_dir():
        return result
    for jar in sorted(mods_dir.rglob("*.jar")):
        try:
            lang_files = language_files_in_zip(jar)
        except (zipfile.BadZipFile, OSError):
            continue
        modids = {f.modid for f in lang_files if f.locale == "en_us"}
        if not modids:
            continue
        seen: set[str] = set()
        try:
            with zipfile.ZipFile(jar) as zf:
                for name in zf.namelist():
                    if not name.endswith(".class"):
                        continue
                    data = zf.read(name)
                    for text in extract_class_strings(data):
                        if text not in seen and looks_like_hardcoded_text(text):
                            seen.add(text)
        except (zipfile.BadZipFile, OSError):
            continue
        if seen:
            for modid in modids:
                result[modid] = result.get(modid, 0) + len(seen)
    return result


def _resolve_engine_fields(config: dict[str, Any], engine: str, o: dict[str, Any]) -> dict[str, Any]:
    base_url = ""
    api_key = ""
    model = ""
    if engine == "openai":
        base_url = o.get("base_url") or config.get("openai_base_url") or os.getenv("MC_AI_BASE_URL") or "https://api.openai.com/v1"
        api_key = o.get("api_key") or config.get("openai_api_key") or os.getenv("MC_AI_API_KEY") or ""
        model = o.get("model") or config.get("openai_model") or os.getenv("MC_AI_MODEL") or "gpt-4o-mini"
    elif engine == "anthropic":
        base_url = o.get("base_url") or config.get("anthropic_base_url") or "https://api.anthropic.com"
        api_key = o.get("api_key") or config.get("anthropic_api_key") or ""
        model = o.get("model") or config.get("anthropic_model") or "claude-3-5-haiku-latest"
    elif engine == "gemini":
        base_url = o.get("base_url") or config.get("gemini_base_url") or "https://generativelanguage.googleapis.com/v1beta"
        api_key = o.get("api_key") or config.get("gemini_api_key") or ""
        model = o.get("model") or config.get("gemini_model") or "gemini-1.5-flash"
    elif engine == "local":
        base_url = o.get("base_url") or config.get("local_base_url") or "http://127.0.0.1:11434/v1"
        api_key = o.get("api_key") or config.get("local_api_key") or ""
        model = o.get("model") or config.get("local_model") or "qwen2.5"
    elif engine == "mymemory":
        model = "mymemory"
    elif engine == "deepl":
        deepl_free = bool(config.get("deepl_free", True))
        base_url = o.get("base_url") or ("https://api-free.deepl.com/v2" if deepl_free else "https://api.deepl.com/v2")
        api_key = o.get("api_key") or config.get("deepl_api_key") or ""
        model = "deepl"
    return {"engine": engine, "base_url": base_url, "api_key": api_key, "model": model}


def resolve_translate_settings(config: dict[str, Any], instance: Path, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    o = overrides or {}
    engine = o.get("engine") or config.get("engine") or "mymemory"
    batch_size = int(o.get("batch_size") or config.get("batch_size") or 30)
    timeout = int(o.get("timeout") or config.get("timeout") or 180)
    delay = float(o.get("delay") or config.get("delay") or 0.2)
    concurrency = max(1, int(o.get("concurrency") or config.get("concurrency") or 1))
    pack_format = resolve_pack_format(config, instance, o)
    pack_name = o.get("pack_name") or config.get("pack_name") or "AI_Translation_LowPriority.zip"
    output_dir = o.get("output_dir") or config.get("output_dir") or str(instance / "resourcepacks")
    fill_community = o.get("fill_community") if o.get("fill_community") is not None else bool(config.get("fill_community", False))
    refine_community = o.get("refine_community") if o.get("refine_community") is not None else bool(config.get("refine_community", False))
    cache_dir = config.get("cache_dir") or str(Path(__file__).resolve().parent / "ferry_cache")
    glossary = dict(BUILTIN_GLOSSARY)
    glossary.update(load_user_glossary())
    user_glossary = config.get("glossary", {})
    if isinstance(user_glossary, dict):
        glossary.update(user_glossary)
    keep = set(BUILTIN_KEEP)
    user_keep = config.get("keep_untranslated", [])
    if isinstance(user_keep, list):
        keep.update(str(x) for x in user_keep if str(x))
    base = {
        "batch_size": batch_size,
        "timeout": timeout,
        "delay": delay,
        "concurrency": concurrency,
        "pack_format": pack_format,
        "pack_name": pack_name,
        "output_dir": output_dir,
        "fill_community": fill_community,
        "refine_community": refine_community,
        "cache_dir": cache_dir,
        "mymemory_email": config.get("mymemory_email", ""),
        "deepl_free": bool(config.get("deepl_free", True)),
        "glossary": glossary,
        "keep": keep,
        "weight": 1.0,
    }
    settings = {**base, **_resolve_engine_fields(config, engine, o)}
    engines: list[dict[str, Any]] = []
    model_pool = config.get("model_pool", [])
    if isinstance(model_pool, list):
        for slot in model_pool:
            if not isinstance(slot, dict):
                continue
            slot_engine = slot.get("engine") or "mymemory"
            try:
                slot_weight = float(slot.get("weight", 1) or 1)
            except (TypeError, ValueError):
                slot_weight = 1.0
            if slot_weight <= 0:
                slot_weight = 1.0
            engines.append({**base, **_resolve_engine_fields(config, slot_engine, slot), "weight": slot_weight})
    validate_engine_network(engine, settings["base_url"])
    for slot in engines:
        validate_engine_network(slot["engine"], slot.get("base_url", ""))
    settings["engines"] = engines
    return settings


def _batch_load(batch: list[Entry]) -> float:
    return float(sum(len(entry.english) for entry in batch) + len(batch) * 8)


def assign_batches(groups: list[list[Entry]], engines: list[dict[str, Any]]) -> list[tuple[list[Entry], dict[str, Any]]]:
    """按「字符量 / 权重」把批次贪心分给各引擎，使各模型承担的工作量（≈资费）尽量平摊。"""
    if not engines:
        return [(batch, {}) for batch in groups]
    if len(engines) == 1:
        return [(batch, engines[0]) for batch in groups]
    weights = [max(0.0001, float(engine.get("weight") or 1)) for engine in engines]
    heap = [(0.0, index) for index in range(len(engines))]
    heapq.heapify(heap)
    assigned: list[tuple[list[Entry], dict[str, Any]]] = []
    for batch in groups:
        load = _batch_load(batch)
        current, index = heapq.heappop(heap)
        assigned.append((batch, engines[index]))
        heapq.heappush(heap, (current + load / weights[index], index))
    return assigned


def format_usage(usage: dict[str, dict[str, int]]) -> str:
    if not usage:
        return ""
    lines = []
    for label, record in sorted(usage.items()):
        lines.append(
            f"· {label}：{record.get('keys', 0)} 条 / {record.get('batches', 0)} 批 / 约 {record.get('chars', 0)} 字"
        )
    return "\n".join(lines)


def translate_entries(entries: list[Entry], settings: dict[str, Any], progress=None, cancel_event=None, usage: dict[str, dict[str, int]] | None = None, corpus: dict[str, dict[str, tuple[str, str]]] | None = None, bypass_cache: bool = False) -> tuple[dict[str, dict[str, str]], list[str], dict[str, list[str]]]:
    cache_dir = Path(settings["cache_dir"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    glossary = settings.get("glossary", {})
    base_keep = settings.get("keep", set())
    groups = group_entries(entries, settings["batch_size"])
    total = len(entries)
    concurrency = max(1, int(settings.get("concurrency", 1)))
    engines = settings.get("engines") or [settings]
    if settings.get("refine_community") and any(engine["engine"] not in {"openai", "anthropic", "gemini", "local"} for engine in engines):
        raise ValueError("人工汉化精加工需要 OpenAI / Anthropic / Gemini / 本地模型；请更换引擎或关闭精加工。")
    usage_lock = threading.Lock()

    def process_batch(batch: list[Entry], engine_settings: dict[str, Any]) -> tuple[dict[str, dict[str, str]], list[str], dict[str, list[str]]]:
        started = time.monotonic()
        modid = batch[0].modid
        keep = set(base_keep) | modid_keep_words(modid)
        refining = bool(settings.get("refine_community") and (corpus or {}).get(modid))
        references = reference_examples(batch, (corpus or {})[modid]) if refining else None
        cache_key_value = cache_key(batch, engine_settings["engine"], engine_settings["model"], glossary, base_keep, references)
        cache_file = cache_dir / f"{cache_key_value}.json"
        if cache_file.exists() and not bypass_cache:
            raw = json.loads(cache_file.read_text(encoding="utf-8"))
        else:
            masked = {entry.key: mask_all(entry.english, glossary, keep)[0] for entry in batch}
            try:
                request_settings = {**engine_settings, "refine_community": refining, "reference_examples": references or []}
                raw = _translate_batch_with_retry(engine_settings["engine"], masked, request_settings, cancel_event=cancel_event)
            except RuntimeError as exc:
                message = f"{modid}（{len(batch)} 条）：{exc}"
                LOG.warning("batch failed: engine=%s mod=%s count=%d duration=%.2fs reason=%s", engine_settings["engine"], modid, len(batch), time.monotonic() - started, redact_secrets(str(exc)))
                print(f"  [失败，跳过本批] {message}", file=sys.stderr)
                return {}, [message], {modid: [entry.key for entry in batch]}
            if all(entry.key in raw for entry in batch):
                cache_file.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            else:
                print(f"  [不缓存] 结果不完整，下次重试", file=sys.stderr)
            time.sleep(engine_settings["delay"])
        batch_translations: dict[str, dict[str, str]] = {}
        batch_failed: dict[str, list[str]] = {}
        for entry in batch:
            value = raw.get(entry.key)
            if value is None:
                print(f"  [保留英文] 引擎缺少 key: {entry.modid}:{entry.key}", file=sys.stderr)
                batch_failed.setdefault(entry.modid, []).append(entry.key)
            else:
                valid = restore_and_validate(entry.english, value, glossary, keep)
                if valid is None:
                    print(f"  [保留英文] 占位符校验失败: {entry.modid}:{entry.key}", file=sys.stderr)
                    batch_failed.setdefault(entry.modid, []).append(entry.key)
                else:
                    batch_translations.setdefault(entry.modid, {})[entry.key] = valid
        if usage is not None:
            count = sum(len(data) for data in batch_translations.values())
            if count:
                label = f"{engine_settings['engine']}:{engine_settings.get('model') or '-'}"
                with usage_lock:
                    record = usage.setdefault(label, {"keys": 0, "chars": 0, "batches": 0})
                    record["keys"] += count
                    record["chars"] += int(_batch_load(batch))
                    record["batches"] += 1
        LOG.info("batch completed: engine=%s mod=%s count=%d translated=%d duration=%.2fs", engine_settings["engine"], modid, len(batch), sum(map(len, batch_translations.values())), time.monotonic() - started)
        return batch_translations, [], batch_failed

    all_translations: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    failed_keys: dict[str, set[str]] = {}
    done = 0

    def merge(batch_translations: dict[str, dict[str, str]], batch_errors: list[str], batch_failed: dict[str, list[str]]) -> None:
        for modid, data in batch_translations.items():
            all_translations.setdefault(modid, {}).update(data)
        errors.extend(batch_errors)
        for modid, keys in batch_failed.items():
            failed_keys.setdefault(modid, set()).update(keys)

    assignments = assign_batches(groups, engines)
    if concurrency <= 1:
        for batch, engine_settings in assignments:
            if cancel_event is not None and cancel_event.is_set():
                break
            merge(*process_batch(batch, engine_settings))
            done += len(batch)
            if progress:
                progress(done, total, batch[0].modid)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures: dict[concurrent.futures.Future, list[Entry]] = {}
            for batch, engine_settings in assignments:
                if cancel_event is not None and cancel_event.is_set():
                    break
                futures[executor.submit(process_batch, batch, engine_settings)] = batch
            for future in concurrent.futures.as_completed(futures):
                batch = futures[future]
                merge(*future.result())
                done += len(batch)
                if progress:
                    progress(done, total, batch[0].modid)

    failed = {modid: sorted(keys) for modid, keys in failed_keys.items()}
    return all_translations, errors, failed


def update_progress(translations: dict[str, dict[str, str]], failed: dict[str, list[str]], path: Path = PROGRESS_FILE) -> None:
    progress = load_progress(path)
    for modid, data in translations.items():
        if modid in progress:
            remaining = [k for k in progress[modid] if k not in data]
            if remaining:
                progress[modid] = remaining
            else:
                del progress[modid]
    for modid, keys in failed.items():
        progress[modid] = sorted(set(progress.get(modid, [])) | set(keys))
    save_progress(progress, path)


def remove_progress_modids(modids: set[str], path: Path = PROGRESS_FILE) -> None:
    progress = load_progress(path)
    for modid in modids:
        progress.pop(modid, None)
    save_progress(progress, path)


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


def command_translate(args: argparse.Namespace) -> int:
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
    scan = scan_inputs(mods, resourcepacks)
    if migrate_legacy_pack(Path(settings["output_dir"]) / settings["pack_name"], settings["pack_format"]):
        scan = scan_inputs(mods, resourcepacks)
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
    all_translations, errors, failed = translate_entries(entries, settings, usage=usage, corpus=community_corpus(scan.english, scan.community + baseline_files) if settings["refine_community"] else None) if entries else ({}, [], {})
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
    translate.set_defaults(func=command_translate)
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    try:
        raise SystemExit(args.func(args))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1)
