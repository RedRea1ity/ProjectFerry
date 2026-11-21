"""文本单元：通用游戏线里「一句可翻的话」的统一表示。

MC 那边用 `Entry(modid, key, english, ...)`，字段名处处写着 MC 和英文。
游戏线用自己的 `Unit`，多出来的几个字段都是 MC 用不上、小众游戏离不开的：

    kind       story（剧情） / menu（菜单、按钮） / name（人名地名） / font（字体名）
    file       属于哪个文件 —— 剧情按文件顺序翻，前文当上下文
    max_bytes  回写时最多能占多少字节。单 exe、未知数据包只能原位等长覆盖，
               这一项不为 0 就是硬约束，超了就不写，绝不挤坏后面的数据
    meta       写回需要的定位信息（偏移、编码、填充方式），由适配器自己解释
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .controls import strip_controls

KINDS = ("story", "menu", "name", "font", "other")

_SENTENCE_MARKS = set("。！？!?…「」『』、，‥―─")
_KANA = re.compile(r"[\u3040-\u30ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]{2,}")


@dataclass
class Unit:
    key: str
    src: str
    kind: str = "story"
    file: str = ""
    order: int = 0
    speaker: str = ""
    max_bytes: int = 0
    encoding: str = ""
    dst: str = ""
    origin: str = ""  # ai / manual / auto / ""
    note: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def translatable(self) -> bool:
        return self.kind != "font"

    @property
    def done(self) -> bool:
        return bool(self.dst) and not self.note.startswith("超长")

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        # 空字段不落盘，译文表要让人用记事本也能看得下去
        return {k: v for k, v in data.items() if v not in ("", 0, {}, None) or k in ("key", "src", "dst")}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Unit":
        return cls(
            key=str(data.get("key", "")),
            src=str(data.get("src", "")),
            kind=str(data.get("kind") or "story"),
            file=str(data.get("file", "")),
            order=int(data.get("order") or 0),
            speaker=str(data.get("speaker", "")),
            max_bytes=int(data.get("max_bytes") or 0),
            encoding=str(data.get("encoding", "")),
            dst=str(data.get("dst", "")),
            origin=str(data.get("origin", "")),
            note=str(data.get("note", "")),
            meta=dict(data.get("meta") or {}),
        )


def classify(text: str) -> str:
    """剧情还是菜单。判据很朴素，但对小众游戏够用：

    - 带句读标点、或者够长 → 剧情
    - 其余的短串 → 菜单 / 按钮 / 系统词
    """
    visible = strip_controls(text).strip()
    if any(ch in _SENTENCE_MARKS for ch in visible):
        return "story"
    if _KANA.search(visible) or _CJK.search(visible):
        return "story" if len(visible) >= 12 else "menu"
    return "story" if len(visible.split()) >= 5 else "menu"


def detect_source_lang(samples: list[str]) -> str:
    """ja / ko / zh / en。只看样本里有没有假名、谚文。"""
    text = "".join(samples[:400])
    if _KANA.search(text):
        return "ja"
    if _HANGUL.search(text):
        return "ko"
    if _CJK.search(text):
        return "zh"
    return "en"


def byte_len(text: str, encoding: str) -> int | None:
    """按写回编码算字节数。编不出来返回 None —— 那句话根本写不进去。"""
    if not encoding:
        return len(text.encode("utf-8"))
    if encoding.lower() in ("cp932-remap", "pc98"):
        # PC-98 字库借壳编码：ASCII 占 1 字节，任意中日文全角字符（含借壳简体字）占 2 字节
        total = 0
        for ch in text:
            if ord(ch) < 0x80 or ("\uff61" <= ch <= "\uff9f"):
                total += 1
            else:
                try:
                    ch.encode("gbk")
                except UnicodeEncodeError:
                    try:
                        ch.encode("cp932")
                    except UnicodeEncodeError:
                        return None
                total += 2
        return total
    try:
        return len(text.encode(encoding))
    except (UnicodeEncodeError, LookupError):
        return None


def chars_budget(max_bytes: int, encoding: str) -> int:
    """字节预算换成「最多几个汉字」，写进提示词给模型看。"""
    if not max_bytes:
        return 0
    enc = (encoding or "").lower().replace("_", "-")
    if enc in ("utf-8", "utf8"):
        return max(1, max_bytes // 3)
    # GBK / Shift-JIS / UTF-16 都是一个汉字两字节
    return max(1, max_bytes // 2)
