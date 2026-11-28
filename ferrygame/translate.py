"""通用游戏线的翻译编排。

跟 MC 的 `translate_entries` 不同的地方，每一条都对着「翻剧情」：

- **按文件分组、组内按顺序翻**。每批带上同文件里前面几句（原文 + 译文）当上下文，
  称呼、语气才接得上。组与组之间并发。
- **请求里用短编号**（"1"、"2"…）代替真实位置 key。位置 key 动辄
  `data/scene01.dat@1a2b:cp932` 这么长，让模型复述既费 token 又容易抄错。
- **字节预算是硬约束**。超了先让模型重写一次更短的；还超就把那句标成
  「超长」留给人，绝不截断硬塞。
- **控制符**走 `controls.mask/unmask`，回来少一个就整句作废。

底层只借 `ferrycore.engine._translate_batch_with_retry`（发请求 + 重试），
提示词和输入格式通过两个钩子换成游戏线自己的。
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ferrycore.base import LOG, app_dir, atomic_write_text, redact_secrets
from ferrycore.engine import TruncatedResponseError

from . import controls
from .profile import PROMPT_VERSION, system_prompt
from .units import Unit, byte_len, chars_budget

CONTEXT_LINES = 6
GLOSSARY_LIMIT = 40

_TRANSLATABLE_CHAR_RE = re.compile(r"[A-Za-z\u00c0-\u024f\u0400-\u04ff\u3040-\u30ff\uac00-\ud7af\u4e00-\u9fff]")

# (engine 名, 已掩码的 {id: 文本}, 请求设置) -> {id: 译文}
CallFn = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]


@dataclass
class TranslateReport:
    requested: int = 0
    translated: int = 0
    too_long: int = 0
    rejected: int = 0
    prefiltered: int = 0
    deduped: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    thought_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False

    def summary(self) -> str:
        parts = [f"本次请求 {self.requested} 条，拿到 {self.translated} 条"]
        if self.input_tokens or self.output_tokens:
            tok = f"消耗输入 {self.input_tokens:,} / 输出 {self.output_tokens:,}"
            if self.cached_tokens:
                tok += f"（前缀缓存命中 {self.cached_tokens:,}）"
            if self.thought_tokens:
                tok += f"（思考 {self.thought_tokens:,}）"
            parts.append(tok + " tokens")
        saved = []
        if self.prefiltered:
            saved.append(f"预过滤 {self.prefiltered} 条")
        if self.deduped:
            saved.append(f"菜单/专名去重 {self.deduped} 条")
        if saved:
            parts.append("零 Token 省流：" + "、".join(saved))
        if self.too_long:
            parts.append(f"{self.too_long} 条超出字节预算（已标记，需人工缩短）")
        if self.rejected:
            parts.append(f"{self.rejected} 条控制符对不上被丢弃")
        if self.errors:
            parts.append(f"{len(self.errors)} 批失败")
        if self.cancelled:
            parts.append("已中途停止，再跑一次会接着翻")
        return "，".join(parts)


def _default_call(engine: str, masked: dict[str, str], settings: dict[str, Any], cancel_event=None) -> dict[str, Any]:
    from ferrycore.engine import _translate_batch_with_retry

    return _translate_batch_with_retry(engine, masked, settings, cancel_event=cancel_event)


def _build_input(masked: dict[str, str], settings: dict[str, Any]) -> str:
    meta = settings.get("_unit_meta") or {}
    strings: dict[str, Any] = {}
    for k, v in masked.items():
        info = meta.get(k) or {}
        # 默认剧情且无附加元数据时直接传纯字符串，省掉每条 {"text":...,"kind":"story"} 冗余。
        if info == {"kind": "story"} or not info:
            strings[k] = v
        else:
            strings[k] = {"text": v, **info}
    payload: dict[str, Any] = {}
    context = settings.get("_context") or []
    glossary = settings.get("_glossary") or {}
    if context:
        payload["context"] = context
    if glossary:
        payload["glossary"] = glossary
    payload["strings"] = strings
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def relevant_glossary(glossary: dict[str, str], units: list[Unit], limit: int = GLOSSARY_LIMIT) -> dict[str, str]:
    if not glossary:
        return {}
    text = "\n".join(u.src for u in units)
    hits = {k: v for k, v in glossary.items() if k and k in text}
    return dict(sorted(hits.items(), key=lambda kv: -len(kv[0]))[:limit])


def _is_non_translatable_unit(unit: Unit) -> bool:
    """仅含控制符、数字、标点或空白的单元无需调模型，直接原样保留（0 Token）。"""
    visible = controls.strip_controls(unit.src).strip()
    return not _TRANSLATABLE_CHAR_RE.search(visible)


class _Cache:
    def __init__(self, folder: Path | None):
        self.folder = Path(folder) if folder else app_dir() / "ferry_cache" / "game"
        self.folder.mkdir(parents=True, exist_ok=True)

    def key(self, *parts: Any) -> str:
        blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.folder / f"{key}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def put(self, key: str, data: dict[str, Any]) -> None:
        try:
            atomic_write_text(self.folder / f"{key}.json", json.dumps(data, ensure_ascii=False) + "\n")
        except OSError as exc:
            LOG.warning("game cache write failed: %s", type(exc).__name__)


def _group_by_file(units: list[Unit]) -> "OrderedDict[str, list[Unit]]":
    groups: OrderedDict[str, list[Unit]] = OrderedDict()
    for unit in sorted(units, key=lambda u: (u.file, u.order)):
        groups.setdefault(unit.file, []).append(unit)
    return groups


def translate_units(
    units: list[Unit],
    settings: dict[str, Any],
    source_lang: str = "ja",
    glossary: dict[str, str] | None = None,
    force: bool = False,
    progress: Callable[[int, int], None] | None = None,
    cancel_event=None,
    call: CallFn | None = None,
    cache_dir: Path | None = None,
    worldbook=None,
) -> TranslateReport:
    """把没译文的单元翻掉，结果直接写进 `unit.dst`。"""
    report = TranslateReport()
    glossary = worldbook.merged_glossary(glossary) if worldbook is not None else dict(glossary or {})
    call = call or (lambda e, m, s: _default_call(e, m, s, cancel_event))
    cache = _Cache(cache_dir)
    base_prompt = system_prompt(source_lang)
    batch_size = max(1, int(settings.get("batch_size") or 60))
    max_batch_chars = int(settings.get("max_batch_chars", 3600) or 0)
    concurrency = max(1, int(settings.get("concurrency") or 1))
    engines = settings.get("engines") or [settings]

    def wanted(unit: Unit) -> bool:
        if not unit.translatable:
            return False
        if unit.origin == "manual":
            return False
        return force or not unit.dst

    groups = _group_by_file(units)
    total = sum(1 for u in units if wanted(u))
    report.requested = total
    state_lock = threading.Lock()
    done = [0]
    # 无剧情上下文依赖的 menu/name 短词去重缓存：(src, kind, encoding, max_bytes) -> dst
    reuse_map: dict[tuple[str, str, str, int], str] = {}
    for u in units:
        if u.dst and not u.note.startswith("超长") and u.kind in ("menu", "name"):
            reuse_map.setdefault((u.src, u.kind, u.encoding, u.max_bytes), u.dst)

    def tick(n: int) -> None:
        with state_lock:
            done[0] += n
            if progress:
                progress(done[0], total)

    def fits(unit: Unit, text: str) -> bool:
        if not unit.max_bytes:
            return byte_len(text, unit.encoding) is not None
        size = byte_len(text, unit.encoding)
        return size is not None and size <= unit.max_bytes

    def request(engine_settings: dict[str, Any], batch: list[Unit], context: list[dict[str, str]],
                retry_long: dict[str, str] | None = None) -> dict[str, str]:
        """发一批，返回 {unit.key: 已还原控制符的译文}。控制符对不上的不返回。"""
        ids = {str(i + 1): unit for i, unit in enumerate(batch)}
        masked: dict[str, str] = {}
        codes: dict[str, list[str]] = {}
        meta: dict[str, dict[str, Any]] = {}
        for sid, unit in ids.items():
            masked[sid], codes[sid] = controls.mask(unit.src)
            info: dict[str, Any] = {"kind": unit.kind if unit.kind in ("story", "menu", "name") else "story"}
            if unit.speaker:
                info["speaker"] = unit.speaker
            limit = chars_budget(unit.max_bytes, unit.encoding)
            if limit:
                info["max_chars"] = limit
            if retry_long and unit.key in retry_long:
                info["too_long"] = retry_long[unit.key]
            meta[sid] = info
        gloss = relevant_glossary(glossary, batch)
        batch_prompt = (
            system_prompt(source_lang, worldbook.prompt_block("\n".join(u.src for u in batch)))
            if worldbook is not None
            else base_prompt
        )
        token_sink: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "thought_tokens": 0}
        request_settings = {
            **engine_settings,
            "source_lang": source_lang,
            "system_prompt_override": batch_prompt,
            "model_input_builder": _build_input,
            "_unit_meta": meta,
            "_context": context,
            "_glossary": gloss,
            "_token_usage": token_sink,
        }
        engine = engine_settings.get("engine") or "openai"
        ckey = cache.key(PROMPT_VERSION, engine, engine_settings.get("model"), engine_settings.get("base_url"),
                         batch_prompt, masked, meta, context, gloss)
        raw = cache.get(ckey)
        if raw is None:
            try:
                raw = call(engine, masked, request_settings)
            except TruncatedResponseError:
                if len(batch) <= 1:
                    raise
                mid = len(batch) // 2
                left = request(engine_settings, batch[:mid], context, retry_long=retry_long)
                right = request(engine_settings, batch[mid:], context, retry_long=retry_long)
                left.update(right)
                return left
            if not isinstance(raw, dict):
                raise RuntimeError("模型返回的不是 JSON 对象")
            if all(sid in raw for sid in ids):
                cache.put(ckey, raw)
            with state_lock:
                report.input_tokens += token_sink["input_tokens"]
                report.output_tokens += token_sink["output_tokens"]
                report.cached_tokens += token_sink["cached_tokens"]
                report.thought_tokens += token_sink["thought_tokens"]
        out: dict[str, str] = {}
        for sid, unit in ids.items():
            value = raw.get(sid)
            restored = controls.unmask(unit.src, value, codes[sid]) if isinstance(value, str) else None
            if restored is None or not controls.strip_controls(restored).strip():
                if value is not None:
                    with state_lock:
                        report.rejected += 1
                continue
            out[unit.key] = restored.strip("\n") if not unit.src.endswith("\n") else restored
        return out

    def run_group(index: int, members: list[Unit]) -> None:
        engine_settings = engines[index % len(engines)]
        pending: list[Unit] = []
        pending_chars = 0

        def flush() -> None:
            nonlocal pending_chars
            if not pending:
                return
            if cancel_event is not None and cancel_event.is_set():
                with state_lock:
                    report.cancelled = True
                pending.clear()
                pending_chars = 0
                return
            batch = list(pending)
            pending.clear()
            pending_chars = 0
            first = members.index(batch[0])
            context = [
                {"src": u.src, "zh": u.dst}
                for u in members[:first] if u.dst and u.translatable
            ][-CONTEXT_LINES:]
            try:
                got = request(engine_settings, batch, context)
                too_long = {}
                for unit in batch:
                    text = got.get(unit.key)
                    if text is None:
                        continue
                    if fits(unit, text):
                        unit.dst, unit.origin, unit.note = text, "ai", ""
                        with state_lock:
                            report.translated += 1
                            if unit.kind in ("menu", "name"):
                                reuse_map[(unit.src, unit.kind, unit.encoding, unit.max_bytes)] = text
                    else:
                        too_long[unit.key] = text
                if too_long:
                    retry = [u for u in batch if u.key in too_long]
                    # 超长缩短只针对单句改写，不需要重复发 6 句历史上下文。
                    again = request(engine_settings, retry, [], retry_long=too_long)
                    for unit in retry:
                        text = again.get(unit.key) or too_long[unit.key]
                        if fits(unit, text):
                            unit.dst, unit.origin, unit.note = text, "ai", ""
                            with state_lock:
                                report.translated += 1
                                if unit.kind in ("menu", "name"):
                                    reuse_map[(unit.src, unit.kind, unit.encoding, unit.max_bytes)] = text
                        else:
                            size = byte_len(text, unit.encoding)
                            unit.dst, unit.origin = text, "ai"
                            unit.note = (f"超长：{size} 字节 > 上限 {unit.max_bytes}" if size is not None
                                         else f"超长：含 {unit.encoding} 编不出的字")
                            with state_lock:
                                report.too_long += 1
            except Exception as exc:  # 一批失败不拖垮整组
                message = f"{batch[0].file}（{len(batch)} 条）：{redact_secrets(str(exc))}"
                LOG.warning("game batch failed: %s", message)
                with state_lock:
                    report.errors.append(message)
            tick(len(batch))

        for unit in members:
            if not wanted(unit):
                continue
            # 零 Token 预过滤：纯符号/控制符直接原样保留。
            if _is_non_translatable_unit(unit):
                unit.dst, unit.origin, unit.note = unit.src, "auto", ""
                with state_lock:
                    report.translated += 1
                    report.prefiltered += 1
                tick(1)
                continue
            # 菜单/专名去重复用：跨文件重复出现的按钮词只翻一次。
            if unit.kind in ("menu", "name"):
                rkey = (unit.src, unit.kind, unit.encoding, unit.max_bytes)
                with state_lock:
                    reused = reuse_map.get(rkey)
                if reused is not None and fits(unit, reused):
                    unit.dst, unit.origin, unit.note = reused, "ai", ""
                    with state_lock:
                        report.translated += 1
                        report.deduped += 1
                    tick(1)
                    continue
            unit_chars = len(unit.src) + 16
            if pending and max_batch_chars > 0 and pending_chars + unit_chars > max_batch_chars:
                flush()
            pending.append(unit)
            pending_chars += unit_chars
            if len(pending) >= batch_size:
                flush()
        flush()

    items = list(groups.values())
    if concurrency <= 1 or len(items) <= 1:
        for index, members in enumerate(items):
            run_group(index, members)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(lambda pair: run_group(*pair), enumerate(items)))
    if cancel_event is not None and cancel_event.is_set():
        report.cancelled = True
    return report
