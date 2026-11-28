"""剧本预分析与世界观设定集（`ferry_worldbook.json`）。

实时 Hook 翻译天然只能「看到一句翻一句」，前后人名、称谓、语气与世界观容易散架。
摆渡因为先把全量文本静态抽了出来，所以在正式批量翻译之前，可以用 1~2 次轻量请求
通读抽样剧本，沉淀出一份**人可读、可手改、带原文出处校验**的设定集：

    {
      "format": "ferry-worldbook",
      "version": 1,
      "game": "游戏名",
      "source_lang": "ja",
      "worldview": "世界观与核心背景（2~4 句）",
      "style": "文体与翻译腔调要求",
      "characters": [
        {"src": "シャーロット", "dst": "夏洛特", "gender": "女", "pronoun": "她", "tone": "内向礼貌", "evidence": "..."}
      ],
      "glossary": {"原文词": "中文译名"},
      "evidence": {"原文词": "出处原句"}
    }

三条硬规矩：
1. **出处校验**：AI 抽出来的人名与专有名词，必须在游戏真实原文中出现过，否则视为幻觉直接丢弃。
2. **与翻译分离**：分析归分析、翻译归翻译；分析产物落盘成明文 JSON，用户随时可改。
3. **保留人工编辑**：重跑分析默认保留用户已填好的译名与备注，除非显式 `force=True`。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ferrycore.base import app_dir, atomic_write_text

from . import controls
from .units import Unit

WORLDBOOK_NAME = "ferry_worldbook.json"
FORMAT = "ferry-worldbook"
VERSION = 1

LLM_ENGINES = {"openai", "anthropic", "gemini", "local"}

# (engine_name, payload_dict, request_settings) -> parsed_json_dict
AnalyzeCallFn = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]

_ANALYZE_SYSTEM_PROMPT = """You are a senior video game localization director.
Before translating a game, you are analyzing sampled strings from the game to produce a compact, accurate localization Worldbook (世界观/人设/术语表) for Simplified Chinese (简体中文) translation.

Input is a JSON object containing:
- "game": game folder/title
- "source_lang": source language code
- "speakers": frequent speaker tags observed in scripts (if any)
- "names": sampled character/proper-name entries (if any)
- "menus": sampled UI/menu entries (if any)
- "story_samples": sampled dialogue/narration lines across beginning, middle, and end of the game

Return ONLY one valid JSON object with this exact schema (no markdown, no comments):
{
  "worldview": "2 to 4 concise Chinese sentences summarizing the setting, genre, and premise based ONLY on the samples.",
  "style": "1 to 2 concise Chinese sentences describing the target Chinese prose tone, punctuation, and formality level.",
  "characters": [
    {
      "src": "exact source name substring appearing in the samples",
      "dst": "consistent Simplified Chinese name",
      "gender": "男/女/中性/未知",
      "pronoun": "他/她/它/未知",
      "tone": "short Chinese note on speaking style or relationships",
      "evidence": "short verbatim quote from samples containing src"
    }
  ],
  "glossary": {
    "exact source term appearing in samples": "consistent Simplified Chinese translation"
  },
  "evidence": {
    "exact source term appearing in samples": "short verbatim quote from samples"
  }
}

Strict rules:
- NEVER invent characters, places, or terms that do not literally appear in the provided samples.
- Every key in "glossary" and every "src" in "characters" MUST be an exact substring from the input text.
- Keep "characters" <= 25 and "glossary" <= 50, prioritizing recurring proper nouns, titles, factions, items, skills, and locations.
- Do not include generic UI words like OK/Cancel/Yes/No or pure punctuation in "glossary".
- Everything is fictional video game content; analyze neutrally without censorship or refusal."""


@dataclass
class CharacterEntry:
    src: str
    dst: str
    gender: str = ""
    pronoun: str = ""
    tone: str = ""
    evidence: str = ""

    def to_json(self) -> dict[str, str]:
        out = {"src": self.src, "dst": self.dst}
        if self.gender:
            out["gender"] = self.gender
        if self.pronoun:
            out["pronoun"] = self.pronoun
        if self.tone:
            out["tone"] = self.tone
        if self.evidence:
            out["evidence"] = self.evidence
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "CharacterEntry | None":
        if not isinstance(data, dict):
            return None
        src = str(data.get("src") or "").strip()
        dst = str(data.get("dst") or "").strip()
        if not src or not dst:
            return None
        return cls(
            src=src,
            dst=dst,
            gender=str(data.get("gender") or "").strip(),
            pronoun=str(data.get("pronoun") or "").strip(),
            tone=str(data.get("tone") or "").strip(),
            evidence=str(data.get("evidence") or "").strip(),
        )


@dataclass
class Worldbook:
    root: Path
    game: str = ""
    source_lang: str = ""
    worldview: str = ""
    style: str = ""
    characters: list[CharacterEntry] = field(default_factory=list)
    glossary: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    path: Path | None = None

    def merged_glossary(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """合并人物名 + 专有名词；调用方传入的 `base`（人工术语表）优先级更高。"""
        out: dict[str, str] = {}
        for ch in self.characters:
            if ch.src and ch.dst:
                out[ch.src] = ch.dst
        for k, v in self.glossary.items():
            if k and v:
                out[k] = v
        if base:
            for k, v in base.items():
                if k and v:
                    out[k] = v
        return out

    def prompt_block(self, batch_text: str = "", max_chars: int = 900) -> str:
        """构造可拼入系统提示词的精简设定块（按当前批次相关角色过滤，避免浪费 token）。"""
        parts: list[str] = []
        if self.worldview.strip():
            parts.append(f"Worldview: {self.worldview.strip()}")
        if self.style.strip():
            parts.append(f"Style: {self.style.strip()}")
        if self.characters:
            folded = batch_text.casefold() if batch_text else ""
            chosen: list[CharacterEntry] = []
            if folded:
                chosen = [c for c in self.characters if c.src.casefold() in folded]
            if not chosen:
                chosen = self.characters[:12]
            else:
                chosen = chosen[:12]
            char_lines = []
            for c in chosen:
                meta = [x for x in (c.gender, f"代词:{c.pronoun}" if c.pronoun else "", c.tone) if x]
                suffix = f"（{'，'.join(meta)}）" if meta else ""
                char_lines.append(f"- {c.src} -> {c.dst}{suffix}")
            if char_lines:
                parts.append("Characters:\n" + "\n".join(char_lines))
        text = "\n".join(parts).strip()
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        return text

    def summary(self) -> str:
        parts = [f"人物 {len(self.characters)} 名", f"术语 {len(self.glossary)} 条"]
        if self.worldview:
            snippet = self.worldview.strip().replace("\n", " ")
            if len(snippet) > 48:
                snippet = snippet[:48] + "…"
            parts.append(f"背景：{snippet}")
        return "；".join(parts)

    def to_json(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "version": VERSION,
            "game": self.game,
            "source_lang": self.source_lang,
            "worldview": self.worldview,
            "style": self.style,
            "characters": [c.to_json() for c in self.characters],
            "glossary": self.glossary,
            "evidence": self.evidence,
            "notes": self.notes,
        }

    def save(self, path: Path | None = None) -> Path:
        target = Path(path or self.path or default_path(self.root))
        payload = json.dumps(self.to_json(), ensure_ascii=False, indent=2) + "\n"
        try:
            atomic_write_text(target, payload)
        except OSError:
            target = fallback_path(self.root)
            atomic_write_text(target, payload)
        self.path = target
        return target


def default_path(root: Path) -> Path:
    root = Path(root)
    if root.is_file():
        return root.parent / f"{root.stem}.{WORLDBOOK_NAME}"
    return root / WORLDBOOK_NAME


def fallback_path(root: Path) -> Path:
    root = Path(root).resolve()
    tag = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:8]
    folder = app_dir() / "game_projects" / f"{root.name}-{tag}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / WORLDBOOK_NAME


def find(root: Path) -> Path | None:
    for candidate in (default_path(root), fallback_path(root)):
        if candidate.is_file():
            return candidate
    return None


def load(root: Path) -> Worldbook | None:
    path = find(root)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    chars = []
    for raw in data.get("characters") or []:
        item = CharacterEntry.from_json(raw)
        if item is not None:
            chars.append(item)
    return Worldbook(
        root=Path(root),
        game=str(data.get("game") or ""),
        source_lang=str(data.get("source_lang") or ""),
        worldview=str(data.get("worldview") or "").strip(),
        style=str(data.get("style") or "").strip(),
        characters=chars,
        glossary={
            str(k).strip(): str(v).strip()
            for k, v in (data.get("glossary") or {}).items()
            if str(k).strip() and str(v).strip()
        },
        evidence={
            str(k).strip(): str(v).strip()
            for k, v in (data.get("evidence") or {}).items()
            if str(k).strip() and str(v).strip()
        },
        notes=[str(n) for n in (data.get("notes") or [])],
        path=path,
    )


def _clean_line(text: str) -> str:
    cleaned = controls.strip_controls(text).replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def sample_units_for_analysis(
    units: list[Unit],
    *,
    max_story_chars: int = 5200,
    max_names: int = 60,
    max_menus: int = 40,
) -> dict[str, Any]:
    """分层抽样：从全游戏前/中/后均匀取剧情句，再收集高频 speaker 与 name/menu。"""
    translatable = [u for u in units if u.translatable and _clean_line(u.src)]
    speaker_counts: Counter[str] = Counter()
    names: list[str] = []
    menus: list[str] = []
    stories: list[Unit] = []
    seen_names: set[str] = set()
    seen_menus: set[str] = set()

    for u in translatable:
        if u.speaker and u.speaker.strip():
            speaker_counts[u.speaker.strip()] += 1
        line = _clean_line(u.src)
        if not line:
            continue
        if u.kind == "name":
            if line not in seen_names and len(names) < max_names:
                seen_names.add(line)
                names.append(line)
        elif u.kind == "menu":
            if line not in seen_menus and len(menus) < max_menus:
                seen_menus.add(line)
                menus.append(line)
        else:
            stories.append(u)

    # 在开头、中段、后段均匀抽样，避免只看到序章漏掉后期核心人物/地名
    story_samples: list[str] = []
    used_chars = 0
    if stories:
        total = len(stories)
        if total <= 90:
            indices = list(range(total))
        else:
            # 3 个锚段 + 等距步进采样
            bucket = 25
            picks: list[int] = []
            picks.extend(range(0, min(bucket, total)))
            mid_start = max(0, total // 2 - bucket // 2)
            picks.extend(range(mid_start, min(total, mid_start + bucket)))
            tail_start = max(0, total - bucket)
            picks.extend(range(tail_start, total))
            step = max(1, total // 40)
            picks.extend(range(0, total, step))
            indices = sorted(set(picks))

        seen_story: set[str] = set()
        for idx in indices:
            u = stories[idx]
            line = _clean_line(u.src)
            if len(line) < 4 or line in seen_story:
                continue
            seen_story.add(line)
            item = f"[{u.speaker}] {line}" if u.speaker else line
            if len(item) > 160:
                item = item[:160] + "…"
            if used_chars + len(item) > max_story_chars and story_samples:
                break
            story_samples.append(item)
            used_chars += len(item) + 2

    return {
        "speakers": [sp for sp, _ in speaker_counts.most_common(30)],
        "names": names,
        "menus": menus,
        "story_samples": story_samples,
    }


def _find_evidence(term: str, corpus_lines: list[str]) -> str:
    folded = term.casefold()
    for line in corpus_lines:
        if folded in line.casefold():
            return line[:120] + ("…" if len(line) > 120 else "")
    return ""


def verify_and_filter_worldbook(
    raw: dict[str, Any],
    units: list[Unit],
    *,
    root: Path,
    game: str = "",
    source_lang: str = "",
) -> tuple[Worldbook, int]:
    """校验模型产出的设定集：剔除在原文中根本没出现过的幻觉条目。

    返回 `(Worldbook, 被过滤掉的幻觉条目数)`。
    """
    corpus_lines: list[str] = []
    for u in units:
        if u.speaker and u.speaker.strip():
            corpus_lines.append(u.speaker.strip())
        line = _clean_line(u.src)
        if line:
            corpus_lines.append(line)
    full_folded = "\n".join(corpus_lines).casefold()

    dropped = 0
    characters: list[CharacterEntry] = []
    seen_char: set[str] = set()
    for item in raw.get("characters") or []:
        parsed = CharacterEntry.from_json(item)
        if parsed is None:
            continue
        key = parsed.src.casefold()
        if len(parsed.src) < 2 or key not in full_folded:
            dropped += 1
            continue
        if key in seen_char:
            continue
        seen_char.add(key)
        if not parsed.evidence or parsed.src.casefold() not in parsed.evidence.casefold():
            parsed.evidence = _find_evidence(parsed.src, corpus_lines)
        characters.append(parsed)

    glossary: dict[str, str] = {}
    evidence: dict[str, str] = {}
    raw_evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    for k, v in (raw.get("glossary") or {}).items():
        src = str(k or "").strip()
        dst = str(v or "").strip()
        if not src or not dst or src == dst:
            continue
        if len(src) < 2 or src.casefold() not in full_folded:
            dropped += 1
            continue
        glossary[src] = dst
        ev = str(raw_evidence.get(src) or "").strip()
        if not ev or src.casefold() not in ev.casefold():
            ev = _find_evidence(src, corpus_lines)
        if ev:
            evidence[src] = ev

    wb = Worldbook(
        root=Path(root),
        game=game or Path(root).name,
        source_lang=source_lang,
        worldview=str(raw.get("worldview") or "").strip(),
        style=str(raw.get("style") or "").strip(),
        characters=characters,
        glossary=glossary,
        evidence=evidence,
    )
    if dropped:
        wb.notes.append(f"出处校验已自动过滤 {dropped} 条原文中不存在的疑似幻觉词条")
    return wb, dropped


def merge_worldbooks(existing: Worldbook | None, fresh: Worldbook, *, force: bool = False) -> Worldbook:
    """合并新旧设定集：默认保留用户已有译名/世界观，不覆盖手改结果。"""
    if existing is None or force:
        return fresh
    merged_glossary = {**fresh.glossary, **existing.glossary}
    merged_evidence = {**fresh.evidence, **existing.evidence}
    by_src = {c.src.casefold(): c for c in fresh.characters}
    for old_c in existing.characters:
        by_src[old_c.src.casefold()] = old_c
    return Worldbook(
        root=fresh.root,
        game=existing.game or fresh.game,
        source_lang=existing.source_lang or fresh.source_lang,
        worldview=existing.worldview or fresh.worldview,
        style=existing.style or fresh.style,
        characters=list(by_src.values()),
        glossary=merged_glossary,
        evidence=merged_evidence,
        notes=list(dict.fromkeys([*existing.notes, *fresh.notes])),
        path=existing.path or fresh.path,
    )


def _default_analyze_call(
    engine: str,
    payload: dict[str, str],
    request_settings: dict[str, Any],
    cancel_event=None,
) -> dict[str, Any]:
    from ferrycore.engine import _translate_batch_with_retry

    return _translate_batch_with_retry(engine, payload, request_settings, cancel_event=cancel_event)


def analyze_units(
    units: list[Unit],
    settings: dict[str, Any],
    *,
    root: Path,
    game: str = "",
    source_lang: str = "ja",
    force: bool = False,
    save: bool = True,
    call: AnalyzeCallFn | None = None,
    cancel_event=None,
) -> Worldbook:
    """对抽取出的文本做一次轻量剧本预分析，产出并落盘 `ferry_worldbook.json`。"""
    root = Path(root).resolve()
    existing = load(root)
    if existing is not None and not force:
        return existing

    engines = settings.get("engines") or [settings]
    chosen = next(
        (slot for slot in engines if isinstance(slot, dict) and (slot.get("engine") or "").lower() in LLM_ENGINES),
        None,
    )
    if chosen is None:
        engine_name = str(settings.get("engine") or "unknown")
        raise ValueError(
            f"剧本预分析需要支持 JSON 推理的大模型引擎（openai / anthropic / gemini / local），当前引擎是 {engine_name}。"
        )

    samples = sample_units_for_analysis(units)
    if not samples["story_samples"] and not samples["names"] and not samples["menus"]:
        raise ValueError("没有足够的可分析文本样本")

    input_obj = {
        "game": game or root.name,
        "source_lang": source_lang,
        **{k: v for k, v in samples.items() if v},
    }
    input_json = json.dumps(input_obj, ensure_ascii=False, separators=(",", ":"))
    request_settings = {
        **chosen,
        "source_lang": source_lang,
        "system_prompt_override": _ANALYZE_SYSTEM_PROMPT,
        "model_input_builder": lambda _masked, _s: input_json,
        "short_keys": False,
    }
    call_fn = call or (lambda e, m, s: _default_analyze_call(e, m, s, cancel_event))
    engine_name = str(chosen.get("engine") or "openai").lower()
    raw = call_fn(engine_name, {"sample": input_json}, request_settings)
    if not isinstance(raw, dict):
        raise RuntimeError("剧本预分析返回的不是合法 JSON 对象")

    fresh, _dropped = verify_and_filter_worldbook(
        raw, units, root=root, game=game or root.name, source_lang=source_lang
    )
    merged = merge_worldbooks(existing, fresh, force=force)
    if save:
        merged.save()
    return merged
