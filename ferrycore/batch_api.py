"""官方 Batch API 开放接口（5 折异步批量翻译）：预览导出、提交、查进度、取回与取消。

支持三家主流官方 Batch API：
- OpenAI 兼容：`/v1/files` 上传 JSONL → `/v1/batches` 创建 → 轮询状态 → 拉取 `output_file_id`
- Anthropic：`/v1/messages/batches` 创建 → 轮询状态 → 拉取 `results_url` JSONL
- Gemini：`/v1beta/models/{model}:batchGenerateContent` → 轮询 LRO/Batch 状态 → 解析响应

设计要点：
1. 与实时翻译共享同一套零 Token 预过滤、去重、条目级 TM 缓存、短序号 Key 和占位符校验；
2. 本地持久化任务清单（`<cache_dir>/batch_jobs/<job_id>.json` + `.jsonl`），关机/重启不丢单；
3. `prepare_batch_job` 零费用即可跑通全流程并生成可检查的 JSONL，适合离线调试或提交前复核。
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ferrycore.base import LOG, app_dir, atomic_write_text, redact_secrets
from ferrycore.engine import (
    _apply_openai_thinking_control,
    _is_openai_reasoning_model,
    _normalized_model_name,
    _wire_decode,
    _wire_encode,
    reference_examples,
    validate_engine_network,
)
from ferrycore.glossary import relevant_term_hints
from ferrycore.models import Entry
from ferrycore.net import ApiRequestError, _http_error_detail, _http_post_json, _is_retryable_status, _parse_retry_after
from ferrycore.text import (
    _model_input,
    _parse_ai_json,
    _system_prompt,
    group_entries,
    mask_all,
    modid_keep_words,
    placeholder_tokens,
    prefilter_translation,
    restore_and_validate,
)
from ferrycore.tm import TranslationMemory, tm_lookup_key

BATCH_PROVIDERS = {"openai", "anthropic", "gemini"}
BATCH_JOBS_DIRNAME = "batch_jobs"

# 国内 + 国际主流 Batch API 预设（均支持 5 折左右异步批量推理；国内平台普遍兼容 OpenAI /files + /batches 协议）。
BATCH_PRESETS: dict[str, dict[str, str]] = {
    "dashscope": {
        "label": "阿里云百炼（DashScope · 5折）",
        "provider": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "endpoint": "/v1/chat/completions",
        "note": "支持 qwen-plus / qwen-turbo / qwen3 等，完全兼容 OpenAI Batch 协议",
    },
    "bigmodel": {
        "label": "智谱开放平台（BigModel GLM · 5折）",
        "provider": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "endpoint": "/v4/chat/completions",
        "note": "智谱 Batch 端点必须为 /v4/chat/completions，已自动适配",
    },
    "siliconflow": {
        "label": "硅基流动（SiliconFlow · 5折）",
        "provider": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
        "endpoint": "/v1/chat/completions",
        "note": "国内直接跑 DeepSeek / Qwen 批量推理，兼容 OpenAI Batch 协议",
    },
    "qianfan": {
        "label": "百度千帆（Qianfan · 5折）",
        "provider": "openai",
        "base_url": "https://qianfan.baidubce.com/v2",
        "model": "deepseek-v3",
        "endpoint": "/v1/chat/completions",
        "note": "支持 DeepSeek / ERNIE 批量推理，兼容 OpenAI Files + Batch 协议",
    },
    "openai": {
        "label": "OpenAI 官方（5折）",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "endpoint": "/v1/chat/completions",
        "note": "官方 24 小时异步批处理，支持任意兼容 /files + /batches 的中转站",
    },
    "anthropic": {
        "label": "Anthropic 官方（5折）",
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "model": "claude-3-5-haiku-latest",
        "endpoint": "/v1/messages/batches",
        "note": "官方 Message Batches API，可与前缀缓存叠加省费",
    },
    "gemini": {
        "label": "Gemini 官方（5折）",
        "provider": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-2.5-flash",
        "endpoint": ":batchGenerateContent",
        "note": "官方 Gemini Batch API",
    },
}


def resolve_batch_endpoint(provider: str, base_url: str, override: str = "") -> str:
    """根据平台与 base_url 推断 Batch 请求的 endpoint 路径（智谱 BigModel 自动用 /v4/chat/completions）。"""
    if override and override.strip():
        return override.strip()
    if provider == "anthropic":
        return "/v1/messages/batches"
    if provider == "gemini":
        return ":batchGenerateContent"
    host = urllib.parse.urlparse(base_url or "").netloc.lower()
    if "bigmodel.cn" in host or (base_url or "").rstrip("/").endswith("/paas/v4"):
        return "/v4/chat/completions"
    return "/v1/chat/completions"


@dataclass
class BatchJob:
    job_id: str
    provider: str
    model: str
    base_url: str
    endpoint: str = "/v1/chat/completions"
    remote_id: str = ""
    status: str = "prepared"  # prepared | submitted | in_progress | completed | failed | cancelled | expired
    created_at: str = ""
    updated_at: str = ""
    request_count: int = 0
    entry_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    prefiltered: int = 0
    corpus_exact: int = 0
    tm_hits: int = 0
    deduped: int = 0
    jsonl_path: str = ""
    output_file_id: str = ""
    results_url: str = ""
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BatchJob":
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    def summary(self) -> str:
        parts = [
            f"任务 {self.job_id} [{self.provider}:{self.model}] 状态={self.status}",
            f"总条目 {self.entry_count} / 远端批次 {self.request_count}",
        ]
        if self.remote_id:
            parts.append(f"远端 ID={self.remote_id}")
        if self.completed_count or self.failed_count:
            parts.append(f"已完成 {self.completed_count} / 失败 {self.failed_count}")
        if self.input_tokens or self.output_tokens:
            tok = f"输入 {self.input_tokens:,} / 输出 {self.output_tokens:,}"
            if self.cached_tokens:
                tok += f"（缓存命中 {self.cached_tokens:,}）"
            parts.append(tok + " tokens")
        saved = []
        if self.prefiltered:
            saved.append(f"预过滤 {self.prefiltered}")
        if self.corpus_exact:
            saved.append(f"语料复用 {self.corpus_exact}")
        if self.tm_hits:
            saved.append(f"条目缓存 {self.tm_hits}")
        if self.deduped:
            saved.append(f"去重 {self.deduped}")
        if saved:
            parts.append("零 Token 省流：" + " / ".join(saved))
        if self.error:
            parts.append(f"错误：{self.error}")
        return " | ".join(parts)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def batch_jobs_dir(cache_dir: Path | str | None = None) -> Path:
    base = Path(cache_dir) if cache_dir else app_dir() / "ferry_cache"
    folder = base / BATCH_JOBS_DIRNAME
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _manifest_path(job_id: str, job_dir: Path) -> Path:
    safe = "".join(ch for ch in job_id if ch.isalnum() or ch in ("-", "_"))
    if not safe:
        raise ValueError(f"无效的 Batch 任务 ID: {job_id}")
    return job_dir / f"{safe}.json"


def _http_request_raw(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 180,
) -> bytes:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = redact_secrets(exc.read().decode("utf-8", "replace")[:300])
        raise ApiRequestError(
            f"{_http_error_detail(exc.code)}：{detail}",
            status_code=exc.code,
            retry_after=_parse_retry_after(getattr(exc, "headers", None)),
            retryable=_is_retryable_status(exc.code),
            detail=detail,
        ) from exc
    except TimeoutError as exc:
        raise ApiRequestError("请求超时", retryable=True) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise ApiRequestError("请求超时", retryable=True) from exc
        raise ApiRequestError(f"网络连接失败：{redact_secrets(str(reason))}", retryable=True) from exc


def _http_get_json_auth(url: str, headers: dict[str, str], timeout: int = 180) -> Any:
    raw = _http_request_raw(url, method="GET", headers=headers, timeout=timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ApiRequestError("服务器返回的不是合法 JSON", retryable=True) from exc


def _encode_multipart_file(field_name: str, filename: str, content: bytes, fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = f"----ProjectFerryBatch{secrets.token_hex(12)}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    chunks.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: application/jsonl\r\n\r\n".encode("utf-8")
    )
    chunks.append(content)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _build_provider_request(
    provider: str,
    custom_id: str,
    wire_masked: dict[str, str],
    request_settings: dict[str, Any],
    *,
    endpoint: str = "/v1/chat/completions",
) -> dict[str, Any]:
    sys_text = _system_prompt(request_settings)
    user_text = _model_input(wire_masked, request_settings)
    model = str(request_settings["model"])
    disable_thinking = bool(request_settings.get("disable_thinking", False))

    if provider == "openai":
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": sys_text},
                {"role": "user", "content": user_text},
            ],
            "response_format": {"type": "json_object"},
        }
        if not _is_openai_reasoning_model(model):
            body["temperature"] = 0.2
        _apply_openai_thinking_control(
            body, model, str(request_settings.get("base_url") or ""), "openai", disable_thinking
        )
        return {
            "custom_id": custom_id,
            "method": "POST",
            "url": endpoint or "/v1/chat/completions",
            "body": body,
        }

    if provider == "anthropic":
        use_cache = bool(request_settings.get("prompt_cache", True))
        system_field: Any = (
            [{"type": "text", "text": sys_text, "cache_control": {"type": "ephemeral"}}]
            if use_cache
            else sys_text
        )
        return {
            "custom_id": custom_id,
            "params": {
                "model": model,
                "max_tokens": 8192,
                "temperature": 0.2,
                "system": system_field,
                "messages": [{"role": "user", "content": user_text}],
            },
        }

    if provider == "gemini":
        gen_config: dict[str, Any] = {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        }
        if disable_thinking:
            name = _normalized_model_name(model)
            if name.startswith("gemini-2.5-flash"):
                gen_config["thinkingConfig"] = {"thinkingBudget": 0}
            elif name.startswith("gemini-2.5-pro"):
                gen_config["thinkingConfig"] = {"thinkingBudget": 128}
        return {
            "request": {
                "systemInstruction": {"parts": [{"text": sys_text}]},
                "contents": [{"parts": [{"text": user_text}]}],
                "generationConfig": gen_config,
            },
            "metadata": {"key": custom_id},
        }

    raise ValueError(f"Batch API 仅支持 {', '.join(sorted(BATCH_PROVIDERS))}，当前引擎：{provider}")


def _save_manifest(manifest: dict[str, Any], job_dir: Path) -> None:
    job_id = manifest["job"]["job_id"]
    path = _manifest_path(job_id, job_dir)
    atomic_write_text(path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def _load_manifest(job_or_id: BatchJob | str, job_dir: Path) -> dict[str, Any]:
    job_id = job_or_id.job_id if isinstance(job_or_id, BatchJob) else str(job_or_id)
    path = _manifest_path(job_id, job_dir)
    if not path.is_file():
        raise FileNotFoundError(f"找不到 Batch 任务记录：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("job"), dict):
        raise ValueError(f"Batch 任务记录损坏：{path}")
    return data


def load_batch_job(job_id: str, cache_dir: Path | str | None = None) -> BatchJob | None:
    folder = batch_jobs_dir(cache_dir)
    try:
        return BatchJob.from_dict(_load_manifest(job_id, folder)["job"])
    except (OSError, ValueError):
        return None


def list_batch_jobs(cache_dir: Path | str | None = None) -> list[BatchJob]:
    folder = batch_jobs_dir(cache_dir)
    jobs: list[BatchJob] = []
    for path in sorted(folder.glob("*.json"), reverse=True):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("job"), dict):
                jobs.append(BatchJob.from_dict(raw["job"]))
        except (OSError, ValueError):
            continue
    return jobs


def prepare_batch_job(
    entries: list[Entry],
    settings: dict[str, Any],
    *,
    corpus: dict[str, dict[str, tuple[str, str]]] | None = None,
    bypass_cache: bool = False,
    job_dir: Path | None = None,
    meta: dict[str, Any] | None = None,
) -> BatchJob:
    """零费用构建 Batch API 离线任务清单与 JSONL 文件。

    会先跑完整的零 Token 预过滤、语料精确复用、SQLite TM 查表和原文去重，
    只有真正需要调模型的条目才会写进 JSONL。
    """
    raw_provider = str(settings.get("engine") or "").lower()
    preset = BATCH_PRESETS.get(raw_provider)
    provider = preset["provider"] if preset else raw_provider
    if provider not in BATCH_PROVIDERS:
        raise ValueError(
            "Batch API 支持 OpenAI 兼容平台（阿里云百炼 / 智谱 BigModel / 硅基流动 / 百度千帆 / OpenAI）"
            f"以及 Anthropic / Gemini，当前引擎：{raw_provider or '未设置'}"
        )
    base_url = str(settings.get("base_url") or (preset["base_url"] if preset else ""))
    model = str(settings.get("model") or (preset["model"] if preset else ""))
    endpoint = resolve_batch_endpoint(
        provider,
        base_url,
        str(settings.get("batch_endpoint") or (preset["endpoint"] if preset else "")),
    )
    cache_dir = Path(settings.get("cache_dir") or app_dir() / "ferry_cache")
    folder = Path(job_dir) if job_dir else batch_jobs_dir(cache_dir)
    folder.mkdir(parents=True, exist_ok=True)

    glossary = settings.get("glossary", {})
    base_keep = settings.get("keep", set())
    source_lang = str(settings.get("source_lang") or "en")
    batch_size = max(1, int(settings.get("batch_size") or 60))
    max_batch_chars = int(settings.get("max_batch_chars", 3600) or 0)

    pre_translations: dict[str, dict[str, str]] = {}
    saved = {"prefiltered": 0, "corpus_exact": 0, "tm_hits": 0, "deduped": 0}

    corpus_exact: dict[str, dict[str, str]] = {}
    if settings.get("refine_community") and corpus:
        for modid, pairs in corpus.items():
            exact_map = {en: zh for _k, (en, zh) in pairs.items() if en and zh}
            if exact_map:
                corpus_exact[modid] = exact_map

    with TranslationMemory(cache_dir) as tm:
        tm_candidates: list[tuple[Entry, str, set[str], bool]] = []
        for entry in entries:
            keep = set(base_keep) | modid_keep_words(entry.modid)
            refining = bool(settings.get("refine_community") and (corpus or {}).get(entry.modid))
            direct = prefilter_translation(entry.english, glossary, keep, source_lang)
            if direct is not None:
                pre_translations.setdefault(entry.modid, {})[entry.key] = direct
                saved["prefiltered"] += 1
                continue
            if refining:
                exact_zh = corpus_exact.get(entry.modid, {}).get(entry.english)
                if exact_zh and placeholder_tokens(entry.english) == placeholder_tokens(exact_zh):
                    pre_translations.setdefault(entry.modid, {})[entry.key] = exact_zh
                    saved["corpus_exact"] += 1
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
                pre_translations.setdefault(entry.modid, {})[entry.key] = cached_val
                saved["tm_hits"] += 1
                continue
            remaining.append((entry, tkey))

    unique_entries: list[Entry] = []
    entry_tkey: dict[tuple[str, str], str] = {}
    dup_followers: dict[tuple[str, str], list[dict[str, str]]] = {}
    rep_by_tkey: dict[str, tuple[str, str]] = {}
    for entry, tkey in remaining:
        identity = (entry.modid, entry.key)
        entry_tkey[identity] = tkey
        if tkey in rep_by_tkey:
            dup_followers.setdefault(rep_by_tkey[tkey], []).append({"modid": entry.modid, "key": entry.key})
            saved["deduped"] += 1
        else:
            rep_by_tkey[tkey] = identity
            unique_entries.append(entry)

    groups = group_entries(unique_entries, batch_size, max_chars=max_batch_chars)
    job_id = f"fb-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    jsonl_path = folder / f"{job_id}.jsonl"

    jsonl_records: list[dict[str, Any]] = []
    request_map: dict[str, dict[str, Any]] = {}
    for index, batch in enumerate(groups, 1):
        custom_id = f"b{index:05d}"
        modid = batch[0].modid
        keep = set(base_keep) | modid_keep_words(modid)
        refining = bool(settings.get("refine_community") and (corpus or {}).get(modid))
        references = reference_examples(batch, (corpus or {})[modid]) if refining else []
        hints = relevant_term_hints(batch, settings.get("term_hints"), set(glossary) | keep)
        masked = {entry.key: mask_all(entry.english, glossary, keep)[0] for entry in batch}
        request_settings = {
            **settings,
            "engine": provider,
            "base_url": base_url,
            "model": model,
            "refine_community": refining,
            "reference_examples": references,
            "term_hints": hints,
        }
        wire_masked, sid_to_key, use_short_tags = _wire_encode(masked, request_settings)
        jsonl_records.append(_build_provider_request(provider, custom_id, wire_masked, request_settings, endpoint=endpoint))
        request_map[custom_id] = {
            "modid": modid,
            "sid_to_key": sid_to_key,
            "use_short_tags": use_short_tags,
            "glossary": dict(glossary),
            "keep": sorted(keep),
            "items": [
                {
                    "key": entry.key,
                    "english": entry.english,
                    "tm_key": entry_tkey.get((entry.modid, entry.key), ""),
                    "duplicates": dup_followers.get((entry.modid, entry.key), []),
                }
                for entry in batch
            ],
        }

    jsonl_text = "\n".join(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) for rec in jsonl_records)
    if jsonl_text:
        jsonl_text += "\n"
    atomic_write_text(jsonl_path, jsonl_text)

    now = _now_iso()
    job = BatchJob(
        job_id=job_id,
        provider=provider,
        model=model,
        base_url=base_url,
        endpoint=endpoint,
        status="completed" if not jsonl_records else "prepared",
        created_at=now,
        updated_at=now,
        request_count=len(jsonl_records),
        entry_count=len(entries),
        prefiltered=saved["prefiltered"],
        corpus_exact=saved["corpus_exact"],
        tm_hits=saved["tm_hits"],
        deduped=saved["deduped"],
        jsonl_path=str(jsonl_path),
        meta=dict(meta or {}),
    )
    manifest = {
        "job": job.to_dict(),
        "pre_translations": pre_translations,
        "requests": request_map,
    }
    _save_manifest(manifest, folder)
    LOG.info("batch prepared: id=%s provider=%s model=%s entries=%d requests=%d", job.job_id, provider, model, len(entries), len(jsonl_records))
    return job


def _provider_headers(provider: str, api_key: str) -> dict[str, str]:
    if provider == "openai":
        return {"Authorization": f"Bearer {api_key}"}
    if provider == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    if provider == "gemini":
        return {"x-goog-api-key": api_key}
    raise ValueError(f"未知 Batch 提供商：{provider}")


def submit_batch_job(
    job_or_id: BatchJob | str,
    settings: dict[str, Any],
    *,
    job_dir: Path | None = None,
) -> BatchJob:
    """把已准备好的 Batch 任务提交到对应云端平台。"""
    cache_dir = Path(settings.get("cache_dir") or app_dir() / "ferry_cache")
    folder = Path(job_dir) if job_dir else batch_jobs_dir(cache_dir)
    manifest = _load_manifest(job_or_id, folder)
    job = BatchJob.from_dict(manifest["job"])
    if job.request_count == 0:
        job.status = "completed"
        job.updated_at = _now_iso()
        manifest["job"] = job.to_dict()
        _save_manifest(manifest, folder)
        return job

    provider = job.provider
    base_url = (settings.get("base_url") or job.base_url).rstrip("/")
    api_key = str(settings.get("api_key") or "")
    timeout = int(settings.get("timeout") or 180)
    validate_engine_network(provider, base_url)
    if not api_key:
        raise ValueError(f"{provider} Batch API 需要 API Key。")

    jsonl_file = Path(job.jsonl_path)
    if not jsonl_file.is_file():
        raise FileNotFoundError(f"找不到 Batch JSONL 文件：{jsonl_file}")
    lines = [json.loads(line) for line in jsonl_file.read_text(encoding="utf-8").splitlines() if line.strip()]

    if provider == "openai":
        body_bytes, content_type = _encode_multipart_file(
            "file", jsonl_file.name, jsonl_file.read_bytes(), {"purpose": "batch"}
        )
        upload_raw = _http_request_raw(
            f"{base_url}/files",
            method="POST",
            data=body_bytes,
            headers={**_provider_headers("openai", api_key), "Content-Type": content_type},
            timeout=timeout,
        )
        uploaded = json.loads(upload_raw.decode("utf-8"))
        file_id = str(uploaded.get("id") or "")
        if not file_id:
            raise RuntimeError("OpenAI 文件上传未返回 file_id")
        batch_endpoint = resolve_batch_endpoint("openai", base_url, str(settings.get("batch_endpoint") or job.endpoint))
        created = _http_post_json(
            f"{base_url}/batches",
            {"input_file_id": file_id, "endpoint": batch_endpoint, "completion_window": "24h"},
            {**_provider_headers("openai", api_key), "Content-Type": "application/json"},
            timeout,
        )
        job.endpoint = batch_endpoint
        job.remote_id = str(created.get("id") or "")
        job.status = "submitted"
    elif provider == "anthropic":
        created = _http_post_json(
            f"{base_url}/v1/messages/batches",
            {"requests": lines},
            {**_provider_headers("anthropic", api_key), "Content-Type": "application/json"},
            timeout,
        )
        job.remote_id = str(created.get("id") or "")
        job.status = "submitted"
    elif provider == "gemini":
        created = _http_post_json(
            f"{base_url}/models/{job.model}:batchGenerateContent",
            {"batch": {"display_name": job.job_id, "input_config": {"requests": {"requests": lines}}}},
            {**_provider_headers("gemini", api_key), "Content-Type": "application/json"},
            timeout,
        )
        job.remote_id = str(created.get("name") or "")
        job.status = "submitted"
    else:
        raise ValueError(f"不支持的 Batch 提供商：{provider}")

    if not job.remote_id:
        raise RuntimeError(f"{provider} Batch 创建未返回任务 ID")
    job.base_url = base_url
    job.updated_at = _now_iso()
    manifest["job"] = job.to_dict()
    _save_manifest(manifest, folder)
    LOG.info("batch submitted: id=%s provider=%s remote_id=%s", job.job_id, provider, job.remote_id)
    return job


def poll_batch_job(
    job_or_id: BatchJob | str,
    settings: dict[str, Any],
    *,
    job_dir: Path | None = None,
) -> BatchJob:
    """查询云端 Batch 任务的最新状态与计数，并同步更新本地存档。"""
    cache_dir = Path(settings.get("cache_dir") or app_dir() / "ferry_cache")
    folder = Path(job_dir) if job_dir else batch_jobs_dir(cache_dir)
    manifest = _load_manifest(job_or_id, folder)
    job = BatchJob.from_dict(manifest["job"])
    if job.request_count == 0 or not job.remote_id:
        return job

    provider = job.provider
    base_url = (settings.get("base_url") or job.base_url).rstrip("/")
    api_key = str(settings.get("api_key") or "")
    timeout = int(settings.get("timeout") or 180)
    validate_engine_network(provider, base_url)

    if provider == "openai":
        data = _http_get_json_auth(f"{base_url}/batches/{job.remote_id}", _provider_headers("openai", api_key), timeout)
        raw_status = str(data.get("status") or "").lower()
        mapping = {
            "validating": "in_progress",
            "pending": "in_progress",
            "queued": "in_progress",
            "running": "in_progress",
            "in_progress": "in_progress",
            "finalizing": "in_progress",
            "completed": "completed",
            "succeeded": "completed",
            "success": "completed",
            "failed": "failed",
            "expired": "expired",
            "cancelling": "cancelled",
            "canceling": "cancelled",
            "cancelled": "cancelled",
            "canceled": "cancelled",
        }
        job.status = mapping.get(raw_status, raw_status or job.status)
        counts = data.get("request_counts") or {}
        job.completed_count = int(counts.get("completed") or 0)
        job.failed_count = int(counts.get("failed") or 0)
        job.output_file_id = str(data.get("output_file_id") or "")
    elif provider == "anthropic":
        data = _http_get_json_auth(
            f"{base_url}/v1/messages/batches/{job.remote_id}", _provider_headers("anthropic", api_key), timeout
        )
        raw_status = str(data.get("processing_status") or "").lower()
        job.status = "completed" if raw_status == "ended" else "in_progress"
        counts = data.get("request_counts") or {}
        job.completed_count = int(counts.get("succeeded") or 0)
        job.failed_count = int(counts.get("errored") or 0) + int(counts.get("expired") or 0) + int(counts.get("canceled") or 0)
        job.results_url = str(data.get("results_url") or "")
    elif provider == "gemini":
        data = _http_get_json_auth(f"{base_url}/{job.remote_id.lstrip('/')}", _provider_headers("gemini", api_key), timeout)
        meta_obj = data.get("metadata") if isinstance(data.get("metadata"), dict) else data
        state = str(meta_obj.get("state") or data.get("state") or "").upper()
        if data.get("done") or state in ("BATCH_STATE_SUCCEEDED", "JOB_STATE_SUCCEEDED"):
            job.status = "completed"
        elif state in ("BATCH_STATE_FAILED", "JOB_STATE_FAILED"):
            job.status = "failed"
        elif state in ("BATCH_STATE_CANCELLED", "JOB_STATE_CANCELLED"):
            job.status = "cancelled"
        elif state in ("BATCH_STATE_EXPIRED", "JOB_STATE_EXPIRED"):
            job.status = "expired"
        else:
            job.status = "in_progress"
        stats = meta_obj.get("batchStats") or {}
        job.completed_count = int(stats.get("successfulRequestCount") or 0)
        job.failed_count = int(stats.get("failedRequestCount") or 0)
        # Gemini 响应可能直接内联在 job 返回体里，缓存下来供 collect 使用。
        if isinstance(data.get("response"), dict):
            manifest["_gemini_response"] = data["response"]

    job.updated_at = _now_iso()
    manifest["job"] = job.to_dict()
    _save_manifest(manifest, folder)
    return job


def _extract_provider_results(
    provider: str,
    job: BatchJob,
    manifest: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[dict[str, tuple[str, dict[str, int]]], list[str]]:
    """拉取远端结果，统一归一化为 {custom_id: (text_content, token_usage)}。"""
    base_url = (settings.get("base_url") or job.base_url).rstrip("/")
    api_key = str(settings.get("api_key") or "")
    timeout = int(settings.get("timeout") or 180)
    results: dict[str, tuple[str, dict[str, int]]] = {}
    errors: list[str] = []

    if provider == "openai":
        if not job.output_file_id:
            raise RuntimeError("OpenAI Batch 尚未产生 output_file_id（可能未完成或全部失败）")
        raw_bytes = _http_request_raw(
            f"{base_url}/files/{job.output_file_id}/content",
            method="GET",
            headers=_provider_headers("openai", api_key),
            timeout=timeout,
        )
        for line in raw_bytes.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            cid = str(item.get("custom_id") or "")
            if item.get("error"):
                errors.append(f"{cid}: {redact_secrets(str(item['error']))}")
                continue
            body = (item.get("response") or {}).get("body") or {}
            usage = body.get("usage") or {}
            p_details = usage.get("prompt_tokens_details") or {}
            tok = {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "cached_tokens": int(
                    ((p_details.get("cached_tokens") or 0) if isinstance(p_details, dict) else 0)
                    or usage.get("prompt_cache_hit_tokens")
                    or 0
                ),
            }
            try:
                text = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                errors.append(f"{cid}: OpenAI Batch 返回格式不正确")
                continue
            results[cid] = (text, tok)
        return results, errors

    if provider == "anthropic":
        if not job.results_url:
            raise RuntimeError("Anthropic Batch 尚未提供 results_url")
        raw_bytes = _http_request_raw(
            job.results_url,
            method="GET",
            headers=_provider_headers("anthropic", api_key),
            timeout=timeout,
        )
        for line in raw_bytes.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            cid = str(item.get("custom_id") or "")
            res = item.get("result") or {}
            if res.get("type") != "succeeded":
                errors.append(f"{cid}: {res.get('type') or 'error'}")
                continue
            msg = res.get("message") or {}
            usage = msg.get("usage") or {}
            tok = {
                "input_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cached_tokens": int(usage.get("cache_read_input_tokens") or 0),
            }
            parts = msg.get("content") or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
            results[cid] = (text, tok)
        return results, errors

    if provider == "gemini":
        resp_obj = manifest.get("_gemini_response")
        if not isinstance(resp_obj, dict):
            polled = _http_get_json_auth(f"{base_url}/{job.remote_id.lstrip('/')}", _provider_headers("gemini", api_key), timeout)
            resp_obj = polled.get("response") or polled
        inlined = (resp_obj.get("inlinedResponses") or {}).get("inlinedResponses") or []
        for idx, item in enumerate(inlined, 1):
            meta_obj = item.get("metadata") or {}
            cid = str(meta_obj.get("key") or f"b{idx:05d}")
            if item.get("error"):
                errors.append(f"{cid}: {redact_secrets(str(item['error']))}")
                continue
            body = item.get("response") or {}
            usage = body.get("usageMetadata") or {}
            tok = {
                "input_tokens": int(usage.get("promptTokenCount") or 0),
                "output_tokens": int(usage.get("candidatesTokenCount") or 0),
                "cached_tokens": int(usage.get("cachedContentTokenCount") or 0),
            }
            try:
                text = body["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError, TypeError):
                errors.append(f"{cid}: Gemini Batch 返回格式不正确")
                continue
            results[cid] = (text, tok)
        return results, errors

    raise ValueError(f"不支持的 Batch 提供商：{provider}")


def collect_batch_results(
    job_or_id: BatchJob | str,
    settings: dict[str, Any],
    *,
    job_dir: Path | None = None,
    write_tm: bool = True,
) -> tuple[dict[str, dict[str, str]], list[str], dict[str, list[str]], BatchJob]:
    """拉取 Batch 结果，还原占位符与去重项，并自动写入条目级 TM 缓存。"""
    cache_dir = Path(settings.get("cache_dir") or app_dir() / "ferry_cache")
    folder = Path(job_dir) if job_dir else batch_jobs_dir(cache_dir)
    manifest = _load_manifest(job_or_id, folder)
    job = BatchJob.from_dict(manifest["job"])

    all_translations: dict[str, dict[str, str]] = {
        modid: dict(data) for modid, data in (manifest.get("pre_translations") or {}).items()
    }
    if job.request_count == 0:
        return all_translations, [], {}, job

    if job.status != "completed":
        job = poll_batch_job(job, settings, job_dir=folder)
        manifest = _load_manifest(job, folder)
        if job.status != "completed":
            raise RuntimeError(f"Batch 任务尚未完成（当前状态：{job.status}），请稍后再取回。")

    raw_results, errors = _extract_provider_results(job.provider, job, manifest, settings)
    failed_keys: dict[str, set[str]] = {}
    tm_rows: list[tuple[str, str, str]] = []
    total_in = total_out = total_cached = 0

    for cid, req_info in (manifest.get("requests") or {}).items():
        modid = req_info["modid"]
        sid_to_key = req_info.get("sid_to_key")
        use_short_tags = bool(req_info.get("use_short_tags", False))
        glossary = req_info.get("glossary") or {}
        keep = set(req_info.get("keep") or [])
        items = req_info.get("items") or []

        if cid not in raw_results:
            for item in items:
                failed_keys.setdefault(modid, set()).add(item["key"])
                for dup in item.get("duplicates") or []:
                    failed_keys.setdefault(dup["modid"], set()).add(dup["key"])
            continue

        content_text, tok = raw_results[cid]
        total_in += tok.get("input_tokens", 0)
        total_out += tok.get("output_tokens", 0)
        total_cached += tok.get("cached_tokens", 0)
        try:
            parsed = _wire_decode(_parse_ai_json(content_text), sid_to_key, use_short_tags)
        except RuntimeError as exc:
            errors.append(f"{cid}: {exc}")
            for item in items:
                failed_keys.setdefault(modid, set()).add(item["key"])
                for dup in item.get("duplicates") or []:
                    failed_keys.setdefault(dup["modid"], set()).add(dup["key"])
            continue

        for item in items:
            key = item["key"]
            english = item["english"]
            val = parsed.get(key)
            valid = restore_and_validate(english, val, glossary, keep) if isinstance(val, str) else None
            if valid is None:
                failed_keys.setdefault(modid, set()).add(key)
                for dup in item.get("duplicates") or []:
                    failed_keys.setdefault(dup["modid"], set()).add(dup["key"])
            else:
                all_translations.setdefault(modid, {})[key] = valid
                if item.get("tm_key"):
                    tm_rows.append((item["tm_key"], english, valid))
                for dup in item.get("duplicates") or []:
                    all_translations.setdefault(dup["modid"], {})[dup["key"]] = valid

    if write_tm and tm_rows:
        with TranslationMemory(cache_dir) as tm:
            tm.put_many(tm_rows, engine=job.provider, model=job.model)

    job.input_tokens = total_in
    job.output_tokens = total_out
    job.cached_tokens = total_cached
    job.updated_at = _now_iso()
    manifest["job"] = job.to_dict()
    _save_manifest(manifest, folder)

    failed = {modid: sorted(keys) for modid, keys in failed_keys.items()}
    return all_translations, errors, failed, job


def cancel_batch_job(
    job_or_id: BatchJob | str,
    settings: dict[str, Any],
    *,
    job_dir: Path | None = None,
) -> BatchJob:
    """取消云端仍在排队/运行中的 Batch 任务。"""
    cache_dir = Path(settings.get("cache_dir") or app_dir() / "ferry_cache")
    folder = Path(job_dir) if job_dir else batch_jobs_dir(cache_dir)
    manifest = _load_manifest(job_or_id, folder)
    job = BatchJob.from_dict(manifest["job"])
    if not job.remote_id:
        job.status = "cancelled"
        job.updated_at = _now_iso()
        manifest["job"] = job.to_dict()
        _save_manifest(manifest, folder)
        return job

    provider = job.provider
    base_url = (settings.get("base_url") or job.base_url).rstrip("/")
    api_key = str(settings.get("api_key") or "")
    timeout = int(settings.get("timeout") or 180)
    validate_engine_network(provider, base_url)

    if provider == "openai":
        _http_post_json(f"{base_url}/batches/{job.remote_id}/cancel", {}, {**_provider_headers("openai", api_key), "Content-Type": "application/json"}, timeout)
    elif provider == "anthropic":
        _http_post_json(f"{base_url}/v1/messages/batches/{job.remote_id}/cancel", {}, {**_provider_headers("anthropic", api_key), "Content-Type": "application/json"}, timeout)
    elif provider == "gemini":
        _http_post_json(f"{base_url}/{job.remote_id.lstrip('/')}:cancel", {}, {**_provider_headers("gemini", api_key), "Content-Type": "application/json"}, timeout)

    job.status = "cancelled"
    job.updated_at = _now_iso()
    manifest["job"] = job.to_dict()
    _save_manifest(manifest, folder)
    return job
