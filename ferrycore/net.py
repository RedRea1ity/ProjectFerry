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


class ApiRequestError(RuntimeError):
    """结构化 HTTP/网络错误：携带状态码、Retry-After 与是否可重试标记。

    继承自 RuntimeError，所有旧的 `except RuntimeError` 调用方无需改动。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        retryable: bool = True,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.retryable = retryable
        self.detail = detail


def _is_retryable_status(code: int) -> bool:
    return code in (408, 429) or code >= 500


def _parse_retry_after(headers: Any) -> float | None:
    if headers is None:
        return None
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if not raw:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    if value <= 0:
        return None
    return min(value, 30.0)


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
        raise ApiRequestError(
            f"{_http_error_detail(exc.code)}：{detail}",
            status_code=exc.code,
            retry_after=_parse_retry_after(getattr(exc, "headers", None)),
            retryable=_is_retryable_status(exc.code),
            detail=detail,
        ) from exc
    except TimeoutError as exc:
        raise ApiRequestError("请求超时", retryable=True) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise ApiRequestError("请求超时", retryable=True) from exc
        raise ApiRequestError(f"网络连接失败：{redact_secrets(str(reason))}", retryable=True) from exc
    except json.JSONDecodeError as exc:
        raise ApiRequestError("服务器返回的不是合法 JSON", retryable=True) from exc


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
