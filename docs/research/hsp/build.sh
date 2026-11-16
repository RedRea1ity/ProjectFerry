#!/usr/bin/env bash
# 构建 msimg32 代理 DLL。
#
# 必须用 **32 位** 编译器：目标游戏（HSP3 ANSI 版引擎）是 x86，
# 用 64 位工具链编出来的 DLL 加载不进 32 位进程，游戏会静默不启动
# （没有报错、没有窗口，很容易误判成"代理把游戏搞坏了"）。
#
# 依赖：i686-w64-mingw32-gcc（本机在 D:\tools\mingw32\bin）。
# 注意：不要链接 -lwinmm / -lmsimg32 —— 代理 DLL 若依赖同名库，
# 加载器会解析到它自己，递归失败，表现同样是游戏起不来。
set -euo pipefail

cd "$(dirname "$0")"

CC="${CC:-i686-w64-mingw32-gcc}"

if ! command -v "$CC" >/dev/null 2>&1; then
    for cand in "D:/tools/mingw32/bin/$CC.exe" "D:/tools/mingw32/bin/$CC"; do
        [ -x "$cand" ] && CC="$cand" && break
    done
fi

if ! command -v "$CC" >/dev/null 2>&1 && [ ! -x "$CC" ]; then
    echo "找不到 32 位 mingw 编译器：$CC" >&2
    echo "装好后重跑，或用 CC=/path/to/i686-w64-mingw32-gcc ./build.sh 指定" >&2
    exit 1
fi

"$CC" -shared -O2 -static-libgcc \
    -o msimg32.dll msimg32_proxy.c \
    -Wl,--kill-at

# 自检：机器类型必须是 0x014c (i386)，否则游戏加载不了
python - <<'PY'
import struct, sys
with open("msimg32.dll", "rb") as f:
    d = f.read()
off = struct.unpack_from("<I", d, 0x3C)[0]
machine = struct.unpack_from("<H", d, off + 4)[0]
print(f"msimg32.dll  {len(d):,} 字节  机器类型 0x{machine:04x}")
if machine != 0x014C:
    print("错误：不是 32 位 DLL（应为 0x014c）", file=sys.stderr)
    sys.exit(1)
PY

echo "OK — 把 msimg32.dll 复制到游戏目录（与 exe 同级）即可生效"
