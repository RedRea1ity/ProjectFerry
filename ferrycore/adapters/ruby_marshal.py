"""Ruby Marshal 4.8 的最小读写（给 RPG Maker .rvdata / .rvdata2 用）。

不依赖第三方、不启动 Ruby。只覆盖 RM 数据文件实际会出现的类型：
nil / bool / int / float / string（带 encoding ivar）/ symbol /
array / hash / object / object-link / symbol-link / bignum / user-defined。

对象表示：
    {"__class__": "RPG::EventCommand", "code": 401, "indent": 0, "parameters": [...]}
ivar 名在加载时去掉前导 `@`；写出时加回。

字符串一律解码成 Python str（UTF-8）。写出时带 `E: True` encoding ivar，
与 Ruby 1.9+ / RGSS3 一致。
"""

from __future__ import annotations

import struct
from typing import Any


class MarshalError(ValueError):
    pass


# ---- load ----


class _Loader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.i = 0
        self.symbols: list[str] = []
        self.objects: list[Any] = []

    def byte(self) -> int:
        if self.i >= len(self.data):
            raise MarshalError("意外 EOF")
        b = self.data[self.i]
        self.i += 1
        return b

    def raw(self, n: int) -> bytes:
        if self.i + n > len(self.data):
            raise MarshalError("意外 EOF")
        chunk = self.data[self.i : self.i + n]
        self.i += n
        return chunk

    def read_int(self) -> int:
        c = self.byte()
        if c == 0:
            return 0
        if 5 < c < 128:
            return c - 5
        if 128 <= c < 251:
            return c - 251
        if c <= 4:
            n = 0
            for j in range(c):
                n |= self.byte() << (8 * j)
            return n
        # 0xfc..0xff → 读 4..1 字节有符号
        nbytes = 256 - c
        n = -1
        for j in range(nbytes):
            n &= ~(0xFF << (8 * j))
            n |= self.byte() << (8 * j)
        return n

    def read_symbol(self) -> str:
        t = self.byte()
        if t == 0x3B:  # ;
            idx = self.read_int()
            return self.symbols[idx]
        if t != 0x3A:  # :
            raise MarshalError(f"期望 symbol，得到 {hex(t)}")
        n = self.read_int()
        sym = self.raw(n).decode("utf-8", errors="replace")
        self.symbols.append(sym)
        return sym

    def register(self, obj: Any) -> Any:
        self.objects.append(obj)
        return obj

    def read(self) -> Any:
        t = self.byte()
        if t == 0x30:  # 0
            return None
        if t == 0x54:  # T
            return True
        if t == 0x46:  # F
            return False
        if t == 0x69:  # i
            return self.read_int()
        if t in (0x3A, 0x3B):
            self.i -= 1
            return self.read_symbol()
        if t == 0x22:  # "
            return self._read_bytestring()
        if t == 0x49:  # I ivar
            return self._read_ivar()
        if t == 0x5B:  # [
            n = self.read_int()
            arr: list[Any] = []
            self.register(arr)
            for _ in range(n):
                arr.append(self.read())
            return arr
        if t == 0x7B:  # {
            n = self.read_int()
            h: dict[Any, Any] = {}
            self.register(h)
            for _ in range(n):
                k = self.read()
                v = self.read()
                h[k] = v
            return h
        if t == 0x6F:  # o
            return self._read_object()
        if t == 0x40:  # @
            idx = self.read_int()
            return self.objects[idx]
        if t == 0x66:  # f
            n = self.read_int()
            s = self.raw(n).decode("ascii", errors="replace")
            # Ruby 用 "nan"/"inf"/"-inf" 文本
            try:
                return float(s)
            except ValueError:
                return s
        if t == 0x6C:  # l bignum
            sign = self.byte()
            nwords = self.read_int()
            raw = self.raw(nwords * 2)
            val = int.from_bytes(raw, "little")
            return val if sign == 0x2B else -val
        if t == 0x75:  # u user-defined _dump
            cls = self.read_symbol()
            n = self.read_int()
            raw = self.raw(n)
            return self.register({"__class__": cls, "__userdef__": raw})
        if t == 0x55:  # U user marshal
            cls = self.read_symbol()
            payload = self.read()
            return self.register({"__class__": cls, "__marshal_data__": payload})
        if t == 0x65:  # e extended object — 少见，当普通 object 的变体读
            # format: e + object + symbol(module)
            obj = self.read()
            mod = self.read_symbol()
            if isinstance(obj, dict):
                obj.setdefault("__extend__", []).append(mod)
            return obj
        if t == 0x63:  # c class
            n = self.read_int()
            return self.raw(n).decode("utf-8", errors="replace")
        if t == 0x6D:  # m module
            n = self.read_int()
            return self.raw(n).decode("utf-8", errors="replace")
        raise MarshalError(f"未支持的 Marshal 类型 {hex(t)} @ {self.i - 1}")

    def _read_bytestring(self) -> bytes:
        n = self.read_int()
        raw = self.raw(n)
        return self.register(raw)

    def _read_ivar(self) -> Any:
        # 内层对象先注册；字符串的 encoding ivar 在后
        obj = self.read()
        n = self.read_int()
        ivars: dict[str, Any] = {}
        for _ in range(n):
            key = self.read_symbol()
            val = self.read()
            ivars[key] = val
        if isinstance(obj, (bytes, bytearray)):
            # E: true → 尝试 UTF-8 文本；解码失败或 E: false → 保留 bytes。
            # RGSS 的 Scripts 正文是 zlib 二进制，有的档 E 还标成 true ——
            # 绝不能 fallback 成 latin-1 str，写回会毁档。
            raw = bytes(obj)
            if ivars.get("E") is True:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
                if text is not None:
                    for i in range(len(self.objects) - 1, -1, -1):
                        if self.objects[i] is obj or self.objects[i] == obj:
                            self.objects[i] = text
                            break
                    return text
            for i in range(len(self.objects) - 1, -1, -1):
                if self.objects[i] is obj or self.objects[i] == obj:
                    self.objects[i] = raw
                    break
            return raw
        if isinstance(obj, dict) and ivars:
            for k, v in ivars.items():
                obj[k.lstrip("@")] = v
        return obj

    def _read_object(self) -> dict:
        cls = self.read_symbol()
        n = self.read_int()
        obj: dict[str, Any] = {"__class__": cls}
        self.register(obj)
        for _ in range(n):
            key = self.read_symbol()
            val = self.read()
            obj[key.lstrip("@")] = val
        return obj


def load(data: bytes) -> Any:
    if len(data) < 2 or data[0] != 4 or data[1] != 8:
        raise MarshalError(f"不是 Marshal 4.8（头={data[:2]!r}）")
    loader = _Loader(data)
    loader.i = 2
    return loader.read()


def load_path(path) -> Any:
    return load(Path_read(path))


def Path_read(path) -> bytes:
    from pathlib import Path

    return Path(path).read_bytes()


# ---- dump ----


class _Dumper:
    def __init__(self) -> None:
        self.buf = bytearray(b"\x04\x08")
        self.symbols: dict[str, int] = {}
        self.objects: dict[int, int] = {}  # id(obj) -> index

    def emit(self, b: bytes | int) -> None:
        if isinstance(b, int):
            self.buf.append(b & 0xFF)
        else:
            self.buf.extend(b)

    def write_int(self, value: int) -> None:
        # 与 Ruby marshal.c 对称：1..122 → byte+5；-123..-1 → byte-5（无符号）
        if value == 0:
            self.emit(0)
        elif 0 < value < 123:
            self.emit(value + 5)
        elif -124 < value < 0:
            self.emit((value - 5) & 0xFF)
        elif value > 0:
            raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "little")
            if len(raw) > 4:
                raw = value.to_bytes(4, "little")
            self.emit(len(raw))
            self.emit(raw)
        else:
            nbytes = 1
            while True:
                try:
                    raw = value.to_bytes(nbytes, "little", signed=True)
                    break
                except OverflowError:
                    nbytes += 1
                    if nbytes > 8:
                        raise MarshalError(f"整数过大：{value}")
            self.emit((256 - nbytes) & 0xFF)
            self.emit(raw)

    def write_symbol(self, sym: str) -> None:
        if sym in self.symbols:
            self.emit(0x3B)  # ;
            self.write_int(self.symbols[sym])
            return
        self.symbols[sym] = len(self.symbols)
        raw = sym.encode("utf-8")
        self.emit(0x3A)  # :
        self.write_int(len(raw))
        self.emit(raw)

    def write(self, obj: Any) -> None:
        if obj is None:
            self.emit(0x30)
            return
        if obj is True:
            self.emit(0x54)
            return
        if obj is False:
            self.emit(0x46)
            return
        if isinstance(obj, int) and not isinstance(obj, bool):
            # 大整数走 bignum
            if -0x40000000 < obj < 0x40000000:
                self.emit(0x69)
                self.write_int(obj)
            else:
                self.emit(0x6C)
                self.emit(0x2B if obj >= 0 else 0x2D)
                raw = abs(obj).to_bytes((abs(obj).bit_length() + 15) // 16 * 2 or 2, "little")
                if len(raw) % 2:
                    raw += b"\x00"
                self.write_int(len(raw) // 2)
                self.emit(raw)
            return
        if isinstance(obj, float):
            self.emit(0x66)
            s = repr(obj).encode("ascii")
            # Ruby 喜欢特殊形式；普通 float 用 Python repr 通常能 roundtrip 数值
            self.write_int(len(s))
            self.emit(s)
            return
        if isinstance(obj, str):
            self._write_string(obj)
            return
        if isinstance(obj, (bytes, bytearray)):
            oid = id(obj)
            if oid in self.objects:
                self.emit(0x40)
                self.write_int(self.objects[oid])
                return
            # 二进制字符串：I " … E false（Scripts 压缩正文走这里）
            raw = bytes(obj)
            self.emit(0x49)
            self.objects[oid] = len(self.objects)
            self.emit(0x22)
            self.write_int(len(raw))
            self.emit(raw)
            self.write_int(1)
            self.write_symbol("E")
            self.emit(0x46)  # False = 8bit
            return
        if isinstance(obj, list):
            oid = id(obj)
            if oid in self.objects:
                self.emit(0x40)
                self.write_int(self.objects[oid])
                return
            self.objects[oid] = len(self.objects)
            self.emit(0x5B)
            self.write_int(len(obj))
            for item in obj:
                self.write(item)
            return
        if isinstance(obj, dict):
            # userdef / usrmarshal 必须先于普通 object：
            # 它们也带 __class__，走 o: 会把 __userdef__ 当内部键丢掉
            # （RPG::Table 的瓦片数据就会变成 0 字节）。
            if "__userdef__" in obj:
                oid = id(obj)
                if oid in self.objects:
                    self.emit(0x40)
                    self.write_int(self.objects[oid])
                    return
                self.objects[oid] = len(self.objects)
                self.emit(0x75)
                self.write_symbol(obj["__class__"])
                raw = obj["__userdef__"]
                self.write_int(len(raw))
                self.emit(raw)
                return
            if "__marshal_data__" in obj:
                oid = id(obj)
                if oid in self.objects:
                    self.emit(0x40)
                    self.write_int(self.objects[oid])
                    return
                self.objects[oid] = len(self.objects)
                self.emit(0x55)
                self.write_symbol(obj["__class__"])
                self.write(obj["__marshal_data__"])
                return
            if "__class__" in obj:
                self._write_object(obj)
                return
            oid = id(obj)
            if oid in self.objects:
                self.emit(0x40)
                self.write_int(self.objects[oid])
                return
            self.objects[oid] = len(self.objects)
            self.emit(0x7B)
            self.write_int(len(obj))
            for k, v in obj.items():
                self.write(k)
                self.write(v)
            return
        raise MarshalError(f"不能序列化 {type(obj).__name__}")

    def _write_string(self, text: str) -> None:
        oid = id(text)
        if oid in self.objects:
            self.emit(0x40)
            self.write_int(self.objects[oid])
            return
        raw = text.encode("utf-8")
        # I + "bytes" + E:true。对象表只登记一次（内层字符串槽），
        # 与加载端「先登记 bytes 再替换成 str」对齐。
        self.emit(0x49)
        self.objects[oid] = len(self.objects)
        self.emit(0x22)
        self.write_int(len(raw))
        self.emit(raw)
        self.write_int(1)
        self.write_symbol("E")
        self.emit(0x54)

    def _write_object(self, obj: dict) -> None:
        oid = id(obj)
        if oid in self.objects:
            self.emit(0x40)
            self.write_int(self.objects[oid])
            return
        self.objects[oid] = len(self.objects)
        cls = obj["__class__"]
        # 保留插入顺序；跳过内部键
        ivars = [
            (k, v)
            for k, v in obj.items()
            if not k.startswith("__")
        ]
        self.emit(0x6F)
        self.write_symbol(cls)
        self.write_int(len(ivars))
        for k, v in ivars:
            name = k if k.startswith("@") else "@" + k
            self.write_symbol(name)
            self.write(v)


def dump(obj: Any) -> bytes:
    d = _Dumper()
    d.write(obj)
    return bytes(d.buf)
