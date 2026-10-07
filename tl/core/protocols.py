"""核心组件协议；实现留到后续阶段。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .models import Artifact, Attempt, Job, JobEvent, LeaseRecord
from .requests import BodyFactory, GenerationRequest, ProviderAttempt
from .results import GenerationResult
from .states import JobState


@runtime_checkable
class JobStoreProtocol(Protocol):
    """Closed stores raise ServiceClosedError for reads and writes.

    Writes settle before propagating cancellation. Catch the CancelledError
    subclass JobStoreWriteCancelledError at the call site to inspect its
    committed/failed outcome and result/error. Failed commits require reopening
    the store; cancellation alone never means that a write was rolled back.
    """

    async def create(self, job: Job) -> Job: ...

    async def get(self, job_id: str) -> Job | None: ...

    async def save(
        self, job: Job, *, expected_revision: int | None = None
    ) -> Job: ...

    async def append_event(self, event: JobEvent) -> None: ...

    async def list_events(self, job_id: str) -> list[JobEvent]: ...

    async def list_jobs(
        self, states: Iterable[JobState | str] | None = None
    ) -> list[Job]: ...

    async def add_attempt(self, attempt: Attempt) -> Attempt: ...

    async def add_artifact(self, job_id: str, artifact: Artifact) -> Artifact: ...

    async def add_lease(self, lease: LeaseRecord) -> LeaseRecord: ...

    async def release_lease(self, lease_id: str) -> None: ...

    async def delete_job(self, job_id: str) -> None:
        """Delete an old terminal Job without unreleased leases; keep an audit row."""
        ...

    async def prune_jobs(self, before: datetime, states: set[JobState]) -> int:
        """Delete eligible terminal Jobs older than both before and retention."""
        ...

    async def expire_leases(self, now: datetime) -> int:
        """Mark active leases past metadata.expires_at expired, pending cleanup."""
        ...

    async def checkpoint(self) -> None:
        """Checkpoint WAL; report CheckpointBusyError if another reader blocks it."""
        ...

    async def close(self) -> None: ...


@runtime_checkable
class ProviderProtocol(Protocol):
    """统一 provider contract。

    ``build_request`` 接收每次独立的 attempt，返回可重复调用的 body
    factory。实现不得缓存上一轮已经发送的 body。
    """

    name: str

    def supports(self, request: GenerationRequest) -> bool: ...

    def normalize(self, request: GenerationRequest) -> GenerationRequest: ...

    async def build_request(
        self, request: GenerationRequest, attempt: ProviderAttempt
    ) -> tuple[str, Mapping[str, str], BodyFactory]: ...

    async def send(
        self,
        request: GenerationRequest,
        attempt: ProviderAttempt,
        *,
        url: str,
        headers: Mapping[str, str],
        body_factory: BodyFactory,
    ) -> Any: ...

    async def parse_response(
        self, response: Any, *, request: GenerationRequest, attempt: ProviderAttempt
    ) -> GenerationResult: ...


@runtime_checkable
class ReferenceServiceProtocol(Protocol):
    def resolve(
        self, references: tuple[str, ...], *, deadline_at: datetime | None = None
    ) -> AsyncIterator[Artifact]: ...


@runtime_checkable
class SchedulerProtocol(Protocol):
    async def submit(self, request: GenerationRequest) -> Job: ...

    async def wait(self, job_id: str, *, timeout: float | None = None) -> Job: ...

    async def cancel(self, job_id: str) -> Job: ...

    async def get_status(self, job_id: str) -> Job | None: ...

    async def close(self) -> None: ...


@runtime_checkable
class ReservationProtocol(Protocol):
    async def release(self) -> None: ...


@runtime_checkable
class ArtifactStoreProtocol(Protocol):
    async def acquire(self, job: Job) -> LeaseRecord: ...

    async def save(self, artifact: Artifact, *, job_id: str) -> Artifact: ...

    async def release(self, lease: LeaseRecord) -> None: ...


@runtime_checkable
class QuotaProtocol(Protocol):
    async def reserve(self, request: GenerationRequest) -> ReservationProtocol: ...


@runtime_checkable
class LimiterProtocol(Protocol):
    async def reserve(self, request: GenerationRequest) -> ReservationProtocol: ...
