#!/usr/bin/env bash
# 摆渡桥构建脚本（唯一入口，别再手敲 gradle 长命令）
#
# 用法：
#   ./build.sh          # 完整 build（产 jar）
#   ./build.sh clean    # 先清 build/classes 再 build
#
# 关键点（都是踩过坑的）：
# 1. 只用 minimal.init.gradle —— **不要**用 mirror.init.gradle
#    实测改仓库顺序会把 ForgeGradle 的 artifactural 仓库体系搞坏，
#    导致 forge:..._mapped_official 解析失败。真正需要的只有 EnvironmentChecks 开关。
# 2. build.gradle 不要再手写 annotationProcessor '...mixin:processor'
#    ——ForgeGradle 已自动注册，重复注册会让 refmap 被写两次抛 FilerException。
# 3. compileJava 失败后重跑前要清 build/classes，否则残留 refmap 会干扰。

set -u

cd "$(dirname "$0")" || exit 1

JAVA_HOME="C:/Program Files/Microsoft/jdk-17.0.19.10-hotspot"
export JAVA_HOME
export GRADLE_USER_HOME="D:\\Temp\\gradle-home"
GRADLE="/d/Temp/gradle-8.1.1/bin/gradle"
LOG="/d/Temp/bridge-build.log"

unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

if [ "${1:-}" = "clean" ]; then
    echo "[clean] 删除 build/classes"
    rm -rf build/classes
fi

echo "[build] 开始，输出见 $LOG"
"$GRADLE" build -I gradle/minimal.init.gradle --no-daemon --console=plain > "$LOG" 2>&1
rc=$?

echo "[build] exit=$rc"
echo
grep -a "^> Task\|^BUILD\|错误:" "$LOG" | head -20

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