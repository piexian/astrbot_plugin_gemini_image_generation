"""统一生图结果模型。"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import redact_sensitive


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
        if any(not isinstance(item, str) or not item for item in self.artifacts):
            raise ValueError("artifacts 只能包含非空字符串")
        if self.attempt_count < 1:
            raise ValueError("attempt_count 必须是正整数")
        object.__setattr__(self, "stats", copy.deepcopy(dict(self.stats)))

    @property
    def has_output(self) -> bool:
        return bool(self.artifacts or (self.text and self.text.strip()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifacts": list(self.artifacts),
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "candidate_id": self.candidate_id,
            "attempt_count": self.attempt_count,
            "partial": self.partial,
            "stats": redact_sensitive(self.stats),
        }
