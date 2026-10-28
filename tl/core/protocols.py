"""核心组件协议；实现留到后续阶段。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .models import Artifact, Attempt, Job, JobEvent, LeaseRecord
from .requests import BodyFactory, GenerationRequest, ProviderAttempt
from .results import GenerationResult
from .states import JobState

if TYPE_CHECKING:
    from .scheduler import BatchHandle


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

    async def save(self, job: Job, *, expected_revision: int | None = None) -> Job: ...

    async def append_event(self, event: JobEvent) -> None: ...

    async def list_events(self, job_id: str) -> list[JobEvent]: ...

    async def list_jobs(
        self, states: Iterable[JobState | str] | None = None
    ) -> list[Job]: ...

    async def add_attempt(self, attempt: Attempt) -> Attempt: ...

    async def add_artifact(self, job_id: str, artifact: Artifact) -> Artifact: ...

    async def add_lease(self, lease: LeaseRecord) -> LeaseRecord: ...

    async def update_lease(
        self,
        lease_id: str,
        *,
        job_id: str,
        status: str,
        error: str | None = None,
        owner_id: str | None = None,
    ) -> None: ...

    async def release_lease(
        self,
        lease_id: str,
        *,
        released_at: datetime | None = None,
        job_id: str | None = None,
        owner_id: str | None = None,
    ) -> None: ...

    async def list_leases(self, job_id: str | None = None) -> list[LeaseRecord]: ...

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

    async def heartbeat(self) -> None: ...


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
        self,
        references: tuple[str, ...],
        *,
        deadline_at: datetime | None = None,
        requester: Mapping[str, str] | None = None,
    ) -> AsyncIterator[Artifact]: ...


@runtime_checkable
class ReferenceStoreProtocol(Protocol):
    """``ref:<kind>:<id>`` 句柄存储的最小协议。

    注意与 scheduler 构造参数 ``reference_store``（ReferenceService 包装，
    承担租约与整体解析）区分：本协议是其内部按 kind 分派的句柄存储，
    只负责把单个 id 解析回产物。
    """

    kind: str

    async def resolve(
        self,
        ref_id: str,
        *,
        requester: Mapping[str, str],
        deadline_at: datetime | None,
    ) -> Artifact: ...


@runtime_checkable
class SchedulerProtocol(Protocol):
    async def submit(
        self,
        request: GenerationRequest,
        *,
        admission: Mapping[str, Any] | None = None,
        delivery: DeliveryProtocol | None = None,
    ) -> Job: ...

    async def submit_batch(
        self,
        parent: GenerationRequest,
        children: Sequence[GenerationRequest],
        *,
        concurrency: int,
        admission: Mapping[str, Any] | None = None,
        delivery: BatchDeliveryProtocol | None = None,
    ) -> BatchHandle:
        """提交批量父子 Job，提交时就创建并落库全部子 Job。"""
        ...

    def attach_delivery(self, job_id: str, delivery: DeliveryProtocol) -> bool: ...

    async def wait(self, job_id: str, *, timeout: float | None = None) -> Job: ...

    async def cancel(self, job_id: str) -> Job: ...

    async def get_status(self, job_id: str) -> Job | None: ...

    async def close(self) -> None: ...


@runtime_checkable
class ReservationProtocol(Protocol):
    """submit 准入预留（quota/limiter 的 ``reserve`` 返回值）。

    ``release`` 必需，幂等，同步或异步均可：Job 到终态（或 task 未及运行就
    结束）时调用；已被接受的 Job 不退款，release 不得回补额度。

    其余成员可选（调度器用 getattr 探测）：

    - ``refund()``：仅 submit 自身失败（后续预留被拒、落库失败、建 task
      失败）回滚时调用，缺省退化为 ``release``；
    - ``runtime_scope()``：不带参数，返回在 provider 执行期间进入的同步上下文
      管理器，与观察者的 ``runtime_scope(job)`` 一起进入 ExitStack；
    - ``token``：写入持久化 lease 记录的标识，缺省由调度器生成。
    """

    def release(self) -> Any: ...


class JobObserverProtocol(Protocol):
    """调度器生命周期观察者；所有钩子均可选（getattr 探测，缺省即跳过）。

    ``job_accepted`` 在 Job QUEUED 落库后、runtime task 创建前调用；
    ``runtime_scope`` 是同步函数，返回只包住 provider 执行的同步上下文
    管理器；``job_result`` 在 RESULT_READY 落库后、转入 SUCCEEDED/PARTIAL
    前调用，result 为未脱敏的运行时结果；``job_finished`` 在 Job 进入终态
    后每个 Job 调用一次。

    批量父 Job（``submit_batch``）的子 Job 另有 ``child_started`` /
    ``child_finished``：子 Job 被启动、进入终态（含从未启动就被取消）时各调用
    一次，``child_finished`` 晚于该子 Job 自己的 ``job_finished``；``result``
    是进程内未脱敏的子结果，子 Job 没有结果时为 None。
    """

    async def job_accepted(self, job: Job) -> None: ...

    def runtime_scope(self, job: Job) -> AbstractContextManager[Any]: ...

    async def job_result(self, job: Job, result: GenerationResult) -> None: ...

    async def job_finished(self, job: Job) -> None: ...

    async def child_started(self, parent_job: Job, child_job: Job) -> None: ...

    async def child_finished(
        self, parent_job: Job, child_job: Job, result: GenerationResult | None
    ) -> None: ...


class DeliveryProtocol(Protocol):
    """Job 结果的投递方：由入口经 ``attach_delivery`` / ``submit(delivery=...)`` 登记。

    登记成功后调度器保证二选一（均用 ``delivery_timeout`` 包住，异常只记日志）：

    - ``deliver``：Job 产出结果后在投递判定点（``job_result`` 观察者之后）接手，
      Job 转 ``DELIVERY_PENDING``；正常返回按 ``result.partial`` 收尾为
      ``SUCCEEDED`` / ``PARTIAL``；抛普通异常或超时则收尾为 ``PARTIAL`` 且
      ``error_code="delivery_failed"``，结果仍保留。``deliver`` 执行期间 Job 被
      取消时 CancelledError 照常传播，Job 收尾为 ``CANCELLED``，不再调用
      ``deliver_failure``。
    - ``deliver_failure``：Job 以无结果的终态（``FAILED`` / ``CANCELLED`` /
      ``INTERRUPTED`` / ``ORPHANED``）结束，且 ``deliver`` 尚未接手（未越过
      判定点，或越过后在 ``deliver`` 开始前就失败、被取消）时调用，早于
      ``job_finished`` 观察者；``job`` 带终态与错误信息。

    例外：runtime task 在首次运行前就被取消、进程崩溃，两者都不会被调用。
    """

    async def deliver(self, job: Job, result: GenerationResult) -> None: ...

    async def deliver_failure(self, job: Job) -> None: ...


class BatchDeliveryProtocol(DeliveryProtocol, Protocol):
    """批量父 Job 的投递方：在 ``DeliveryProtocol`` 之外可选实现子 Job 进度钩子。

    ``child_started`` / ``child_finished`` 的调用时机与参数同观察者的同名钩子
    （见 ``JobObserverProtocol``），都用 getattr 探测，缺省即跳过；它们在父 Job
    运行期间由父 runtime task 依次调用，异常与超时只记日志。
    """

    async def child_started(self, parent_job: Job, child_job: Job) -> None: ...

    async def child_finished(
        self, parent_job: Job, child_job: Job, result: GenerationResult | None
    ) -> None: ...


@runtime_checkable
class ArtifactStoreProtocol(Protocol):
    async def acquire(self, job: Job) -> LeaseRecord: ...

    async def save(self, artifact: Artifact, *, job_id: str) -> Artifact: ...

    async def release(self, lease: LeaseRecord) -> None: ...


@runtime_checkable
class QuotaProtocol(Protocol):
    """额度准入组件：在 ``submit`` 内、落库前预留额度。

    ``reserve`` 同步或异步均可；返回 ``None`` 表示本次请求不需要预留。
    拒绝时抛出领域异常（如 ``QuotaExceededError``），调度器不创建 Job 并把
    异常原样抛给 ``submit`` 的调用方。``admission`` 是调用方传入的、不持久化
    的准入上下文（例如命令事件）。
    """

    def reserve(
        self, request: GenerationRequest, admission: Mapping[str, Any] | None
    ) -> ReservationProtocol | None | Awaitable[ReservationProtocol | None]: ...


@runtime_checkable
class LimiterProtocol(Protocol):
    """并发/队列准入组件：契约同 ``QuotaProtocol``，在额度预留之后调用。

    后续预留失败时，已拿到的额度预留由调度器回滚（优先 ``refund``）。
    """

    def reserve(
        self, request: GenerationRequest, admission: Mapping[str, Any] | None
    ) -> ReservationProtocol | None | Awaitable[ReservationProtocol | None]: ...
