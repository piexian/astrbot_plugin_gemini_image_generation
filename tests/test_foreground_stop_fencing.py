"""Foreground stop arbitration using temporary SQLite and synthetic events."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tl.background_notify import ForegroundStopped, wait_foreground_job
from tl.core.errors import StaleJobError
from tl.core.job_store import SQLiteJobStore
from tl.core.models import Job
from tl.core.requests import GenerationRequest
from tl.core.scheduler import JobScheduler
from tl.core.states import JobState


@pytest.mark.asyncio
async def test_preserve_settled_cancel_does_not_retry_foreign_owner(tmp_path):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    scheduler = JobScheduler(store, provider=object())
    owner = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job(
        job_id="foreign-job",
        request=GenerationRequest(prompt="synthetic", source="test"),
        state=JobState.RUNNING,
        owner_id=owner.owner_id,
        fencing_token=owner.fencing_token,
    )
    try:
        await owner.create(job)
        with pytest.raises(StaleJobError):
            await asyncio.wait_for(
                scheduler.cancel(job.job_id, preserve_settled=True), timeout=1
            )
        saved = await store.get(job.job_id)
        assert saved.state is JobState.RUNNING
        assert saved.owner_id == owner.owner_id
        assert saved.revision == 0
        assert saved.cancel_requested is False
    finally:
        await owner.close()
        await scheduler.close()


@pytest.mark.asyncio
async def test_preserve_settled_cancel_retries_after_revision_advances(
    tmp_path, monkeypatch
):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    scheduler = JobScheduler(store, provider=object())
    job = Job(
        job_id="queued-job",
        request=GenerationRequest(prompt="synthetic", source="test"),
        state=JobState.QUEUED,
    )
    original_save = store.save
    attempts = []

    async def save(candidate, *, expected_revision=None):
        if candidate.state is JobState.CANCELLED:
            attempts.append(candidate.revision)
            if len(attempts) == 1:
                winner = await store.get(candidate.job_id)
                await original_save(winner)
        return await original_save(candidate, expected_revision=expected_revision)

    try:
        await store.create(job)
        monkeypatch.setattr(store, "save", save)
        stopped = await scheduler.cancel(job.job_id, preserve_settled=True)
        assert attempts == [0, 1]
        assert stopped.revision == 2
        assert stopped.state is JobState.CANCELLED
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_foreground_stop_does_not_propagate_a_fencing_conflict():
    async def cancel(_job_id, *, preserve_settled=False):
        assert preserve_settled
        raise StaleJobError("synthetic owner conflict")

    service = SimpleNamespace(
        get_status=AsyncMock(return_value=SimpleNamespace(state=JobState.RUNNING)),
        cancel=cancel,
        wait=AsyncMock(),
    )
    event = SimpleNamespace(is_stopped=lambda: True)
    with pytest.raises(ForegroundStopped):
        await wait_foreground_job(event, service, "synthetic-job")
    service.wait.assert_not_awaited()
