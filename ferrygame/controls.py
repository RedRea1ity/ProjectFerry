"""控制符保护：把游戏文本里的控制码换成占位符再交给模型，回来再换回去。

MC 那边的占位符正则只认 `%s` `§a` `$(br)`，拿来翻游戏会把 RPG Maker 的 `\\C[2]`、
Ren'Py 的 `{w}`、Tyrano 的 `[r]` 当成正文一起翻 —— 轻则颜色错乱，重则游戏当场报错。

规矩只有两条：

1. **宁可多保护，不可少保护**。多保护的代价是模型看不到某个符号，少保护的代价是崩游戏。
2. **回来少一个占位符就整句作废**，保留原文，交给人工。不猜它「大概放哪」。
"""

from __future__ import annotations

import re

# 顺序就是优先级：长的、特异的在前。
_PATTERNS = [
    # RPG Maker / Wolf：\C[2] \N[1] \V[10] \I[64] \FS[20] \px[3]
    r"\\[A-Za-z]{1,4}\[[^\]\n]{0,16}\]",
    # YEP 系插件：\fb<...>、\msgposx<...>
    r"\\[A-Za-z]{1,12}<[^>\n]{0,24}>",
    # RM 单符号控制：\{ \} \. \| \! \> \< \^ \$ \G \\
    r"\\[{}.|!><^$G\\]",
    # 转义字母：\n \r \t \fb \fi 之类（写在文本里的字面反斜杠序列）
    r"\\[A-Za-z]{1,3}",
    # printf：%s %d %1$s %05.2f %%
    r"%%|%(?:\d+\$)?[-+#0 ]*\d*(?:\.\d+)?[sdifxXcuo]",
    # Ren'Py 文字标签 / C# 格式串：{w} {p=1.0} {color=#f00} {/color} {0} {name}
    r"\{[^{}\n]{0,40}\}",
    # Tyrano / KAG / Ren'Py 插值：[r] [l] [p] [cm] [ruby text="か"] [player_name]
    r"\[[^\[\]\n]{1,48}\]",
    # HTML 式标签：<br> <color=red> </i>
    r"</?[A-Za-z][^<>\n]{0,30}>",
    # 颜色码：#FF00FF
    r"#[0-9A-Fa-f]{6}\b",
    # PC-98 / DOS BASIC 控制码与字符串变量：^m（换行） ^s（等待按键） B$ Y$ Z$ 以及 c1..c9 颜色码
    r"\^[A-Za-z]",
    r"[A-Z]\$",
    r"c[1-9](?=\^|[A-Z]\$| *[\u3000-\u9fff\uff00-\uffef0-9])",
]

CONTROL_RE = re.compile("|".join(f"(?:{p})" for p in _PATTERNS))
_TOKEN_RE = re.compile(r"__T(\d+)__")
_PRINTF_RE = re.compile(r"%%|%(?:\d+\$)?[-+#0 ]*\d*(?:\.\d+)?[sdifxXcuo]")


def mask(text: str) -> tuple[str, list[str]]:
    """返回 (带占位符的文本, 按序号排列的原控制码)。"""
    found: list[str] = []

    def repl(match: re.Match[str]) -> str:
        found.append(match.group(0))
        return f"__T{len(found) - 1}__"

    return CONTROL_RE.sub(repl, text), found


def unmask(source: str, translated: str, codes: list[str]) -> str | None:
    """把占位符换回控制码。占位符缺失 / 重复 / 编造，一律返回 None。"""
    if not isinstance(translated, str):
        return None
    seen = [int(n) for n in _TOKEN_RE.findall(translated)]
    if sorted(seen) != list(range(len(codes))):
        return None
    # printf 类参数有顺序语义，换序就是换参数，必须拒收。
    printf_ids = [i for i, code in enumerate(codes) if _PRINTF_RE.fullmatch(code)]
    if [i for i in seen if i in printf_ids] != printf_ids:
        return None
    out = _TOKEN_RE.sub(lambda m: codes[int(m.group(1))], translated)
    return out


def strip_controls(text: str) -> str:
    """去掉控制码后的「肉眼可见文字」。用来判断一句话有没有实际内容、估长度。"""
    return CONTROL_RE.sub("", text)
