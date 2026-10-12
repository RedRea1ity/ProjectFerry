"""密钥本地保护：落盘加密 + 日志脱敏。

为什么需要它
------------
`ferry_config.json` 里存着模型的 API Key。之前是**明文**：只要这个文件被别人
拿到（网盘同步、QQ 传文件、误传到群里、备份泄漏……），密钥就等于送出去了。
配置读写都在本机完成，所以保护也做在本地 —— **不动云端、不引依赖**。

怎么做的
--------
Windows 自带 **DPAPI**（`CryptProtectData` / `CryptUnprotectData`）：用当前
**用户账户的凭据**加密。密文换一台机器、换一个 Windows 账户都解不开 —— 这正好
对应「文件泄漏了也没用」这个真实威胁模型。

- 只加密「值」，不改配置结构；密钥字段照旧存在，只是值变成 `dpapi:<base64>`。
- 前缀可随时识别：没前缀 = 明文（老配置 / 非 Windows），有前缀 = 密文。
- **只用标准库**（ctypes 直接调 Crypt32），不引入任何第三方依赖 —— 项目承诺
  「零配置可跑」，不能为了加密去装 keyring / cryptography。
- 非 Windows 平台没有 DPAPI：退化为不加密（明文），并在日志里说清楚。摆渡是
  Windows 桌面工具（tkinter + PyInstaller exe），这类平台不是目标环境。

注意：DPAPI 保护的是「落到磁盘上的那份」。进程内存里的密钥仍是明文（这是必然，
否则没法拿去调 API）。要防的是**文件泄漏**，不是防同机内存抓取。
"""
from __future__ import annotations

import base64
import ctypes
import logging
from typing import Any

from .base import LOG

# 密文前缀：见到它就说明这个值已经被 DPAPI 保护过，不要重复加密。
ENC_PREFIX = "dpapi:"

# CRYPTPROTECT_UI_FORBIDDEN：不要弹任何 UI（后台线程里调用安全）。
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


def is_secret_field(name: str) -> bool:
    """判断某个配置键是不是「密钥字段」。

    覆盖 `openai_api_key` 这类带前缀的，以及模型池 / 自定义接口里裸的 `api_key`。
    `mymemory_email` 这类邮箱不算密钥（它本来就要求填，且不是凭证）。
    """
    lowered = (name or "").lower()
    return lowered == "api_key" or lowered.endswith("_api_key")


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def has_plaintext_secret(node: Any, key: str = "") -> bool:
    """配置树里还有没有任何**明文**密钥字段。

    用于「老配置自愈迁移」：升级上来的 `ferry_config.json` 里密钥是明文，
    只要还能扫出一个明文密钥，就该把它加密写回。
    """
    if isinstance(node, dict):
        return any(has_plaintext_secret(value, name) for name, value in node.items())
    if isinstance(node, list):
        return any(has_plaintext_secret(item, key) for item in node)
    return bool(
        isinstance(node, str) and node and is_secret_field(key) and not is_encrypted(node)
    )


# --------------------------------------------------------------------------
# DPAPI（ctypes 薄封装）
# --------------------------------------------------------------------------


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]


def _windows_crypt32():
    """拿 crypt32 句柄；非 Windows / 拿不到就返回 None。"""
    if not hasattr(ctypes, "windll"):
        return None
    try:
        return ctypes.windll.crypt32  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None


def available() -> bool:
    """当前环境能不能用 DPAPI 加密。"""
    return _windows_crypt32() is not None


def _call_dpapi(protect: bool, data: bytes) -> bytes:
    """调 CryptProtectData / CryptUnprotectData。失败抛 OSError。

    注意：这几个 API 必须显式声明 argtypes —— 默认按 int 传参会在 64 位下把
    指针截断（`OverflowError: int too long to convert`），这是很典型的 ctypes 坑。
    """
    crypt32 = _windows_crypt32()
    if crypt32 is None:
        raise OSError("DPAPI 不可用（非 Windows 环境）")
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    # 入参缓冲要自己保活，否则可能被 GC 掉导致读到野指针。
    in_bytes = ctypes.create_string_buffer(bytes(data), len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(in_bytes, ctypes.c_void_p))
    blob_out = _DataBlob()

    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    func.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_DataBlob),
    ]
    func.restype = ctypes.c_int

    ok = func(
        ctypes.byref(blob_in),
        None,               # szDataDescr / ppszDataDescr
        None,               # pOptionalEntropy：不用额外熵，换账户已足够隔离
        None,               # pvReserved
        None,               # pPromptStruct
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        if blob_out.pbData:
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
            kernel32.LocalFree(blob_out.pbData)


def protect(value: str) -> str:
    """加密一个密钥值。已经是密文 / 空值 / 环境不支持时原样返回。"""
    if not value or is_encrypted(value):
        return value
    if not available():
        return value
    try:
        raw = _call_dpapi(True, value.encode("utf-8"))
    except OSError:
        LOG.warning("密钥加密失败，将以明文保存（本机 DPAPI 不可用或调用出错）")
        return value
    return ENC_PREFIX + base64.b64encode(raw).decode("ascii")


def unprotect(value: str) -> str:
    """解密一个密钥值。明文 / 空值原样返回；解密失败返回空串（让用户重填）。"""
    if not isinstance(value, str) or not is_encrypted(value):
        return value
    try:
        raw = base64.b64decode(value[len(ENC_PREFIX):])
        return _call_dpapi(False, raw).decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        # 换了机器 / 换了账户 / 文件损坏都会走到这里。静默清空比抛异常好：
        # 用户重新填一次即可，不至于整个程序起不来。
        LOG.warning("密钥解密失败（可能换过机器或账户），已清空，请重新填写")
        return ""


# --------------------------------------------------------------------------
# 整个配置树的加解密
# --------------------------------------------------------------------------


def protect_config(config: dict[str, Any]) -> dict[str, Any]:
    """返回一份**新的**配置：所有密钥字段加密。不修改入参。"""
    return _walk(config, protect)


def unprotect_config(config: dict[str, Any]) -> dict[str, Any]:
    """返回一份**新的**配置：所有密钥字段解密。不修改入参。"""
    return _walk(config, unprotect)


def _walk(node: Any, transform, key: str = "") -> Any:
    if isinstance(node, dict):
        return {k: _walk(v, transform, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(item, transform, key) for item in node]
    if is_secret_field(key) and isinstance(node, str):
        return transform(node)
    return node
