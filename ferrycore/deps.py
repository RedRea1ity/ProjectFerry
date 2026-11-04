"""汉化依赖：有些游戏，光改文本不够，还得先有外部工具。

这层的存在理由很具体。汉化一本 HSP 游戏时，文本能干净地抽出来、也能写回去，
但游戏按 Shift-JIS 解码，写进去的 UTF-8/GBK 中文会变成乱码 —— 这时唯一的
出路是编一个 `winmm.dll` 代理去改它的编码转换，而那需要 C 编译器。
用户机器上没有编译器，于是整件事卡在一个跟"翻译"毫无关系的地方。

卡住本身不是问题，**静默卡住才是**。所以这套东西的规矩是：

1. **绝不静默下载。** 必须由用户点「下载」才联网。一个汉化工具偷偷往
   系统里装 100MB 编译器，无论理由多正当都是越界。
2. **必须说清为什么。** 弹窗里不能只写"缺少 mingw32"，得写"这本游戏按
   Shift-JIS 解码中文，要编一个编码转换代理才能显示中文"。
3. **架构必须匹配。** 32 位游戏只能加载 32 位 DLL。用 64 位编译器编出来的
   代理塞进去，游戏连启动都不会启动 —— 而且不会报错，只是什么都没发生。
   所以 `mingw32` 和 `mingw64` 是**两个不同的依赖**，不能合并。
4. **找不到就说找不到。** `locate()` 返回 None 时不要猜"大概装了吧"。

依赖由适配器声明（`ProbeReport.needs`），不是全局扫描。否则用户一打开
摆渡就被问"要不要装编译器"，而他只是想翻一个 Minecraft 整合包。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# 用户没指定装在哪儿时用这个。放 D 盘是因为它属于"大件工具"，
# 跟摆渡本体的配置文件不该混在一个目录里。
DEFAULT_TOOLS_DIR = Path("D:/tools")


@dataclass(frozen=True)
class Dependency:
    """一个可以被"缺了"的外部工具。

    字段刻意都跟用户可读信息绑在一起 —— 一个只有 id 的依赖表没法弹窗。
    """

    id: str
    name: str
    # 给用户看的一句话：为什么没有它这个游戏就汉化不了
    why: str
    size_mb: float
    # 可执行文件名。存在这些之一就算装好了。
    binaries: tuple[str, ...]
    target: Path
    # 打包下载地址。zip 用标准库解，7z 需要有本机 7-Zip。
    zip_url: str = ""
    sevenz_url: str = ""
    # 除了 PATH 和 target，还去哪儿找（已经装过的位置）
    search: tuple[Path, ...] = ()

    def locate(self) -> Path | None:
        """找到就算装好了。先看 PATH，再看约定目录，最后看已知安装位置。"""
        for name in self.binaries:
            found = shutil.which(name)
            if found:
                return Path(found)
        roots = [self.target, *self.search]
        for root in roots:
            for sub in ("bin", ""):
                for name in self.binaries:
                    candidate = root / sub / name if sub else root / name
                    if candidate.is_file():
                        return candidate
        return None

    @property
    def installed(self) -> bool:
        return self.locate() is not None

    def prompt(self) -> str:
        """弹窗正文。三段：缺什么、为什么、多大。"""
        return (
            f"这本游戏需要「{self.name}」才能汉化。\n\n"
            f"原因：{self.why}\n\n"
            f"体积约 {self.size_mb:.0f} MB，会装在：\n{self.target}\n\n"
            "要现在下载吗？（不装的话这本游戏翻不了，其它功能不受影响）"
        )


# ---- 依赖表 ----
# 只登记"摆渡自己会用到"的，不做通用软件仓库。
# 加新依赖时问三件事：真的绕不过去吗？能不能用标准库替代？体积会不会吓到人？


def _mingw(arch: str) -> Dependency:
    """MinGW-w64 工具链。32/64 位是两份独立的东西，不是一个的选项。"""
    tag = "i686-posix-dwarf" if arch == "32" else "x86_64-posix-seh"
    triple = "i686-w64-mingw32" if arch == "32" else "x86_64-w64-mingw32"
    other = "64" if arch == "32" else "32"
    base = (
        "https://github.com/brechtsanders/winlibs_mingw/releases/download/"
        "16.2.0posix-23.1.3-14.0.0-ucrt-r3/"
    )
    stem = f"winlibs-{tag}-gcc-16.2.0-mingw-w64ucrt-14.0.0-r3"
    return Dependency(
        id=f"mingw{arch}",
        name=f"MinGW-w64 {arch} 位编译器",
        why=(
            "某些引擎（如 HSP）按 Shift-JIS 解码文本，直接写中文会显示成乱码，"
            "得编一个 DLL 代理改掉它的编码转换 —— 那需要 C 编译器。"
            f"{arch} 位游戏只能用 {arch} 位编译器编，"
            f"换成 {other} 位的那份，代理根本不会被加载，而且不会报错"
        ),
        size_mb=108.0 if arch == "32" else 106.5,
        binaries=(f"{triple}-gcc.exe",),
        target=DEFAULT_TOOLS_DIR / f"mingw{arch}",
        zip_url=base + stem + ".zip",
        sevenz_url=base + stem + ".7z",
    )


REGISTRY: dict[str, Dependency] = {
    "mingw32": _mingw("32"),
    "mingw64": _mingw("64"),
}


@dataclass
class DepStatus:
    """一次依赖体检的结果。"""

    missing: list[Dependency] = field(default_factory=list)
    # 适配器要了但注册表里没有 —— 说明摆渡版本跟适配器对不上，要明说
    unknown: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unknown

    def summary(self) -> str:
        parts = []
        for dep in self.missing:
            parts.append(f"缺 {dep.name}（约 {dep.size_mb:.0f} MB）")
        for name in self.unknown:
            parts.append(f"需要未知依赖 {name}（摆渡版本可能过旧）")
        return "；".join(parts)


def resolve(ids) -> DepStatus:
    """按适配器声明的依赖 id 列表做体检。空列表＝什么都不缺。"""
    status = DepStatus()
    for dep_id in ids or ():
        dep = REGISTRY.get(dep_id)
        if dep is None:
            status.unknown.append(dep_id)
        elif not dep.installed:
            status.missing.append(dep)
    return status


# ---- 安装 ----
# 只做"下载 + 解压到 target"，不写注册表、不改 PATH、不装系统级东西。
# 用户想彻底删掉时，删目录就行。


def find_7z() -> Path | None:
    """本机有没有 7-Zip。只是为了让 108MB 的包比 270MB 的少下一半。"""
    exe = shutil.which("7z")
    if exe:
        return Path(exe)
    for base in (
        Path("C:/Program Files/7-Zip/7z.exe"),
        Path("C:/Program Files (x86)/7-Zip/7z.exe"),
    ):
        if base.is_file():
            return base
    return None


def install(dep: Dependency, progress=None) -> tuple[bool, str]:
    """下载并解压。返回 (是否成功, 说明)。

    `progress(已下载字节, 总字节)` 可选，给 GUI 画进度条用。
    整个过程不碰 target 以外的地方。
    """
    has_7z = find_7z() is not None
    url = dep.sevenz_url if (has_7z and dep.sevenz_url) else dep.zip_url
    if not url:
        return False, f"{dep.name} 没有可用的下载地址"

    dep.target.parent.mkdir(parents=True, exist_ok=True)
    archive = dep.target.parent / f".{dep.id}.download"
    try:
        _download(url, archive, progress)
    except Exception as exc:  # 网络问题不该把 GUI 拖崩
        archive.unlink(missing_ok=True)
        return False, f"下载失败：{exc}"

    try:
        _extract(archive, dep.target)
    except Exception as exc:
        return False, f"解压失败：{exc}（压缩包留在 {archive}）"
    archive.unlink(missing_ok=True)

    if not dep.installed:
        return False, f"装完了但找不到 {dep.binaries[0]}，可能解压层级不对：{dep.target}"
    return True, f"{dep.name} 已装到 {dep.target}"


def _download(url: str, dest: Path, progress=None) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "ferry-localizer"})
    with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)


def _extract(archive: Path, target: Path) -> None:
    """解压到 target。压缩包里通常带一层同名目录，这里把它剥掉。"""
    target.mkdir(parents=True, exist_ok=True)
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            prefix = _common_prefix(names)
            for info in zf.infolist():
                rel = info.filename[len(prefix):] if prefix else info.filename
                if not rel or rel.endswith("/"):
                    continue
                out = target / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, out.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        return

    sevenz = find_7z()
    if sevenz is None:
        raise RuntimeError("需要 7-Zip 才能解开 .7z")
    # -spe 去掉路径里的多余层级，实测没生效，所以解到临时目录再搬
    tmp = target.parent / f".{target.name}.unpack"
    subprocess.run(
        [str(sevenz), "x", str(archive), f"-o{tmp}", "-y", "-bso0", "-bsp0"],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    roots = [p for p in tmp.iterdir() if p.is_dir()]
    src = roots[0] if len(roots) == 1 else tmp
    for item in src.iterdir():
        dest = target / item.name
        if dest.exists():
            shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
        shutil.move(str(item), str(dest))
    shutil.rmtree(tmp, ignore_errors=True)


def _common_prefix(names: list[str]) -> str:
    """所有条目共有的第一层目录（含尾部斜杠）。只有一层时返回它，否则空串。"""
    dirs = {n.split("/")[0] for n in names if n}
    if len(dirs) != 1:
        return ""
    only = next(iter(dirs))
    # 唯一的第一层必须是个目录（所有条目都以它开头且都还有下文）
    if all(n.startswith(only + "/") for n in names if n):
        return only + "/"
    return ""
