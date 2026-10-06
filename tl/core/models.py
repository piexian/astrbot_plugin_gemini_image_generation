"""可持久化的 Job、attempt、artifact 和事件模型。"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .errors import StateTransitionError, redact_sensitive
from .requests import GenerationRequest
from .results import GenerationResult
from .states import JobState, can_transition, coerce_state


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class Artifact:
    artifact_id: str
    kind: str = "image"
    location: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "location": self.location,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "metadata": redact_sensitive(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: str
    job_id: str
    number: int
    provider: str | None = None
    model: str | None = None
    status: str = "started"
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "job_id": self.job_id,
            "number": self.number,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "started_at": _timestamp(self.started_at),
            "finished_at": _timestamp(self.finished_at) if self.finished_at else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    lease_id: str
    job_id: str
    kind: str
    status: str = "active"
    acquired_at: datetime = field(default_factory=utc_now)
    released_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status,
            "acquired_at": _timestamp(self.acquired_at),
            "released_at": _timestamp(self.released_at) if self.released_at else None,
            "metadata": redact_sensitive(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class JobEvent:
    event_id: str
    job_id: str
    event_type: str
    created_at: datetime = field(default_factory=utc_now)
    state: JobState | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "job_id": self.job_id,
            "event_type": self.event_type,
            "created_at": _timestamp(self.created_at),
            "state": self.state.value if self.state else None,
            "data": copy.deepcopy(dict(self.data)),
        }


@dataclass(slots=True)
class Job:
    """调度器和 JobStore 共享的领域聚合。"""

    job_id: str
    request: GenerationRequest
    state: JobState = JobState.ACCEPTED
    parent_job_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    deadline_at: datetime | None = None
    result: GenerationResult | None = None
    error_code: str | None = None
    error_message: str | None = None
    cancel_requested: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        """兼容旧 tracker 使用的字符串状态字段。"""

        return self.state.value

    @property
    def is_terminal(self) -> bool:
        from .states import TERMINAL_STATES

        return self.state in TERMINAL_STATES

    @classmethod
    def create(
        cls,
        request: GenerationRequest,
        *,
        job_id: str | None = None,
        parent_job_id: str | None = None,
        now: datetime | None = None,
    ) -> Job:
        timestamp = now or utc_now()
        return cls(
            job_id=job_id or f"job-{uuid.uuid4().hex}",
            request=request,
            parent_job_id=parent_job_id or request.parent_job_id,
            created_at=timestamp,
            updated_at=timestamp,
            deadline_at=request.deadline_at,
        )

    def transition(
        self, target: JobState | str, *, now: datetime | None = None
    ) -> None:
        target_state = coerce_state(target)
        if target_state == self.state:
            return
        if not can_transition(self.state, target_state):
            raise StateTransitionError(
                f"Job {self.job_id} 不能从 {self.state.value} 转为 {target_state.value}",
                details={
                    "job_id": self.job_id,
                    "from": self.state.value,
                    "to": target_state.value,
                },
            )
        self.state = target_state
        self.updated_at = now or utc_now()

    def request_cancel(self) -> None:
        self.cancel_requested = True
        self.updated_at = utc_now()

    def set_result(
        self, result: GenerationResult, *, now: datetime | None = None
    ) -> None:
        self.result = result
        self.error_code = None
        self.error_message = None
        self.updated_at = now or utc_now()

    def set_error(
        self,
        code: str,
        message: str,
        *,
        now: datetime | None = None,
    ) -> None:
        self.error_code = str(code)
        self.error_message = str(message)
        self.updated_at = now or utc_now()

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "parent_job_id": self.parent_job_id,
            "created_at": _timestamp(self.created_at),
            "updated_at": _timestamp(self.updated_at),
            "deadline_at": _timestamp(self.deadline_at) if self.deadline_at else None,
            "request": {
                "prompt": self.request.prompt,
                "source": self.request.source,
                "image_count": self.request.image_count,
                "provider": self.request.provider,
                "model": self.request.model,
                "candidate_id": self.request.candidate_id,
                "resolution": self.request.resolution,
                "aspect_ratio": self.request.aspect_ratio,
                "negative_prompt": self.request.negative_prompt,
                "quality": self.request.quality,
                "watermark": self.request.watermark,
                "reference_images": list(self.request.reference_images),
                "parent_job_id": self.request.parent_job_id,
                "requester": dict(self.request.requester),
                "metadata": redact_sensitive(self.request.metadata),
            },
            "result": self.result.as_dict() if self.result else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "cancel_requested": self.cancel_requested,
            "metadata": redact_sensitive(self.metadata),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.as_dict()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Job:
        request_data = payload.get("request")
        if not isinstance(request_data, Mapping):
            raise ValueError("Job 缺少 request")
        request = GenerationRequest.from_values(**dict(request_data))
        created = _parse_timestamp(payload.get("created_at")) or utc_now()
        updated = _parse_timestamp(payload.get("updated_at")) or created
        deadline = _parse_timestamp(payload.get("deadline_at"))
        result_data = payload.get("result")
        if isinstance(result_data, Mapping):
            result_values = dict(result_data)
            result_values["artifacts"] = tuple(result_values.get("artifacts") or ())
            result = GenerationResult(**result_values)
        else:
            result = None
        return cls(
            job_id=str(payload.get("job_id") or ""),
            request=request,
            state=coerce_state(payload.get("state", JobState.ACCEPTED.value)),
            parent_job_id=payload.get("parent_job_id"),
            created_at=created,
            updated_at=updated,
            deadline_at=deadline,
            result=result,
            error_code=payload.get("error_code"),
            error_message=payload.get("error_message"),
            cancel_requested=bool(payload.get("cancel_requested", False)),
            metadata=dict(payload.get("metadata") or {}),
        )
