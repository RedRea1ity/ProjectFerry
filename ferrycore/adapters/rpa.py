"""Ren'Py `.rpa` 归档读写（目前只要抽出 `.rpy`）。

Ren'Py 加载顺序：`game/` 散装文件优先于 archive。所以汉化策略是：

    从 archive.rpa 抽出 *.rpy → 改散装 → 不重打包。

只实现 RPA-3.0（现行主流）和 RPA-2.0；读索引用 pickle + zlib，
与引擎一致。不依赖 unrpa。
"""

from __future__ import annotations

import pickle
import zlib
from dataclasses import dataclass
from pathlib import Path


class RpaError(ValueError):
    pass


@dataclass(frozen=True)
class RpaEntry:
    name: str
    # 解出后的完整字节（已处理 XOR / start prefix）
    # 大资源不要用这个类一次全读；本模块只服务小体积脚本。


@dataclass
class RpaArchive:
    path: Path
    version: int
    key: int
    # name -> list of (offset, length, prefix_bytes)
    index: dict[str, list[tuple[int, int, bytes]]]

    def names(self) -> list[str]:
        return sorted(self.index)

    def scripts(self) -> list[str]:
        return [n for n in self.names() if n.lower().endswith(".rpy")]

    def read(self, name: str) -> bytes:
        chunks = self.index.get(name)
        if chunks is None:
            # 容忍正斜杠/反斜杠
            alt = name.replace("\\", "/")
            chunks = self.index.get(alt)
            if chunks is None:
                for key, val in self.index.items():
                    if key.replace("\\", "/") == alt:
                        chunks = val
                        break
        if chunks is None:
            raise KeyError(name)
        parts: list[bytes] = []
        with self.path.open("rb") as fh:
            for offset, length, prefix in chunks:
                need = length - len(prefix)
                if need < 0:
                    raise RpaError(f"条目长度异常：{name}")
                fh.seek(offset)
                body = fh.read(need)
                if len(body) != need:
                    raise RpaError(f"条目截断：{name}")
                parts.append(prefix + body)
        return b"".join(parts)

    def extract_scripts(self, dest: Path) -> list[Path]:
        """把所有 .rpy 抽到 dest（通常是 game/）。已存在且非空则跳过。"""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for name in self.scripts():
            rel = Path(*name.replace("\\", "/").split("/"))
            target = dest / rel
            if target.is_file() and target.stat().st_size > 0:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.read(name))
            written.append(target)
        return written


def open_archive(path: Path) -> RpaArchive:
    path = Path(path)
    with path.open("rb") as fh:
        header = fh.readline().decode("ascii", errors="replace").strip()
        parts = header.split()
        if not parts or not parts[0].startswith("RPA-"):
            raise RpaError(f"不是 RPA：{path.name}（头={header[:40]!r}）")
        ver_s = parts[0].split("-", 1)[1]
        try:
            # "3.0" -> 3
            version = int(ver_s.split(".", 1)[0])
        except ValueError as exc:
            raise RpaError(f"未知 RPA 版本：{parts[0]}") from exc
        if version == 3:
            if len(parts) < 3:
                raise RpaError("RPA-3.0 头字段不足")
            index_off = int(parts[1], 16)
            key = int(parts[2], 16)
        elif version == 2:
            if len(parts) < 2:
                raise RpaError("RPA-2.0 头字段不足")
            index_off = int(parts[1], 16)
            key = 0
        else:
            raise RpaError(f"不支持的 RPA 版本：{parts[0]}")

        fh.seek(index_off)
        try:
            raw_index = pickle.loads(zlib.decompress(fh.read()))
        except Exception as exc:  # noqa: BLE001 — pickle/zlib 错误种类多
            raise RpaError(f"索引损坏：{path.name}（{exc}）") from exc

    if not isinstance(raw_index, dict):
        raise RpaError("索引不是 dict")

    index: dict[str, list[tuple[int, int, bytes]]] = {}
    for name, metas in raw_index.items():
        if not isinstance(name, str) or not isinstance(metas, (list, tuple)):
            continue
        chunks: list[tuple[int, int, bytes]] = []
        for item in metas:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            offset = int(item[0]) ^ key
            length = int(item[1]) ^ key
            prefix = b""
            if len(item) >= 3 and item[2] is not None:
                start = item[2]
                if isinstance(start, bytes):
                    prefix = start
                elif isinstance(start, str):
                    prefix = start.encode("latin-1", errors="replace")
            chunks.append((offset, length, prefix))
        if chunks:
            index[name.replace("\\", "/")] = chunks

    return RpaArchive(path=path, version=version, key=key, index=index)


def find_archives(game_dir: Path) -> list[Path]:
    game_dir = Path(game_dir)
    if not game_dir.is_dir():
        return []
    out = sorted(game_dir.glob("*.rpa")) + sorted(game_dir.glob("**/*.rpa"))
    # 去重、只留文件
    seen: set[Path] = set()
    uniq: list[Path] = []
    for path in out:
        if not path.is_file():
            continue
        key = path.resolve()
        if key in seen:
            continue
        # 跳过深层 cache
        if "cache" in path.parts or "saves" in path.parts:
            continue
        seen.add(key)
        uniq.append(path)
    return uniq
