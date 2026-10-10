"""统一生图结果模型。"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import (
    MAX_ARTIFACTS,
    bounded_json,
    redact_artifact_reference,
    redact_text,
)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """Provider 输出被调度器归一化后的结果。

    artifacts 中只允许稳定的 artifact id/path/url 引用；原始响应和密钥不
    应写入此对象。
    """

    artifacts: tuple[str, ...] = ()
    text: str | None = None
    provider: str | None = None
    model: str | None = None
    candidate_id: str | None = None
    attempt_count: int = 1
    partial: bool = False
    stats: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        if len(self.artifacts) > MAX_ARTIFACTS:
            raise ValueError("artifacts 超过数量限制")
        if any(not isinstance(item, str) or not item for item in self.artifacts):
            raise ValueError("artifacts 只能包含非空字符串")
        if self.attempt_count < 1:
            raise ValueError("attempt_count 必须是正整数")
        object.__setattr__(self, "stats", copy.deepcopy(dict(self.stats)))

    @property
    def has_output(self) -> bool:
        return bool(self.artifacts or (self.text and self.text.strip()))

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "artifacts": [redact_artifact_reference(item) for item in self.artifacts],
            "text": redact_text(self.text) if self.text else None,
            "provider": self.provider,
            "model": self.model,
            "candidate_id": self.candidate_id,
            "attempt_count": self.attempt_count,
            "partial": self.partial,
            "stats": bounded_json(
                self.stats, limit=64 * 1024, field_name="result.stats"
            ),
        }

    def to_public_dict(self) -> dict[str, Any]:
        return self.to_storage_dict()

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "artifact_count": len(self.artifacts),
            "provider": self.provider,
            "model": self.model,
            "candidate_id": self.candidate_id,
            "attempt_count": self.attempt_count,
            "partial": self.partial,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.to_public_dict()
