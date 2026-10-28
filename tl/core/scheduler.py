"""Minimal, isolated JobScheduler foundation.

This module is intentionally not wired into commands, tools, WebUI or SDK.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import inspect
import math
import uuid
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from astrbot.api import logger

from .dispatch import dispatch_provider
from .errors import (
    MAX_ARTIFACTS,
    BatchAllFailedError,
    InvalidRequestError,
    JobNotFoundError,
    ProviderRouteError,
    RecoveryRequiredError,
    ServiceClosedError,
    StaleJobError,
)
from .leases import Lease, LeaseKind, LeaseSet
from .models import Job, JobEvent, LeaseRecord
from .recovery import recoverable_jobs
from .requests import GenerationRequest
from .results import GenerationResult
from .states import TERMINAL_STATES, JobState


@dataclass(frozen=True, slots=True)
class JobHandle:
    job_id: str
    state: JobState


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    job_id: str
    state: JobState
    revision: int
    result: GenerationResult | None
    error_code: str | None
    error_message: str | None
    cancel_requested: bool

    @classmethod
    def from_job(cls, job: Job) -> JobSnapshot:
        return cls(
            job_id=job.job_id,
            state=job.state,
            revision=job.revision,
            result=copy.deepcopy(job.result),
            error_code=job.error_code,
            error_message=job.error_message,
            cancel_requested=job.cancel_requested,
        )


@dataclass(frozen=True, slots=True)
class JobResult:
    job_id: str
    state: JobState
    result: GenerationResult | None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def artifacts(self) -> tuple[str, ...]:
        return self.result.artifacts if self.result else ()

    @property
    def text(self) -> str | None:
        return self.result.text if self.result else None


@dataclass(frozen=True, slots=True)
class BatchHandle:
    """submit_batch 的返回：父 Job 与全部子 Job（按 children 的顺序）。"""

    job_id: str
    child_job_ids: tuple[str, ...]
    state: JobState


@dataclass(slots=True)
class _ChildOutcome:
    """子 Job 收尾时留给父结果汇总的信息。"""

    job_id: str
    state: JobState | None
    requested_images: int
    artifacts: tuple[str, ...] = ()
    error_code: str | None = None


@dataclass(slots=True)
class _BatchPlan:
    """父 Job 的运行时登记：子 Job 的顺序、并发上限与各子 Job 的进展。

    仅在事件循环内串行访问；随父 runtime task 结束而注销。
    """

    child_ids: tuple[str, ...]
    concurrency: int
    admission: Mapping[str, Any] | None
    tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    # 已进入收尾流程（job_finished / child_finished）的子 Job，保证每个只收尾一次。
    claimed: set[str] = field(default_factory=set)
    outcomes: dict[str, _ChildOutcome] = field(default_factory=dict)
    aborting: bool = False


async def _call(value: Any, *args: Any, **kwargs: Any) -> Any:
    result = value(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


# Router 路径兜底余量：截断由 Router 按自身 deadline 执行，包裹超时留出余量。
_ROUTER_EXECUTE_GRACE_SECONDS = 1.0


# wait() 读回的运行时结果登记容量：按插入顺序淘汰最旧项，保持有界。
_RUNTIME_RESULT_CAPACITY = 256

# 以这些终态结束的 Job 没有可投递的结果，登记的投递方改走 deliver_failure。
_NO_RESULT_STATES = frozenset(
    {
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.INTERRUPTED,
        JobState.ORPHANED,
    }
)
# Job.error_message 里投递失败原因的长度上限。
_DELIVERY_ERROR_MAX_CHARS = 200


def _delivery_error_text(error: Exception, timeout: float) -> str:
    """投递失败原因的简短文本（落库时还会整体脱敏）。"""

    if isinstance(error, TimeoutError):
        return f"投递超时（{timeout:g} 秒）"
    detail = str(error).strip()
    text = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
    return text[:_DELIVERY_ERROR_MAX_CHARS]


class _GuardedObserverScope:
    """包装观察者/准入预留的上下文：退出异常只记日志，且不吞掉 provider 异常。"""

    def __init__(self, context: Any, owner_name: str) -> None:
        self._context = context
        self._owner_name = owner_name

    def __enter__(self) -> Any:
        return self._context.__enter__()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            self._context.__exit__(exc_type, exc, tb)
        except Exception as error:
            logger.warning(
                "[v2] %s runtime_scope 退出失败: %s",
                self._owner_name,
                type(error).__name__,
            )
        # 恒为 False：观察者与准入预留都不得抑制 provider 自身抛出的异常。
        return False


class JobScheduler:
    def __init__(
        self,
        store: Any,
        *,
        provider: Any,
        quota: Any = None,
        limiter: Any = None,
        reference_store: Any = None,
        artifact_store: Any = None,
        delivery_store: Any = None,
        default_timeout: float = 60.0,
        max_attempts: int = 3,
        retry_backoff: float = 0.05,
        heartbeat_interval: float = 5.0,
        cleanup_timeout: float = 5.0,
        observers: Sequence[Any] = (),
        observer_timeout: float = 120.0,
        delivery_timeout: float = 600.0,
    ) -> None:
        self.store = store
        self.provider = provider
        self.quota = quota
        self.limiter = limiter
        self.reference_store = reference_store
        self.artifact_store = artifact_store
        self.delivery_store = delivery_store
        self.default_timeout = float(default_timeout)
        self.max_attempts = max(int(max_attempts), 1)
        self.retry_backoff = max(float(retry_backoff), 0.0)
        self.heartbeat_interval = max(float(heartbeat_interval), 0.001)
        self.cleanup_timeout = max(float(cleanup_timeout), 0.1)
        self.observers = tuple(observers)
        self.observer_timeout = float(observer_timeout)
        self.delivery_timeout = float(delivery_timeout)
        self._closed = False
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._cleanup_tasks: set[asyncio.Task[Any]] = set()
        # 持久化只存脱敏描述符，原始引用归调度器内存持有，随 runtime task
        # 生命周期清理；登记/读取/清理都在事件循环内串行执行，dict 操作
        # 原子，无需加锁。
        self._original_references: dict[str, tuple[str, ...]] = {}
        # wait() 需要读回未脱敏的运行时结果（存储/快照仍只保留脱敏形态）；
        # 与 _original_references 相同，仅在事件循环内串行访问。
        self._runtime_results: OrderedDict[str, GenerationResult] = OrderedDict()
        # submit 时预留的准入资源，按 QUOTA、LIMITER 顺序登记；_run 取出后转成
        # 持久化 lease，task 在 _run 取走之前就结束时由 _task_done 释放。
        self._admissions: dict[str, tuple[tuple[LeaseKind, Any], ...]] = {}
        # 已登记、尚未交接的结果投递方；仅在事件循环内串行访问（见 attach_delivery）。
        self._deliveries: dict[str, Any] = {}
        # 已越过投递判定点（或已进入收尾）的 Job：不再接受新的投递登记。
        self._delivery_decided: set[str] = set()
        # 批量父 Job 的运行时登记（见 submit_batch），随父 runtime task 结束清理。
        self._batches: dict[str, _BatchPlan] = {}

    async def submit(
        self,
        request: GenerationRequest,
        *,
        admission: Mapping[str, Any] | None = None,
        delivery: Any = None,
    ) -> JobHandle:
        """提交 Job。

        ``admission`` 是不持久化的准入上下文（如命令事件），只交给 quota 与
        limiter 组件；任一准入组件拒绝时其异常原样抛出，且不创建 Job。
        ``delivery`` 在 Job 被接受后登记，等价于提交后立即 attach_delivery。
        """

        async with self._lock:
            if self._closed:
                raise ServiceClosedError("Scheduler 已关闭")
            if request.deadline_at is None:
                request = replace(
                    request,
                    deadline_at=datetime.now(timezone.utc)
                    + timedelta(seconds=self.default_timeout),
                )
            # 准入先于落库：预留或落库任一步失败都不留下 Job，已拿到的预留按
            # 逆序回滚（此时 Job 从未被接受，额度退款）。
            reserved: list[tuple[LeaseKind, Any]] = []
            try:
                await self._reserve_admissions(request, admission, reserved)
                job = Job.create(request, job_id=f"job-{uuid.uuid4().hex}")
                # Persistence is deliberately before create_task.
                await self.store.create(job)
                job.transition(JobState.QUEUED)
                job = await self.store.save(job)
            except BaseException:
                await self._rollback_admissions(reserved)
                raise
            if reserved:
                self._admissions[job.job_id] = tuple(reserved)
            if delivery is not None:
                self._deliveries[job.job_id] = delivery
            # 持久化成功后登记原始引用，供 _run 解析回真实输入。
            if request.reference_images:
                self._original_references[job.job_id] = request.reference_images
            done = asyncio.Event()
            self._done[job.job_id] = done
            task: asyncio.Task[Any] | None = None
            runtime = self._run(job.job_id)
            try:
                # 仍在锁内调用：保证先于该 Job 的其他钩子；失败只记日志不影响
                # Job。放在 try 内，使其被取消时也走下面的孤儿回滚。
                await self._notify_observers("job_accepted", job)
                task = asyncio.create_task(runtime, name=f"job:{job.job_id}")
                self._tasks[job.job_id] = task
                task.add_done_callback(
                    lambda finished, job_id=job.job_id: self._task_done(
                        job_id, finished
                    )
                )
            except BaseException:
                # 先摘下预留：task 若已创建，其 done 回调不能把它当作已接受 Job
                # 的预留去释放（不退款）。
                reservations = self._admissions.pop(job.job_id, ())
                if task is None:
                    runtime.close()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self._tasks.pop(job.job_id, None)
                await self._orphan(job.job_id)
                self._original_references.pop(job.job_id, None)
                # Job 从未运行，按 submit 失败口径退款。
                await self._rollback_admissions(reservations)
                done.set()
                # 该 Job 已触发过 job_accepted，孤儿终态同样要通知观察者。
                await self._deliver_failure(
                    job.job_id, self._deliveries.pop(job.job_id, None)
                )
                await self._notify_job_finished(job.job_id)
                raise
            self._ensure_heartbeat()
            return JobHandle(job.job_id, job.state)

    @staticmethod
    def _validate_batch(
        children: Sequence[GenerationRequest], concurrency: int
    ) -> None:
        if not children:
            raise InvalidRequestError("批量任务至少需要一个子请求")
        if type(concurrency) is not int or concurrency < 1:
            raise InvalidRequestError("concurrency 必须是正整数")
        for child in children:
            if not isinstance(child, GenerationRequest):
                raise InvalidRequestError("子请求必须是 GenerationRequest")
            seconds = child.metadata.get("deadline_seconds")
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds)
                or seconds <= 0
            ):
                raise InvalidRequestError(
                    "子请求必须在 metadata.deadline_seconds 声明正数的执行时限"
                )

    async def submit_batch(
        self,
        parent: GenerationRequest,
        children: Sequence[GenerationRequest],
        *,
        concurrency: int,
        admission: Mapping[str, Any] | None = None,
        delivery: Any = None,
    ) -> BatchHandle:
        """提交批量父子 Job：提交时就创建并落库全部子 Job，之后由父 runtime task 调度。

        父 Job 与 ``submit`` 一样先过 quota → limiter 准入（各组件按请求 metadata
        自行判断是否需要预留），不套用默认 deadline（它不执行 provider）。子 Job 的
        ``parent_job_id`` 指向父 Job，提交时不预留任何资源：父 runtime task 在并发名额
        空出时逐个启动它们，启动时才预留 limiter，并把子请求与 Job 的 deadline 一并
        设为“启动时刻 + ``metadata["deadline_seconds"]``”（子请求提交时的
        ``deadline_at`` 会被覆盖）。每个 Job 依次触发 ``job_accepted``（父先、子按
        顺序）。任一步失败都会回滚已拿到的预留，并把已落库的 Job 标为 ORPHANED。

        ``delivery`` 登记在父 Job 上（语义同 ``submit``），可另外实现可选钩子
        ``child_started(parent_job, child_job)`` 与
        ``child_finished(parent_job, child_job, result)``；观察者同样可选实现这两个
        钩子。``result`` 是进程内未脱敏的子结果，子 Job 没有结果时为 None。
        """

        children = tuple(children)
        self._validate_batch(children, concurrency)
        async with self._lock:
            if self._closed:
                raise ServiceClosedError("Scheduler 已关闭")
            reserved: list[tuple[LeaseKind, Any]] = []
            persisted: list[str] = []
            try:
                await self._reserve_admissions(parent, admission, reserved)
                parent_job = await self._persist_queued(parent, persisted)
                child_jobs = [
                    await self._persist_queued(
                        replace(child, parent_job_id=parent_job.job_id),
                        persisted,
                        parent_job_id=parent_job.job_id,
                    )
                    for child in children
                ]
            except BaseException:
                await self._rollback_admissions(reserved)
                await self._orphan_all(persisted)
                raise
            parent_id = parent_job.job_id
            child_ids = tuple(job.job_id for job in child_jobs)
            if reserved:
                self._admissions[parent_id] = tuple(reserved)
            if delivery is not None:
                self._deliveries[parent_id] = delivery
            for job in child_jobs:
                # 子 Job 要到启动时才解析引用，原始引用先随登记保留。
                if job.request.reference_images:
                    self._original_references[job.job_id] = job.request.reference_images
            self._batches[parent_id] = _BatchPlan(child_ids, concurrency, admission)
            done = asyncio.Event()
            self._done[parent_id] = done
            task: asyncio.Task[Any] | None = None
            accepted: list[str] = []
            runtime = self._run(parent_id)
            try:
                for job in (parent_job, *child_jobs):
                    await self._notify_observers("job_accepted", job)
                    accepted.append(job.job_id)
                task = asyncio.create_task(runtime, name=f"job:{parent_id}")
                self._tasks[parent_id] = task
                task.add_done_callback(
                    lambda finished, job_id=parent_id: self._task_done(job_id, finished)
                )
            except BaseException:
                reservations = self._admissions.pop(parent_id, ())
                if task is None:
                    runtime.close()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self._tasks.pop(parent_id, None)
                self._batches.pop(parent_id, None)
                for job_id in child_ids:
                    self._original_references.pop(job_id, None)
                await self._orphan_all((parent_id, *child_ids))
                # 批量从未运行，按 submit 失败口径退款。
                await self._rollback_admissions(reservations)
                done.set()
                await self._deliver_failure(
                    parent_id, self._deliveries.pop(parent_id, None)
                )
                # 触发过 job_accepted 的 Job，孤儿终态同样要通知观察者。
                for job_id in accepted:
                    await self._notify_job_finished(job_id)
                raise
            self._ensure_heartbeat()
            return BatchHandle(parent_id, child_ids, parent_job.state)

    async def _persist_queued(
        self,
        request: GenerationRequest,
        persisted: list[str],
        *,
        parent_job_id: str | None = None,
    ) -> Job:
        """创建 Job 并落库到 QUEUED；id 先登记，失败回滚时才能覆盖到它。"""

        job = Job.create(
            request, job_id=f"job-{uuid.uuid4().hex}", parent_job_id=parent_job_id
        )
        persisted.append(job.job_id)
        await self.store.create(job)
        job.transition(JobState.QUEUED)
        return await self.store.save(job)

    async def _orphan_all(self, job_ids: Sequence[str]) -> None:
        for job_id in job_ids:
            try:
                await self._orphan(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # 回滚中的清理失败不能遮蔽 submit_batch 的原始异常。
                logger.warning(
                    "[v2] Job %s 标记孤儿失败: %s", job_id, type(error).__name__
                )

    async def _reserve_admissions(
        self,
        request: GenerationRequest,
        admission: Mapping[str, Any] | None,
        reserved: list[tuple[LeaseKind, Any]],
    ) -> None:
        """按 quota、limiter 顺序预留；每成功一个就登记到 reserved，供失败时回滚。"""

        for kind, component in (
            (LeaseKind.QUOTA, self.quota),
            (LeaseKind.LIMITER, self.limiter),
        ):
            if component is None:
                continue
            reservation = await _call(component.reserve, request, admission)
            if reservation is not None:
                reserved.append((kind, reservation))

    async def _rollback_admissions(
        self, reservations: Sequence[tuple[LeaseKind, Any]]
    ) -> None:
        """submit 失败回滚：按预留的逆序退款，组件没有 refund 时退化为 release。"""

        for kind, reservation in reversed(reservations):
            method = getattr(reservation, "refund", None) or getattr(
                reservation, "release", None
            )
            if method is None:
                continue
            try:
                await _call(method)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # 回滚失败不能遮蔽 submit 的原始异常，仅记录。
                logger.warning(
                    "[v2] 准入预留 %s 回滚失败: %s", kind.value, type(error).__name__
                )

    def _ensure_heartbeat(self) -> None:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="job-store-heartbeat"
            )

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self.heartbeat_interval)
                if self._closed:
                    break
                await self.store.heartbeat()
        except asyncio.CancelledError:
            raise
        except BaseException:
            # The next JobStore operation will fence this owner if its heartbeat
            # has been revoked; never create a replacement loop from the loop.
            return

    def attach_delivery(self, job_id: str, delivery: Any) -> bool:
        """给运行中的 Job 登记结果投递方；返回 False 表示未登记，结果由调用方自行处理。

        必须保持同步（不得 await）：_run 在投递判定点同样同步地读取登记表，
        两者之间不会交错，所以 True 保证 Job 结束时 deliver（有结果）或
        deliver_failure（无结果的终态）二者之一被调用一次，False 保证两者都不会被调用。
        Job 未知或已无 runtime task、已登记过、已越过判定点、调度器已关闭时返回 False。
        """

        if (
            self._closed
            or job_id not in self._tasks
            or job_id in self._deliveries
            or job_id in self._delivery_decided
        ):
            return False
        self._deliveries[job_id] = delivery
        return True

    def _task_done(self, job_id: str, task: asyncio.Task[Any]) -> None:
        self._tasks.pop(job_id, None)
        self._original_references.pop(job_id, None)
        # 登记在 task 结束后不会再有人取用；已越过判定点的标记同样随之清理。
        self._deliveries.pop(job_id, None)
        self._delivery_decided.discard(job_id)
        plan = self._batches.pop(job_id, None)
        if plan is not None:
            # 兜底：父 task 没来得及收拢子 Job 就结束（如首次运行前被取消）。
            self._start_batch_abort(job_id, plan)
        # 泄漏防护：task 在 _run 取走准入预留之前就结束（提交即取消、close）时
        # 在此释放；已接受的 Job 不退款。
        self._release_admissions(job_id)
        event = self._done.get(job_id)
        if event is not None:
            event.set()
            self._done.pop(job_id, None)
        if not task.cancelled() and task.exception() is not None:
            # The persisted failed state is the public outcome; task exceptions
            # remain observable through the event loop's normal task callback.
            return

    def _release_admissions(self, job_id: str) -> None:
        """释放仍登记在册的准入预留（不退款）；返回 awaitable 的交给清理 task。"""

        for kind, reservation in self._admissions.pop(job_id, ()):
            release = getattr(reservation, "release", None)
            if release is None:
                continue
            try:
                pending = release()
            except Exception as error:
                logger.warning(
                    "[v2] 准入预留 %s 释放失败: %s", kind.value, type(error).__name__
                )
                continue
            if not inspect.isawaitable(pending):
                continue
            cleanup = asyncio.create_task(
                self._await_release(kind, pending), name=f"admission-release:{job_id}"
            )
            self._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._cleanup_tasks.discard)

    async def _await_release(self, kind: LeaseKind, pending: Any) -> None:
        try:
            await pending
        except Exception as error:
            logger.warning(
                "[v2] 准入预留 %s 释放失败: %s", kind.value, type(error).__name__
            )

    async def _orphan(self, job_id: str) -> None:
        job = await self.store.get(job_id)
        if job is None or job.state in TERMINAL_STATES:
            return
        try:
            job.transition(JobState.ORPHANED)
            await self.store.save(job)
        except (StaleJobError, ValueError):
            return

    async def _acquire(self, component: Any, kind: LeaseKind, job: Job) -> Lease:
        if component is None:
            return Lease(f"noop-{kind.value}-{job.job_id}", kind, job.job_id)
        method = getattr(component, "reserve", None) or getattr(
            component, "acquire", None
        )
        if method is None:
            raise TypeError(f"{kind.value} component 缺少 reserve/acquire")
        argument = job.request
        if kind in {LeaseKind.ARTIFACT, LeaseKind.DELIVERY}:
            argument = job
        elif kind is LeaseKind.REFERENCE:
            argument = job.request.reference_images
        value = await _call(method, argument)
        if isinstance(value, Lease):
            return value
        release = getattr(value, "release", None)
        token = getattr(value, "token", None) or f"{kind.value}-{uuid.uuid4().hex}"
        if release is None:
            release_method = getattr(component, "release", None)
            if release_method is None:
                raise TypeError(f"{kind.value} reservation 缺少 release")

            async def release() -> Any:
                result = release_method(value)
                return await result if inspect.isawaitable(result) else result

        return Lease(token, kind, job.job_id, release)

    @staticmethod
    def _admission_lease(kind: LeaseKind, job: Job, reservation: Any) -> Lease:
        """把 submit 预留包成 Lease；release 直接委托预留对象（幂等）。"""

        token = (
            getattr(reservation, "token", None) or f"{kind.value}-{uuid.uuid4().hex}"
        )
        return Lease(token, kind, job.job_id, getattr(reservation, "release", None))

    async def _deadline_call(self, job: Job, operation: Any, label: str) -> Any:
        if job.deadline_at is None:
            return await _call(operation)
        remaining = (job.deadline_at - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"{label} deadline exceeded")
        return await asyncio.wait_for(_call(operation), timeout=remaining)

    async def _persist_lease(self, job: Job, lease: Lease) -> Lease:
        record = LeaseRecord(
            lease_id=lease.token,
            job_id=job.job_id,
            kind=lease.kind.value
            if isinstance(lease.kind, LeaseKind)
            else str(lease.kind),
            status="active",
            metadata={
                **dict(lease.metadata),
                "owner_id": job.owner_id,
                "fencing_token": job.fencing_token,
            },
        )
        try:
            await self._deadline_call(
                job, lambda: self.store.add_lease(record), "lease persist"
            )
        except BaseException:
            try:
                await lease.release()
            except BaseException:
                pass
            raise
        callback = lease.release_callback

        async def release_and_persist() -> Any:
            try:
                if callback is not None:
                    result = callback()
                    if inspect.isawaitable(result):
                        await result
                await self.store.release_lease(
                    lease.token, job_id=job.job_id, owner_id=job.owner_id
                )
            except BaseException as error:
                await self.store.update_lease(
                    lease.token,
                    job_id=job.job_id,
                    status="release_failed",
                    error=f"{type(error).__name__}: {error}",
                    owner_id=job.owner_id,
                )
                raise

        lease.release_callback = release_and_persist
        return lease

    async def _resolve_references(
        self, job: Job, references: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not references:
            return ()
        if self.reference_store is None or not hasattr(self.reference_store, "resolve"):
            raise RuntimeError("reference resolver 不可用")

        async def resolve_all() -> Any:
            resolved = self.reference_store.resolve(
                references,
                deadline_at=job.deadline_at,
                requester=job.request.requester,
            )
            resolved = await resolved if inspect.isawaitable(resolved) else resolved
            if hasattr(resolved, "__aiter__"):
                return [item async for item in resolved]
            return resolved

        values = await self._deadline_call(job, resolve_all, "reference resolve")
        if not values:
            raise RuntimeError("reference resolver 返回空结果")
        # resolver 产出 Artifact 时取落盘路径，字符串成员保持原样。
        return tuple(
            item.location if hasattr(item, "location") else str(item) for item in values
        )

    async def _run(self, job_id: str, *, notify_finished: bool = True) -> None:
        """运行一个 Job；``notify_finished=False`` 时终态通知交给调用方（批量子 Job）。"""

        leases = LeaseSet()
        # 终态通知只覆盖真正进入运行流程的 Job；恢复时已是终态的直接返回不通知。
        started = False
        try:
            job = await self.store.get(job_id)
            if job is None:
                return
            if job.state in TERMINAL_STATES:
                return
            started = True
            job.owner_id = self.store.owner_id
            job.fencing_token = self.store.fencing_token
            job.transition(JobState.RUNNING)
            job = await self.store.save(job)
            # submit 时预留的准入资源在此转成持久化 lease（审计 + 终态释放）。
            # 先全部入 LeaseSet 再逐个落库：某个落库失败时，其余预留也由
            # finally 的 release_all 释放，不会因已摘出登记表而泄漏。
            admissions = self._admissions.pop(job_id, ())
            admission_leases = [
                leases.add(self._admission_lease(kind, job, reservation))
                for kind, reservation in admissions
            ]
            for lease in admission_leases:
                await self._persist_lease(job, lease)
            batch = self._batches.get(job_id)
            if batch is not None:
                # 批量父 Job 不调用 provider：子 Job 各自运行，汇总后的收尾与普通
                # Job 共用（return 仍会经过下面的 finally）。
                await self._complete_job(
                    job_id, await self._run_batch_children(job_id, batch)
                )
                return
            original_references = self._original_references.get(job_id)
            reference_job = job
            if original_references is not None:
                reference_job = replace(
                    job,
                    request=replace(job.request, reference_images=original_references),
                )
            if job.request.reference_images:
                leases.add(
                    await self._persist_lease(
                        job,
                        await self._deadline_call(
                            job,
                            lambda: self._acquire(
                                self.reference_store,
                                LeaseKind.REFERENCE,
                                reference_job,
                            ),
                            "reference acquire",
                        ),
                    )
                )
            leases.add(
                await self._persist_lease(
                    job,
                    await self._deadline_call(
                        job,
                        lambda: self._acquire(
                            self.artifact_store, LeaseKind.ARTIFACT, job
                        ),
                        "artifact acquire",
                    ),
                )
            )
            leases.add(
                await self._persist_lease(
                    job,
                    await self._deadline_call(
                        job,
                        lambda: self._acquire(
                            self.delivery_store, LeaseKind.DELIVERY, job
                        ),
                        "delivery acquire",
                    ),
                )
            )
            # 登记的原始引用优先；无登记（重启恢复）回退 store 描述符。
            resolved = await self._resolve_references(
                job, original_references or job.request.reference_images
            )
            runtime_job = copy.deepcopy(job)
            runtime_job.request = replace(
                runtime_job.request, reference_images=resolved
            )
            # 观察者的 runtime_scope 只包住 provider 执行这一步（artifact
            # 落库在 scope 之外）；进入/退出失败只记日志，不影响 provider。
            with self._provider_scope_stack(runtime_job, admissions):
                # Router 专属入口：provider contract 无 execute 方法。
                if callable(getattr(self.provider, "execute", None)):
                    # retry 与 deadline 截断由 Router 自身持有；包裹仅作兜底，
                    # 留出余量避免与 Router 在同一 deadline 时刻竞争。
                    net = copy.copy(runtime_job)
                    if net.deadline_at is not None:
                        net.deadline_at = net.deadline_at + timedelta(
                            seconds=_ROUTER_EXECUTE_GRACE_SECONDS
                        )
                    result = await self._deadline_call(
                        net,
                        lambda: self.provider.execute(
                            runtime_job.request, deadline_at=runtime_job.deadline_at
                        ),
                        "provider execute",
                    )
                else:
                    result = await dispatch_provider(
                        self.provider,
                        runtime_job,
                        max_attempts=self.max_attempts,
                        backoff=self.retry_backoff,
                    )
            if self.artifact_store is not None and hasattr(self.artifact_store, "save"):
                method = self.artifact_store.save
                parameters = inspect.signature(method).parameters
                if "job_id" in parameters:

                    def save_operation() -> Any:
                        return method(result, job_id=job.job_id)
                else:

                    def save_operation() -> Any:
                        return method(result, job)

                saved = await self._deadline_call(job, save_operation, "artifact save")
                if isinstance(saved, GenerationResult):
                    result = saved
            await self._complete_job(job_id, result)
        except asyncio.CancelledError:
            job = await self.store.get(job_id)
            if job is not None and job.state not in TERMINAL_STATES:
                try:
                    job.request_cancel()
                    job.transition(JobState.CANCELLED)
                    await self.store.save(job)
                except (StaleJobError, ValueError):
                    pass
            raise
        except StaleJobError:
            # Another owner won the CAS; never overwrite its newer result.
            return
        except BaseException as error:
            job = await self.store.get(job_id)
            if job is not None and job.state not in TERMINAL_STATES:
                try:
                    await self.store.append_event(
                        JobEvent(
                            event_id=f"event-{uuid.uuid4().hex}",
                            job_id=job_id,
                            event_type="job_runtime_error",
                            state=JobState.FAILED,
                            data={"error_type": type(error).__name__},
                        )
                    )
                    # CoreError 携带公开 code，其余异常回退异常类名。
                    code = getattr(error, "code", None) or type(error).__name__
                    message = str(error)
                    # Router 聚合错误的原因链更具体（LegacyProviderError 的
                    # code/message）；deadline_exceeded 保留聚合码。
                    if (
                        isinstance(error, ProviderRouteError)
                        and code != "deadline_exceeded"
                        and getattr(error.__cause__, "code", None)
                    ):
                        code = getattr(error.__cause__, "code")
                        message = str(error.__cause__)
                    job.set_error(str(code), message)
                    job.transition(JobState.FAILED)
                    await self.store.save(job)
                except (StaleJobError, ValueError):
                    pass
        finally:
            # 批量父 Job 先收拢仍未收尾的子 Job（子在前、父在后）；非批量 Job 即刻返回。
            await self._settle_batch_children(job_id)
            # 进入收尾后不再接受投递登记；仍在册的投递方此刻取出，稍后兜底通知。
            pending_delivery = self._deliveries.pop(job_id, None)
            self._delivery_decided.add(job_id)
            cleanup = asyncio.create_task(
                leases.release_all(), name=f"cleanup:{job_id}"
            )
            self._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._cleanup_tasks.discard)
            try:
                await asyncio.wait_for(
                    asyncio.shield(cleanup), timeout=self.cleanup_timeout
                )
            except BaseException:
                # Lease records retain release_failed/active state for recovery.
                pass
            # lease 清理完成后先通知投递方、再通知终态；取消路径此处 await 仍可
            # 执行，任务只收到过一次 cancel 交付，新的 await 不会被再次打断。
            try:
                await self._deliver_failure(job_id, pending_delivery)
            finally:
                if started and notify_finished:
                    await self._notify_job_finished(job_id)

    async def _complete_job(self, job_id: str, result: GenerationResult) -> None:
        """有结果的 Job 收尾：RESULT_READY → job_result 观察者 → 投递判定 → 终态。"""

        job = await self.store.get(job_id)
        if job is None:
            return
        job.set_result(result)
        job.transition(JobState.RESULT_READY)
        job = await self.store.save(job)
        await self._notify_observers("job_result", job, result)
        # 投递判定点：读取登记表与标记已判定之间没有 await，和同步的
        # attach_delivery 不会交错；此后的登记一律被拒绝。
        delivery = self._deliveries.get(job_id)
        self._delivery_decided.add(job_id)
        if delivery is None:
            job.transition(JobState.PARTIAL if result.partial else JobState.SUCCEEDED)
            await self.store.save(job)
        else:
            job.transition(JobState.DELIVERY_PENDING)
            job = await self.store.save(job)
            # 交给 deliver 之后的失败由它自己负责，不再走 deliver_failure 兜底；
            # 落库 DELIVERY_PENDING 期间被取消则仍留在登记表里由兜底通知。
            self._deliveries.pop(job_id, None)
            await self._deliver_result(job, result, delivery)
        # 终态落库成功后才登记：并发赢家、失败、取消路径不登记，
        # 避免 wait() 读回的结果遮蔽 store 里的最终结果。
        self._remember_runtime_result(job_id, result)

    # ===== 批量父子 Job =====

    async def _run_batch_children(
        self, parent_id: str, plan: _BatchPlan
    ) -> GenerationResult:
        """父 runtime task 的主体：按并发上限依次启动子 Job，全部收尾后汇总父结果。

        被取消或失败时不在这里善后：仍在运行或未启动的子 Job 由 ``_run`` 的 finally
        统一收拢（见 ``_settle_batch_children``）。
        """

        running: dict[asyncio.Task[Any], str] = {}
        for child_id in plan.child_ids:
            while len(running) >= plan.concurrency:
                await self._reap_batch_children(parent_id, plan, running)
            task = await self._start_batch_child(parent_id, plan, child_id)
            if task is not None:
                running[task] = child_id
        while running:
            await self._reap_batch_children(parent_id, plan, running)
        return self._aggregate_batch(plan)

    async def _reap_batch_children(
        self,
        parent_id: str,
        plan: _BatchPlan,
        running: dict[asyncio.Task[Any], str],
    ) -> None:
        done, _ = await asyncio.wait(
            tuple(running), return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            await self._settle_child(parent_id, plan, running.pop(task))

    async def _start_batch_child(
        self, parent_id: str, plan: _BatchPlan, child_id: str
    ) -> asyncio.Task[Any] | None:
        """启动一个子 Job；返回它的 runtime task，没能启动（已结束、被限流拒绝）返回 None。"""

        reservation: Any = None
        try:
            child = await self.store.get(child_id)
            if child is None or child.state in TERMINAL_STATES:
                # 提交后、启动前就被单独取消的子 Job：只补收尾，不再启动。
                await self._settle_child(parent_id, plan, child_id)
                return None
            seconds = float(child.request.metadata["deadline_seconds"])
            # 执行时限从启动时刻起算，晚启动的子 Job 不会因排队而提前超时。
            deadline = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            child.deadline_at = deadline
            child.request = replace(child.request, deadline_at=deadline)
            child = await self.store.save(child)
            if self.limiter is not None:
                reservation = await _call(
                    self.limiter.reserve, child.request, plan.admission
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # 被拒（队列满等）只让这个子 Job 失败，不影响其他子 Job。
            await self._fail_unstarted_child(child_id, error)
            await self._settle_child(parent_id, plan, child_id)
            return None
        # 取得预留到 task 创建之间没有 await：取消不会让预留漏出登记表。
        if reservation is not None:
            self._admissions[child_id] = ((LeaseKind.LIMITER, reservation),)
        self._done[child_id] = asyncio.Event()
        runtime = self._run(child_id, notify_finished=False)
        try:
            task = asyncio.create_task(runtime, name=f"job:{child_id}")
        except BaseException:
            runtime.close()
            self._release_admissions(child_id)
            self._done.pop(child_id, None)
            raise
        self._tasks[child_id] = task
        plan.tasks[child_id] = task
        task.add_done_callback(
            lambda finished, job_id=child_id: self._task_done(job_id, finished)
        )
        await self._notify_batch_hook(parent_id, "child_started", child)
        return task

    async def _fail_unstarted_child(self, child_id: str, error: BaseException) -> None:
        """没能启动的子 Job 直接失败；状态机要求先转 RUNNING 才能转 FAILED。"""

        job = await self.store.get(child_id)
        if job is None or job.state in TERMINAL_STATES:
            return
        try:
            job.owner_id = self.store.owner_id
            job.fencing_token = self.store.fencing_token
            job.transition(JobState.RUNNING)
            job = await self.store.save(job)
            await self.store.append_event(
                JobEvent(
                    event_id=f"event-{uuid.uuid4().hex}",
                    job_id=child_id,
                    event_type="job_runtime_error",
                    state=JobState.FAILED,
                    data={"error_type": type(error).__name__},
                )
            )
            job.set_error(
                str(getattr(error, "code", None) or type(error).__name__), str(error)
            )
            job.transition(JobState.FAILED)
            await self.store.save(job)
        except (StaleJobError, ValueError):
            pass

    async def _cancel_stranded_job(self, job: Job) -> None:
        """把没能走完 runtime 流程的非终态 Job 补成 CANCELLED。"""

        try:
            job.request_cancel()
            job.transition(JobState.CANCELLED)
            await self.store.save(job)
        except (StaleJobError, ValueError):
            pass

    async def _settle_child(
        self, parent_id: str, plan: _BatchPlan, child_id: str
    ) -> None:
        """子 Job 的统一收尾，每个子 Job 只做一次：确保终态 → job_finished → child_finished。

        批量子 Job 的 ``job_finished`` 由这里发出（``_run`` 以 notify_finished=False
        运行），所以运行过的、被限流拒绝的、从未启动的子 Job 通知口径一致。
        """

        if child_id in plan.claimed:
            return
        child: Job | None = None
        try:
            child = await self.store.get(child_id)
            if child is not None and child.state not in TERMINAL_STATES:
                await self._cancel_stranded_job(child)
                child = await self.store.get(child_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "[v2] 批量子 Job %s 收尾读取失败: %s", child_id, type(error).__name__
            )
        if child_id in plan.claimed:
            return
        plan.claimed.add(child_id)
        # 没跑过的子 Job 没有 task 的 done 回调来清理这条登记。
        self._original_references.pop(child_id, None)
        result: GenerationResult | None = None
        if child is not None and child.state in (JobState.SUCCEEDED, JobState.PARTIAL):
            result = self._runtime_results.get(child_id)
        plan.outcomes[child_id] = _ChildOutcome(
            job_id=child_id,
            state=child.state if child is not None else None,
            requested_images=child.request.image_count if child is not None else 0,
            artifacts=result.artifacts if result is not None else (),
            error_code=child.error_code if child is not None else None,
        )
        if child is None:
            return
        await self._notify_job_finished(child_id)
        await self._notify_batch_hook(parent_id, "child_finished", child, result)

    async def _notify_batch_hook(
        self, parent_id: str, hook: str, child: Job, *extra: Any
    ) -> None:
        """child_started / child_finished：先通知观察者，再通知父 Job 的投递方。

        钩子都是可选的；异常和超时只记日志，不影响任何 Job 的状态。
        """

        try:
            parent = await self.store.get(parent_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "[v2] 批量钩子 %s 读取父 Job 失败: %s", hook, type(error).__name__
            )
            return
        if parent is None:
            return
        await self._notify_observers(hook, parent, child, *extra)
        delivery = self._deliveries.get(parent_id)
        method = getattr(delivery, hook, None)
        if not callable(method):
            return
        try:
            await asyncio.wait_for(
                _call(method, parent, child, *extra), timeout=self.delivery_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "[v2] 投递方 %s 的 %s 调用失败: %s",
                type(delivery).__name__,
                hook,
                type(error).__name__,
            )

    @staticmethod
    def _aggregate_batch(plan: _BatchPlan) -> GenerationResult:
        """汇总父结果：子 artifacts 按子顺序拼接；没有任何产出则整体失败。"""

        artifacts: list[str] = []
        children: list[dict[str, Any]] = []
        partial = False
        for child_id in plan.child_ids:
            outcome = plan.outcomes[child_id]
            artifacts.extend(outcome.artifacts)
            if (
                outcome.state is not JobState.SUCCEEDED
                or len(outcome.artifacts) < outcome.requested_images
            ):
                partial = True
            children.append(
                {
                    "job_id": child_id,
                    "state": outcome.state.value if outcome.state else None,
                    "requested_images": outcome.requested_images,
                    "generated_images": len(outcome.artifacts),
                    "error_code": outcome.error_code,
                }
            )
        if not artifacts:
            raise BatchAllFailedError("所有批量任务均生成失败")
        if len(artifacts) > MAX_ARTIFACTS:
            artifacts = artifacts[:MAX_ARTIFACTS]
            partial = True
        return GenerationResult(
            artifacts=tuple(artifacts), partial=partial, stats={"children": children}
        )

    def _start_batch_abort(
        self, parent_id: str, plan: _BatchPlan
    ) -> asyncio.Task[Any] | None:
        """还有子 Job 没收尾时，用调度器自有的 task 收拢它们；每个批量只启动一次。"""

        if plan.aborting or len(plan.claimed) >= len(plan.child_ids):
            return None
        plan.aborting = True
        cleanup = asyncio.create_task(
            self._abort_batch(parent_id, plan), name=f"batch-abort:{parent_id}"
        )
        self._cleanup_tasks.add(cleanup)
        cleanup.add_done_callback(self._cleanup_tasks.discard)
        return cleanup

    async def _abort_batch(self, parent_id: str, plan: _BatchPlan) -> None:
        """取消仍在运行的子 Job，并让所有未收尾的子 Job 收尾（未启动的直接转 CANCELLED）。"""

        try:
            running = [
                task
                for child_id, task in plan.tasks.items()
                if child_id not in plan.claimed and not task.done()
            ]
            for task in running:
                # close() 已取消的 task 正在走自己的取消收尾，再取消一次会打断它。
                if task.cancelling() == 0:
                    task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            for child_id in plan.child_ids:
                await self._settle_child(parent_id, plan, child_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "[v2] 批量 %s 收拢子 Job 失败: %s", parent_id, type(error).__name__
            )

    async def _settle_batch_children(self, parent_id: str) -> None:
        """_run 的 finally 调用：父 Job 收尾前先收拢子 Job；非批量 Job 即刻返回。"""

        plan = self._batches.get(parent_id)
        if plan is None:
            return
        cleanup = self._start_batch_abort(parent_id, plan)
        if cleanup is None:
            return
        try:
            await asyncio.shield(cleanup)
        except BaseException:
            # 与 lease 清理同口径：收拢由独立 task 完成，二次取消不会打断它，
            # close() 会等待它结束。
            pass

    async def _deliver_result(
        self, job: Job, result: GenerationResult, delivery: Any
    ) -> None:
        """投递阶段：Job 此时是 DELIVERY_PENDING，结果在任何出口都保留。

        deliver 成功按 result.partial 收尾；抛出普通异常或超时则以 PARTIAL +
        delivery_failed 收尾。CancelledError 照常传播，由 _run 的取消分支收尾。
        """

        try:
            await asyncio.wait_for(
                _call(delivery.deliver, job, result), timeout=self.delivery_timeout
            )
        except Exception as error:
            logger.warning("[v2] Job %s 投递失败: %s", job.job_id, type(error).__name__)
            job.set_error(
                "delivery_failed", _delivery_error_text(error, self.delivery_timeout)
            )
            job.transition(JobState.PARTIAL)
        else:
            job.transition(JobState.PARTIAL if result.partial else JobState.SUCCEEDED)
        await self.store.save(job)

    async def _deliver_failure(self, job_id: str, delivery: Any) -> None:
        """Job 无结果结束且投递方尚未交接时通知其失败；异常/超时只记日志。"""

        if delivery is None:
            return
        try:
            job = await self.store.get(job_id)
        except Exception as error:
            logger.warning("[v2] 投递失败通知读取 Job 失败: %s", type(error).__name__)
            return
        if job is None or job.state not in _NO_RESULT_STATES:
            return
        try:
            await asyncio.wait_for(
                _call(delivery.deliver_failure, job), timeout=self.delivery_timeout
            )
        except Exception as error:
            logger.warning(
                "[v2] 投递方 %s 的 deliver_failure 调用失败: %s",
                type(delivery).__name__,
                type(error).__name__,
            )

    async def _notify_observers(self, hook: str, *args: Any) -> None:
        """逐观察者调用可选钩子；异常/超时只记日志，CancelledError 照常传播。"""

        for observer in self.observers:
            method = getattr(observer, hook, None)
            if not callable(method):
                continue
            name = type(observer).__name__
            try:
                await asyncio.wait_for(
                    _call(method, *args), timeout=self.observer_timeout
                )
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                logger.warning(
                    "[v2] 观察者 %s 钩子 %s 调用失败: %s",
                    name,
                    hook,
                    type(error).__name__,
                )

    async def _notify_job_finished(self, job_id: str) -> None:
        """Job 处于终态时通知观察者；非终态（异常中断前）不通知。"""

        try:
            job = await self.store.get(job_id)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            logger.warning("[v2] 终态通知读取 Job 失败: %s", type(error).__name__)
            return
        if job is None or job.state not in TERMINAL_STATES:
            return
        await self._notify_observers("job_finished", job)

    def _provider_scope_stack(
        self, job: Job, admissions: Sequence[tuple[LeaseKind, Any]] = ()
    ) -> contextlib.ExitStack:
        """依次进入准入预留与观察者的 runtime_scope；失败只记日志并跳过。"""

        stack = contextlib.ExitStack()
        # 预留的 runtime_scope() 不带参数，观察者的 runtime_scope(job) 带 Job。
        owners = [("准入预留", reservation, ()) for _kind, reservation in admissions]
        owners.extend(("观察者", observer, (job,)) for observer in self.observers)
        for role, owner, args in owners:
            factory = getattr(owner, "runtime_scope", None)
            if not callable(factory):
                continue
            name = f"{role} {type(owner).__name__}"
            try:
                stack.enter_context(_GuardedObserverScope(factory(*args), name))
            except Exception as error:
                logger.warning(
                    "[v2] %s runtime_scope 进入失败，跳过: %s",
                    name,
                    type(error).__name__,
                )
        return stack

    def result_wait_timeout(self, deadline_seconds: float) -> float:
        """入口等待上限：deadline 之外再容纳 job_result/job_finished 两个
        观察者钩子（各自最长 observer_timeout）与 provider 兜底余量。"""

        return (
            deadline_seconds
            + _ROUTER_EXECUTE_GRACE_SECONDS
            + 2 * self.observer_timeout
            + 10
        )

    def _remember_runtime_result(self, job_id: str, result: GenerationResult) -> None:
        """登记未脱敏运行时结果供 wait() 读回；超容量按插入顺序淘汰。"""

        self._runtime_results[job_id] = result
        while len(self._runtime_results) > _RUNTIME_RESULT_CAPACITY:
            self._runtime_results.popitem(last=False)

    async def wait(self, job_id: str, timeout: float | None = None) -> JobResult:
        job = await self.store.get(job_id)
        if job is None:
            raise JobNotFoundError(f"Job 不存在: {job_id}")
        # 成功终态已提交但 runtime task 未收尾时，运行时结果登记可能尚未
        # 发生：与非终态一样先等任务结束，避免直接返回脱敏结果。
        pending_registration = (
            job.state in (JobState.SUCCEEDED, JobState.PARTIAL)
            and job_id in self._tasks
        )
        if job.state not in TERMINAL_STATES or pending_registration:
            if job_id not in self._tasks:
                raise RecoveryRequiredError(
                    f"Job {job_id} 没有 runtime task，请先恢复 Scheduler"
                )
            event = self._done.setdefault(job_id, asyncio.Event())
            if timeout is None:
                await event.wait()
            else:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            job = await self.store.get(job_id)
            if job is None:
                raise JobNotFoundError(f"Job 不存在: {job_id}")
        result = copy.deepcopy(job.result)
        if (
            job.state in (JobState.SUCCEEDED, JobState.PARTIAL)
            and job_id in self._runtime_results
        ):
            # 进程内登记的运行时原文优先；条目淘汰或重启恢复回退脱敏结果。
            result = copy.deepcopy(self._runtime_results[job_id])
        return JobResult(
            job.job_id,
            job.state,
            result,
            job.error_code,
            job.error_message,
        )

    async def cancel(self, job_id: str) -> JobSnapshot:
        job = await self.store.get(job_id)
        if job is None:
            raise JobNotFoundError(f"Job 不存在: {job_id}")
        task = self._tasks.get(job_id)
        if job.state not in TERMINAL_STATES:
            job.request_cancel()
            try:
                job.transition(JobState.CANCELLED)
                job = await self.store.save(job)
            except StaleJobError:
                job = await self.store.get(job_id)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        job = await self.store.get(job_id)
        if job is None:
            raise JobNotFoundError(f"Job 不存在: {job_id}")
        return JobSnapshot.from_job(job)

    async def get_status(self, job_id: str) -> JobSnapshot | None:
        job = await self.store.get(job_id)
        return JobSnapshot.from_job(job) if job is not None else None

    async def recover(self) -> list[Job]:
        """Expose JobStore owner recovery snapshots without replaying them."""
        return await recoverable_jobs(self.store)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = [task for task in self._tasks.values() if not task.done()]
            self._original_references.clear()
            self._runtime_results.clear()
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # 被取消 task 的投递登记已由 _task_done 清理，这里兜住没有 task 的残留。
        self._deliveries.clear()
        self._delivery_decided.clear()
        self._batches.clear()
        # 被取消 task 的预留已由 _task_done 释放；这里保证关闭后登记表为空，
        # 返回 awaitable 的释放进入 _cleanup_tasks，由下面统一等待。
        for job_id in list(self._admissions):
            self._release_admissions(job_id)
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
        if self._cleanup_tasks:
            cleanup = asyncio.gather(
                *tuple(self._cleanup_tasks), return_exceptions=True
            )
            try:
                await asyncio.wait_for(
                    asyncio.shield(cleanup), timeout=self.cleanup_timeout
                )
            except asyncio.TimeoutError:
                for task in tuple(self._cleanup_tasks):
                    task.cancel()
                await asyncio.gather(
                    *tuple(self._cleanup_tasks), return_exceptions=True
                )
        await self.store.close()
