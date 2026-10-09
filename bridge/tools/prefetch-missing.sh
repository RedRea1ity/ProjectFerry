#!/usr/bin/env bash
# 摆渡计划 - 补抓缺失构件到本地 maven 仓库
#
# 为什么需要：BMCLAPI 对部分构件返回带签名、会过期的云存储直链
# （media-qhxn-fj-home.qh6oss.ctyunxs.cn），Gradle HEAD 一下就 403。
# 本地仓库走 file:// 协议，完全不发 HTTP 请求，是最稳的兜底。
#
# 用法：
#   ./prefetch-missing.sh                       # 抓下面 COORDS 里列的所有
#   ./prefetch-missing.sh org.antlr:antlr4:4.9.1  # 只抓指定的
#
# 判据：只看实际字节数 + 前两字节是否 PK(zip magic)，不看 HTTP 状态码。
# BMCLAPI 对不存在的文件会返回 200 + 9 字节 "Not Found"。

set -u

LOCAL_M2="${LOCAL_M2:-D:/Temp/forge-prefetch/m2}"
CENTRAL="https://repo1.maven.org/maven2"

# groupId:artifactId:version[:classifier]
COORDS=(
    "org.antlr:antlr4:4.9.1"
    "net.minecrell:terminalconsoleappender:1.2.0"
    "net.java.dev.jna:jna:5.12.1"
    "org.lwjgl:lwjgl-jemalloc:3.3.1:natives-macos-arm64"
    "org.lwjgl:lwjgl-tinyfd:3.3.1:natives-linux-arm64"
    "org.lwjgl:lwjgl-tinyfd:3.3.1:natives-macos-arm64"
)

# 为什么 lwjgl 要下全平台：
# ForgeGradle 解析 ':_compileJava_1' 这个内部配置时**只查本地仓库**
# （实测：项目仓库列表里明明有 MavenRepo 和 BMCLAPI，但报错里
#   "Searched in the following locations" 只有 FerryLocal 一条）。
# 而且它会把 arm64 等用不到的平台 natives 也一起解析。
# 所以这里按 模块 × 平台 全矩阵预置，一次性解决，不再逐个补。
LWJGL_MODULES=(lwjgl lwjgl-glfw lwjgl-openal lwjgl-opengl lwjgl-stb lwjgl-jemalloc lwjgl-tinyfd)
LWJGL_CLASSIFIERS=(natives-windows natives-windows-x86 natives-windows-arm64
                   natives-macos natives-macos-arm64
                   natives-linux natives-linux-arm64 natives-linux-arm32)

for m in "${LWJGL_MODULES[@]}"; do
    COORDS+=("org.lwjgl:$m:3.3.1")
    for c in "${LWJGL_CLASSIFIERS[@]}"; do
        COORDS+=("org.lwjgl:$m:3.3.1:$c")
    done
done

ok=0; fail=0; skip=0

fetch() {
    local url="$1" dst="$2" label="$3"
    if [ -s "$dst" ]; then
        skip=$((skip+1))
        return 0
    fi
    mkdir -p "$(dirname "$dst")"
    curl -sSL --max-time 120 --retry 2 -o "$dst.part" "$url" 2>/dev/null
    local sz
    sz=$(stat -c%s "$dst.part" 2>/dev/null || echo 0)
    if [ "$sz" -lt 100 ]; then
        rm -f "$dst.part"
        echo "  MISS  $label  (${sz}B <- $url)"
        fail=$((fail+1))
        return 1
    fi
    # 校验 zip magic
    local magic
    magic=$(head -c2 "$dst.part" | od -An -tx1 | tr -d ' \n')
    if [ "$magic" != "504b" ]; then
        rm -f "$dst.part"
        echo "  BAD   $label  (不是 zip, magic=$magic)"
        fail=$((fail+1))
        return 1
    fi
    mv "$dst.part" "$dst"
    echo "  OK    $label  ($((sz/1024)) KB)"
    ok=$((ok+1))
    return 0
}

for c in "${COORDS[@]}"; do
    IFS=':' read -r g a v cls <<< "$c"
    gpath="${g//./\/}"
    base="$LOCAL_M2/$gpath/$a/$v"

    # POM：Gradle 解析模块元数据要用
    pom="$base/$a-$v.pom"
    if [ ! -s "$pom" ]; then
        mkdir -p "$(dirname "$pom")"
        curl -sSL --max-time 120 -o "$pom.part" "$CENTRAL/$gpath/$a/$v/$a-$v.pom" 2>/dev/null
        psz=$(stat -c%s "$pom.part" 2>/dev/null || echo 0)
        if [ "$psz" -gt 100 ] && head -c5 "$pom.part" | grep -q '<'; then
            mv "$pom.part" "$pom"
        else
            rm -f "$pom.part"
            echo "  NOPOM $g:$a:$v"
        fi
    fi

    if [ -n "${cls:-}" ]; then
        fetch "$CENTRAL/$gpath/$a/$v/$a-$v-$cls.jar" "$base/$a-$v-$cls.jar" "$a-$v-$cls.jar"
    else
        fetch "$CENTRAL/$gpath/$a/$v/$a-$v.jar" "$base/$a-$v.jar" "$a-$v.jar"
    fi
done

echo
echo "完成: 新增 $ok / 失败 $fail / 已存在 $skip  (仓库: $LOCAL_M2)"
