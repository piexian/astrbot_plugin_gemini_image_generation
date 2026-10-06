"""核心组件协议；实现留到后续阶段。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .models import Artifact, Job, JobEvent, LeaseRecord
from .requests import BodyFactory, GenerationRequest, ProviderAttempt
from .results import GenerationResult


@runtime_checkable
class JobStoreProtocol(Protocol):
    async def create(self, job: Job) -> Job: ...

    async def get(self, job_id: str) -> Job | None: ...

    async def save(self, job: Job) -> Job: ...

    async def append_event(self, event: JobEvent) -> None: ...

    async def list_events(self, job_id: str) -> list[JobEvent]: ...


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
