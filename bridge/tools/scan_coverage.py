#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
摆渡计划 · mod 文本覆盖率扫描器（只读）

回答一个问题：整包里到底有多少文本能被资源包方案覆盖？
- 有 lang 文件的 → 资源包可覆盖
- 没 lang 文件的 → 需要看能不能从常量池捞到硬编码字符串

只读 jar，不解压不修改，秒级完成。
"""
import io
import json
import os
import re
import sys
import zipfile
from collections import defaultdict

MODS = sys.argv[1] if len(sys.argv) > 1 else r"D:\Minecraft\versions\香草纪元：食旅纪行 Release2.7.0\mods"

# 常量池里的字符串常量特征：长度 4-80，全 ASCII 可打印或含中文
STR_CONST = re.compile(rb"[\x20-\x7e]{4,80}")

# Java 字符串字面量在常量池里的真实存储：CONSTANT_Utf8 = 1 tag + 2 字节长度 + 字节
def extract_utf8_consts(data: bytes, limit=4000):
    """只扫描 tag=1（Utf8）的常量池项，跳过 Class/String/NameAndType 等其它 tag。"""
    out = []
    if len(data) < 12:
        return out
    i = 10  # 跳过 magic(4) + minor(2) + major(2) + constant_pool_count(2)
    count = int.from_bytes(data[8:10], "big")
    n = 0
    while i < len(data) and n < count:
        tag = data[i]
        if tag == 1:
            if i + 3 > len(data):
                break
            ln = int.from_bytes(data[i + 1:i + 3], "big")
            s = data[i + 3:i + 3 + ln]
            if 4 <= ln <= 200:
                out.append(s)
            i += 3 + ln
        elif tag in (7, 8, 16, 19, 20):
            i += 3
        elif tag == 15:
            i += 4
        elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
            i += 5
        elif tag in (5, 6):  # long/double 占两个槽
            i += 9
            n += 1
        else:
            break
        n += 1
    return out[:limit]


def looks_like_prose(s: bytes):
    """像自然语言界面文本：含空格，或长度适中且不是标识符/路径/类名。"""
    try:
        t = s.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if t.startswith(("java/", "javax/", "net/", "org/", "com/", "Ljava", "Lnet", "Lorg", "Lcom")):
        return False
    if "\\" in t or "/" in t or t.endswith(".class") or t.endswith(".json"):
        return False
    if t.count(".") >= 3 or ":" in t:
        return False
    if not re.search(r"[A-Za-z一-鿿]", t):
        return False
    # 排除全大写常量 / 全小写标识符
    if re.fullmatch(r"[A-Z0-9_]{4,}", t):
        return False
    if re.fullmatch(r"[a-z][a-z0-9_]{2,}", t) and "_" in t:
        return False
    return True


def main():
    if not os.path.isdir(MODS):
        print(f"[!] 目录不存在: {MODS}")
        return 1

    jars = sorted(f for f in os.listdir(MODS) if f.lower().endswith(".jar"))
    print(f"扫描目录: {MODS}")
    print(f"jar 数量: {len(jars)}\n")

    stat = {
        "with_lang": [], "en_only": [], "zh_ok": [],
        "no_lang": [], "broken": [],
    }
    total_en_keys = 0
    total_zh_keys = 0
    per_mod = []
    hardcode_candidates = {}   # jar -> [字符串]

    for jar in jars:
        path = os.path.join(MODS, jar)
        try:
            zf = zipfile.ZipFile(path)
        except Exception as e:
            stat["broken"].append((jar, str(e)[:60]))
            continue

        try:
            names = zf.namelist()
            lang_files = [n for n in names if "/lang/" in n and n.endswith(".json")]
            en_keys = set()
            zh_keys = set()
            for lf in lang_files:
                base = lf.rsplit("/", 1)[-1].lower()
                try:
                    raw = zf.read(lf)
                    data = json.loads(raw.decode("utf-8"))
                    if not isinstance(data, dict):
                        continue
                except Exception:
                    continue
                target = en_keys if base.startswith("en_us") else (zh_keys if base.startswith("zh_cn") else None)
                if target is not None:
                    target.update(k for k in data if isinstance(k, str))

            total_en_keys += len(en_keys)
            total_zh_keys += len(zh_keys)

            if not en_keys and not zh_keys:
                stat["no_lang"].append(jar)
            else:
                stat["with_lang"].append(jar)
                if en_keys and not zh_keys:
                    stat["en_only"].append(jar)

            per_mod.append((jar, len(en_keys), len(zh_keys)))

            # 没 en_us 的 mod，扫常量池找疑似硬编码界面文本
            if not en_keys:
                found = set()
                for n in names:
                    if not n.endswith(".class"):
                        continue
                    if len(found) > 200:
                        break
                    try:
                        cb = zf.read(n)
                    except Exception:
                        continue
                    for s in extract_utf8_consts(cb):
                        if looks_like_prose(s):
                            try:
                                found.add(s.decode("utf-8"))
                            except UnicodeDecodeError:
                                pass
                hardcode_candidates[jar] = sorted(found)
        finally:
            zf.close()

    # ── 报告 ──
    print("=" * 62)
    print("一、lang 文件覆盖情况")
    print("=" * 62)
    n_all = len(jars)
    n_lang = len(stat["with_lang"])
    n_en_only = len(stat["en_only"])
    n_no_lang = len(stat["no_lang"])
    print(f"  有 en_us 或 zh_cn 的 jar : {n_lang:4d}  ({n_lang*100//max(n_all,1)}%)")
    print(f"    其中：有英文、零中文    : {n_en_only:4d}  ← 资源包方案的主战场")
    print(f"  完全没有任何 lang 文件的 jar: {n_no_lang:4d}  ({n_no_lang*100//max(n_all,1)}%)")
    if stat["broken"]:
        print(f"  无法打开（损坏/加密）      : {len(stat['broken']):4d}")
    print()
    print(f"  英文 key 总数 : {total_en_keys}")
    print(f"  中文 key 总数 : {total_zh_keys}")
    gap = total_en_keys - total_zh_keys
    pct = (total_zh_keys * 100 / total_en_keys) if total_en_keys else 0
    print(f"  key 缺口      : {gap}  (中文覆盖率 {pct:.1f}%)")

    print()
    print("=" * 62)
    print("二、没 en_us 的 mod —— 常量池扫到的疑似硬编码文本")
    print("=" * 62)
    if not hardcode_candidates:
        print("  无")
    else:
        ranked = sorted(hardcode_candidates.items(), key=lambda kv: -len(kv[1]))
        tot = sum(len(v) for _, v in ranked)
        print(f"  这类 mod 共 {len(ranked)} 个，捞到疑似文本 {tot} 条\n")
        for jar, items in ranked[:25]:
            print(f"  {jar}  ({len(items)} 条)")
            for s in items[:4]:
                print(f"      {s[:70]}")
            if len(items) > 4:
                print(f"      ... 还有 {len(items)-4} 条")
        if len(ranked) > 25:
            print(f"\n  ... 还有 {len(ranked)-25} 个 jar 未列出")

    print()
    print("=" * 62)
    print("三、结论")
    print("=" * 62)
    if n_all:
        reach = n_lang * 100 / n_all
        print(f"  资源包方案可直接覆盖 : {reach:.1f}% 的 jar")
        print(f"  需要额外手段的        : {n_no_lang*100/n_all:.1f}% 的 jar")
        if hardcode_candidates:
            rescue = sum(1 for _, v in hardcode_candidates.items() if len(v) >= 5)
            print(f"  其中常量池能捞到文本  : {rescue} 个 jar（捞到 >=5 条）")

    out = os.path.join(os.path.dirname(__file__) or ".", "scan_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "mods_dir": MODS,
            "jar_total": n_all,
            "with_lang": n_lang,
            "en_only": n_en_only,
            "no_lang": n_no_lang,
            "en_keys": total_en_keys,
            "zh_keys": total_zh_keys,
            "per_mod": per_mod,
            "hardcode": {k: v for k, v in hardcode_candidates.items()},
        }, f, ensure_ascii=False, indent=1)
    print(f"\n明细已写入: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
