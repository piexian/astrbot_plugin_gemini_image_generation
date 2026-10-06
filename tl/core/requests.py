"""统一生图请求和 provider attempt 输入。"""

from __future__ import annotations

import copy
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .errors import InvalidRequestError

BodyFactory = Callable[[], Any]


def _clean_optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidRequestError(f"{field_name} 必须是字符串")
    value = value.strip()
    return value or None


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
        if not isinstance(self.source, str) or not self.source.strip():
            raise InvalidRequestError("source 必须是非空字符串")
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
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.reference_images
        ):
            raise InvalidRequestError("reference_images 只能包含非空字符串")
        if not isinstance(self.requester, Mapping):
            raise InvalidRequestError("requester 必须是对象")
        if not isinstance(self.metadata, Mapping):
            raise InvalidRequestError("metadata 必须是对象")
        object.__setattr__(self, "requester", dict(self.requester))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        if self.deadline_at is not None and not isinstance(self.deadline_at, datetime):
            raise InvalidRequestError("deadline_at 必须是 datetime")

    @classmethod
    def from_values(cls, **values: Any) -> GenerationRequest:
        """兼容旧入口的 list 参考图输入并创建不可变请求。"""

        references = values.get("reference_images")
        if references is None:
            values["reference_images"] = ()
        elif isinstance(references, (list, tuple)):
            values["reference_images"] = tuple(str(item) for item in references)
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
        if self.deadline_at is not None and not isinstance(self.deadline_at, datetime):
            raise InvalidRequestError("attempt deadline_at 必须是 datetime")

    def remaining_seconds(self, now: datetime) -> float | None:
        """计算剩余 deadline；负值表示本次尝试已超时。"""

        if self.deadline_at is None:
            return None
        remaining = (self.deadline_at - now).total_seconds()
        if not math.isfinite(remaining):
            return 0.0
        return max(remaining, 0.0)
