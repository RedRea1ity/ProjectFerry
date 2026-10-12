#!/usr/bin/env bash
# 摆渡桥构建脚本（唯一入口，别再手敲 gradle 长命令）
#
# 用法：
#   ./build.sh          # 完整 build（产 jar），先离线跑，失败自动转联网补依赖
#   ./build.sh clean    # 先清 build/classes 再 build
#   ./build.sh online   # 直接联网（新机器 / 首次构建 / 缓存缺件时用）
#
# 关键点（都是踩过坑的）：
# 1. 只用 minimal.init.gradle —— **不要**用 mirror.init.gradle
#    实测改仓库顺序会把 ForgeGradle 的 artifactural 仓库体系搞坏，
#    导致 forge:..._mapped_official 解析失败。真正需要的只有 EnvironmentChecks 开关。
# 2. 2026-10-12（L1）起本模组**零 Mixin**：聊天栏与物品 tooltip 两条替换路径
#    都改走 Forge 官方事件，build.gradle 里那套 Mixin 注解处理器 / refmap /
#    SRG 混淆映射的配置已整体删除。因此现在**不需要** gradle/mappings/ 下的
#    混淆映射文件，也不需要预跑 tools/tsrg2_to_srg.py 之类的转换脚本。
#    （历史：那套东西只为生成 refmap；缺了它注入点会在生产环境静默失效，
#     症状是「游戏能启动、日志零 ERROR、功能就是不动」。见
#     docs/PLAN_SCALE_UP.md 第 2 节。）
# 3. compileJava 失败后重跑前要清 build/classes，否则残留产物会干扰。
# 4. 离线优先。ForgeGradle 联网解析 artifactural 依赖时可能很慢/静默等待，
#    所以正常情况走 --offline（缓存齐了构建约 1 分钟）。
#    但**缓存缺件时会报 "No cached version ... available for offline mode"**，
#    这时用 ./build.sh online 联网补一次，之后又能离线跑。
#    （注意：日志里大量 "because no repositories are defined" 是 ForgeGradle
#      自己吞掉的探测噪声，不是真错误 —— 真正的失败看最后的 "* What went wrong"。）

set -u

cd "$(dirname "$0")" || exit 1

JAVA_HOME="C:/Program Files/Microsoft/jdk-17.0.19.10-hotspot"
export JAVA_HOME

# ── 为什么不用 D:\Temp（2026-10-12 踩坑，别再改回去）────────────────
# D:\Temp 是系统临时目录（本机 $env:TEMP 就指向它），**会被定期清空**。
# 早先 GRADLE_USER_HOME / 预置仓库 / gradle 发行版全放那里，结果被清一次
# 直接构建挂掉（exit=127，找不到 gradle，预置仓库蒸发）。
# 现在：编译缓存复用系统默认 ~/.gradle（不在临时区、且已有 1.3G 依赖），
# gradle 用 ~/.gradle/wrapper/dists 里已有的发行版，本地预置仓库放项目内
# 的 ../.forge-prefetch/m2（由 tools/prefetch-forge-deps.sh 生成，已 gitignore）。
export GRADLE_USER_HOME="C:\\Users\\zetad\\.gradle"

# 从 wrapper/dists 里挑一个可用的 gradle 发行版（不联网、不依赖 PATH）。
GRADLE=""
for c in \
  "/c/Users/zetad/.gradle/wrapper/dists/gradle-8.2.1-all/d8pvvlun5bx6sdtwqhf8y9z4b/gradle-8.2.1/bin/gradle" \
  "/c/Users/zetad/.gradle/wrapper/dists/gradle-8.2.1-all/3ou6vv8g2udg8ml25g7mesvsk/gradle-8.2.1/bin/gradle" \
  ; do
    if [ -x "$c" ]; then GRADLE="$c"; break; fi
done
if [ -z "$GRADLE" ]; then
    GRADLE="$(ls -d /c/Users/zetad/.gradle/wrapper/dists/gradle-*/[0-9a-z]*/gradle-*/bin/gradle 2>/dev/null | head -1)"
fi
if [ -z "$GRADLE" ]; then
    echo "[错] 找不到 gradle 发行版。检查 C:\\Users\\zetad\\.gradle\\wrapper\\dists\\。" >&2
    exit 127
fi

# 日志放项目内的 .gradle/（已 gitignore），别再往临时目录写。
LOG="$(pwd)/.gradle/bridge-build.log"
mkdir -p "$(pwd)/.gradle"

unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

MODE="${1:-}"
if [ "$MODE" = "clean" ]; then
    echo "[clean] 删除 build/classes"
    rm -rf build/classes
    MODE=""
fi

run_build() {
    # $1 = "" 离线 / "--online"
    if [ "${1:-}" = "--online" ]; then
        echo "[build] 联网构建（补依赖）..."
        "$GRADLE" build -I gradle/minimal.init.gradle --no-daemon --console=plain > "$LOG" 2>&1
    else
        echo "[build] 离线构建..."
        "$GRADLE" build -I gradle/minimal.init.gradle --offline --no-daemon --console=plain > "$LOG" 2>&1
    fi
    return $?
}

echo "[build] 开始，输出见 $LOG"

if [ "$MODE" = "online" ]; then
    run_build --online
    rc=$?
else
    run_build
    rc=$?
    # 离线缺件 → 自动联网补一次
    if [ $rc -ne 0 ] && grep -qa "available for offline mode" "$LOG"; then
        echo "[build] 离线缓存缺件，自动转联网重试..."
        run_build --online
        rc=$?
    fi
fi

echo "[build] exit=$rc"
echo
grep -a "^> Task\|^BUILD\|错误:" "$LOG" | head -20

if [ $rc -ne 0 ]; then
    echo
    echo "--- 真正的失败原因（* What went wrong）---"
    grep -a -A 12 "\* What went wrong" "$LOG" | head -16
fi

JAR="build/libs/ferrybridge-1.0.0.jar"
if [ -f "$JAR" ]; then
    echo
    echo "[产物] $JAR  ($(du -k "$JAR" | cut -f1) KB)"
    echo "        magic: $(head -c2 "$JAR" | od -An -tx1 | tr -d ' \n')"
else
    echo
    echo "[产物] 无 jar"
    echo "--- 最近的 Java 编译错误 ---"
    grep -a "\.java:" "$LOG" | head -10
fi
exit $rc