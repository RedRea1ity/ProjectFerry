#!/usr/bin/env python
"""把 msimg32.dll 压成 zlib+base64 内嵌进 Python 模块。

为什么内嵌而不是让用户装编译器：摆渡的卖点是单 exe、零配置。
为了一个引擎要求用户装 300 MB 的 MinGW，等于把这个卖点取消掉。

为什么存成文本而不是直接把 .dll 放进仓库：
二进制进 git 既不可 diff，又会跟源码静默不同步。base64 文本至少能在
评审时看到「它变了」，而且 build.sh 能一个字重新生成。

用法（改了 msimg32_proxy.c 之后必须重跑）：

    ./build.sh                  # 编出 msimg32.dll
    python embed.py             # 重新生成 ferrycore/adapters/hsp_assets.py
"""

from __future__ import annotations

import base64
import subprocess
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
DLL = HERE / "msimg32.dll"
TARGET = HERE.parents[2] / "ferrycore" / "adapters" / "hsp_assets.py"

HEADER = '''"""HSP 代理 DLL 的内嵌副本 —— 由 `docs/research/hsp/embed.py` 生成，别手改。

内容就是 `docs/research/hsp/msimg32.dll` 的 zlib+base64。它做两件事：

1. 替身 `msimg32.dll`，被加载时 patch 主模块 IAT 里的
   `TabbedTextOutA` → 自己（转宽字符后调 `TabbedTextOutW`），
   `CreateFontIndirectA` → 自己（把 FixedSys 换成宋体）
2. 原样转发 `msimg32` 的 3 个导出，让游戏的图形混合照旧

内嵌而不是随包带 .dll，是为了 PyInstaller 单文件打包不用改 spec。
改了 `msimg32_proxy.c` 就得重跑 `embed.py`，否则这里跟源码不同步。
"""

from __future__ import annotations

import base64
import zlib

# zlib 压缩后的 PE 映像，约 {size_kb:.0f} KB
_PAYLOAD = (
{payload}
)


def load() -> bytes:
    """解出 DLL 的原始字节。只在真正部署时调一次。"""
    return zlib.decompress(base64.b64decode("".join(_PAYLOAD)))
'''


def main() -> int:
    if not DLL.is_file():
        print(f"没有 {DLL}，先跑 ./build.sh", file=sys.stderr)
        return 2
    raw = DLL.read_bytes()
    # 产物必须是 32 位：摆渡要替身的那些游戏都是 32 位 HSP 运行时
    machine = int.from_bytes(raw[raw.index(b"PE\0\0") + 4: raw.index(b"PE\0\0") + 6], "little")
    if machine != 0x014C:
        print(f"机器类型 0x{machine:04x} 不是 i386(0x014c)，这个 DLL 挂不上 32 位游戏", file=sys.stderr)
        return 1

    payload = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    lines = [f'    "{payload[i:i + 96]}"' for i in range(0, len(payload), 96)]
    TARGET.write_text(
        HEADER.format(size_kb=len(raw) / 1024, payload="\n".join(lines)),
        encoding="utf-8",
    )
    print(f"写好了 {TARGET}")
    print(f"  原始 {len(raw):,}B → base64 {len(payload):,}B（{len(lines)} 行）")

    # 自检：解回来必须逐字节相同，否则等于把坏 DLL 打包出去
    ns: dict = {}
    exec(compile(TARGET.read_text(encoding="utf-8"), str(TARGET), "exec"), ns)
    if ns["load"]() != raw:
        print("自检失败：解出来的字节跟源 DLL 不一致", file=sys.stderr)
        return 1
    print("自检通过：解压后与源 DLL 逐字节一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
