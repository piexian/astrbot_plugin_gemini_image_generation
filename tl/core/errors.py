"""调度领域错误。

错误对象只携带可公开的 code/message/retryable 元数据，避免把 provider
配置或 API key 原文带入 WebUI、历史记录或日志序列化结果。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "authorization",
    "credential",
)


def redact_sensitive(value: Any, *, key: str | None = None) -> Any:
    """递归移除将要进入 API/历史对象的凭据原文。"""

    normalized_key = str(key or "").lower().replace("-", "_")
    if normalized_key and any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
        return "***"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_sensitive(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    return value


class CoreError(Exception):
    """所有核心领域错误的基类。"""

    code = "core_error"
    retryable: bool | None = None

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = str(message)
        if code is not None:
            self.code = str(code)
        if retryable is not None:
            self.retryable = retryable
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        """返回适合 API 的错误信封，不暴露异常 repr 或 secrets。"""

        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.retryable is not None:
            result["retryable"] = self.retryable
        if self.details:
            result["details"] = redact_sensitive(self.details)
        return result


class InvalidRequestError(CoreError):
    code = "invalid_request"
    retryable = False


class QueueFullError(CoreError):
    code = "queue_full"
    retryable = True


class ServiceClosedError(CoreError):
    code = "service_closed"
    retryable = False


class JobNotFoundError(CoreError):
    code = "job_not_found"
    retryable = False


class JobCancelledError(CoreError):
    code = "job_cancelled"
    retryable = False


class StateTransitionError(CoreError):
    code = "invalid_state_transition"
    retryable = False


class LeaseError(CoreError):
    code = "lease_error"
    retryable = False
