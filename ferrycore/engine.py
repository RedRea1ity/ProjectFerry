"""翻译引擎：各家 API 调用、批次翻译、重试、用量统计。
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import os
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
from ferrycore.net import _http_error_detail, _http_post_json
from ferrycore.text import _batch_load, _model_input, _parse_ai_json, _system_prompt, assign_batches, cache_key, group_entries, mask_all, modid_keep_words, restore_and_validate
from ferrycore.versions import resolve_pack_format


ENGINES_WITH_KEY = {"openai", "anthropic", "gemini", "deepl"}


CLOUD_ENGINES = {"openai", "anthropic", "gemini", "deepl"}


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
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("翻译已取消")
        try:
            return translate_batch(engine, masked, settings, cancel_event)
        except RuntimeError as exc:
            last_exc = exc
            if attempt < retries:
                delay = 1.5 * (attempt + 1)
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
    batch_size = int(o.get("batch_size") or config.get("batch_size") or 30)
    timeout = int(o.get("timeout") or config.get("timeout") or 180)
    delay = float(o.get("delay") or config.get("delay") or 0.2)
    concurrency = max(1, int(o.get("concurrency") or config.get("concurrency") or 1))
    pack_format = resolve_pack_format(config, instance, o)
    pack_name = o.get("pack_name") or config.get("pack_name") or "AI_Translation_LowPriority.zip"
    output_dir = o.get("output_dir") or config.get("output_dir") or str(instance / "resourcepacks")
    fill_community = o.get("fill_community") if o.get("fill_community") is not None else bool(config.get("fill_community", False))
    refine_community = o.get("refine_community") if o.get("refine_community") is not None else bool(config.get("refine_community", False))
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
    groups = group_entries(entries, settings["batch_size"])
    total = len(entries)
    concurrency = max(1, int(settings.get("concurrency", 1)))
    engines = settings.get("engines") or [settings]
    if settings.get("refine_community") and any(engine["engine"] not in {"openai", "anthropic", "gemini", "local"} for engine in engines):
        raise ValueError("人工汉化精加工需要 OpenAI / Anthropic / Gemini / 本地模型；请更换引擎或关闭精加工。")
    usage_lock = threading.Lock()

    all_translations: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    failed_keys: dict[str, set[str]] = {}
    done = 0
    assignments = assign_batches(groups, engines)
    batch_total = len(assignments)

    def emit_batch_progress(batch_index: int, modid: str, size: int, in_batch_done: int) -> None:
        # 批内进度回调：批开始 / 每条 / 批结束各推一次，UI 线程侧再做节流。
        if batch_progress:
            try:
                batch_progress(batch_index, batch_total, total, modid, min(in_batch_done, size), size)
            except Exception as exc:
                LOG.warning("batch_progress callback failed: %s", type(exc).__name__)

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
        if cache_file.exists() and not bypass_cache:
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(cached, dict):
                    raw = cached
                else:
                    LOG.warning("Ignoring invalid translation cache: %s", cache_file)
            except (OSError, ValueError, UnicodeError) as exc:
                LOG.warning("Ignoring unreadable translation cache %s: %s", cache_file, type(exc).__name__)
        if raw is None:
            masked = {entry.key: mask_all(entry.english, glossary, keep)[0] for entry in batch}
            try:
                request_settings = {
                    **engine_settings,
                    "refine_community": refining,
                    "reference_examples": references or [],
                    "term_hints": hints,
                }
                raw = _translate_batch_with_retry(engine_settings["engine"], masked, request_settings, cancel_event=cancel_event)
                if not isinstance(raw, dict):
                    raise RuntimeError("模型返回内容不是 JSON 对象")
            except Exception as exc:
                message = f"{modid}（{len(batch)} 条）：{redact_secrets(str(exc))}"
                LOG.warning("batch failed: engine=%s mod=%s count=%d duration=%.2fs reason=%s", engine_settings["engine"], modid, len(batch), time.monotonic() - started, redact_secrets(str(exc)))
                print(f"  [失败，跳过本批] {message}", file=sys.stderr)
                emit_batch_progress(batch_index, modid, len(batch), len(batch))
                return {}, [message], {modid: [entry.key for entry in batch]}
            if all(entry.key in raw for entry in batch):
                try:
                    atomic_write_text(cache_file, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
                except OSError as exc:
                    LOG.warning("Could not save translation cache %s: %s", cache_file, type(exc).__name__)
            else:
                print(f"  [不缓存] 结果不完整，下次重试", file=sys.stderr)
            time.sleep(engine_settings["delay"])
        batch_translations: dict[str, dict[str, str]] = {}
        batch_failed: dict[str, list[str]] = {}
        for position, entry in enumerate(batch):
            value = raw.get(entry.key)
            if value is None:
                print(f"  [保留英文] 引擎缺少 key: {entry.modid}:{entry.key}", file=sys.stderr)
                batch_failed.setdefault(entry.modid, []).append(entry.key)
            else:
                try:
                    valid = restore_and_validate(entry.english, value, glossary, keep) if isinstance(value, str) else None
                except (TypeError, ValueError, AttributeError) as exc:
                    LOG.warning("Invalid translation value for %s:%s: %s", entry.modid, entry.key, type(exc).__name__)
                    valid = None
                if valid is None:
                    print(f"  [保留英文] 占位符校验失败: {entry.modid}:{entry.key}", file=sys.stderr)
                    batch_failed.setdefault(entry.modid, []).append(entry.key)
                else:
                    batch_translations.setdefault(entry.modid, {})[entry.key] = valid
            emit_batch_progress(batch_index, modid, len(batch), position + 1)
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

    def merge(batch_translations: dict[str, dict[str, str]], batch_errors: list[str], batch_failed: dict[str, list[str]]) -> None:
        for modid, data in batch_translations.items():
            all_translations.setdefault(modid, {}).update(data)
        errors.extend(batch_errors)
        for modid, keys in batch_failed.items():
            failed_keys.setdefault(modid, set()).update(keys)

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
            done += len(batch)
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
                done += len(batch)
                if progress:
                    progress(done, total, batch[0].modid)

    failed = {modid: sorted(keys) for modid, keys in failed_keys.items()}
    return all_translations, errors, failed
