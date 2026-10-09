#!/usr/bin/env bash
# 摆渡计划 - ForgeGradle 网络诊断
#
# 用途：构建卡住时先跑这个，判断是"电脑慢"还是"线路慢"。
# 背景：本机到 maven.minecraftforge.net 只有 ~20 KB/s，ForgeGradle 会静默挂死
#      （实测 35 分钟 CPU 只烧 28 秒）。所以先定位瓶颈再动手。
#
# 用法：bash tools/diagnose-forge-network.sh

set -u

SLOW_HOST="maven.minecraftforge.net"
TEST_PATH="net/minecraftforge/forge/1.20.1-47.3.0/forge-1.20.1-47.3.0-userdev.jar"
TEST_FILE="forge-1.20.1-47.3.0-userdev.jar"

MIRRORS=(
  "https://bmclapi2.bangbang93.com/maven|bmclapi2 (国服首选)"
  "https://bmclapi.bangbang93.com/maven|bmclapi"
  "https://maven.aliyun.com/repository/public|aliyun"
  "https://mirrors.cloud.tencent.com/nexus/repository/maven-public|tencent"
  "https://repo.huaweicloud.com/repository/maven|huawei"
  "https://repo1.maven.org/maven2|maven-central"
  "https://libraries.minecraft.net|mojang-libraries"
)

echo "=============================================="
echo " ForgeGradle 网络诊断"
echo "=============================================="
echo

# 注意：这些镜像对不存在的文件也返回 HTTP 200（假阳性），
# 所以必须看 size_download 的实际字节数，不能只看 HTTP 状态码。
probe() {
  local base="$1"
  curl -sL -o /dev/null \
       -w "HTTP=%{http_code} 字节=%{size_download} 速度=%{speed_download}B/s 耗时=%{time_total}s" \
       --max-time 20 -r 0-3000000 "${base}/${TEST_PATH}" 2>/dev/null
}

echo "--- 1. 官方 Forge 源（慢就是罪魁祸首）---"
R=$(probe "https://${SLOW_HOST}")
echo "  ${SLOW_HOST}"
echo "  ${R}"
OFFICIAL_SPEED=$(echo "$R" | grep -o '速度=[0-9]*' | cut -d= -f2)
echo

echo "--- 2. 备选镜像（按字节数判定有效性）---"
BEST=""
BEST_SPEED=0
for entry in "${MIRRORS[@]}"; do
  base="${entry%%|*}"
  name="${entry##*|}"
  R=$(probe "$base")
  SIZE=$(echo "$R" | grep -o '字节=[0-9]*' | cut -d= -f2)
  SPEED=$(echo "$R" | grep -o '速度=[0-9]*' | cut -d= -f2)
  if [ "${SIZE:-0}" -gt 100000 ] 2>/dev/null; then
    MARK="可用"
    if [ "${SPEED:-0}" -gt "$BEST_SPEED" ]; then
      BEST="$base"; BEST_SPEED=$SPEED
    fi
  else
    MARK="无此件"
  fi
  printf "  %-16s %-8s %s\n" "$name" "$MARK" "$R"
done
echo

echo "--- 3. 结论 ---"
if [ -n "$BEST" ]; then
  echo "  最快镜像: $BEST  (${BEST_SPEED} B/s)"
  if [ "${OFFICIAL_SPEED:-0}" -gt 0 ]; then
    echo "  官方源:   ${OFFICIAL_SPEED} B/s"
    echo "  提速倍数: $(( BEST_SPEED / (OFFICIAL_SPEED>0?OFFICIAL_SPEED:1) ))x"
  fi
  echo
  echo "  构建命令加上镜像脚本："
  echo "    gradle build -I gradle/mirror.init.gradle"
else
  echo "  所有镜像都没有 userdev.jar，只能走官方源（慢）。"
  echo "  建议改用 GitHub Actions 构建。"
fi
echo
echo "  另注：build.gradle 里 ForgeGradle 版本必须钉死（如 6.0.24），"
echo "        不能用 [6.0,6.2) 动态范围 —— 动态范围要拉 maven-metadata.xml，"
echo "        多仓库时 metadata 会互相干扰导致插件解析失败。"