#!/usr/bin/env bash
# 摆渡计划 - 预置 ForgeGradle 运行时依赖到本地 maven 仓库（D 盘）
#
# 为什么需要：BMCLAPI 镜像虽然缓存了工件文件，但 maven-metadata.xml 的索引
# 没更新，所以 `cpw.mods:modlauncher:10.0.+` 这类动态版本解析不出来
# （报 "Could not find any version that matches 10.0.+"）。
# 解法：把工件按标准 maven 布局放到本地仓库，并在 metadata 里手动登记版本。
#
# 用法：bash tools/prefetch-forge-deps.sh
# 产物：D:\Temp\forge-prefetch\m2\

set -u

MIRROR="https://bmclapi2.bangbang93.com/maven"
M2="${FERRY_M2_DIR:-D:/Temp/forge-prefetch/m2}"

# group_path:artifact:version[:classifier]
ARTIFACTS=(
  "cpw/mods:modlauncher:10.0.9"
  "cpw/mods:modlauncher:10.0.1"
  "cpw/mods:securejarhandler:2.1.4"
  "cpw/mods:access-transformers:8.0.4"
  "cpw/mods:bootstraplauncher:1.1.2"
  "net/minecraftforge:eventbus:6.0.5"
  "net/minecraftforge:forgespi:7.0.1"
  "net/minecraftforge:securemodules:2.2.4"
  "net/minecraftforge:accesstransformers:8.2.0"
  "net/minecraftforge:mergetool:1.1.7:fatjar"
  "net/minecraftforge:installertools:1.3.2:fatjar"
  "net/minecraftforge:ForgeAutoRenamingTool:0.1.22:all"
  "net/minecraftforge:JarCompatibilityChecker:0.1.28:all"
  "net/minecraftforge:binarypatcher:1.1.1:fatjar"
  "net/minecraftforge:Srg2Source:8.2.0:fatjar"
  "org/spongepowered:mixin:0.8.5"
  "org/ow2/asm:asm:9.5"
  "org/ow2/asm:asm-tree:9.5"
  "org/ow2/asm:asm-commons:9.5"
  "org/ow2/asm:asm-analysis:9.5"
  "org/ow2/asm:asm-util:9.5"
)

fetch_one() {
  local gpath="$1" art="$2" ver="$3" cls="${4:-}"
  local dir="$M2/$gpath/$art/$ver"
  local fname="$art-$ver"
  [ -n "$cls" ] && fname="$fname-$cls"
  local url="$MIRROR/$gpath/$art/$ver/$fname.jar"

  mkdir -p "$dir"

  # 已存在且是合法 jar 就跳过
  if [ -f "$dir/$fname.jar" ] && [ "$(head -c 2 "$dir/$fname.jar" | xxd -p)" = "504b" ]; then
    echo "  已有  $art-$ver"
    return 0
  fi

  local n
  n=$(curl -sL -o "$dir/$fname.jar" -w "%{size_download}" --max-time 180 "$url" 2>/dev/null)
  local magic
  magic=$(head -c 2 "$dir/$fname.jar" 2>/dev/null | xxd -p)

  if [ "$magic" != "504b" ]; then
    # 镜像假 200（9 字节 Not Found）或空响应
    rm -f "$dir/$fname.jar"
    echo "  缺失  $art-$ver  ($n 字节，非 jar)"
    return 1
  fi
  echo "  下载  $art-$ver  ($n 字节)"
}

# 写 metadata，让动态版本（如 10.0.+）能解析到
write_metadata() {
  local gpath="$1" art="$2"
  local dir="$M2/$gpath/$art"
  local versions
  versions=$(ls "$dir" 2>/dev/null | grep -E '^[0-9]' | tr '\n' ' ')
  [ -z "$versions" ] && return 0

  local body="  <versions>"
  for v in $versions; do
    body="$body"$'\n'"    <version>$v</version>"
  done
  body="$body"$'\n'"  </versions>"

  cat > "$dir/maven-metadata.xml" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<metadata>
  <groupId>$(echo "$gpath" | tr '/' '.')</groupId>
  <artifactId>$art</artifactId>
  <versioning>
$body
  </versioning>
</metadata>
EOF
  echo "  索引  $art: $versions"
}

echo "=============================================="
echo " 预置 ForgeGradle 依赖 -> $M2"
echo "=============================================="
echo

OK=0; FAIL=0
for spec in "${ARTIFACTS[@]}"; do
  IFS=':' read -r gpath art ver cls <<< "$spec"
  if fetch_one "$gpath" "$art" "$ver" "$cls"; then
    OK=$((OK+1))
  else
    FAIL=$((FAIL+1))
  fi
done

echo
echo "--- 生成 maven-metadata.xml（供动态版本解析）---"
for gpath in $(printf '%s\n' "${ARTIFACTS[@]}" | cut -d: -f1 | sort -u); do
  for art in $(printf '%s\n' "${ARTIFACTS[@]}" | grep "^$gpath:" | cut -d: -f2 | sort -u); do
    write_metadata "$gpath" "$art"
  done
done

echo
echo "完成：成功 $OK，失败 $FAIL"
echo
echo "在 settings.gradle 里启用本地仓库："
echo "  maven { name = 'FerryLocal'; url = uri('${M2}') }"