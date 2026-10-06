from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from tl.core.job_store import SQLiteJobStore
from tl.core.models import Artifact, Attempt, Job, LeaseRecord
from tl.core.requests import GenerationRequest
from tl.core.results import GenerationResult
from tl.core.states import JobState


def _request(prompt: str = "draw") -> GenerationRequest:
    return GenerationRequest(prompt=prompt, source="test")


@pytest.mark.asyncio
async def test_sqlite_job_store_schema_and_roundtrip(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job.create(_request(), job_id="job-1")

    await store.create(job)
    job.transition(JobState.QUEUED)
    await store.save(job)
    loaded = await store.get("job-1")

    assert loaded is not None
    assert loaded.status == "queued"
    assert [event.event_type for event in await store.list_events("job-1")] == [
        "job_created",
        "state_changed",
    ]

    connection = sqlite3.connect(tmp_path / "jobs.sqlite3")
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "schema_migrations",
        "jobs",
        "attempts",
        "artifacts",
        "leases",
        "job_events",
    } <= tables
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    connection.close()
    await store.close()


@pytest.mark.asyncio
async def test_job_store_persists_attempt_artifact_and_lease_rows(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    await store.create(Job.create(_request(), job_id="job-1"))

    await store.add_attempt(Attempt(attempt_id="attempt-1", job_id="job-1", number=1))
    await store.add_artifact("job-1", Artifact("artifact-1", location="gallery/a.png"))
    await store.add_lease(LeaseRecord(lease_id="lease-1", job_id="job-1", kind="quota"))
    await store.release_lease("lease-1")

    artifacts = await store.list_artifacts("job-1")
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        lease = connection.execute(
            "SELECT status FROM leases WHERE lease_id = 'lease-1'"
        ).fetchone()
        attempts = connection.execute(
            "SELECT number FROM attempts WHERE attempt_id = 'attempt-1'"
        ).fetchone()

    assert artifacts[0].location == "gallery/a.png"
    assert lease[0] == "released"
    assert attempts[0] == 1
    await store.close()


@pytest.mark.asyncio
async def test_job_store_serializes_concurrent_writes(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)

    async def create(index: int) -> None:
        await store.create(Job.create(_request(f"draw-{index}"), job_id=f"job-{index}"))

    await asyncio.gather(*(create(index) for index in range(20)))
    jobs = await store.list_jobs()

    assert len(jobs) == 20
    assert len({job.job_id for job in jobs}) == 20
    await store.close()


@pytest.mark.asyncio
async def test_restart_recovers_runtime_states_without_fixed_temp_files(
    tmp_path,
) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    accepted = Job.create(_request("accepted"), job_id="accepted")
    queued = Job.create(_request("queued"), job_id="queued")
    queued.transition(JobState.QUEUED)
    running = Job.create(_request("running"), job_id="running")
    running.transition(JobState.QUEUED)
    running.transition(JobState.RUNNING)
    ready = Job.create(_request("ready"), job_id="ready")
    ready.transition(JobState.QUEUED)
    ready.transition(JobState.RUNNING)
    ready.set_result(GenerationResult(artifacts=("a.png",)))
    ready.transition(JobState.RESULT_READY)
    for job in (accepted, queued, running, ready):
        await store.create(job)
    await store.close()

    restored = SQLiteJobStore(tmp_path, import_legacy=False)
    states = {job.job_id: job.state for job in await restored.list_jobs()}

    assert states == {
        "accepted": JobState.ORPHANED,
        "queued": JobState.INTERRUPTED,
        "running": JobState.INTERRUPTED,
        "ready": JobState.ORPHANED,
    }
    assert not (tmp_path / "jobs.sqlite3.tmp").exists()
    await restored.close()


@pytest.mark.asyncio
async def test_legacy_json_is_imported_and_bad_json_is_backed_up(tmp_path) -> None:
    (tmp_path / "generation_history.json").write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "job_id": "history-1",
                        "source": "webui",
                        "status": "partial_success",
                        "prompt": "history prompt",
                        "params": {"provider": "google"},
                        "requested_images": 2,
                        "images": ["gallery/a.png"],
                        "text_content": "partial",
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "background_tasks.json").write_text("not-json", encoding="utf-8")

    store = SQLiteJobStore(tmp_path)
    history = await store.get("history-1")
    events = await store.list_events("history-1")

    assert history is not None
    assert history.state is JobState.PARTIAL
    assert history.result is not None
    assert history.result.artifacts == ("gallery/a.png",)
    assert events[0].event_type == "legacy_import"
    assert list(tmp_path.glob("background_tasks.json.corrupt-*"))
    await store.close()


def test_store_handles_empty_or_unknown_legacy_records(tmp_path) -> None:
    (tmp_path / "generation_history.json").write_text(
        json.dumps({"jobs": [{"status": "unknown"}, "bad"]}),
        encoding="utf-8",
    )
    store = SQLiteJobStore(tmp_path)

    assert asyncio.run(store.list_jobs()) == []
    store.close_sync()
