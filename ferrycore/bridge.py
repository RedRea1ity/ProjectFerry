"""摆渡桥与 LiteralBridge 的映射表构建 / 导出、游戏内采集文件解析。
"""

from __future__ import annotations

import json
import re
import time
import zipfile

from pathlib import Path
from typing import Any

from ferrycore.base import atomic_write_text
from ferrycore.models import Entry, LangFile
from ferrycore.scan import is_excluded_mod


def build_bridge_groups(english: list[LangFile], community: list[LangFile], ai_by_modid: dict[str, dict[str, str]] | None = None, hardcoded: dict[str, dict[str, str]] | None = None) -> dict[str, dict[str, str]]:
    """按 modid 汇总 英文原文 -> 中文 显示映射；人工译文优先于 AI，硬编码译文并入。"""
    en_by_mod: dict[str, dict[str, str]] = {}
    for file in english:
        en_by_mod.setdefault(file.modid, {}).update(file.data)
    zh_by_mod: dict[str, dict[str, str]] = {}
    for modid, data in (ai_by_modid or {}).items():
        zh_by_mod.setdefault(modid, {}).update({k: v for k, v in data.items() if v and str(v).strip()})
    for file in community:
        zh_by_mod.setdefault(file.modid, {}).update({k: v for k, v in file.data.items() if v and str(v).strip()})
    groups: dict[str, dict[str, str]] = {}
    for modid, entries in en_by_mod.items():
        if is_excluded_mod(modid):
            continue
        zhs = zh_by_mod.get(modid, {})
        pairs: dict[str, str] = {}
        for key, english_text in entries.items():
            zh = zhs.get(key)
            if english_text.strip() and zh and str(zh).strip() and english_text != zh:
                pairs.setdefault(english_text, str(zh))
        for english_text, zh in (hardcoded or {}).get(modid, {}).items():
            if english_text.strip() and str(zh).strip():
                pairs.setdefault(english_text, str(zh))
        if pairs:
            groups[modid] = pairs
    # 只有硬编码候选、连 en_us key 都没有的模组（少见但存在）
    for modid, entries in (hardcoded or {}).items():
        if modid in groups or is_excluded_mod(modid):
            continue
        pairs = {en: zh for en, zh in entries.items() if en.strip() and str(zh).strip()}
        if pairs:
            groups[modid] = pairs
    return groups


def build_bridge_mapping(english: list[LangFile], community: list[LangFile], ai_by_modid: dict[str, dict[str, str]] | None = None, hardcoded: dict[str, dict[str, str]] | None = None) -> dict[str, str]:
    """扁平化的 英文原文 -> 中文 映射（build_bridge_groups 的合并视图）。"""
    pairs: dict[str, str] = {}
    for data in build_bridge_groups(english, community, ai_by_modid, hardcoded).values():
        pairs.update(data)
    return pairs


LITERALBRIDGE_PAGE = "https://github.com/Losketch/LiteralBridge"


def build_literalbridge_export(groups: dict[str, dict[str, str]]) -> dict[str, dict]:
    """把映射组转成 LiteralBridge 外部规则文件结构。

    1.2.0 实测（用户游戏内 A/B）：直填 translation 可用，translationKey 路线不可用。
    根节点必须是纯组。低于 4 字符的规则不导出（子串匹配会啃玩家名）。
    """
    rules_doc: dict[str, dict] = {}
    for modid, pairs in groups.items():
        safe = re.sub(r"[^a-z0-9_.-]", "_", modid.lower())
        rules: list[dict[str, str]] = []
        for english_text, chinese in pairs.items():
            if len(english_text.strip()) < 4 or not any(ch.isalpha() for ch in english_text):
                continue
            rules.append({"original": english_text, "translation": chinese})
        if rules:
            rules_doc[modid] = {"name": modid, "targetMod": modid, "enabled": True, "rules": rules}
    return rules_doc


# ── 摆渡桥 FerryBridge：游戏内采集清单 → 外置映射表 ──────────────────────
# 桥在游戏里把「出现过、但映射表里没有译文」的英文记成 COLLECTED_NAME
# （见 bridge 的 core/FerryBridgeCollector.java）。
# 本工具读它 → 批量翻译 → 写 FERRYBRIDGE_EXTERNAL_NAME 到游戏目录，
# 桥每秒比对一次 mtime 热重载 —— 玩家不用重启游戏。
COLLECTED_NAME = "ferrybridge-collected.txt"


FERRYBRIDGE_EXTERNAL_NAME = "ferrybridge-mappings.json"


# 采集清单的分组头，格式由 FerryBridgeCollector.flush() 固定：
#   ===== 聊天栏（12 条）=====
_COLLECTED_BUCKET_HEADER = re.compile(r"^=====\s*(.+?)（\s*(\d+)\s*条）=====$")


# 采集文本统一挂在这个合成 modid 下走翻译引擎，避免和真实模组混淆。
COLLECTED_MODID = "ferrybridge_collected"


def parse_collected_file(path: Path) -> dict[str, list[str]]:
    """解析桥写的采集清单，返回 {来源分组: [英文原文, ...]}。

    文件不存在或格式对不上时返回空 dict —— 调用方据此提示「先去游戏里玩一圈」，
    而不是抛异常。分组名保持桥那边的原始中文（聊天栏 / 物品 tooltip）。
    """
    buckets: dict[str, list[str]] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return buckets
    current: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\r").strip()
        match = _COLLECTED_BUCKET_HEADER.match(line)
        if match:
            current = match.group(1).strip()
            buckets.setdefault(current, [])
            continue
        if current is None or not line:
            continue
        buckets[current].append(line)
    # 去重 + 排序：同一个句子可能来自多个模组，翻一次就够。
    return {name: sorted(set(items)) for name, items in buckets.items() if items}


def collected_entries(buckets: dict[str, list[str]]) -> list[Entry]:
    """把采集到的文本转成翻译引擎认得的 Entry 列表。

    key 直接用英文原文：摆渡桥的映射表本来就是「原文 → 译文」，
    用原文当 key 可以省掉一层毫无意义的 key 映射。
    """
    entries: list[Entry] = []
    seen: set[str] = set()
    for bucket in sorted(buckets):
        for text in buckets[bucket]:
            if text in seen:
                continue
            seen.add(text)
            entries.append(Entry(modid=COLLECTED_MODID, key=text, english=text, source=bucket))
    return entries


def build_ferrybridge_external(pairs: dict[str, str]) -> dict[str, Any]:
    """构造摆渡桥外部映射表结构，与 jar 内置表同构（{"map": {英文: 中文}}）。"""
    return {
        "_说明": (
            "摆渡桥 FerryBridge 外部映射表。由摆渡计划批量翻译后写入，"
            "放在游戏目录下；桥每秒比对一次 mtime 热重载，无需重启游戏。"
        ),
        "format": 1,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(pairs),
        "map": pairs,
    }


def load_ferrybridge_external(instance: Path) -> dict[str, str]:
    """读回游戏目录里已存在的外部映射表；文件不存在或损坏时返回空 dict。"""
    target = instance / FERRYBRIDGE_EXTERNAL_NAME
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    table = raw.get("map")
    if not isinstance(table, dict):
        return {}
    return {str(k): str(v) for k, v in table.items() if isinstance(k, str) and isinstance(v, str)}


def write_ferrybridge_external(instance: Path, pairs: dict[str, str], *, merge: bool = True) -> Path:
    """把外部映射表原子写入**游戏目录**。

    注意路径：桥读的是游戏目录下的 FERRYBRIDGE_EXTERNAL_NAME，
    不是 config/ferrybridge/（那是早期版本的历史位置，现在的桥不读那里）。

    默认**合并**：先读回已有表，把新条目盖上去再整体写回。这样
    「导出桥接映射」（全量扫描结果）与「导入游戏采集」（增量补漏）不会互相清空
    —— 历史 bug：两者都是整表覆盖，后点的会把先点的全部抹掉。
    传 merge=False 可强制整表替换。
    """
    target = instance / FERRYBRIDGE_EXTERNAL_NAME
    table = dict(pairs)
    if merge:
        existing = load_ferrybridge_external(instance)
        existing.update(table)  # 新译覆盖同 key 的旧译，其余原样保留
        table = existing
    atomic_write_text(target, json.dumps(build_ferrybridge_external(table), ensure_ascii=False, indent=1))
    return target


def merge_bridge_lang(pack: Path, zh: dict[str, str], en: dict[str, str]) -> Path:
    """把 text.ferrybridge.* 的 key 合并进资源包的 assets/literalbridge/lang/，供 LiteralBridge 查表。"""
    langs = {
        "assets/literalbridge/lang/zh_cn.json": dict(zh),
        "assets/literalbridge/lang/en_us.json": dict(en),
    }
    entries: dict[str, bytes] = {}
    if pack.is_file():
        try:
            with zipfile.ZipFile(pack) as zf:
                for name in zf.namelist():
                    if name.endswith("/"):
                        continue
                    try:
                        entries[name] = zf.read(name)
                    except (OSError, KeyError, zipfile.BadZipFile):
                        continue
        except (zipfile.BadZipFile, OSError):
            pass
    for name, extra in langs.items():
        if not extra:
            continue
        merged: dict[str, str] = {}
        if name in entries:
            try:
                loaded = json.loads(entries[name].decode("utf-8-sig"))
                if isinstance(loaded, dict):
                    merged = loaded
            except (ValueError, UnicodeDecodeError):
                merged = {}
        merged.update(extra)
        entries[name] = json.dumps(merged, ensure_ascii=False, indent=1).encode("utf-8")
    tmp = pack.with_name(pack.name + ".bridge.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    tmp.replace(pack)
    return pack
