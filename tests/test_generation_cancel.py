"""Generation-only stop races against real SQLite writes; no external calls."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tl import background_notify
from tl.core.errors import JobNotFoundError, StaleJobError
from tl.core.job_store import SQLiteJobStore
from tl.core.models import Job
from tl.core.requests import GenerationRequest
from tl.core.results import GenerationResult
from tl.core.scheduler import JobScheduler
from tl.core.service import GenerationService
from tl.core.states import TERMINAL_STATES, JobState


class GatedProvider:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def execute(self, request, *, deadline_at):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return GenerationResult(artifacts=("synthetic-image",))


class ResultGate:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def hold(self):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def job_result(self, job, result):
        await self.hold()

    async def deliver(self, job, result):
        await self.hold()


def make_service(tmp_path, **kwargs):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    provider = GatedProvider()
    scheduler = JobScheduler(store, provider=provider, **kwargs)
    return GenerationService(scheduler), scheduler, store, provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", [JobState.RESULT_READY, JobState.DELIVERY_PENDING, *TERMINAL_STATES]
)
async def test_generation_cancel_preserves_ready_and_terminal_jobs(tmp_path, state):
    service, scheduler, store, _ = make_service(tmp_path)
    job = Job(
        job_id="preserved",
        request=GenerationRequest(prompt="synthetic", source="test"),
        state=state,
        result=GenerationResult(artifacts=("saved-result",)),
    )
    try:
        await store.create(job)
        snapshot = await service.cancel_generation(job.job_id)
        assert snapshot.state is state
        assert snapshot.revision == 0
        assert snapshot.cancel_requested is False
        assert snapshot.result.artifacts == ("saved-result",)
    finally:
        await scheduler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [JobState.ACCEPTED, JobState.QUEUED])
async def test_generation_cancel_settles_unstarted_jobs(tmp_path, state):
    service, scheduler, store, provider = make_service(tmp_path)
    job = Job(
        job_id="unstarted",
        request=GenerationRequest(prompt="synthetic", source="test"),
        state=state,
    )
    try:
        await store.create(job)
        snapshot = await service.cancel_generation(job.job_id)
        assert snapshot.state is JobState.CANCELLED
        assert snapshot.cancel_requested is True
        assert snapshot.revision == 1
        assert not provider.started.is_set()
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_generation_cancel_retries_newer_running_revision(tmp_path, monkeypatch):
    service, scheduler, store, provider = make_service(tmp_path)
    original_save = store.save
    cancellation_revisions = []

    async def save(job, *, expected_revision=None):
        if job.state is JobState.CANCELLED:
            cancellation_revisions.append(expected_revision)
            if len(cancellation_revisions) == 1:
                winner = await store.get(job.job_id)
                await original_save(winner)
        return await original_save(job, expected_revision=expected_revision)

    try:
        handle = await service.submit(
            GenerationRequest(prompt="synthetic", source="test")
        )
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        initial = await store.get(handle.job_id)
        monkeypatch.setattr(store, "save", save)
        snapshot = await service.cancel_generation(handle.job_id)
        assert snapshot.state is JobState.CANCELLED
        assert snapshot.cancel_requested is True
        assert cancellation_revisions == [initial.revision, initial.revision + 1]
        assert provider.cancelled
        assert (await service.wait(handle.job_id)).state is JobState.CANCELLED
    finally:
        await scheduler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ready_state", [JobState.RESULT_READY, JobState.DELIVERY_PENDING]
)
@pytest.mark.parametrize("via_stop_registry", [False, True])
async def test_ready_write_wins_stop_without_cancelling_worker(
    tmp_path, monkeypatch, ready_state, via_stop_registry
):
    gate = ResultGate()
    kwargs = {"observers": [gate]} if ready_state is JobState.RESULT_READY else {}
    service, scheduler, store, provider = make_service(tmp_path, **kwargs)
    original_save = store.save
    cancellation_writes = 0
    supervisor = None

    async def save(job, *, expected_revision=None):
        nonlocal cancellation_writes
        if job.state is JobState.CANCELLED:
            cancellation_writes += 1
            provider.release.set()
            await asyncio.wait_for(gate.started.wait(), timeout=2)
        return await original_save(job, expected_revision=expected_revision)

    try:
        handle = await service.submit(
            GenerationRequest(prompt="synthetic", source="test"),
            delivery=gate if ready_state is JobState.DELIVERY_PENDING else None,
        )
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        runtime = scheduler._tasks[handle.job_id]
        monkeypatch.setattr(store, "save", save)
        if via_stop_registry:
            registry = SimpleNamespace(
                register_background_task=lambda event, task: None
            )
            monkeypatch.setattr(background_notify, "active_event_registry", registry)
            supervisor = await background_notify.register_background_job_stop(
                SimpleNamespace(is_stopped=lambda: False), service, handle.job_id
            )
            supervisor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await supervisor
        else:
            snapshot = await service.cancel_generation(handle.job_id)
            assert snapshot.state is ready_state
        saved = await store.get(handle.job_id)
        assert saved.state is ready_state
        assert saved.cancel_requested is False
        assert saved.result.artifacts == ("synthetic-image",)
        assert cancellation_writes == 1
        assert not runtime.done() and runtime.cancelling() == 0
        assert not gate.cancelled
        gate.release.set()
        result = await asyncio.wait_for(service.wait(handle.job_id), timeout=2)
        assert result.state is JobState.SUCCEEDED
        assert result.artifacts == ("synthetic-image",)
    finally:
        provider.release.set()
        gate.release.set()
        if supervisor is not None and not supervisor.done():
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
        await scheduler.close()


@pytest.mark.asyncio
async def test_generation_cancel_missing_job_reports_not_found(tmp_path):
    service, scheduler, _, _ = make_service(tmp_path)
    try:
        with pytest.raises(JobNotFoundError):
            await service.cancel_generation("missing")
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_generation_cancel_does_not_retry_foreign_owner_fencing(tmp_path):
    service, scheduler, store, _ = make_service(tmp_path)
    owner = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job(
        job_id="foreign-owner",
        request=GenerationRequest(prompt="synthetic", source="test"),
        state=JobState.RUNNING,
        owner_id=owner.owner_id,
        fencing_token=owner.fencing_token,
    )
    try:
        await owner.create(job)
        with pytest.raises(StaleJobError):
            await asyncio.wait_for(service.cancel_generation(job.job_id), timeout=2)
        saved = await store.get(job.job_id)
        assert saved.state is JobState.RUNNING
        assert saved.revision == 0
        assert saved.cancel_requested is False
    finally:
        await owner.close()
        await scheduler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("via_stop_registry", [False, True])
async def test_generation_cancel_retries_local_worker_claim(
    tmp_path, monkeypatch, via_stop_registry
):
    service, scheduler, store, provider = make_service(tmp_path)
    original_save = store.save
    claim_waiting = asyncio.Event()
    allow_claim = asyncio.Event()
    cancellation_revisions = []
    supervisor = None

    async def save(job, *, expected_revision=None):
        if job.state is JobState.RUNNING:
            claim_waiting.set()
            await allow_claim.wait()
        elif job.state is JobState.CANCELLED:
            cancellation_revisions.append(expected_revision)
            if len(cancellation_revisions) == 1:
                # Commit the real worker's local ownership after stop read QUEUED.
                allow_claim.set()
                await asyncio.wait_for(provider.started.wait(), timeout=2)
        return await original_save(job, expected_revision=expected_revision)

    monkeypatch.setattr(store, "save", save)
    try:
        handle = await service.submit(
            GenerationRequest(prompt="synthetic", source="test")
        )
        await asyncio.wait_for(claim_waiting.wait(), timeout=2)
        queued = await store.get(handle.job_id)
        assert queued.state is JobState.QUEUED
        assert queued.owner_id is None
        if via_stop_registry:
            monkeypatch.setattr(
                background_notify,
                "active_event_registry",
                SimpleNamespace(register_background_task=lambda event, task: None),
            )
            supervisor = await background_notify.register_background_job_stop(
                SimpleNamespace(is_stopped=lambda: False), service, handle.job_id
            )
            supervisor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await supervisor
        else:
            snapshot = await service.cancel_generation(handle.job_id)
            assert snapshot.state is JobState.CANCELLED
        saved = await store.get(handle.job_id)
        assert saved.state is JobState.CANCELLED
        assert saved.cancel_requested is True
        assert saved.owner_id == store.owner_id
        assert saved.fencing_token == store.fencing_token
        assert cancellation_revisions == [queued.revision, queued.revision + 1]
        assert saved.revision == queued.revision + 2
        assert provider.cancelled
        assert (await service.wait(handle.job_id)).state is JobState.CANCELLED
    finally:
        allow_claim.set()
        provider.release.set()
        if supervisor is not None and not supervisor.done():
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
        await scheduler.close()


@pytest.mark.asyncio
async def test_generation_cancel_rejects_stale_without_revision_progress(
    tmp_path, monkeypatch
):
    service, scheduler, store, _ = make_service(tmp_path)
    job = Job(
        job_id="no-progress",
        request=GenerationRequest(prompt="synthetic", source="test"),
        state=JobState.QUEUED,
    )
    attempts = 0

    async def stale_save(job, *, expected_revision=None):
        nonlocal attempts
        attempts += 1
        raise StaleJobError(
            "synthetic stale without progress",
            details={
                "expected_revision": expected_revision,
                "current_revision": expected_revision,
            },
        )

    try:
        await store.create(job)
        monkeypatch.setattr(store, "save", stale_save)
        with pytest.raises(StaleJobError):
            await asyncio.wait_for(service.cancel_generation(job.job_id), timeout=2)
        assert attempts == 1
        saved = await store.get(job.job_id)
        assert saved.state is JobState.QUEUED
        assert saved.revision == 0
        assert saved.cancel_requested is False
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_generation_cancel_rejects_new_foreign_worker_claim(
    tmp_path, monkeypatch
):
    service, scheduler, store, _ = make_service(tmp_path)
    owner = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job(
        job_id="foreign-claim",
        request=GenerationRequest(prompt="synthetic", source="test"),
        state=JobState.QUEUED,
    )
    original_save = store.save
    attempts = 0

    async def save(job, *, expected_revision=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            claimed = await owner.get(job.job_id)
            claimed.owner_id = owner.owner_id
            claimed.fencing_token = owner.fencing_token
            claimed.transition(JobState.RUNNING)
            await owner.save(claimed)
        return await original_save(job, expected_revision=expected_revision)

    try:
        await store.create(job)
        monkeypatch.setattr(store, "save", save)
        with pytest.raises(StaleJobError):
            await asyncio.wait_for(service.cancel_generation(job.job_id), timeout=2)
        assert attempts == 1
        saved = await store.get(job.job_id)
        assert saved.state is JobState.RUNNING
        assert saved.owner_id == owner.owner_id
        assert saved.fencing_token == owner.fencing_token
        assert saved.revision == 1
        assert saved.cancel_requested is False
    finally:
        await owner.close()
        await scheduler.close()
