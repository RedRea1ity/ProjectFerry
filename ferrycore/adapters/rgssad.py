"""RGSSAD 档案（RPG Maker XP / VX / VX Ace）。

只实现摆渡需要的部分：列目录、按路径取字节、整包解压到磁盘。
不重打包 —— Ace 的可写路径是「解压后挪走 .rgss3a，让引擎读散装文件」。

- 头 8 字节：`RGSSAD\\0` + 版本（1 = .rgssad/.rgss2a，3 = .rgss3a）
- v3：元数据固定 key（seed*9+3）；文件内容各自 file_key，每 4 字节 key=key*7+3
- v1：全程一把 key，从 0xDEADCAFE 起，每处理一字节（或一个 u32）就 key=key*7+3
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


class RgssadError(ValueError):
    pass


@dataclass(frozen=True)
class RgssadEntry:
    name: str  # 分隔符已归一成 /
    offset: int
    size: int
    key: int


def _decrypt_v3(data: bytes, key: int) -> bytes:
    out = bytearray(len(data))
    k = key & 0xFFFFFFFF
    kb = k.to_bytes(4, "little")
    j = 0
    for i in range(len(data)):
        if j == 4:
            j = 0
            k = (k * 7 + 3) & 0xFFFFFFFF
            kb = k.to_bytes(4, "little")
        out[i] = data[i] ^ kb[j]
        j += 1
    return bytes(out)


def _decrypt_v1(data: bytes, key: int) -> tuple[bytes, int]:
    out = bytearray(len(data))
    k = key & 0xFFFFFFFF
    for i in range(len(data)):
        out[i] = data[i] ^ (k & 0xFF)
        k = (k * 7 + 3) & 0xFFFFFFFF
    return bytes(out), k


def _norm_name(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").replace("\\", "/")


class RgssadArchive:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data = self.path.read_bytes()
        if len(self.data) < 8 or self.data[:6] != b"RGSSAD":
            raise RgssadError(f"不是 RGSSAD：{self.path.name}")
        self.version = self.data[7]
        if self.version == 3:
            self.entries = self._read_v3()
        elif self.version == 1:
            self.entries = self._read_v1()
        else:
            raise RgssadError(f"不支持的 RGSSAD 版本 {self.version}：{self.path.name}")
        self._by_name = {e.name.lower(): e for e in self.entries}

    def _read_v3(self) -> list[RgssadEntry]:
        key = (struct.unpack_from("<I", self.data, 8)[0] * 9 + 3) & 0xFFFFFFFF
        pos = 12
        out: list[RgssadEntry] = []
        while True:
            if pos + 16 > len(self.data):
                break
            offset = struct.unpack_from("<I", self.data, pos)[0] ^ key
            pos += 4
            if offset == 0:
                break
            size = struct.unpack_from("<I", self.data, pos)[0] ^ key
            pos += 4
            file_key = struct.unpack_from("<I", self.data, pos)[0] ^ key
            pos += 4
            name_len = struct.unpack_from("<I", self.data, pos)[0] ^ key
            pos += 4
            if name_len < 0 or pos + name_len > len(self.data):
                raise RgssadError("v3 文件名长度异常")
            name_bytes = bytearray(self.data[pos : pos + name_len])
            pos += name_len
            kb = key.to_bytes(4, "little")
            for i in range(len(name_bytes)):
                name_bytes[i] ^= kb[i % 4]
            out.append(
                RgssadEntry(
                    name=_norm_name(bytes(name_bytes)),
                    offset=offset,
                    size=size,
                    key=file_key,
                )
            )
        return out

    def _read_v1(self) -> list[RgssadEntry]:
        key = 0xDEADCAFE
        pos = 8
        out: list[RgssadEntry] = []
        while pos + 4 <= len(self.data):
            name_len = struct.unpack_from("<I", self.data, pos)[0] ^ key
            pos += 4
            key = (key * 7 + 3) & 0xFFFFFFFF
            if name_len <= 0 or pos + name_len + 4 > len(self.data):
                break
            name_bytes = bytearray(self.data[pos : pos + name_len])
            pos += name_len
            for i in range(name_len):
                name_bytes[i] ^= key & 0xFF
                key = (key * 7 + 3) & 0xFFFFFFFF
            size = struct.unpack_from("<I", self.data, pos)[0] ^ key
            pos += 4
            key = (key * 7 + 3) & 0xFFFFFFFF
            if size < 0 or pos + size > len(self.data):
                break
            out.append(
                RgssadEntry(
                    name=_norm_name(bytes(name_bytes)),
                    offset=pos,
                    size=size,
                    key=key,
                )
            )
            # 内容解密会推进 key；下一条元数据要用推进后的 key
            _, key = _decrypt_v1(self.data[pos : pos + size], key)
            pos += size
        return out

    def read(self, name: str) -> bytes:
        entry = self._by_name.get(name.replace("\\", "/").lower())
        if entry is None:
            raise KeyError(name)
        chunk = self.data[entry.offset : entry.offset + entry.size]
        if len(chunk) != entry.size:
            raise RgssadError(f"截断：{name}")
        if self.version == 3:
            return _decrypt_v3(chunk, entry.key)
        blob, _ = _decrypt_v1(chunk, entry.key)
        return blob

    def extract_all(self, dest: Path) -> int:
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        count = 0
        for entry in self.entries:
            target = dest.joinpath(*entry.name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.read(entry.name))
            count += 1
        return count

    def data_files(self, suffix: str | None = None) -> list[RgssadEntry]:
        out = [e for e in self.entries if e.name.lower().startswith("data/")]
        if suffix:
            suf = suffix.lower()
            out = [e for e in out if e.name.lower().endswith(suf)]
        return out


def find_archive(root: Path) -> Path | None:
    root = Path(root)
    for name in (
        "Game.rgss3a",
        "Game.rgss2a",
        "Game.rgssad",
        "game.rgss3a",
        "game.rgss2a",
        "game.rgssad",
    ):
        cand = root / name
        if cand.is_file():
            return cand
    for pat in ("*.rgss3a", "*.rgss2a", "*.rgssad"):
        hits = sorted(root.glob(pat))
        if hits:
            return hits[0]
    return None
