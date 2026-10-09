#!/usr/bin/env bash
# 摆渡计划 - 预置 ForgeGradle 构建器依赖到本地 maven 仓库（D 盘）
#
# 为什么需要（第三层索引/传输问题）：
# BMCLAPI 虽然缓存了 jar，但它的 jar 存在「带签名且已限速的云存储直链」上
# （media-qhxn-fj-home.qh6oss.ctyunxs.cn，x-amz-limitrate=51200）。
# Gradle 解析依赖时会先发 HEAD 请求探测，签名已过期 → 403 Forbidden；
# 就算 GET 成功也被限速到 50 KB/s。
#
# 所以凡是 ForgeGradle 构建器自己需要的 jar，全部预置到本地仓库，
# 本地仓库是 file:// 协议，不走网络，彻底绕开这个问题。
#
# 用法：bash tools/prefetch-builder-deps.sh
# 产物：D:\Temp\forge-prefetch\m2\

set -u

MIRROR="https://bmclapi2.bangbang93.com/maven"
CENTRAL="https://repo1.maven.org/maven2"
M2="${FERRY_M2_DIR:-D:/Temp/forge-prefetch/m2}"

# 通用库走 Maven Central（快且稳定），Forge 专有走 BMCLAPI。
# 格式: group_path|artifact|version|source
ARTIFACTS=(
  # --- ForgeGradle POM 声明的 12 个直接依赖 ---
  "commons-io|commons-io|2.11.0|C"
  "com/google/code/gson|gson|2.10.1|C"
  "com/google/guava|guava|31.1-jre|C"
  "de/siegmar|fastcsv|2.2.1|C"
  "net/minecraftforge|artifactural|3.0.18|B"
  "net/minecraftforge|unsafe|0.2.0|B"
  "org/apache/maven|maven-artifact|3.9.1|C"
  "org/apache/httpcomponents|httpclient|4.5.14|C"
  "net/minecraftforge|srgutils|0.5.10|B"
  "net/minecraftforge|DiffPatch|2.0.12|B"
  "net/minecraftforge|JarJarMetadata|0.3.19|B"
  "net/minecraftforge|JarJarSelector|0.3.19|B"

  # --- httpclient 的传递依赖 ---
  "org/apache/httpcomponents|httpcore|4.4.16|C"
  "commons-codec|commons-codec|1.11|C"
  "commons-logging|commons-logging|1.2|C"

  # --- guava 的传递依赖（少量用到，但不补会在解析期报错） ---
  "com/google/guava|failureaccess|1.0.1|C"
  "com/google/guava|listenablefuture|9999.0-empty-to-avoid-conflict-with-guava|C"
  "com/google/code/findbugs|jsr305|3.0.2|C"
  "org/checkerframework|checker-qual|3.33.0|C"
  "com/google/errorprone|error_prone_annotations|2.11.0|C"
  "com/google/j2objc|j2objc-annotations|1.3|C"

  # --- maven-artifact 的传递依赖 ---
  "org/apache/maven|maven-model|3.9.1|C"
  "org/apache/maven|maven-repository-metadata|3.9.1|C"
  "org/codehaus/plexus|plexus-utils|3.5.1|C"
  "org/apache/maven|maven-artifact|3.9.1|C"

  # --- 构建过程中 Gradle/ForgeGradle 用到的工具 ---
  "org/jetbrains|annotations|24.0.1|C"
  "net/jodah|typetools|0.6.3|C"
  "org/jline|jline-terminal|3.12.1|C"
  "org/jline|jline-reader|3.12.1|C"
  "org/jline|jline|3.12.1|C"
  "net/sf|jmclauncher|1.2.0|C"

  # --- ASM（重混淆用） ---
  "org/ow2/asm|asm|9.5|C"
  "org/ow2/asm|asm-tree|9.5|C"
  "org/ow2/asm|asm-commons|9.5|C"
  "org/ow2/asm|asm-analysis|9.5|C"
  "org/ow2/asm|asm-util|9.5|C"

  # --- Mixin ---
  "org/spongepowered|mixin|0.8.5|C"
  "org/ow2/asm|asm|9.5|C"

  # --- CPW / Forge 运行期 ---
  "cpw/mods|modlauncher|10.0.1|B"
  "cpw/mods|securejarhandler|2.1.4|B"
  "cpw/mods|bootstraplauncher|1.1.2|B"
  "cpw/mods|securejar|1.4.10|B"
  "net/minecraftforge|eventbus|6.0.5|B"
  "net/minecraftforge|forgespi|7.0.1|B"
  "net/minecraftforge|securemodules|2.2.4|B"
  "net/minecraftforge|accesstransformers|8.2.0|B"
  "net/minecraftforge|mergetool|1.1.7|B"
)

fetch_one() {
  local gpath="$1" art="$2" ver="$3" src="$4"
  local base
  [ "$src" = "C" ] && base="$CENTRAL" || base="$MIRROR"

  local dir="$M2/$gpath/$art/$ver"
  local fname="$art-$ver"
  mkdir -p "$dir"

  # 已存在且合法就跳过
  if [ -f "$dir/$fname.jar" ] && [ "$(head -c 2 "$dir/$fname.jar" | xxd -p)" = "504b" ]; then
    echo "  已有  $art-$ver"
    return 0
  fi

  # jar 优先，404 再试 sources-less 变体；有些 artifact 只有 jar 没 pom
  local n
  n=$(curl -sL -o "$dir/$fname.jar" -w "%{size_download}" --max-time 120 "$base/$gpath/$art/$ver/$fname.jar" 2>/dev/null)
  local magic
  magic=$(head -c 2 "$dir/$fname.jar" 2>/dev/null | xxd -p)
  if [ "$magic" != "504b" ]; then
    rm -f "$dir/$fname.jar"
    echo "  缺失  $art-$ver ($n 字节)"
    return 1
  fi

  # pom 是可选的，拿到就放（Gradle 有 pom 会更信任本地仓库）
  curl -sL -o "$dir/$fname.pom" --max-time 60 "$base/$gpath/$art/$ver/$fname.pom" 2>/dev/null
  if [ "$(head -c 2 "$dir/$fname.pom" 2>/dev/null | xxd -p)" != "3c78" ]; then
    # 不是 <?xml 开头就删掉，避免污染仓库
    grep -q "<project" "$dir/$fname.pom" 2>/dev/null || rm -f "$dir/$fname.pom"
  fi

  echo "  下载  $art-$ver ($n 字节)"
}

write_metadata() {
  local gpath="$1" art="$2"
  local dir="$M2/$gpath/$art"
  local versions=""
  for d in "$dir"/*/; do
    [ -d "$d" ] || continue
    ls "$d"/*.jar >/dev/null 2>&1 && versions="$versions $(basename "$d")"
  done
  [ -z "$versions" ] && return 0
  local gpath_dotted
  gpath_dotted=$(echo "$gpath" | tr '/' '.')
  local body="  <versions>"
  for v in $versions; do body="$body"$'\n'"    <version>$v</version>"; done
  body="$body"$'\n'"  </versions>"
  printf '<?xml version="1.0" encoding="UTF-8"?>\n<metadata>\n  <groupId>%s</groupId>\n  <artifactId>%s</artifactId>\n  <versioning>\n%s\n  </versioning>\n</metadata>\n' \
    "$gpath_dotted" "$art" "$body" > "$dir/maven-metadata.xml"
}

echo "=============================================="
echo " 预置构建器依赖 -> $M2"
echo "=============================================="
echo

OK=0; FAIL=0
for spec in "${ARTIFACTS[@]}"; do
  IFS='|' read -r gpath art ver src <<< "$spec"
  if fetch_one "$gpath" "$art" "$ver" "$src"; then
    OK=$((OK+1))
  else
    FAIL=$((FAIL+1))
  fi
done

echo
echo "--- 生成 metadata ---"
for gpath in $(printf '%s\n' "${ARTIFACTS[@]}" | cut -d'|' -f1 | sort -u); do
  for art in $(printf '%s\n' "${ARTIFACTS[@]}" | grep "^$gpath|" | cut -d'|' -f2 | sort -u); do
    write_metadata "$gpath" "$art"
  done
done

echo
echo "完成：成功 $OK，失败 $FAIL"
echo "仓库 jar 总数：$(find "$M2" -name '*.jar' | wc -l)"
echo "总大小：$(du -sm "$M2" | cut -f1) MB"