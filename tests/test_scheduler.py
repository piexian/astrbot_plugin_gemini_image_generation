from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tl.core.dispatch import ProviderDispatchError
from tl.core.job_store import SQLiteJobStore
from tl.core.models import Job
from tl.core.requests import GenerationRequest
from tl.core.results import GenerationResult
from tl.core.scheduler import JobScheduler
from tl.core.states import JobState


class FakeProvider:
    def __init__(self, *, failures: int = 0, block: asyncio.Event | None = None):
        self.failures = failures
        self.attempts = 0
        self.started = asyncio.Event()
        self.block = block

    async def supports(self, request):
        return True

    async def normalize(self, request):
        return request

    async def build_request(self, request, attempt):
        self.attempts += 1
        self.started.set()
        if self.block is not None:
            await self.block.wait()
        return "https://provider.test/generate", {}, lambda: {"attempt": attempt.number}

    async def send(self, request, attempt, *, url, headers, body_factory):
        if self.failures:
            self.failures -= 1
            raise ProviderDispatchError("upstream 5xx", status_code=503)
        return {"attempt": attempt.number}

    async def parse_response(self, response, *, request, attempt):
        return GenerationResult(artifacts=(f"artifact-{attempt.number}",))


class FakeReservation:
    def __init__(self, *, release_gate: asyncio.Event | None = None):
        self.releases = 0
        self.release_gate = release_gate

    async def release(self):
        if self.release_gate is not None:
            await self.release_gate.wait()
        self.releases += 1


class FakeLeaseSource:
    def __init__(self, *, release_gate: asyncio.Event | None = None):
        self.reservations: list[FakeReservation] = []
        self.release_gate = release_gate

    async def reserve(self, request):
        reservation = FakeReservation(release_gate=self.release_gate)
        self.reservations.append(reservation)
        return reservation

    async def resolve(self, references):
        return tuple(f"resolved:{item}" for item in references)


def request(*, timeout: float = 2.0, references: tuple[str, ...] = ()):
    return GenerationRequest(
        prompt="draw",
        source="test",
        reference_images=references,
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=timeout),
    )


def make_scheduler(tmp_path, provider=None, **kwargs):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    scheduler = JobScheduler(store, provider=provider or FakeProvider(), **kwargs)
    return scheduler, store


@pytest.mark.asyncio
async def test_submit_persists_before_runtime_task(tmp_path):
    provider = FakeProvider()
    scheduler, store = make_scheduler(tmp_path, provider)
    try:
        handle = await scheduler.submit(request())
        assert await store.get(handle.job_id) is not None
        await provider.started.wait()
        assert (await store.get(handle.job_id)).state in {
            JobState.RUNNING,
            JobState.RESULT_READY,
            JobState.SUCCEEDED,
        }
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_create_attach_failure_has_no_orphan_task(tmp_path, monkeypatch):
    provider = FakeProvider()
    scheduler, store = make_scheduler(tmp_path, provider)
    try:

        async def fail_create(job):
            raise RuntimeError("create failed")

        monkeypatch.setattr(store, "create", fail_create)
        with pytest.raises(RuntimeError):
            await scheduler.submit(request())
        assert scheduler._tasks == {}
    finally:
        await scheduler.close()


def scheduler_module_create_task():
    import tl.core.scheduler as module

    return module.asyncio, module.asyncio.create_task


@pytest.mark.asyncio
async def test_attach_failure_has_no_orphan_task(tmp_path, monkeypatch):
    scheduler, store = make_scheduler(tmp_path)
    module, original = scheduler_module_create_task()

    def fail_create_task(*args, **kwargs):
        raise RuntimeError("attach failed")

    monkeypatch.setattr(module, "create_task", fail_create_task)
    try:
        with pytest.raises(RuntimeError):
            await scheduler.submit(request())
        assert scheduler._tasks == {}
        snapshot = await scheduler.get_status(next(iter(scheduler._done), "missing"))
        assert snapshot is None or snapshot.state is JobState.ORPHANED
    finally:
        monkeypatch.setattr(module, "create_task", original)
        await scheduler.close()


@pytest.mark.asyncio
async def test_cancel_waits_for_cleanup(tmp_path):
    block = asyncio.Event()
    provider = FakeProvider(block=block)
    gate = asyncio.Event()
    quota = FakeLeaseSource(release_gate=gate)
    scheduler, store = make_scheduler(tmp_path, provider, quota=quota)
    try:
        handle = await scheduler.submit(request())
        await provider.started.wait()
        cancelling = asyncio.create_task(scheduler.cancel(handle.job_id))
        await asyncio.sleep(0)
        assert not cancelling.done()
        gate.set()
        snapshot = await cancelling
        assert snapshot.state is JobState.CANCELLED
        assert quota.reservations[0].releases == 1
    finally:
        block.set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_close_rejects_new_jobs(tmp_path):
    scheduler, _ = make_scheduler(tmp_path)
    await scheduler.close()
    with pytest.raises(Exception, match="关闭"):
        await scheduler.submit(request())


@pytest.mark.asyncio
async def test_provider_5xx_retries_by_deadline(tmp_path):
    provider = FakeProvider(failures=2)
    scheduler, _ = make_scheduler(tmp_path, provider, retry_backoff=0.01)
    try:
        result = await scheduler.wait((await scheduler.submit(request())).job_id)
        assert result.state is JobState.SUCCEEDED
        assert provider.attempts == 3
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_retry_does_not_exceed_deadline(tmp_path):
    provider = FakeProvider(failures=100)
    scheduler, _ = make_scheduler(tmp_path, provider, retry_backoff=0.05)
    try:
        result = await scheduler.wait(
            (await scheduler.submit(request(timeout=0.03))).job_id
        )
        assert result.state is JobState.FAILED
        assert provider.attempts <= 2
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_all_resource_leases_are_released(tmp_path):
    provider = FakeProvider()
    sources = [FakeLeaseSource() for _ in range(5)]
    scheduler, _ = make_scheduler(
        tmp_path,
        provider,
        quota=sources[0],
        limiter=sources[1],
        reference_store=sources[2],
        artifact_store=sources[3],
        delivery_store=sources[4],
    )
    try:
        result = await scheduler.wait(
            (await scheduler.submit(request(references=("reference-1",)))).job_id
        )
        assert result.state is JobState.SUCCEEDED
        assert all(source.reservations[0].releases == 1 for source in sources)
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_restart_recovery_status_is_observable(tmp_path):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job(
        job_id="restart-job",
        request=request(),
        state=JobState.RUNNING,
    )
    await store.create(job)
    await store.close()
    restored_store = SQLiteJobStore(tmp_path, import_legacy=False)
    scheduler = JobScheduler(restored_store, provider=FakeProvider())
    try:
        snapshot = await scheduler.get_status(job.job_id)
        assert snapshot is not None
        assert snapshot.state is JobState.INTERRUPTED
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_stale_worker_cannot_overwrite_new_result(tmp_path):
    block = asyncio.Event()
    provider = FakeProvider(block=block)
    scheduler, store = make_scheduler(tmp_path, provider)
    try:
        handle = await scheduler.submit(request())
        await provider.started.wait()
        winner = await store.get(handle.job_id)
        winner.set_result(GenerationResult(artifacts=("winner",)))
        winner.transition(JobState.RESULT_READY)
        await store.save(winner)
        winner.transition(JobState.SUCCEEDED)
        await store.save(winner)
        block.set()
        result = await scheduler.wait(handle.job_id)
        assert result.result is not None
        assert result.result.artifacts == ("winner",)
    finally:
        block.set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_heartbeat_keeps_long_provider_owner_live(tmp_path):
    block = asyncio.Event()
    provider = FakeProvider(block=block)
    scheduler, store = make_scheduler(
        tmp_path,
        provider,
        heartbeat_interval=0.02,
    )
    second = None
    try:
        handle = await scheduler.submit(request(timeout=1.0))
        await provider.started.wait()
        await asyncio.sleep(0.08)
        second = SQLiteJobStore(tmp_path, import_legacy=False, stale_owner_timeout=0.05)
        snapshot = await second.get(handle.job_id)
        assert snapshot is not None
        assert snapshot.state is JobState.RUNNING
    finally:
        block.set()
        if second is not None:
            await second.close()
        await scheduler.close()
