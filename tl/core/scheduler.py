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
from .errors import JobNotFoundError, ServiceClosedError, StaleJobError
from .leases import Lease, LeaseKind, LeaseSet
from .models import Job
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
        self._closed = False
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

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
            return JobHandle(job.job_id, job.state)

    def _task_done(self, job_id: str, task: asyncio.Task[Any]) -> None:
        self._tasks.pop(job_id, None)
        event = self._done.get(job_id)
        if event is not None:
            event.set()
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
        try:
            value = await _call(method, argument)
        except TypeError as first_error:
            fallback = job if argument is not job else job.request
            try:
                value = await _call(method, fallback)
            except TypeError:
                raise first_error
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

    async def _run(self, job_id: str) -> None:
        leases = LeaseSet()
        try:
            job = await self.store.get(job_id)
            if job is None:
                return
            if job.state in TERMINAL_STATES:
                return
            job.transition(JobState.RUNNING)
            job = await self.store.save(job)
            for component, kind in (
                (self.quota, LeaseKind.QUOTA),
                (self.limiter, LeaseKind.LIMITER),
            ):
                leases.add(await self._acquire(component, kind, job))
            if job.request.reference_images:
                leases.add(
                    await self._acquire(self.reference_store, LeaseKind.REFERENCE, job)
                )
            leases.add(
                await self._acquire(self.artifact_store, LeaseKind.ARTIFACT, job)
            )
            leases.add(
                await self._acquire(self.delivery_store, LeaseKind.DELIVERY, job)
            )
            result = await dispatch_provider(
                self.provider,
                job,
                max_attempts=self.max_attempts,
                backoff=self.retry_backoff,
            )
            if self.artifact_store is not None and hasattr(self.artifact_store, "save"):
                try:
                    saved = await _call(self.artifact_store.save, result, job)
                except TypeError:
                    saved = await _call(
                        self.artifact_store.save, result, job_id=job.job_id
                    )
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
                    job.set_error(type(error).__name__, str(error))
                    job.transition(JobState.FAILED)
                    await self.store.save(job)
                except (StaleJobError, ValueError):
                    pass
        finally:
            try:
                await leases.release_all()
            except BaseException:
                # Cleanup failure remains visible on each Lease; do not revive
                # or overwrite a newer Job result while reporting it.
                pass

    async def wait(self, job_id: str, timeout: float | None = None) -> JobResult:
        job = await self.store.get(job_id)
        if job is None:
            raise JobNotFoundError(f"Job 不存在: {job_id}")
        if job.state not in TERMINAL_STATES:
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
        await self.store.close()
