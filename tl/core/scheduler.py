"""Minimal, isolated JobScheduler foundation.

This module is intentionally not wired into commands, tools, WebUI or SDK.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from .dispatch import dispatch_provider
from .errors import (
    JobNotFoundError,
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


async def _call(value: Any, *args: Any, **kwargs: Any) -> Any:
    result = value(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


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
        self._closed = False
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._cleanup_tasks: set[asyncio.Task[Any]] = set()

    async def submit(self, request: GenerationRequest) -> JobHandle:
        async with self._lock:
            if self._closed:
                raise ServiceClosedError("Scheduler 已关闭")
            if request.deadline_at is None:
                request = replace(
                    request,
                    deadline_at=datetime.now(timezone.utc)
                    + timedelta(seconds=self.default_timeout),
                )
            job = Job.create(request, job_id=f"job-{uuid.uuid4().hex}")
            # Persistence is deliberately before create_task.
            await self.store.create(job)
            job.transition(JobState.QUEUED)
            job = await self.store.save(job)
            done = asyncio.Event()
            self._done[job.job_id] = done
            task: asyncio.Task[Any] | None = None
            runtime = self._run(job.job_id)
            try:
                task = asyncio.create_task(runtime, name=f"job:{job.job_id}")
                self._tasks[job.job_id] = task
                task.add_done_callback(
                    lambda finished, job_id=job.job_id: self._task_done(
                        job_id, finished
                    )
                )
            except BaseException:
                if task is None:
                    runtime.close()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self._tasks.pop(job.job_id, None)
                await self._orphan(job.job_id)
                done.set()
                raise
            self._ensure_heartbeat()
            return JobHandle(job.job_id, job.state)

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

    def _task_done(self, job_id: str, task: asyncio.Task[Any]) -> None:
        self._tasks.pop(job_id, None)
        event = self._done.get(job_id)
        if event is not None:
            event.set()
            self._done.pop(job_id, None)
        if not task.cancelled() and task.exception() is not None:
            # The persisted failed state is the public outcome; task exceptions
            # remain observable through the event loop's normal task callback.
            return

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

    async def _resolve_references(self, job: Job) -> tuple[str, ...]:
        if not job.request.reference_images:
            return ()
        if self.reference_store is None or not hasattr(self.reference_store, "resolve"):
            raise RuntimeError("reference resolver 不可用")

        async def resolve_all() -> Any:
            resolved = self.reference_store.resolve(job.request.reference_images)
            resolved = await resolved if inspect.isawaitable(resolved) else resolved
            if hasattr(resolved, "__aiter__"):
                return [item async for item in resolved]
            return resolved

        values = await self._deadline_call(job, resolve_all, "reference resolve")
        if not values:
            raise RuntimeError("reference resolver 返回空结果")
        return tuple(str(item) for item in values)

    async def _run(self, job_id: str) -> None:
        leases = LeaseSet()
        try:
            job = await self.store.get(job_id)
            if job is None:
                return
            if job.state in TERMINAL_STATES:
                return
            job.owner_id = self.store.owner_id
            job.fencing_token = self.store.fencing_token
            job.transition(JobState.RUNNING)
            job = await self.store.save(job)
            for component, kind in (
                (self.quota, LeaseKind.QUOTA),
                (self.limiter, LeaseKind.LIMITER),
            ):
                leases.add(
                    await self._persist_lease(
                        job,
                        await self._deadline_call(
                            job,
                            lambda component=component, kind=kind: self._acquire(
                                component, kind, job
                            ),
                            f"{kind.value} acquire",
                        ),
                    )
                )
            if job.request.reference_images:
                leases.add(
                    await self._persist_lease(
                        job,
                        await self._deadline_call(
                            job,
                            lambda: self._acquire(
                                self.reference_store, LeaseKind.REFERENCE, job
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
            resolved = await self._resolve_references(job)
            runtime_job = copy.deepcopy(job)
            runtime_job.request = replace(
                runtime_job.request, reference_images=resolved
            )
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
            job = await self.store.get(job_id)
            if job is None:
                return
            job.set_result(result)
            job.transition(JobState.RESULT_READY)
            job = await self.store.save(job)
            job.transition(JobState.PARTIAL if result.partial else JobState.SUCCEEDED)
            await self.store.save(job)
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
                    job.set_error(type(error).__name__, str(error))
                    job.transition(JobState.FAILED)
                    await self.store.save(job)
                except (StaleJobError, ValueError):
                    pass
        finally:
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

    async def wait(self, job_id: str, timeout: float | None = None) -> JobResult:
        job = await self.store.get(job_id)
        if job is None:
            raise JobNotFoundError(f"Job 不存在: {job_id}")
        if job.state not in TERMINAL_STATES:
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
        return JobResult(
            job.job_id,
            job.state,
            copy.deepcopy(job.result),
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
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
