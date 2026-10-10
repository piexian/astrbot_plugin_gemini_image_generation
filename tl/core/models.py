"""可持久化的 Job、attempt、artifact 和事件模型。"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .errors import (
    MAX_EVENT_DATA_BYTES,
    MAX_METADATA_BYTES,
    StateTransitionError,
    bounded_json,
    redact_artifact_reference,
    redact_text,
)
from .requests import GenerationRequest, reference_descriptor
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


def _parse_deadline(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("deadline_at 必须是带时区的 ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("deadline_at 不是有效的 ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("deadline_at 不允许使用 naive datetime")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Artifact:
    artifact_id: str
    kind: str = "image"
    location: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": redact_artifact_reference(self.artifact_id),
            "kind": self.kind,
            "location": redact_artifact_reference(self.location)
            if self.location
            else None,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "metadata": bounded_json(
                self.metadata, limit=MAX_METADATA_BYTES, field_name="artifact.metadata"
            ),
        }

    def to_public_dict(self) -> dict[str, Any]:
        return self.to_storage_dict()

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": redact_artifact_reference(self.artifact_id),
            "kind": self.kind,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.to_public_dict()


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

    def to_storage_dict(self) -> dict[str, Any]:
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
            "error_message": redact_text(self.error_message)
            if self.error_message
            else None,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return self.to_storage_dict()

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "job_id": self.job_id,
            "number": self.number,
            "status": self.status,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.to_public_dict()


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    lease_id: str
    job_id: str
    kind: str
    status: str = "active"
    acquired_at: datetime = field(default_factory=utc_now)
    released_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status,
            "acquired_at": _timestamp(self.acquired_at),
            "released_at": _timestamp(self.released_at) if self.released_at else None,
            "metadata": bounded_json(
                self.metadata, limit=MAX_METADATA_BYTES, field_name="lease.metadata"
            ),
        }

    def to_public_dict(self) -> dict[str, Any]:
        return self.to_storage_dict()

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.to_public_dict()


@dataclass(frozen=True, slots=True)
class JobEvent:
    event_id: str
    job_id: str
    event_type: str
    created_at: datetime = field(default_factory=utc_now)
    state: JobState | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_storage_dict(self) -> dict[str, Any]:
        data = bounded_json(
            self.data, limit=MAX_EVENT_DATA_BYTES, field_name="event.data"
        )
        return {
            "event_id": self.event_id,
            "job_id": self.job_id,
            "event_type": self.event_type,
            "created_at": _timestamp(self.created_at),
            "state": self.state.value if self.state else None,
            "data": data,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return self.to_storage_dict()

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "job_id": self.job_id,
            "event_type": self.event_type,
            "state": self.state.value if self.state else None,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.to_public_dict()


@dataclass(slots=True)
class Job:
    """调度器和 JobStore 共享的领域聚合。"""

    job_id: str
    request: GenerationRequest
    state: JobState = JobState.ACCEPTED
    revision: int = 0
    owner_id: str | None = None
    fencing_token: int = 0
    parent_job_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    deadline_at: datetime | None = None
    result: GenerationResult | None = None
    error_code: str | None = None
    error_message: str | None = None
    cancel_requested: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision 必须是非负整数")
        if self.owner_id is not None and (
            not isinstance(self.owner_id, str) or not self.owner_id
        ):
            raise ValueError("owner_id 必须是非空字符串")
        if type(self.fencing_token) is not int or self.fencing_token < 0:
            raise ValueError("fencing_token 必须是非负整数")
        if self.deadline_at is not None:
            if not isinstance(self.deadline_at, datetime):
                raise ValueError("deadline_at 必须是 datetime")
            if self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None:
                raise ValueError("deadline_at 不允许使用 naive datetime")
            deadline = self.deadline_at.astimezone(timezone.utc)
            request_deadline = self.request.deadline_at
            if request_deadline is not None:
                request_deadline = request_deadline.astimezone(timezone.utc)
                if request_deadline != deadline:
                    raise ValueError(
                        "Job.deadline_at 与 GenerationRequest.deadline_at 不一致"
                    )
            if self.request.deadline_at != deadline:
                from dataclasses import replace

                object.__setattr__(
                    self,
                    "request",
                    replace(self.request, deadline_at=deadline),
                )
            object.__setattr__(self, "deadline_at", deadline)
        elif self.request.deadline_at is not None:
            object.__setattr__(
                self,
                "deadline_at",
                self.request.deadline_at.astimezone(timezone.utc),
            )

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
            revision=0,
            owner_id=None,
            fencing_token=0,
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

    def _request_storage_dict(self) -> dict[str, Any]:
        requester = {
            key: redact_text(value)
            for key, value in self.request.requester.items()
            if key
            in {"user_id", "user_name", "group_id", "chat_type", "session_id", "umo"}
        }
        request = {
            "prompt": redact_text(self.request.prompt),
            "source": redact_text(self.request.source),
            "image_count": self.request.image_count,
            "provider": self.request.provider,
            "model": self.request.model,
            "candidate_id": self.request.candidate_id,
            "resolution": self.request.resolution,
            "aspect_ratio": self.request.aspect_ratio,
            "negative_prompt": redact_text(self.request.negative_prompt)
            if self.request.negative_prompt
            else None,
            "quality": self.request.quality,
            "watermark": self.request.watermark,
            "reference_images": [
                reference_descriptor(item) for item in self.request.reference_images
            ],
            "parent_job_id": self.request.parent_job_id,
            "requester": requester,
            "metadata": bounded_json(
                self.request.metadata,
                limit=MAX_METADATA_BYTES,
                field_name="request.metadata",
            ),
        }
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "revision": self.revision,
            "owner_id": self.owner_id,
            "fencing_token": self.fencing_token,
            "parent_job_id": self.parent_job_id,
            "created_at": _timestamp(self.created_at),
            "updated_at": _timestamp(self.updated_at),
            "deadline_at": _timestamp(self.deadline_at) if self.deadline_at else None,
            "request": request,
            "result": self.result.to_storage_dict() if self.result else None,
            "error_code": redact_text(self.error_code) if self.error_code else None,
            "error_message": redact_text(self.error_message)
            if self.error_message
            else None,
            "cancel_requested": self.cancel_requested,
            "metadata": bounded_json(
                self.metadata, limit=MAX_METADATA_BYTES, field_name="job.metadata"
            ),
        }

    def to_storage_dict(self) -> dict[str, Any]:
        return self._request_storage_dict()

    def to_public_dict(self) -> dict[str, Any]:
        return self._request_storage_dict()

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "revision": self.revision,
            "source": redact_text(self.request.source),
            "provider": self.request.provider,
            "model": self.request.model,
            "artifact_count": len(self.result.artifacts) if self.result else 0,
            "error": redact_text(self.error_message) if self.error_message else None,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.to_public_dict()

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
        deadline = _parse_deadline(payload.get("deadline_at"))
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
            revision=payload.get("revision", 0),
            owner_id=payload.get("owner_id"),
            fencing_token=payload.get("fencing_token", 0),
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
