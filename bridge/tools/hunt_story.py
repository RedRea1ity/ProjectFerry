#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
摆渡计划 · 剧情文本 mod 猎手（只读，静态字节码分析）

目标：找出"把一整段文字塞进聊天框"的模组。
判定依据（两条都命中才算）：
  1. 调用了聊天消息发送相关方法（sendSystemMessage / sendMessage / displayClientMessage / gui.chat）
  2. 该类里存在长度 >= 80 的长字符串常量（剧情文本的特征）

这正是"桥"唯一不可替代的场景：这类文本如果没进 en_us.json，
资源包方案抓不到，只有运行时拦截或字节码注入才能命中。

用法: python hunt_story.py <mods目录> [阈值长度]
"""
import json
import os
import re
import sys
import zipfile

MODS = sys.argv[1] if len(sys.argv) > 1 else r"D:\Minecraft\versions\1.20.1-单独仙境吃屎版\mods"
MIN_LEN = int(sys.argv[2]) if len(sys.argv) > 2 else 80

# 聊天消息发送相关的调用点（混淆名无关，按常量池里的方法名+描述符匹配）
CHAT_CALLS = [
    b"sendSystemMessage",
    b"sendMessage",
    b"sendRichMessage",
    b"displayClientMessage",
    b"sendChatMessage",
    b"sendLocalChatMessage",
    b"addMessage",
    b"appendChatMessage",
]

# 长文本：连续可打印/中文，允许换行符和颜色码
LONG_TEXT = re.compile(rb"[\x20-\x7e\xe4-\xe9\x80-\xbf\n]{%d,}" % MIN_LEN)


def utf8_consts(data, limit=3000):
    """只扫 tag=1（CONSTANT_Utf8）项 —— 这是字符串常量，不是方法名/类名。"""
    out = []
    if len(data) < 10:
        return out
    n = 0
    count = int.from_bytes(data[8:10], "big")
    i = 10
    while i < len(data) and n < count and len(out) < limit:
        tag = data[i]
        try:
            if tag == 1:
                if i + 3 > len(data):
                    break
                ln = int.from_bytes(data[i + 1:i + 3], "big")
                if ln <= 900:
                    out.append(data[i + 3:i + 3 + ln])
                i += 3 + ln
            elif tag in (7, 8, 16, 19, 20):
                i += 3
            elif tag == 15:
                i += 4
            elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
                i += 5
            elif tag in (5, 6):
                i += 9
                n += 1
            else:
                break
        except Exception:
            break
        n += 1
    return out


def has_chat_call(d):
    return any(c in d for c in CHAT_CALLS)


def long_prose(s):
    """长字符串里，去掉噪声、看是不是自然语言段落。"""
    try:
        t = s.decode("utf-8")
    except UnicodeDecodeError:
        return None
    t2 = t.strip()
    if len(t2) < MIN_LEN:
        return None
    # 排除代码/符号堆砌
    if sum(c.isalnum() or c == " " for c in t2) / len(t2) < 0.6:
        return None
    # 至少要有两个空格分隔的词，或含中文
    if t2.count(" ") < 3 and not re.search(r"[\u4e00-\u9fff]", t2):
        return None
    return t2


def main():
    if not os.path.isdir(MODS):
        print(f"[!] 目录不存在: {MODS}")
        return 1
    jars = sorted(f for f in os.listdir(MODS) if f.lower().endswith(".jar"))
    print(f"目录: {MODS}")
    print(f"jar: {len(jars)} 个 | 长文本阈值: {MIN_LEN} 字符\n")

    # 第一步：哪些 mod 有 lang 文件（这些走资源包，不用管）
    have_lang = set()
    for jar in jars:
        try:
            z = zipfile.ZipFile(os.path.join(MODS, jar))
            if any("/lang/" in n and n.endswith(".json") for n in z.namelist()):
                have_lang.add(jar)
            z.close()
        except Exception:
            pass

    hits = {}
    for jar in jars:
        try:
            z = zipfile.ZipFile(os.path.join(MODS, jar))
        except Exception:
            continue
        found = {}
        for n in z.namelist():
            if not n.endswith(".class"):
                continue
            try:
                d = z.read(n)
            except Exception:
                continue
            if not has_chat_call(d):
                continue
            for s in utf8_consts(d):
                t = long_prose(s)
                if t:
                    found.setdefault(t, n)
        z.close()
        if found:
            # 取最长的若干条作为特征
            items = sorted(found.items(), key=lambda kv: -len(kv[0]))
            hits[jar] = items

    print("=" * 64)
    print("命中：有聊天发送调用 + 长文本常量")
    print("=" * 64)
    if not hits:
        print("  无命中")
    for jar, items in sorted(hits.items(), key=lambda kv: -len(kv[1])):
        langmark = "有lang" if jar in have_lang else "★无lang"
        longest = items[0][0]
        print(f"\n  {jar[:60]}")
        print(f"    [{langmark}] 长文本 {len(items)} 条，最长 {len(longest)} 字符")
        print(f"    在: {items[0][1]}")
        print(f"    ── 开头 ──")
        head = longest[:180].replace("\n", " ⏎ ")
        print(f"    {head}")
        if len(items) > 1:
            print(f"    ── 其它 ──")
            for t, _ in items[1:3]:
                print(f"    {t[:100].replace(chr(10), ' ⏎ ')}")

    print()
    print("=" * 64)
    print("结论")
    print("=" * 64)
    no_lang_hits = [j for j in hits if j not in have_lang]
    print(f"  命中 jar 总数        : {len(hits)}")
    print(f"  其中【没有 lang 文件】: {len(no_lang_hits)}  ← 桥的唯一战场")
    for j in no_lang_hits:
        print(f"      {j}")
    print()
    print("  有 lang 的命中 = 资源包方案能覆盖（不需要桥）")
    print("  无 lang 的命中 = 桥/字节码注入才抓得到")

    out = os.path.join(os.path.dirname(__file__) or ".", "hunt_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({j: [{"text": t, "cls": c} for t, c in v] for j, v in hits.items()},
                  f, ensure_ascii=False, indent=1)
    print(f"\n明细: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
