"""翻译引擎：各家 API 调用、批次翻译、重试、用量统计。
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from pathlib import Path
from typing import Any

from ferrycore.base import LOG, app_dir, atomic_write_text, redact_secrets
from ferrycore.glossary import BUILTIN_GLOSSARY, BUILTIN_KEEP, load_user_glossary, relevant_term_hints
from ferrycore.models import Entry
from ferrycore.net import ApiRequestError, _http_error_detail, _http_post_json
from ferrycore.text import (
    _batch_load,
    _model_input,
    _parse_ai_json,
    _system_prompt,
    assign_batches,
    cache_key,
    group_entries,
    mask_all,
    modid_keep_words,
    placeholder_tokens,
    prefilter_translation,
    restore_and_validate,
)
from ferrycore.tm import TranslationMemory, tm_lookup_key
from ferrycore.versions import resolve_pack_format


ENGINES_WITH_KEY = {"openai", "anthropic", "gemini", "deepl"}


CLOUD_ENGINES = {"openai", "anthropic", "gemini", "deepl"}

_PH_TOKEN_RE = re.compile(r"__MC_PH_(\d+)__")
_TERM_TOKEN_RE = re.compile(r"__MC_TERM_(\d+)__")
_SHORT_PH_RE = re.compile(r"<p(\d+)>")
_SHORT_TERM_RE = re.compile(r"<t(\d+)>")


class TruncatedResponseError(RuntimeError):
    """模型输出触达 max_tokens 被截断；由上层拆半重发，不再原样硬重试。"""


def validate_engine_network(engine: str, base_url: str) -> None:
    if engine not in CLOUD_ENGINES:
        return
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{engine} 云模型必须使用 HTTPS 远程 API；本地地址请把引擎设为 local。")


def reference_examples(batch: list[Entry], corpus: dict[str, tuple[str, str]], limit: int = 12) -> list[dict[str, str]]:
    prefixes = {entry.key.rsplit(".", 1)[0] for entry in batch}
    ranked = sorted(corpus, key=lambda key: (0 if key.rsplit(".", 1)[0] in prefixes else 1, key))
    return [{"key": key, "en": corpus[key][0], "zh": corpus[key][1]} for key in ranked[:limit]]


def _wire_encode(masked: dict[str, str], settings: dict[str, Any]) -> tuple[dict[str, str], dict[str, str] | None, bool]:
    """在发往 LLM 前把长 key 压缩为 "1"、"2"…，并尽量将长掩码缩为 <p0>/<t0>。

    仅在未提供自定义 `model_input_builder`（ferrygame 自带短编号）且 `short_keys` 未显式关闭时启用。
    """
    if callable(settings.get("model_input_builder")) or not settings.get("short_keys", True):
        return masked, None, False
    sid_to_key = {str(index + 1): key for index, key in enumerate(masked)}
    joined = "\n".join(masked.values())
    use_short_tags = ("<p" not in joined) and ("<t" not in joined)
    wire: dict[str, str] = {}
    for sid, key in sid_to_key.items():
        text = masked[key]
        if use_short_tags:
            text = _PH_TOKEN_RE.sub(r"<p\1>", text)
            text = _TERM_TOKEN_RE.sub(r"<t\1>", text)
        wire[sid] = text
    return wire, sid_to_key, use_short_tags


def _wire_decode(raw: dict[str, str], sid_to_key: dict[str, str] | None, use_short_tags: bool) -> dict[str, str]:
    if sid_to_key is None:
        return raw
    decoded: dict[str, str] = {}
    for sid, real_key in sid_to_key.items():
        val = raw.get(sid)
        if val is None and real_key in raw and real_key not in sid_to_key:
            val = raw[real_key]
        if isinstance(val, str):
            if use_short_tags:
                val = _SHORT_PH_RE.sub(r"__MC_PH_\1__", val)
                val = _SHORT_TERM_RE.sub(r"__MC_TERM_\1__", val)
            decoded[real_key] = val
    return decoded


def _record_token_usage(
    settings: dict[str, Any],
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    thought_tokens: int = 0,
) -> None:
    sink = settings.get("_token_usage")
    if not isinstance(sink, dict):
        return
    sink["input_tokens"] = int(sink.get("input_tokens", 0)) + max(0, int(input_tokens or 0))
    sink["output_tokens"] = int(sink.get("output_tokens", 0)) + max(0, int(output_tokens or 0))
    sink["cached_tokens"] = int(sink.get("cached_tokens", 0)) + max(0, int(cached_tokens or 0))
    sink["thought_tokens"] = int(sink.get("thought_tokens", 0)) + max(0, int(thought_tokens or 0))


def _normalized_model_name(model: str) -> str:
    return (model or "").strip().lower().rsplit("/", 1)[-1]


def _is_openai_reasoning_model(model: str) -> bool:
    name = _normalized_model_name(model)
    return name.startswith(("o1", "o3", "o4", "gpt-5"))


def _apply_openai_thinking_control(
    payload: dict[str, Any],
    model: str,
    base_url: str,
    engine: str,
    disable_thinking: bool,
) -> None:
    """仅当用户显式开启「关闭深度思考」时，按接口惯例附加关思考参数。

    若第三方中转不认该字段返回 400，调用方会自动剥离并重试，不会阻断翻译。
    """
    if not disable_thinking:
        return
    name = _normalized_model_name(model)
    host = urllib.parse.urlparse(base_url or "").netloc.lower()
    if name.startswith("gpt-5"):
        payload["reasoning_effort"] = "minimal"
    elif name.startswith(("o1", "o3", "o4")):
        payload["reasoning_effort"] = "low"
    elif name.startswith(("gemini-2.5", "gemini-3")):
        payload["reasoning_effort"] = "none"
    elif (
        name.startswith(("qwen3", "qwq"))
        or "dashscope" in host
        or "siliconflow" in host
        or engine == "local"
    ):
        payload["enable_thinking"] = False
    elif (
        name.startswith(("deepseek", "doubao", "glm-4.5", "glm-4.6", "glm-z1", "kimi-k2"))
        or any(h in host for h in ("deepseek.com", "volces.com", "bigmodel.cn", "moonshot.cn"))
        or "think" in name
        or "reason" in name
    ):
        payload["thinking"] = {"type": "disabled"}


_THINKING_PAYLOAD_KEYS = ("reasoning_effort", "thinking", "enable_thinking")


def _translate_openai(masked: dict[str, str], settings: dict[str, Any]) -> dict[str, str]:
    wire_masked, sid_to_key, use_short_tags = _wire_encode(masked, settings)
    model = str(settings["model"])
    base_url = str(settings.get("base_url") or "")
    engine = str(settings.get("engine") or "openai")
    reasoning = _is_openai_reasoning_model(model)
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _system_prompt(settings)},
            {"role": "user", "content": _model_input(wire_masked, settings)},
        ],
        "response_format": {"type": "json_object"},
    }
    if not reasoning:
        payload["temperature"] = 0.2
    _apply_openai_thinking_control(
        payload,
        model,
        base_url,
        engine,
        bool(settings.get("disable_thinking", False)),
    )

    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {settings['api_key']}"}
    try:
        body = _http_post_json(url, payload, headers, settings["timeout"])
    except ApiRequestError as exc:
        if exc.status_code == 400 and any(k in payload for k in _THINKING_PAYLOAD_KEYS):
            # 先仅剥离关思考参数，尽量保留 response_format=json_object。
            no_think = {k: v for k, v in payload.items() if k not in _THINKING_PAYLOAD_KEYS}
            try:
                body = _http_post_json(url, no_think, headers, settings["timeout"])
            except ApiRequestError as exc2:
                if exc2.status_code == 400 and "response_format" in no_think:
                    minimal = {k: v for k, v in no_think.items() if k != "response_format"}
                    body = _http_post_json(url, minimal, headers, settings["timeout"])
                else:
                    raise
        elif exc.status_code == 400 and "response_format" in payload:
            minimal = {k: v for k, v in payload.items() if k != "response_format"}
            body = _http_post_json(url, minimal, headers, settings["timeout"])
        else:
            raise
    usage = body.get("usage") if isinstance(body, dict) else None
    if isinstance(usage, dict):
        prompt_details = usage.get("prompt_tokens_details") or {}
        comp_details = usage.get("completion_tokens_details") or {}
        _record_token_usage(
            settings,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cached_tokens=int(
                ((prompt_details.get("cached_tokens") or 0) if isinstance(prompt_details, dict) else 0)
                or usage.get("prompt_cache_hit_tokens")
                or 0
            ),
            thought_tokens=int((comp_details.get("reasoning_tokens") or 0) if isinstance(comp_details, dict) else 0),
        )
    try:
        choice = body["choices"][0]
        if choice.get("finish_reason") == "length":
            raise TruncatedResponseError("OpenAI 输出达到长度上限被截断")
        content = choice["message"]["content"]
    except TruncatedResponseError:
        raise
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("OpenAI 返回格式不正确") from exc
    return _wire_decode(_parse_ai_json(content), sid_to_key, use_short_tags)


def _translate_anthropic(masked: dict[str, str], settings: dict[str, Any]) -> dict[str, str]:
    wire_masked, sid_to_key, use_short_tags = _wire_encode(masked, settings)
    sys_text = _system_prompt(settings)
    use_cache = bool(settings.get("prompt_cache", True))
    system_field: Any = (
        [{"type": "text", "text": sys_text, "cache_control": {"type": "ephemeral"}}]
        if use_cache
        else sys_text
    )
    payload: dict[str, Any] = {
        "model": settings["model"],
        "max_tokens": 8192,
        "temperature": 0.2,
        "system": system_field,
        "messages": [{"role": "user", "content": _model_input(wire_masked, settings)}],
    }
    url = settings["base_url"].rstrip("/") + "/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": settings["api_key"],
        "anthropic-version": "2023-06-01",
    }
    try:
        body = _http_post_json(url, payload, headers, settings["timeout"])
    except ApiRequestError as exc:
        if exc.status_code == 400 and use_cache:
            fallback = {**payload, "system": sys_text}
            body = _http_post_json(url, fallback, headers, settings["timeout"])
        else:
            raise
    usage = body.get("usage") if isinstance(body, dict) else None
    if isinstance(usage, dict):
        in_tok = (
            int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0)
        )
        _record_token_usage(
            settings,
            input_tokens=in_tok,
            output_tokens=int(usage.get("output_tokens") or 0),
            cached_tokens=int(usage.get("cache_read_input_tokens") or 0),
        )
    if isinstance(body, dict) and body.get("stop_reason") == "max_tokens":
        raise TruncatedResponseError("Anthropic 输出达到 max_tokens 被截断")
    try:
        parts = body["content"]
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Anthropic 返回格式不正确") from exc
    return _wire_decode(_parse_ai_json(text), sid_to_key, use_short_tags)


def _translate_gemini(masked: dict[str, str], settings: dict[str, Any]) -> dict[str, str]:
    wire_masked, sid_to_key, use_short_tags = _wire_encode(masked, settings)
    gen_config: dict[str, Any] = {
        "temperature": 0.2,
        "responseMimeType": "application/json",
    }
    if settings.get("disable_thinking", False):
        name = _normalized_model_name(str(settings["model"]))
        if name.startswith("gemini-2.5-flash"):
            gen_config["thinkingConfig"] = {"thinkingBudget": 0}
        elif name.startswith("gemini-2.5-pro"):
            gen_config["thinkingConfig"] = {"thinkingBudget": 128}
    payload: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": _system_prompt(settings)}]},
        "contents": [{"parts": [{"text": _model_input(wire_masked, settings)}]}],
        "generationConfig": gen_config,
    }
    base = settings["base_url"].rstrip("/")
    url = f"{base}/models/{settings['model']}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": settings["api_key"]}
    try:
        body = _http_post_json(url, payload, headers, settings["timeout"])
    except ApiRequestError as exc:
        if exc.status_code == 400 and ("thinkingConfig" in gen_config or "responseMimeType" in gen_config):
            fallback_cfg = {"temperature": 0.2}
            fallback = {**payload, "generationConfig": fallback_cfg}
            body = _http_post_json(url, fallback, headers, settings["timeout"])
        else:
            raise
    usage = body.get("usageMetadata") if isinstance(body, dict) else None
    if isinstance(usage, dict):
        _record_token_usage(
            settings,
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0),
            cached_tokens=int(usage.get("cachedContentTokenCount") or 0),
            thought_tokens=int(usage.get("thoughtsTokenCount") or 0),
        )
    try:
        candidate = body["candidates"][0]
        if candidate.get("finishReason") == "MAX_TOKENS":
            raise TruncatedResponseError("Gemini 输出达到 MAX_TOKENS 被截断")
        text = candidate["content"]["parts"][0]["text"]
    except TruncatedResponseError:
        raise
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Gemini 返回格式不正确") from exc
    return _wire_decode(_parse_ai_json(text), sid_to_key, use_short_tags)


def _translate_mymemory(masked: dict[str, str], settings: dict[str, Any], cancel_event=None) -> dict[str, str]:
    email = settings.get("mymemory_email", "")
    result: dict[str, str] = {}
    for key, text in masked.items():
        if cancel_event is not None and cancel_event.is_set():
            break
        if len(text) > 450:
            print(f"  [跳过] MyMemory 文本超长: {key} ({len(text)} 字符)", file=sys.stderr)
            continue
        source = str(settings.get("source_lang") or "en").lower()
        params = urllib.parse.urlencode({"q": text, "langpair": f"{source}|zh-CN"})
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
        [("text", t) for t in texts] + [("source_lang", str(settings.get("source_lang") or "en").upper()), ("target_lang", "ZH")]
    ).encode("utf-8")
    url = settings["base_url"].rstrip("/") + "/translate"
    request = urllib.request.Request(url, data=form, headers={"Authorization": f"DeepL-Auth-Key {settings['api_key']}"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=settings["timeout"]) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = redact_secrets(exc.read().decode("utf-8", "replace")[:300])
        raise ApiRequestError(
            f"{_http_error_detail(exc.code)}：{detail}",
            status_code=exc.code,
            retryable=exc.code in (429,) or exc.code >= 500,
            detail=detail,
        ) from exc
    except TimeoutError as exc:
        raise ApiRequestError("DeepL 请求超时", retryable=True) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise ApiRequestError("DeepL 请求超时", retryable=True) from exc
        raise ApiRequestError(f"DeepL 网络连接失败：{reason}", retryable=True) from exc
    except json.JSONDecodeError as exc:
        raise ApiRequestError("DeepL 返回的不是合法 JSON", retryable=True) from exc
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
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("翻译已取消")
        try:
            return translate_batch(engine, masked, settings, cancel_event)
        except TruncatedResponseError:
            # 截断不要原地重试，交给上层对半拆批。
            raise
        except ApiRequestError as exc:
            last_exc = exc
            if not exc.retryable:
                # 400/401/403/404/456 属于不可恢复错误，立刻止损。
                raise
            if attempt < retries:
                delay = exc.retry_after if exc.retry_after is not None else 1.5 * (2 ** attempt)
                if cancel_event is not None:
                    if cancel_event.wait(delay):
                        raise RuntimeError("翻译已取消") from exc
                else:
                    time.sleep(delay)
        except RuntimeError as exc:
            last_exc = exc
            if attempt < retries:
                delay = 1.5 * (2 ** attempt)
                if cancel_event is not None:
                    if cancel_event.wait(delay):
                        raise RuntimeError("翻译已取消") from exc
                else:
                    time.sleep(delay)
    assert last_exc is not None
    raise last_exc


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
    batch_size = int(o.get("batch_size") or config.get("batch_size") or 60)
    max_batch_chars = int(o.get("max_batch_chars") if o.get("max_batch_chars") is not None else config.get("max_batch_chars", 3600))
    timeout = int(o.get("timeout") or config.get("timeout") or 180)
    delay = float(o.get("delay") or config.get("delay") or 0.2)
    concurrency = max(1, int(o.get("concurrency") or config.get("concurrency") or 1))
    pack_format = resolve_pack_format(config, instance, o)
    pack_name = o.get("pack_name") or config.get("pack_name") or "AI_Translation_LowPriority.zip"
    output_dir = o.get("output_dir") or config.get("output_dir") or str(instance / "resourcepacks")
    fill_community = o.get("fill_community") if o.get("fill_community") is not None else bool(config.get("fill_community", False))
    refine_community = o.get("refine_community") if o.get("refine_community") is not None else bool(config.get("refine_community", False))
    disable_thinking = o.get("disable_thinking") if o.get("disable_thinking") is not None else bool(config.get("disable_thinking", False))
    prompt_cache = o.get("prompt_cache") if o.get("prompt_cache") is not None else bool(config.get("prompt_cache", True))
    cache_dir = config.get("cache_dir") or str(app_dir() / "ferry_cache")
    # Automatically collected Mojang terms are contextual hints, not forced replacements.
    # Only terms the user explicitly put in ferry_config.json.glossary are strict.
    term_hints = dict(BUILTIN_GLOSSARY)
    term_hints.update(load_user_glossary())
    glossary: dict[str, str] = {}
    user_glossary = config.get("glossary", {})
    if isinstance(user_glossary, dict):
        glossary.update(user_glossary)
        term_hints.update(user_glossary)
    keep = set(BUILTIN_KEEP)
    user_keep = config.get("keep_untranslated", [])
    if isinstance(user_keep, list):
        keep.update(str(x) for x in user_keep if str(x))
    base = {
        "batch_size": batch_size,
        "max_batch_chars": max_batch_chars,
        "timeout": timeout,
        "delay": delay,
        "concurrency": concurrency,
        "pack_format": pack_format,
        "pack_name": pack_name,
        "output_dir": output_dir,
        "fill_community": fill_community,
        "refine_community": refine_community,
        "disable_thinking": disable_thinking,
        "prompt_cache": prompt_cache,
        "cache_dir": cache_dir,
        "mymemory_email": config.get("mymemory_email", ""),
        "deepl_free": bool(config.get("deepl_free", True)),
        "glossary": glossary,
        "term_hints": term_hints,
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


def translate_entries(entries: list[Entry], settings: dict[str, Any], progress=None, cancel_event=None, usage: dict[str, dict[str, int]] | None = None, corpus: dict[str, dict[str, tuple[str, str]]] | None = None, bypass_cache: bool = False, batch_progress=None) -> tuple[dict[str, dict[str, str]], list[str], dict[str, list[str]]]:
    cache_dir = Path(settings["cache_dir"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    glossary = settings.get("glossary", {})
    base_keep = settings.get("keep", set())
    source_lang = str(settings.get("source_lang") or "en")
    max_batch_chars = int(settings.get("max_batch_chars", 3600) or 0)
    total = len(entries)
    concurrency = max(1, int(settings.get("concurrency", 1)))
    engines = settings.get("engines") or [settings]
    if settings.get("refine_community") and any(engine["engine"] not in {"openai", "anthropic", "gemini", "local"} for engine in engines):
        raise ValueError("人工汉化精加工需要 OpenAI / Anthropic / Gemini / 本地模型；请更换引擎或关闭精加工。")
    usage_lock = threading.Lock()

    all_translations: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    failed_keys: dict[str, set[str]] = {}
    saved_stats = {
        "prefiltered": 0,
        "corpus_exact": 0,
        "tm_hits": 0,
        "batch_cache_hits": 0,
        "deduped": 0,
    }

    # 构建同模组人工语料精确表（refine_community 时 0 Token 直接复用完全相同的原文）。
    corpus_exact: dict[str, dict[str, str]] = {}
    if settings.get("refine_community") and corpus:
        for modid, pairs in corpus.items():
            exact_map: dict[str, str] = {}
            for _k, (en_text, zh_text) in pairs.items():
                if en_text and zh_text and en_text not in exact_map:
                    exact_map[en_text] = zh_text
            if exact_map:
                corpus_exact[modid] = exact_map

    with TranslationMemory(cache_dir) as tm:
        # ── 第一道省流闸门：零 Token 预过滤 + 语料精确复用 + 条目级 TM 查表 ──
        tm_candidates: list[tuple[Entry, str, set[str], bool]] = []
        pre_done = 0
        for entry in entries:
            keep = set(base_keep) | modid_keep_words(entry.modid)
            refining = bool(settings.get("refine_community") and (corpus or {}).get(entry.modid))
            direct = prefilter_translation(entry.english, glossary, keep, source_lang)
            if direct is not None:
                all_translations.setdefault(entry.modid, {})[entry.key] = direct
                saved_stats["prefiltered"] += 1
                pre_done += 1
                continue
            if refining:
                exact_zh = corpus_exact.get(entry.modid, {}).get(entry.english)
                if exact_zh and placeholder_tokens(entry.english) == placeholder_tokens(exact_zh):
                    all_translations.setdefault(entry.modid, {})[entry.key] = exact_zh
                    saved_stats["corpus_exact"] += 1
                    pre_done += 1
                    continue
            tkey = tm_lookup_key(
                entry.english,
                source_lang=source_lang,
                glossary=glossary,
                keep=keep,
                modid=entry.modid,
                refine_community=refining,
            )
            tm_candidates.append((entry, tkey, keep, refining))

        tm_hits = tm.get_many(tkey for _, tkey, _, _ in tm_candidates) if (tm_candidates and not bypass_cache) else {}
        remaining: list[tuple[Entry, str]] = []
        for entry, tkey, _keep, _refining in tm_candidates:
            cached_val = tm_hits.get(tkey)
            if cached_val is not None and placeholder_tokens(entry.english) == placeholder_tokens(cached_val):
                all_translations.setdefault(entry.modid, {})[entry.key] = cached_val
                saved_stats["tm_hits"] += 1
                pre_done += 1
                continue
            remaining.append((entry, tkey))

        # ── 第二道省流闸门：原文级去重（相同 tm_key 的多条只发代表条给模型）──
        dedup_enabled = bool(settings.get("dedup_entries", True))
        unique_entries: list[Entry] = []
        entry_tkey: dict[tuple[str, str], str] = {}
        dup_followers: dict[tuple[str, str], list[Entry]] = {}
        rep_by_tkey: dict[str, tuple[str, str]] = {}
        for entry, tkey in remaining:
            identity = (entry.modid, entry.key)
            entry_tkey[identity] = tkey
            if dedup_enabled and tkey in rep_by_tkey:
                rep_id = rep_by_tkey[tkey]
                dup_followers.setdefault(rep_id, []).append(entry)
                saved_stats["deduped"] += 1
            else:
                rep_by_tkey[tkey] = identity
                unique_entries.append(entry)

        done = pre_done
        if pre_done and progress:
            progress(done, total, entries[0].modid if entries else "")

        groups = group_entries(unique_entries, settings["batch_size"], max_chars=max_batch_chars)
        assignments = assign_batches(groups, engines)
        batch_total = len(assignments)

        def emit_batch_progress(batch_index: int, modid: str, size: int, in_batch_done: int) -> None:
            # 批内进度回调：批开始 / 每条 / 批结束各推一次，UI 线程侧再做节流。
            if batch_progress:
                try:
                    batch_progress(batch_index, batch_total, total, modid, min(in_batch_done, size), size)
                except Exception as exc:
                    LOG.warning("batch_progress callback failed: %s", type(exc).__name__)

        def invoke_with_split(
            sub_batch: list[Entry],
            engine_settings: dict[str, Any],
            keep: set[str],
            refining: bool,
            references: list[dict[str, str]] | None,
            token_sink: dict[str, int],
        ) -> dict[str, str]:
            masked = {entry.key: mask_all(entry.english, glossary, keep)[0] for entry in sub_batch}
            hints = relevant_term_hints(sub_batch, settings.get("term_hints"), set(glossary) | keep)
            request_settings = {
                **engine_settings,
                "refine_community": refining,
                "reference_examples": references or [],
                "term_hints": hints,
                "_token_usage": token_sink,
            }
            try:
                raw = _translate_batch_with_retry(engine_settings["engine"], masked, request_settings, cancel_event=cancel_event)
                if not isinstance(raw, dict):
                    raise RuntimeError("模型返回内容不是 JSON 对象")
                return raw
            except TruncatedResponseError:
                if len(sub_batch) <= 1:
                    raise
                mid = len(sub_batch) // 2
                LOG.info("batch truncated, splitting %d -> %d + %d (mod=%s)", len(sub_batch), mid, len(sub_batch) - mid, sub_batch[0].modid)
                left = invoke_with_split(sub_batch[:mid], engine_settings, keep, refining, references, token_sink)
                right = invoke_with_split(sub_batch[mid:], engine_settings, keep, refining, references, token_sink)
                merged_raw = dict(left)
                merged_raw.update(right)
                return merged_raw

        def process_batch(batch: list[Entry], engine_settings: dict[str, Any], batch_index: int = -1) -> tuple[dict[str, dict[str, str]], list[str], dict[str, list[str]]]:
            started = time.monotonic()
            modid = batch[0].modid
            emit_batch_progress(batch_index, modid, len(batch), 0)
            keep = set(base_keep) | modid_keep_words(modid)
            refining = bool(settings.get("refine_community") and (corpus or {}).get(modid))
            references = reference_examples(batch, (corpus or {})[modid]) if refining else None
            hints = relevant_term_hints(batch, settings.get("term_hints"), set(glossary) | keep)
            cache_key_value = cache_key(
                batch, engine_settings["engine"], engine_settings["model"], glossary, keep,
                references, engine_settings.get("base_url", ""), hints,
            )
            cache_file = cache_dir / f"{cache_key_value}.json"
            raw = None
            from_batch_cache = False
            if cache_file.exists() and not bypass_cache:
                try:
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    if isinstance(cached, dict):
                        raw = cached
                        from_batch_cache = True
                    else:
                        LOG.warning("Ignoring invalid translation cache: %s", cache_file)
                except (OSError, ValueError, UnicodeError) as exc:
                    LOG.warning("Ignoring unreadable translation cache %s: %s", cache_file, type(exc).__name__)
            token_sink: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "thought_tokens": 0}
            if raw is None:
                try:
                    raw = invoke_with_split(batch, engine_settings, keep, refining, references, token_sink)
                except Exception as exc:
                    message = f"{modid}（{len(batch)} 条）：{redact_secrets(str(exc))}"
                    LOG.warning("batch failed: engine=%s mod=%s count=%d duration=%.2fs reason=%s", engine_settings["engine"], modid, len(batch), time.monotonic() - started, redact_secrets(str(exc)))
                    print(f"  [失败，跳过本批] {message}", file=sys.stderr)
                    emit_batch_progress(batch_index, modid, len(batch), len(batch))
                    return {}, [message], {modid: [entry.key for entry in batch]}
                if engine_settings.get("delay", 0) > 0:
                    time.sleep(engine_settings["delay"])
            else:
                with usage_lock:
                    saved_stats["batch_cache_hits"] += len(batch)

            def validate_entries(target_batch: list[Entry], raw_map: dict[str, Any]) -> tuple[dict[str, str], list[Entry]]:
                ok_map: dict[str, str] = {}
                bad_list: list[Entry] = []
                for entry in target_batch:
                    value = raw_map.get(entry.key)
                    if value is None:
                        bad_list.append(entry)
                        continue
                    try:
                        valid = restore_and_validate(entry.english, value, glossary, keep) if isinstance(value, str) else None
                    except (TypeError, ValueError, AttributeError) as exc:
                        LOG.warning("Invalid translation value for %s:%s: %s", entry.modid, entry.key, type(exc).__name__)
                        valid = None
                    if valid is None:
                        bad_list.append(entry)
                    else:
                        ok_map[entry.key] = valid
                return ok_map, bad_list

            validated, failed_entries = validate_entries(batch, raw)

            # ── 局部差量重试：批内只丢/错少量条目时，仅重发失败那几条，不重翻已成功的 ──
            if (
                not from_batch_cache
                and failed_entries
                and validated
                and len(batch) > 1
                and len(failed_entries) <= max(5, len(batch) // 3)
                and engine_settings["engine"] in {"openai", "anthropic", "gemini", "local"}
                and (cancel_event is None or not cancel_event.is_set())
            ):
                try:
                    retry_raw = invoke_with_split(failed_entries, engine_settings, keep, refining, references, token_sink)
                    recovered, still_failed = validate_entries(failed_entries, retry_raw)
                    if recovered:
                        validated.update(recovered)
                        raw = {**raw, **{k: retry_raw[k] for k in recovered if k in retry_raw}}
                    failed_entries = still_failed
                except Exception as exc:
                    LOG.warning("differential retry failed: mod=%s count=%d reason=%s", modid, len(failed_entries), redact_secrets(str(exc)))

            # 无论整批是否 100% 完整，立刻把已校验成功的条目落盘到条目级 TM，绝不白烧。
            if validated and not from_batch_cache:
                tm_rows = [
                    (entry_tkey[(entry.modid, entry.key)], entry.english, validated[entry.key])
                    for entry in batch
                    if entry.key in validated and (entry.modid, entry.key) in entry_tkey
                ]
                tm.put_many(tm_rows, engine=engine_settings["engine"], model=str(engine_settings.get("model") or ""))

            # 兼容旧整批缓存文件：整批齐全时仍写盘。
            if not from_batch_cache:
                if all(entry.key in raw for entry in batch):
                    try:
                        atomic_write_text(cache_file, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
                    except OSError as exc:
                        LOG.warning("Could not save translation cache %s: %s", cache_file, type(exc).__name__)
                else:
                    print("  [局部落盘] 本批存在缺失 key，已将成功条目写入条目缓存，缺失项下次仅补翻", file=sys.stderr)

            batch_translations: dict[str, dict[str, str]] = {}
            batch_failed: dict[str, list[str]] = {}
            failed_set = {entry.key for entry in failed_entries}
            for position, entry in enumerate(batch):
                if entry.key in validated:
                    batch_translations.setdefault(entry.modid, {})[entry.key] = validated[entry.key]
                else:
                    if raw.get(entry.key) is None:
                        print(f"  [保留英文] 引擎缺少 key: {entry.modid}:{entry.key}", file=sys.stderr)
                    else:
                        print(f"  [保留英文] 占位符校验失败: {entry.modid}:{entry.key}", file=sys.stderr)
                    if entry.key in failed_set:
                        batch_failed.setdefault(entry.modid, []).append(entry.key)
                emit_batch_progress(batch_index, modid, len(batch), position + 1)

            if usage is not None:
                count = sum(len(data) for data in batch_translations.values())
                has_tokens = token_sink["input_tokens"] > 0 or token_sink["output_tokens"] > 0
                if count or has_tokens:
                    label = f"{engine_settings['engine']}:{engine_settings.get('model') or '-'}"
                    with usage_lock:
                        record = usage.setdefault(label, {
                            "keys": 0, "chars": 0, "batches": 0,
                            "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "thought_tokens": 0,
                        })
                        record["keys"] += count
                        record["chars"] += int(_batch_load(batch))
                        record["batches"] += 1
                        record["input_tokens"] += token_sink["input_tokens"]
                        record["output_tokens"] += token_sink["output_tokens"]
                        record["cached_tokens"] += token_sink["cached_tokens"]
                        record["thought_tokens"] += token_sink["thought_tokens"]
            LOG.info(
                "batch completed: engine=%s mod=%s count=%d translated=%d in_tok=%d out_tok=%d cached_tok=%d duration=%.2fs",
                engine_settings["engine"],
                modid,
                len(batch),
                sum(map(len, batch_translations.values())),
                token_sink["input_tokens"],
                token_sink["output_tokens"],
                token_sink["cached_tokens"],
                time.monotonic() - started,
            )
            return batch_translations, [], batch_failed

        def merge(batch_translations: dict[str, dict[str, str]], batch_errors: list[str], batch_failed: dict[str, list[str]]) -> None:
            for modid, data in batch_translations.items():
                all_translations.setdefault(modid, {}).update(data)
                # 把代表条的译文同步回填给所有被去重的同源条。
                for key, zh_val in data.items():
                    for dup in dup_followers.get((modid, key), ()):
                        all_translations.setdefault(dup.modid, {})[dup.key] = zh_val
            errors.extend(batch_errors)
            for modid, keys in batch_failed.items():
                failed_keys.setdefault(modid, set()).update(keys)
                for key in keys:
                    for dup in dup_followers.get((modid, key), ()):
                        failed_keys.setdefault(dup.modid, set()).add(dup.key)

        def batch_effective_count(batch: list[Entry]) -> int:
            return len(batch) + sum(len(dup_followers.get((entry.modid, entry.key), ())) for entry in batch)

        def safe_process_batch(batch: list[Entry], engine_settings: dict[str, Any], batch_index: int = -1):
            try:
                return process_batch(batch, engine_settings, batch_index)
            except Exception as exc:
                message = f"{batch[0].modid}（{len(batch)} 条）：{type(exc).__name__}"
                LOG.warning("Unexpected batch failure: %s", message)
                emit_batch_progress(batch_index, batch[0].modid, len(batch), len(batch))
                return {}, [message], {batch[0].modid: [entry.key for entry in batch]}

        if concurrency <= 1:
            for batch_index, (batch, engine_settings) in enumerate(assignments):
                if cancel_event is not None and cancel_event.is_set():
                    break
                merge(*safe_process_batch(batch, engine_settings, batch_index))
                done += batch_effective_count(batch)
                if progress:
                    progress(done, total, batch[0].modid)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures: dict[concurrent.futures.Future, list[Entry]] = {}
                for batch_index, (batch, engine_settings) in enumerate(assignments):
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    futures[executor.submit(safe_process_batch, batch, engine_settings, batch_index)] = batch
                for future in concurrent.futures.as_completed(futures):
                    batch = futures[future]
                    merge(*future.result())
                    done += batch_effective_count(batch)
                    if progress:
                        progress(done, total, batch[0].modid)

    if usage is not None and any(saved_stats.values()):
        usage["_saved"] = dict(saved_stats)

    failed = {modid: sorted(keys) for modid, keys in failed_keys.items()}
    return all_translations, errors, failed
