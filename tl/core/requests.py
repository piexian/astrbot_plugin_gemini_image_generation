"""统一生图请求和 provider attempt 输入。"""

from __future__ import annotations

import copy
import hashlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .errors import (
    MAX_METADATA_BYTES,
    InvalidRequestError,
    bounded_json,
    redact_artifact_reference,
)

BodyFactory = Callable[[], Any]
MAX_PROMPT_CHARS = 10_000
MAX_SOURCE_CHARS = 256
MAX_REFERENCE_IMAGES = 32
MAX_REFERENCE_VALUE_CHARS = 4_096
MAX_REFERENCE_URL_CHARS = 2_048
ALLOWED_REQUESTER_FIELDS = frozenset(
    {"user_id", "user_name", "group_id", "chat_type", "session_id", "umo"}
)


def _utc_deadline(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise InvalidRequestError(f"{field_name} 必须是 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidRequestError(f"{field_name} 不允许使用 naive datetime")
    return value.astimezone(timezone.utc)


def _clean_optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidRequestError(f"{field_name} 必须是字符串")
    value = value.strip()
    return value or None


def reference_descriptor(value: str) -> dict[str, str]:
    raw = str(value).strip()
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()
    safe = redact_artifact_reference(raw)
    if safe.lower().startswith(("http://", "https://")):
        from urllib.parse import urlsplit

        parsed = urlsplit(safe)
        host = parsed.hostname or "unknown"
        display = parsed.path.rsplit("/", 1)[-1] or host
    elif safe.startswith("artifact:"):
        host, display = "local", safe
    elif safe.startswith("data:"):
        host, display = "inline", "data-uri"
    else:
        host, display = "reference", safe.rsplit("/", 1)[-1] or "reference"
    return {
        "reference_id": f"ref:{digest[:32]}",
        "artifact_id": f"artifact:{digest[:32]}",
        "host": host[:255],
        "hash": digest,
        "display": display[:255],
    }


def reference_id_from_value(value: Any) -> str:
    if isinstance(value, Mapping):
        stable = (
            value.get("reference_id") or value.get("artifact_id") or value.get("hash")
        )
        if not stable:
            raise InvalidRequestError("reference descriptor 缺少稳定 id")
        return str(stable)
    return str(value)


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """入口提交给调度器的不可变请求快照。

    ``reference_images`` 只保存入口提供的引用标识；实际读取、下载和
    临时文件生命周期由后续 ``ReferenceService`` 接管。
    """

    prompt: str
    source: str
    image_count: int = 1
    provider: str | None = None
    model: str | None = None
    candidate_id: str | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    negative_prompt: str | None = None
    quality: str | None = None
    watermark: bool | None = None
    reference_images: tuple[str, ...] = ()
    deadline_at: datetime | None = None
    parent_job_id: str | None = None
    requester: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise InvalidRequestError("prompt 必须是非空字符串")
        if len(self.prompt) > MAX_PROMPT_CHARS:
            raise InvalidRequestError("prompt 超过长度限制")
        if not isinstance(self.source, str) or not self.source.strip():
            raise InvalidRequestError("source 必须是非空字符串")
        if len(self.source) > MAX_SOURCE_CHARS:
            raise InvalidRequestError("source 超过长度限制")
        if type(self.image_count) is not int or self.image_count < 1:
            raise InvalidRequestError("image_count 必须是正整数")
        if self.watermark is not None and type(self.watermark) is not bool:
            raise InvalidRequestError("watermark 必须是布尔值")
        for name in (
            "provider",
            "model",
            "candidate_id",
            "resolution",
            "aspect_ratio",
            "negative_prompt",
            "quality",
            "parent_job_id",
        ):
            _clean_optional_text(getattr(self, name), name)
        if not isinstance(self.reference_images, tuple):
            raise InvalidRequestError("reference_images 必须是不可变序列")
        if len(self.reference_images) > MAX_REFERENCE_IMAGES:
            raise InvalidRequestError("reference_images 超过数量限制")
        if any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > MAX_REFERENCE_VALUE_CHARS
            or (
                item.lower().startswith(("http://", "https://"))
                and len(item) > MAX_REFERENCE_URL_CHARS
            )
            for item in self.reference_images
        ):
            raise InvalidRequestError("reference_images 内容或数量超过限制")
        if not isinstance(self.requester, Mapping):
            raise InvalidRequestError("requester 必须是对象")
        if not isinstance(self.metadata, Mapping):
            raise InvalidRequestError("metadata 必须是对象")
        requester = {
            str(key): str(value)[:256]
            for key, value in self.requester.items()
            if key in ALLOWED_REQUESTER_FIELDS and value is not None
        }
        metadata = copy.deepcopy(dict(self.metadata))
        try:
            bounded_json(metadata, limit=MAX_METADATA_BYTES, field_name="metadata")
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from exc
        object.__setattr__(self, "requester", requester)
        object.__setattr__(self, "metadata", metadata)
        if self.deadline_at is not None:
            object.__setattr__(
                self, "deadline_at", _utc_deadline(self.deadline_at, "deadline_at")
            )

    @classmethod
    def from_values(cls, **values: Any) -> GenerationRequest:
        """兼容旧入口的 list 参考图输入并创建不可变请求。"""

        references = values.get("reference_images")
        if references is None:
            values["reference_images"] = ()
        elif isinstance(references, (list, tuple)):
            values["reference_images"] = tuple(
                reference_id_from_value(item) for item in references
            )
        else:
            raise InvalidRequestError("reference_images 必须是数组")
        return cls(**values)


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    """一次 provider 尝试的最小输入。

    ``body_factory`` 每次调用都必须重新构造请求体，避免 retry 重用已经被
    provider 或 aiohttp 消费、修改或过期的 payload。
    """

    number: int
    deadline_at: datetime | None = None
    provider: str | None = None
    model: str | None = None
    candidate_id: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.number) is not int or self.number < 1:
            raise InvalidRequestError("attempt number 必须从 1 开始")
        if self.deadline_at is not None:
            object.__setattr__(
                self,
                "deadline_at",
                _utc_deadline(self.deadline_at, "attempt deadline_at"),
            )

    def remaining_seconds(self, now: datetime) -> float | None:
        """计算剩余 deadline；负值表示本次尝试已超时。"""

        if self.deadline_at is None:
            return None
        now = _utc_deadline(now, "now")
        remaining = (self.deadline_at - now).total_seconds()
        if not math.isfinite(remaining):
            return 0.0
        return max(remaining, 0.0)
