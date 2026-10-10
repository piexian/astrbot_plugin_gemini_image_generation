"""调度领域错误。

错误对象只携带可公开的 code/message/retryable 元数据，避免把 provider
配置或 API key 原文带入 WebUI、历史记录或日志序列化结果。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "authorization",
    "credential",
)
MAX_METADATA_BYTES = 64 * 1024
MAX_EVENT_DATA_BYTES = 32 * 1024
MAX_ARTIFACTS = 256
_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_CREDENTIAL_RE = re.compile(
    r"(?i)([\"']?(?:authorization|api[_-]?key|token|secret|password)[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)([^\"'\s,;}]+)"
)


def redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return value
    netloc = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port:
        netloc += f":{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def redact_text(value: Any) -> str:
    text = str(value or "")
    text = _URL_RE.sub(lambda match: redact_url(match.group(0)), text)
    return _CREDENTIAL_RE.sub(r"\1***", text)


def redact_artifact_reference(value: Any) -> str:
    reference = redact_text(value)
    parsed = urlsplit(reference)
    if parsed.scheme.lower() == "file" or os.path.isabs(reference):
        digest = hashlib.sha256(reference.encode("utf-8", "replace")).hexdigest()
        return f"artifact:{digest[:32]}"
    return reference


def bounded_json(value: Any, *, limit: int, field_name: str) -> Any:
    safe = redact_sensitive(value)
    try:
        encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} 必须是可序列化 JSON") from exc
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError(f"{field_name} 超过 {limit} 字节限制")
    return safe


def redact_sensitive(value: Any, *, key: str | None = None) -> Any:
    """递归移除将要进入 API/历史对象的凭据原文。"""

    normalized_key = str(key or "").lower().replace("-", "_")
    if normalized_key and any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
        return "***"
    if isinstance(value, str):
        return redact_text(value)
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
        self.message = redact_text(self.message)
        if code is not None:
            self.code = str(code)
        if retryable is not None:
            self.retryable = retryable
        self.details = dict(details or {})

    def to_storage_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": redact_text(self.message),
        }
        if self.retryable is not None:
            result["retryable"] = self.retryable
        if self.details:
            result["details"] = redact_sensitive(self.details)
        return result

    def to_public_dict(self) -> dict[str, Any]:
        return self.to_storage_dict()

    def to_log_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": redact_text(self.message)}

    def as_dict(self) -> dict[str, Any]:
        """返回适合 API 的错误信封，不暴露异常 repr 或 secrets。"""

        return self.to_public_dict()


class InvalidRequestError(CoreError):
    code = "invalid_request"
    retryable = False


class QueueFullError(CoreError):
    code = "queue_full"
    retryable = True


class ServiceClosedError(CoreError):
    code = "service_closed"
    retryable = False


class JobStoreWriteCancelledError(asyncio.CancelledError):
    """Cancellation delivered after a database operation has finished.

    Catch at the write call to inspect ``outcome`` and ``result``/``error``.
    ``failed`` reports the original operation error; it does not claim that a
    failed COMMIT was rolled back. A broken store must be reopened to inspect it.
    """

    def __init__(
        self,
        *args: Any,
        outcome: Literal["committed", "failed"],
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        super().__init__(*args)
        self.outcome = outcome
        self.result = result
        self.error = error


class JobNotFoundError(CoreError):
    code = "job_not_found"
    retryable = False


class JobDeletionError(CoreError):
    code = "job_deletion_blocked"
    retryable = False


class RecoveryRequiredError(CoreError):
    code = "recovery_required"
    retryable = False


class CheckpointBusyError(CoreError):
    code = "checkpoint_busy"
    retryable = True


class StaleJobError(CoreError):
    code = "stale_job"
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


class ProviderRouteError(CoreError):
    """Router 候选链全部失败后的聚合错误；details 携带逐候选结果。"""

    code = "provider_route_failed"


class NoSupportedProviderError(ProviderRouteError):
    code = "no_supported_provider"
    retryable = False
