"""条目级翻译记忆库（SQLite）：跨批次、跨模型池、跨 batch_size 复用已校验译文。

与旧的「整批 JSON 哈希缓存」互补：
- 整批缓存只要多 1 条 key、改了 batch_size、换了中转 base_url 或多模型池换槽位就会全批失效；
- 条目级 TM 只按「源语言 + 生效于该条的术语/保留词 + 原文（精加工模式附带 modid）」索引，
  批内哪怕只成功了 29/30 条，那 29 条也会立刻落盘，下次续跑 0 Token 命中。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

from ferrycore.base import LOG

TM_SCHEMA_VERSION = "tm-v1"
TM_FILENAME = "translation_memory.sqlite3"


def _effective_glossary(source: str, glossary: dict[str, str] | None) -> list[tuple[str, str]]:
    if not glossary:
        return []
    folded = source.casefold()
    matched = [
        (str(k), str(v))
        for k, v in glossary.items()
        if k and str(k).casefold() in folded
    ]
    matched.sort()
    return matched


def _effective_keep(source: str, keep: Iterable[str] | None) -> list[str]:
    if not keep:
        return []
    folded = source.casefold()
    matched = [
        str(w)
        for w in keep
        if w and len(str(w)) >= 2 and str(w).casefold() in folded
    ]
    matched.sort()
    return matched


def tm_lookup_key(
    source: str,
    *,
    source_lang: str = "en",
    glossary: dict[str, str] | None = None,
    keep: Iterable[str] | None = None,
    modid: str = "",
    refine_community: bool = False,
) -> str:
    """计算单条文本的翻译记忆键。

    只把「实际出现在该句中的术语/保留词」纳入哈希，避免新增无关术语导致全库失效。
    """
    payload = {
        "v": TM_SCHEMA_VERSION,
        "lang": (source_lang or "en").lower(),
        "src": source,
        "g": _effective_glossary(source, glossary),
        "k": _effective_keep(source, keep),
        "scope": modid if refine_community else "",
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class TranslationMemory:
    """线程安全的轻量级 SQLite 翻译记忆库，异常时静默降级，绝不阻断翻译流程。"""

    def __init__(self, cache_dir: Path) -> None:
        self.path = Path(cache_dir) / TM_FILENAME
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=10.0, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entries (
                    lookup_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    engine TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.commit()
            self._conn = conn
        except (OSError, sqlite3.Error) as exc:
            LOG.warning("Could not initialize translation memory %s: %s", self.path, type(exc).__name__)
            self._conn = None

    def __enter__(self) -> "TranslationMemory":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    def get_many(self, keys: Iterable[str]) -> dict[str, str]:
        key_list = list(dict.fromkeys(keys))
        if not key_list or self._conn is None:
            return {}
        result: dict[str, str] = {}
        with self._lock:
            if self._conn is None:
                return {}
            try:
                # SQLite 默认单条语句最多 999 个参数，按 500 分块查。
                for start in range(0, len(key_list), 500):
                    chunk = key_list[start:start + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    rows = self._conn.execute(
                        f"SELECT lookup_key, target FROM entries WHERE lookup_key IN ({placeholders})",
                        chunk,
                    ).fetchall()
                    for k, target in rows:
                        if isinstance(target, str) and target:
                            result[str(k)] = target
            except sqlite3.Error as exc:
                LOG.warning("Translation memory read failed: %s", type(exc).__name__)
        return result

    def put_many(self, items: Iterable[tuple[str, str, str]], *, engine: str = "", model: str = "") -> None:
        rows = [(k, src, dst, engine, model, int(time.time())) for k, src, dst in items if k and dst]
        if not rows or self._conn is None:
            return
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.executemany(
                    """
                    INSERT INTO entries (lookup_key, source, target, engine, model, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(lookup_key) DO UPDATE SET
                        target = excluded.target,
                        engine = excluded.engine,
                        model = excluded.model,
                        updated_at = excluded.updated_at
                    """,
                    rows,
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                LOG.warning("Translation memory write failed: %s", type(exc).__name__)
