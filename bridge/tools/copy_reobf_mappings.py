#!/usr/bin/env python3
"""
固化 ForgeGradle 算出的 reobf 映射，供 Mixin 注解处理器生成 refmap。

## 为什么需要（2026-10-10）

Mixin AP 要生成 refmap，必须知道「可读方法名 → 生产环境标识名」的对应关系。
不配置时构建报：

    错误: Unable to locate obfuscation mapping for @Inject target addMessage

而因为 @Inject 同时设了 require = 0，这个失败在**运行时是静默的**：
游戏正常启动、日志零 ERROR，但所有注入点被跳过，功能就是不生效。
属于最难查的一类问题，所以 build.gradle 里有 verifyRefmap 守着。

## 为什么必须用 ForgeGradle 的 reobf 产物，而不是官方 mappings

我试过三个来源，只有这一个能用，原因值得记下来：

| 来源 | 为什么不行 |
|---|---|
| Mojang 官方 `client_mappings.txt` 转的 tsrg | **ProGuard 对重载方法用同一个混淆名**。ChatComponent 的三个 addMessage 混淆名全是 `a`。而 AP 内部用 guava `BiMap.forcePut` 建索引，混淆名必须唯一，否则相互覆盖 → 查找失败 |
| `srg_to_official_*.tsrg` | 1.20.1 早期产物，ChatComponent 里只有 2 个 addMessage，缺单参数那个 |
| `mcp_mappings.tsrg` | 是 notch→srg（`C_252363_` 风格），不含 official 名，方向不对 |

SRG 名（`m_93785_`）天然按「类+签名+序号」生成，**重载之间必然不同**，
这正是 BiMap 需要的性质。所以只能走 SRG。

## 用法

    python tools/copy_reobf_mappings.py

需要在跑过一次 gradle build 之后（reobfJar 才会产出映射）。
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "build" / "reobfJar" / "mappings.tsrg"
DST = ROOT / "gradle" / "mappings" / "official_to_srg.tsrg"


def main() -> int:
    if not SRC.exists():
        print(f"找不到 {SRC}", file=sys.stderr)
        print("先跑一次 gradle build（不带 clean），让 reobfJar 产出映射。", file=sys.stderr)
        return 1

    DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SRC, DST)
    size_mb = DST.stat().st_size / 1024 / 1024
    print(f"已固化 {DST}（{size_mb:.1f} MB）")

    # 自检：ChatComponent 的三个 addMessage 重载必须在，且混淆名互不相同
    #（相同就说明拿到的是 ProGuard 映射，会触发 BiMap 冲突）
    lines = DST.read_text(encoding="utf-8", errors="ignore").splitlines()
    key = "net/minecraft/client/gui/components/ChatComponent "
    for i, line in enumerate(lines):
        if line.startswith(key):
            hits = []
            for ln in lines[i + 1:]:
                if not ln.startswith("\t"):
                    break
                if ln.strip().startswith("addMessage "):
                    hits.append(ln.strip())
            obfs = {h.rsplit(" ", 1)[-1] for h in hits}
            print(f"自检 ChatComponent：{len(hits)} 个 addMessage 重载")
            for h in hits:
                print("   ", h)
            if len(obfs) != len(hits):
                print("自检失败：混淆名有重复，这份是 ProGuard 映射，不能用", file=sys.stderr)
                return 1
            print("混淆名互不相同 ✅（SRG 名，可以给 AP 用）")
            return 0
    print("自检失败：输出里找不到 ChatComponent", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
