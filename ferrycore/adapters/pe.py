"""PE 导入表的最小解析器 —— 纯标准库，只为回答两个问题。

    exe 是 32 位还是 64 位？        代理 DLL 必须同位数，否则挂不上
    它静态导入了哪些 DLL、哪些函数？  代理只能替身「被静态导入」的 DLL

第二个问题是被踩出来的：上一轮用 `winmm.dll` 做代理，游戏立刻弹
`#Error 38 Internal Error`。原因是 HSP 运行时还会 `GetProcAddress("PlaySound")`，
而代理没这个导出就返回 NULL。所以判断一个 DLL 能不能当替身，光看静态导入不够，
还要看 exe 里有没有「动态取它家函数」的痕迹。

**这里只读不写。** 任何需要改 PE 的活（patch 分支）都不在范围内 ——
上一轮已经证明那条路收益极低（改完还要跟着版本升级重来）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_FILE_MACHINE_I386 = 0x014C
IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_FILE_MACHINE_ARM64 = 0xAA64

_MACHINE_NAMES = {
    IMAGE_FILE_MACHINE_I386: "x86",
    IMAGE_FILE_MACHINE_AMD64: "x64",
    IMAGE_FILE_MACHINE_ARM64: "arm64",
}

# 这些 DLL 从不被游戏「动态取函数」，而且导入的函数极少，是最干净的替身空位。
# 判断顺序就是这里的顺序：越前面越安全。
PROXY_CANDIDATES = (
    "msimg32.dll",  # 只有 3 个导出，图形混合。八星帝实测可用
    "version.dll",  # 版本查询，通常只导入 1-2 个
    "dwmapi.dll",  # 桌面合成，老游戏一般不碰
    "dbghelp.dll",
    "wtsapi32.dll",
)


@dataclass
class ImportTable:
    machine: int = 0
    pe32_plus: bool = False
    # DLL 名（小写）-> 静态导入的函数名列表
    imports: dict[str, list[str]] = field(default_factory=dict)
    # 解析过程中的问题。空列表 = 全部读通。
    problems: list[str] = field(default_factory=list)

    @property
    def bits(self) -> int:
        return 64 if self.pe32_plus else 32

    @property
    def arch(self) -> str:
        return _MACHINE_NAMES.get(self.machine, f"0x{self.machine:04x}")

    def summary(self) -> str:
        return f"{self.arch}，导入 {len(self.imports)} 个 DLL"


def read_imports(path: Path) -> ImportTable | None:
    """读 PE 导入表。不是 PE、或结构异常时返回 None（不抛异常）。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    table = ImportTable()
    try:
        _parse(data, table)
    except (struct.error, IndexError, ValueError) as exc:
        table.problems.append(f"PE 结构读到底了：{exc}")
        if not table.imports:
            return None
    return table


def exe_of(root: Path) -> Path | None:
    """游戏目录里最大的那个 exe。HSP 游戏常带一堆小工具 exe，主程序总是最大的。"""
    root = Path(root)
    best = None
    best_size = 0
    try:
        candidates = list(root.glob("*.exe"))
    except OSError:
        return None
    for path in candidates:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > best_size:
            best, best_size = path, size
    # 小于 100 KB 的基本是启动器/卸载器，不是主程序
    return best if best_size > 100_000 else None


def proxy_candidates(table: ImportTable) -> list[str]:
    """能拿来当代理的 DLL，按推荐度排。

    条件：被静态导入 + 导入的函数不超过 4 个。
    函数多了意味着代理要转发一堆签名各异的导出，很容易漏。
    """
    out = []
    for name in PROXY_CANDIDATES:
        funcs = table.imports.get(name)
        if funcs and len(funcs) <= 4:
            out.append(name)
    # 白名单没命中，就退而求其次：任何导入函数 ≤2 个的 DLL
    if not out:
        for name, funcs in sorted(table.imports.items()):
            if 0 < len(funcs) <= 2 and not _is_system_critical(name):
                out.append(name)
    return out


def _is_system_critical(name: str) -> bool:
    """这些 DLL 挂了游戏连窗口都出不来，不做替身。"""
    return name in {
        "kernel32.dll", "user32.dll", "gdi32.dll", "advapi32.dll",
        "ntdll.dll", "shell32.dll", "ole32.dll", "oleaut32.dll",
        "comctl32.dll", "comdlg32.dll", "winmm.dll", "ws2_32.dll",
    }


def read_exports(dll_path: Path) -> list[str]:
    """读 DLL 的导出函数名。读不到返回空列表（不抛异常）。

    只取名字，不关心地址 —— 判断「这个 DLL 能不能当替身」只需要知道
    它有哪些导出，好跟 exe 里出现的字符串做差集。
    """
    try:
        data = Path(dll_path).read_bytes()
    except OSError:
        return []
    try:
        return _parse_export_names(data)
    except (struct.error, IndexError, ValueError):
        return []


def system_dll(name: str) -> Path | None:
    """找系统里的 DLL。

    游戏是 32 位就去 SysWOW64，64 位去 System32 —— 重定向会让 System32 下的
    同名文件对 32 位程序不可见，反过来的坑也真实存在。导出名两份一致，
    但取对了才不会读到一个 WOW64 桩。
    """
    import os
    import platform

    windir = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    for sub in ("SysWOW64", "System32"):
        candidate = Path(windir) / sub / name
        if candidate.is_file():
            return candidate
    return None


def proxy_verdict(exe_path: Path, dll_name: str, table: ImportTable) -> tuple[bool, str]:
    """这个 DLL 能不能安全当代理？返回 (能不能, 理由)。

    **判据来自一次真实的踩坑**：用 winmm 做代理时游戏立刻弹 `#Error 38`。
    原因是 HSP 运行时还会 `GetProcAddress("PlaySound")` —— 静态导入里看不见它，
    但代理没这个导出就返回 NULL，HSP 当场报内部错误。

    可执行的判法是：拿 DLL 的**全部导出**去 exe 里搜，凡是被搜到、却又不在
    静态导入列表里的函数名，就是被动态取用的证据。这样既不用反汇编，
    也不需要猜 HSP 的代码逻辑。
    """
    static = set(table.imports.get(dll_name, []))
    if not static:
        # 没被静态导入 = 加载器根本不会去找同名文件，替身放进去也没人加载。
        # 这一条比「安全」重要得多，必须先判。
        return False, f"exe 没有静态导入 {dll_name}，代理放进去也不会被加载"

    dll_path = system_dll(dll_name)
    exports = read_exports(dll_path) if dll_path else []
    if not exports:
        return True, f"读不到 {dll_name} 的导出表，只确认了静态导入 {len(static)} 个函数"

    try:
        blob = Path(exe_path).read_bytes()
    except OSError:
        return False, "exe 读不了"

    seen = {name for name in exports if len(name) > 3 and name.encode("ascii", "replace") in blob}
    extra = seen - static
    if extra:
        return False, (
            f"{dll_name} 有 {len(extra)} 个导出函数出现在 exe 里但不在静态导入表"
            f"（{', '.join(sorted(extra)[:4])}）—— 运行时会被动态取用，代理漏了就会报错"
        )
    return True, f"{dll_name} 只有静态导入的 {len(static)} 个函数被用到，可以安全替身"


def _parse_export_names(data: bytes) -> list[str]:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("不是 PE 文件")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise ValueError("PE 签名不对")
    coff = pe_offset + 4
    section_count = struct.unpack_from("<H", data, coff + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff + 16)[0]
    optional = coff + 20
    magic = struct.unpack_from("<H", data, optional)[0]
    pe32_plus = magic == 0x20B
    dirs_at = optional + (112 if pe32_plus else 96)
    export_rva = struct.unpack_from("<I", data, dirs_at)[0]
    if not export_rva:
        return []

    sections_at = optional + optional_size
    sections = []
    for index in range(section_count):
        base = sections_at + index * 40
        virtual_size, virtual_addr, raw_size, raw_ptr = struct.unpack_from("<IIII", data, base + 8)
        sections.append((virtual_addr, max(virtual_size, raw_size), raw_ptr, raw_size))

    def to_offset(rva: int) -> int:
        for virtual_addr, span, raw_ptr, raw_size in sections:
            if virtual_addr <= rva < virtual_addr + span:
                return raw_ptr + (rva - virtual_addr)
        raise ValueError(f"RVA {rva:#x} 不在任何 section")

    exports_at = to_offset(export_rva)
    name_count = struct.unpack_from("<I", data, exports_at + 24)[0]
    names_rva = struct.unpack_from("<I", data, exports_at + 32)[0]
    if not names_rva or name_count > 65535:
        return []
    table_at = to_offset(names_rva)
    out = []
    for index in range(name_count):
        name_rva = struct.unpack_from("<I", data, table_at + index * 4)[0]
        try:
            offset = to_offset(name_rva)
        except ValueError:
            continue
        end = data.find(b"\0", offset)
        if end < 0:
            continue
        text = data[offset:end].decode("ascii", "replace")
        if text:
            out.append(text)
    return out


# ---- 内部解析 ----


def _parse(data: bytes, table: ImportTable) -> None:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("不是 PE 文件")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise ValueError("PE 签名不对")
    coff = pe_offset + 4
    table.machine = struct.unpack_from("<H", data, coff)[0]
    section_count = struct.unpack_from("<H", data, coff + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff + 16)[0]
    optional = coff + 20
    magic = struct.unpack_from("<H", data, optional)[0]
    table.pe32_plus = magic == 0x20B
    if magic not in (0x10B, 0x20B):
        raise ValueError(f"未知的 optional header magic 0x{magic:04x}")

    # 数据目录：PE32 从 optional+96 开始，PE32+ 从 optional+112
    dirs_at = optional + (112 if table.pe32_plus else 96)
    import_rva = struct.unpack_from("<I", data, dirs_at + 8)[0]
    import_size = struct.unpack_from("<I", data, dirs_at + 12)[0]
    if not import_rva or not import_size:
        table.problems.append("没有导入表（静态链接？）")
        return

    sections_at = optional + optional_size
    sections = []
    for index in range(section_count):
        base = sections_at + index * 40
        virtual_size, virtual_addr, raw_size, raw_ptr = struct.unpack_from("<IIII", data, base + 8)
        sections.append((virtual_addr, max(virtual_size, raw_size), raw_ptr, raw_size))

    def to_offset(rva: int) -> int:
        for virtual_addr, span, raw_ptr, raw_size in sections:
            if virtual_addr <= rva < virtual_addr + span:
                return raw_ptr + (rva - virtual_addr)
        raise ValueError(f"RVA {rva:#x} 不属于任何 section")

    def read_cstring(rva: int) -> str:
        offset = to_offset(rva)
        end = data.find(b"\0", offset)
        if end < 0:
            raise ValueError("字符串没结束")
        return data[offset:end].decode("ascii", "replace")

    offset = to_offset(import_rva)
    thunk_size = 8 if table.pe32_plus else 4
    for _ in range(512):  # 防御：正常不超过几十个
        desc = data[offset:offset + 20]
        if len(desc) < 20 or desc == b"\0" * 20:
            break
        original_thunk, _stamp, _forward, name_rva, first_thunk = struct.unpack("<IIIII", desc)
        if not name_rva:
            break
        dll = read_cstring(name_rva).lower()
        funcs: list[str] = []
        thunk_rva = original_thunk or first_thunk
        if thunk_rva:
            walk = to_offset(thunk_rva)
            for _ in range(4096):
                entry = data[walk:walk + thunk_size]
                if len(entry) < thunk_size:
                    break
                value = struct.unpack("<Q" if table.pe32_plus else "<I", entry)[0]
                if value == 0:
                    break
                # 最高位是序号导入标志，那种没有名字
                if not value & (1 << (thunk_size * 8 - 1)):
                    try:
                        # 名字在 IMAGE_IMPORT_BY_NAME 里，前 2 字节是 hint
                        funcs.append(read_cstring(value + 2))
                    except ValueError:
                        pass
                walk += thunk_size
        table.imports[dll] = funcs
        offset += 20
