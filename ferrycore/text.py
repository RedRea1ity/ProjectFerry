"""文本层：语言文件解析、占位符 / 术语掩码、提示词、分批。

只依赖 `models`，不碰文件系统也不碰网络 —— 纯函数，好测。
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from typing import Any

from .models import Entry

PLACEHOLDER_RE = re.compile(
    r"%%"
    r"|%(?:\([A-Za-z_][A-Za-z0-9_]*\))?(?:\d+\$)?[-+#0]*(?:\d+)?(?:\.\d+)?(?:[hlLzjt])?[a-zA-Z]"
    r"|\$\{[^}]+\}|\{\d+\}|\{[A-Za-z_][A-Za-z0-9_.-]*\}"
    r"|§[0-9a-fk-orx]|\$\([^\s)]*\)",
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

TRANSLATION_PROMPT_VERSION = "contextual-glossary-v2"

REFINE_PROMPT = """Use the provided human English/Chinese pairs as terminology and style references for this mod.
Translate only the untranslated keys in `strings` into natural, consistent Simplified Chinese.
The human reference translations must remain unchanged; they are context only and must never appear as output keys.
Return ONLY a JSON object for the keys in `strings`.
Preserve all placeholders and formatting codes exactly. Do not invent meanings or add keys.
"""


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


def _parse_lang_raw(raw: bytes, ext: str, source: str) -> dict[str, str]:
    return load_json_bytes(raw, source) if ext == "json" else load_lang_bytes(raw, source)


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
    # 同形占位符（例如两个 %s）对应的是有顺序的参数；token 被交换也必须拒收。
    expected_placeholders = [token for token in expected if token.startswith("__MC_PH_")]
    translated_order = re.findall(r"__MC_PH_\d+__", translated)
    if translated_order != expected_placeholders:
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


def cache_key(entries: list[Entry], engine: str, model: str, glossary: dict[str, str] | None = None, keep: set[str] | None = None, references: list[dict[str, str]] | None = None, base_url: str = "", term_hints: dict[str, str] | None = None) -> str:
    context = ""
    if glossary or keep or term_hints:
        context = json.dumps({
            "g": sorted((glossary or {}).items()),
            "k": sorted(keep or set()),
            "h": sorted((term_hints or {}).items()),
        }, ensure_ascii=False, sort_keys=True)
    payload = json.dumps([(e.modid, e.key, e.english, e.kind, e.book, e.rel_path, e.json_pointer) for e in entries], ensure_ascii=False, sort_keys=True)
    reference_context = json.dumps(references, ensure_ascii=False, sort_keys=True) if references is not None else ""
    parts = (TRANSLATION_PROMPT_VERSION, engine, model, base_url, context, payload, reference_context)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _model_input(masked: dict[str, str], settings: dict[str, Any]) -> str:
    if settings.get("refine_community"):
        return json.dumps({"strings": masked, "references": settings.get("reference_examples", [])}, ensure_ascii=False)
    return json.dumps(masked, ensure_ascii=False)


def _system_prompt(settings: dict[str, Any]) -> str:
    prompt = SYSTEM_PROMPT + ("\n" + REFINE_PROMPT if settings.get("refine_community") else "")
    hints = settings.get("term_hints") or {}
    if hints:
        prompt += (
            "\nTerminology hints for this batch (English -> Simplified Chinese). "
            "These are contextual suggestions, not unconditional substitutions; use a term only when its meaning fits the sentence.\n"
            + json.dumps(hints, ensure_ascii=False, sort_keys=True)
        )
    return prompt


def _parse_ai_json(content: Any) -> dict[str, str]:
    if isinstance(content, dict):
        return {str(k): v for k, v in content.items() if isinstance(v, str)}
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
    return {str(k): v for k, v in value.items() if isinstance(v, str)}


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
