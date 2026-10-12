"""最底层的 HTTP 小工具：GET / POST JSON、统一的错误文案。

零业务依赖，谁都能 import。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ferrycore.base import redact_secrets


def _http_error_detail(code: int) -> str:
    if code == 400:
        return "请求参数错误（400）"
    if code == 401:
        return "API key 无效或未授权（401）"
    if code == 403:
        return "无权限/访问被拒（403），可能是区域限制或账号问题"
    if code == 404:
        return "接口地址或模型名不存在（404），请检查 Base URL 和模型名"
    if code == 429:
        return "请求过于频繁，已被限流（429），稍后重试"
    if code == 456:
        return "DeepL 免费版配额已用完（456）"
    if code >= 500:
        return f"服务器内部错误（{code}），请稍后重试"
    return f"HTTP 错误（{code}）"


def _http_post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = redact_secrets(exc.read().decode("utf-8", "replace")[:300])
        raise RuntimeError(f"{_http_error_detail(exc.code)}：{detail}") from exc
    except TimeoutError as exc:
        raise RuntimeError("请求超时") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise RuntimeError("请求超时") from exc
        raise RuntimeError(f"网络连接失败：{redact_secrets(str(reason))}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("服务器返回的不是合法 JSON") from exc


def _fetch_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "ProjectFerry/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def _http_get_json(url: str, timeout: int) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "ProjectFerry/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_http_error_detail(exc.code)) from exc
    except TimeoutError as exc:
        raise RuntimeError("下载超时") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise RuntimeError("下载超时") from exc
        raise RuntimeError(f"网络连接失败：{reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("返回的不是合法 JSON") from exc
