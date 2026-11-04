"""容器层：游戏数据「装在哪儿」，跟「文本长什么样」是两个正交的问题。

卡住汉化的几乎从来不是引擎，而是容器。

    zip 追加在 exe 尾部（nw.js / Electron 打包的老游戏）→ 必须重建 zip 拼回尾
    纯目录散装（大量解包过的游戏）→ 直接改文件
    xp3 / arc / pck / wolf（Kirikiri / BGI / Siglus / WOLF）→ 需要打包器

最后一种本模块不管（得有专门的打包器），前两种在这里。

所以「N 个引擎」不该长成 N 个适配器：容器维度 × 文本格式维度，组合复用。
Tyrano 今天用的是「zip-in-exe」，以后 Ren'Py 用「散装 + .rpy」，
两边的容器代码可以直接借。

**容器只负责把字节拿出来放回去，不懂任何文本格式。**
"""

from __future__ import annotations

import io
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

_EOCD = b"PK\x05\x06"
_EOCD_SIZE = 22
_MAX_COMMENT = 65535

# 摆渡自己产出的副本后缀。**必须排除**：否则第二次运行会把副本当成原版，
# 读到的是已经翻过的文本，然后给「副本的副本」再翻一遍。
SELF_OUTPUT_SUFFIX = ".cn.exe"


@dataclass
class ZipSpan:
    """exe 尾部那段 zip 的位置。"""

    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start


def locate_appended_zip(path: Path) -> ZipSpan | None:
    """找出追加在 exe 尾部的那段 zip，返回它在文件里的 [start, end)。

    为什么不直接 `zipfile.ZipFile(path)`：它**能**读这种前置垃圾的档案（会自己
    算偏移），但读得出不等于知道该往哪儿写回去。重建之后必须知道「前缀多长」，
    才能把新 zip 精确拼回同一个位置。

    坑：一个 exe 里可能有**多个** zip（本仓库实测过 Tyrano 官方示例包和真游戏
    各一个）。判据是「EOCD 之后只剩可选的 archive comment，正好贴着文件尾」，
    满足的那个才是真的档案，其余是打包时误带进去的。
    """
    size = path.stat().st_size
    if size < _EOCD_SIZE:
        return None
    span = min(size, _EOCD_SIZE + _MAX_COMMENT)
    with path.open("rb") as handle:
        handle.seek(size - span)
        tail = handle.read(span)

    cursor = len(tail)
    while cursor > 0:
        index = tail.rfind(_EOCD, 0, cursor)
        if index < 0:
            return None
        try:
            _, _, _, _, _, cd_size, cd_offset, comment_len = struct.unpack_from("<IHHHHIIH", tail, index)
        except struct.error:
            return None
        eocd_pos = size - span + index
        if eocd_pos + _EOCD_SIZE + comment_len == size:
            start = eocd_pos - cd_size - cd_offset
            if 0 <= start < eocd_pos:
                return ZipSpan(start, size)
        cursor = index
    return None


def _rebuild_zip(archive_bytes: bytes, replacements: dict[str, bytes]) -> bytes:
    """按原顺序、原压缩方式重写整个档案，只替换给定条目。"""
    source = zipfile.ZipFile(io.BytesIO(archive_bytes))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        for info in source.infolist():
            data = replacements.get(info.filename)
            if data is None:
                if info.flag_bits & 0x1:
                    raise ValueError(f"条目 {info.filename} 加密了，读不出来，不能安全重建")
                data = source.read(info.filename)
            fresh = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            fresh.compress_type = info.compress_type
            fresh.external_attr = info.external_attr
            fresh.create_system = info.create_system
            target.writestr(fresh, data)
    return buffer.getvalue()


@dataclass
class Document:
    """一份文本文件的内容 + 它原来的编码外衣。

    BOM 和换行风格必须原样保留：剧本文件被改成不带 BOM 的 UTF-8，
    有的引擎会按系统代码页去读，于是全篇变乱码 —— 这类事故看起来像
    「翻译把游戏搞坏了」，其实是回写时丢了三个字节。
    """

    text: str
    bom: bool = False
    newline: str = "\n"


def unique_output(path: Path) -> Path:
    """副本名已被占用时，依次试 `xxx.cn2.exe` / `xxx.cn3.exe`。

    绝不覆盖：上一个副本可能是人工校对过的，静默覆盖等于删了别人的活。
    也不弹窗问 —— 批量试游戏时每换一次都问一遍很烦，改个名继续更顺。
    """
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for index in range(2, 100):
        candidate = path.with_name(f"{stem}{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"副本名被占用太多次：{path}")


class Container:
    """容器统一接口：拿文本出来、放文本回去、产出一个副本。"""

    kind = "unknown"

    def __init__(self, root: Path, source: Path) -> None:
        self.root = root
        self.source = source
        self._pending: dict[str, bytes] = {}
        self.notes: list[str] = []

    def list_files(self, suffixes: tuple[str, ...]) -> list[str]:
        raise NotImplementedError

    def read_bytes(self, name: str) -> bytes:
        raise NotImplementedError

    def read_text(self, name: str) -> str:
        return self.read_document(name).text

    def read_document(self, name: str) -> Document:
        raw = self.read_bytes(name)
        bom = raw.startswith(b"\xef\xbb\xbf")
        newline = "\r\n" if b"\r\n" in raw else "\n"
        return Document(text=_decode(raw), bom=bom, newline=newline)

    def write_text(self, name: str, text: str, bom: bool = True) -> None:
        self._pending[name] = text.encode("utf-8-sig" if bom else "utf-8")

    def write_document(self, name: str, text: str, doc: Document) -> None:
        """按原文件的外衣写回去：原来有 BOM 就带 BOM，原来是 CRLF 就还是 CRLF。"""
        if doc.newline != "\n":
            text = text.replace("\r\n", "\n").replace("\n", doc.newline)
        self.write_text(name, text, bom=doc.bom)

    def changed(self) -> dict[str, bytes]:
        return dict(self._pending)

    def commit(self, out: Path) -> Path:
        raise NotImplementedError


class FolderGame(Container):
    """散装目录：文本就是普通文件，改完拷一份目录出来当副本。"""

    kind = "folder"

    def list_files(self, suffixes: tuple[str, ...]) -> list[str]:
        found: list[str] = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path.suffix.lower() in suffixes:
                found.append(path.relative_to(self.root).as_posix())
        return found

    def read_bytes(self, name: str) -> bytes:
        return (self.root / name).read_bytes()

    def commit(self, out: Path) -> Path:
        import shutil

        if out.exists():
            # 只覆盖副本，绝不动原目录
            raise FileExistsError(f"副本目录已存在：{out}")
        shutil.copytree(self.root, out)
        for name, data in self._pending.items():
            target = out / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return out


class ZipInExe(Container):
    """游戏数据是追加在 exe 尾部的一整段 zip。

    实测教训（Tyrano/nw.js, Chromium 41）：把整包解到 exe 旁边**没有用**，
    引擎照样读内嵌的那份。唯一可行的是解 zip → 改 → 重建 → 拼回尾。
    """

    kind = "zip-in-exe"

    def __init__(self, root: Path, source: Path, span: ZipSpan) -> None:
        super().__init__(root, source)
        self.span = span
        with source.open("rb") as handle:
            handle.seek(span.start)
            self._archive = handle.read(span.size)

    def list_files(self, suffixes: tuple[str, ...]) -> list[str]:
        with zipfile.ZipFile(io.BytesIO(self._archive)) as archive:
            return [n for n in archive.namelist() if n.lower().endswith(suffixes) and not n.endswith("/")]

    def read_bytes(self, name: str) -> bytes:
        if name in self._pending:
            return self._pending[name]
        with zipfile.ZipFile(io.BytesIO(self._archive)) as archive:
            return archive.read(name)

    def commit(self, out: Path) -> Path:
        rebuilt = _rebuild_zip(self._archive, self._pending)
        if out.exists():
            raise FileExistsError(f"副本已存在：{out}")
        with self.source.open("rb") as handle, out.open("wb") as target:
            handle.seek(0)
            remaining = self.span.start
            while remaining > 0:
                chunk = handle.read(min(1 << 20, remaining))
                if not chunk:
                    raise IOError("读原 exe 前缀时提前结束")
                target.write(chunk)
                remaining -= len(chunk)
            target.write(rebuilt)
        return out


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def open_container(root: Path) -> Container | None:
    """自动判断这个游戏的数据装在哪儿。

    **exe 优先于目录**，这条是有实测依据的：nw.js/Chromium 41 的游戏旁边就算
    躺着一份解包好的 `package.nw/`，引擎照样读内嵌 zip。反过来若先认目录，
    会改一份游戏根本不读的文件，然后报告「已汉化」——玩家打开还是日文。
    这种「改了但没生效」比报错难查得多。
    """
    if not root.is_dir():
        return None
    for candidate in sorted(root.glob("*.exe")):
        if candidate.name.lower().endswith(SELF_OUTPUT_SUFFIX):
            continue
        span = locate_appended_zip(candidate)
        if span is None:
            continue
        container = ZipInExe(root, candidate, span)
        if container.list_files((".ks",)):
            return container
    if any(root.rglob("*.ks")):
        return FolderGame(root, root)
    return None
