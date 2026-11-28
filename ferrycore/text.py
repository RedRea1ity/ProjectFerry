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
    r"|§[0-9a-fk-orx]|\$\([^\s)]*\)"
    r"|</?(?:color|sprite|size|style|align|alpha|cspace|font|indent|line-height|line-indent|link|margin|mark|mspace|noparse|pos|rotate|space|voffset|width|b|i|u|s|sup|sub|br)\b[^>\n]*>",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """You are a careful Minecraft Java Edition localization assistant.
Translate English Minecraft mod strings into Simplified Chinese.
Rules:
- Return ONLY a valid JSON object mapping each input key (or id in `strings`) to its translation.
- Preserve every placeholder, formatting code, mask tag (such as <p0>, <t0>, __MC_PH_0__), and newline marker exactly and in order.
- Do not translate registry ids, technical identifiers, proper mod names, or values inside placeholders.
- If `hints` is provided, treat it as contextual terminology suggestions; use a term only when it fits.
- Use concise natural in-game Chinese. Item/block/entity names should be noun phrases.
- Keep the original meaning; do not add explanations, notes, markdown, or extra keys.
"""

TRANSLATION_PROMPT_VERSION = "compact-cache-v3"

REFINE_PROMPT = """Use `references` (English -> Chinese human pairs) as terminology and style context for this mod.
Translate ONLY the keys in `strings` into natural, consistent Simplified Chinese; never output keys from `references`.
Return ONLY a JSON object for the keys in `strings`.
Preserve all placeholders and mask tags exactly. Do not invent meanings or add keys.
"""

_MASK_TOKEN_RE = re.compile(r"__MC_(?:PH|TERM)_\d+__|__T\d+__")
_TRANSLATABLE_CHAR_RE = re.compile(r"[A-Za-z\u00c0-\u024f\u0400-\u04ff\u3040-\u30ff\uac00-\ud7af\u4e00-\u9fff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


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


def load_json_document(raw: bytes, source: str) -> dict[str, Any]:
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
    return value


def load_json_bytes(raw: bytes, source: str) -> dict[str, str]:
    value = load_json_document(raw, source)
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


def prefilter_translation(
    source: str,
    glossary: dict[str, str],
    keep: set[str],
    source_lang: str = "en",
) -> str | None:
    """零 Token 预过滤：判断一条原文能否不经过 AI 直接得到合法译文。

    返回非 None 表示可直接采用（0 Token）；返回 None 表示仍需送给模型翻译。
    覆盖三类场景：
    1. 掩码后不含任何自然语言字符（纯占位符/纯数字/纯符号，或整句被术语表/保留词完全覆盖）；
    2. 英文源（MC 等）里本身就已经是纯中文的条目（注意：日文源绝不跳过纯汉字词如「設定」）。
    """
    masked, _ = mask_all(source, glossary, keep)
    residual = _MASK_TOKEN_RE.sub("", masked).strip()
    if not _TRANSLATABLE_CHAR_RE.search(residual):
        return restore_and_validate(source, masked, glossary, keep)
    if (source_lang or "en").lower().startswith("en"):
        if _CJK_RE.search(residual) and not _LATIN_RE.search(residual):
            return restore_and_validate(source, masked, glossary, keep)
    return None


def group_entries(entries: list[Entry], batch_size: int, max_chars: int = 0) -> list[list[Entry]]:
    """按 modid + 条数上限 + 字符预算动态分批。

    - 短词条可按 `batch_size` 凑成更大批，摊薄固定系统提示词成本；
    - 长段落（如 Patchouli / FTB / 剧情文本）触及 `max_chars` 自动切批，防止撞 max_tokens 截断。
    """
    groups: list[list[Entry]] = []
    current: list[Entry] = []
    current_modid: str | None = None
    current_chars = 0
    for entry in entries:
        entry_chars = len(entry.english) + 12
        over_chars = max_chars > 0 and current and (current_chars + entry_chars > max_chars)
        if current and (entry.modid != current_modid or len(current) >= batch_size or over_chars):
            groups.append(current)
            current = []
            current_chars = 0
        current.append(entry)
        current_modid = entry.modid
        current_chars += entry_chars
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


def _compact_references(raw_refs: Any) -> dict[str, str]:
    """把参考例句列表压成紧凑的 {en: zh} 映射，去掉无意义的超长 key。"""
    if isinstance(raw_refs, dict):
        return {str(k): str(v) for k, v in raw_refs.items() if k and v}
    result: dict[str, str] = {}
    if isinstance(raw_refs, list):
        for item in raw_refs:
            if isinstance(item, dict):
                en = str(item.get("en") or "").strip()
                zh = str(item.get("zh") or "").strip()
                if en and zh and en not in result:
                    result[en] = zh
    return result


def _model_input(masked: dict[str, str], settings: dict[str, Any]) -> str:
    # 通用游戏线（ferrygame）自带输入格式（上下文、字节预算），由它自己拼。
    builder = settings.get("model_input_builder")
    if callable(builder):
        return builder(masked, settings)
    hints = settings.get("term_hints") or {}
    refs = _compact_references(settings.get("reference_examples")) if settings.get("refine_community") else {}
    if hints or refs:
        payload: dict[str, Any] = {}
        if refs:
            payload["references"] = refs
        if hints:
            payload["hints"] = dict(sorted(hints.items()))
        payload["strings"] = masked
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(masked, ensure_ascii=False, separators=(",", ":"))


def _system_prompt(settings: dict[str, Any]) -> str:
    # 保持 system prompt 100% 静态（动态 term_hints 已移入 user 消息），
    # 避免每批系统提示词变化击穿 OpenAI / DeepSeek / Anthropic / Gemini 的前缀缓存。
    override = settings.get("system_prompt_override")
    if override:
        return str(override)
    return SYSTEM_PROMPT + ("\n" + REFINE_PROMPT if settings.get("refine_community") else "")


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
    saved = usage.get("_saved")
    for label, record in sorted(usage.items()):
        if label == "_saved":
            continue
        base = f"· {label}：{record.get('keys', 0)} 条 / {record.get('batches', 0)} 批"
        in_tok = int(record.get("input_tokens", 0) or 0)
        out_tok = int(record.get("output_tokens", 0) or 0)
        cached_tok = int(record.get("cached_tokens", 0) or 0)
        thought_tok = int(record.get("thought_tokens", 0) or 0)
        if in_tok or out_tok:
            tok_parts = [f"输入 {in_tok:,}", f"输出 {out_tok:,}"]
            if cached_tok:
                tok_parts.append(f"前缀缓存命中 {cached_tok:,}")
            if thought_tok:
                tok_parts.append(f"思考 {thought_tok:,}")
            base += " / " + "，".join(tok_parts) + " tokens"
        else:
            base += f" / 约 {record.get('chars', 0)} 字"
        lines.append(base)
    if isinstance(saved, dict):
        parts = []
        if saved.get("prefiltered"):
            parts.append(f"预过滤直出 {saved['prefiltered']} 条")
        if saved.get("corpus_exact"):
            parts.append(f"语料直复用 {saved['corpus_exact']} 条")
        if saved.get("tm_hits"):
            parts.append(f"条目缓存命中 {saved['tm_hits']} 条")
        if saved.get("batch_cache_hits"):
            parts.append(f"批次缓存命中 {saved['batch_cache_hits']} 条")
        if saved.get("deduped"):
            parts.append(f"去重省翻 {saved['deduped']} 条")
        if parts:
            lines.append("· 零 Token 省流：" + " / ".join(parts))
    return "\n".join(lines)
