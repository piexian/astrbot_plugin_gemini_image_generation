from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from tl.core.errors import (
    CoreError,
    InvalidRequestError,
    JobDeletionError,
    JobStoreWriteCancelledError,
    ServiceClosedError,
    StaleJobError,
    StateTransitionError,
)
from tl.core.job_store import SQLiteJobStore
from tl.core.models import Artifact, Attempt, Job, JobEvent, LeaseRecord
from tl.core.requests import GenerationRequest
from tl.core.results import GenerationResult
from tl.core.states import JobState


def _request(prompt: str = "draw") -> GenerationRequest:
    return GenerationRequest(prompt=prompt, source="test")


def _write_background_tasks(path, records):
    path.write_text(json.dumps({"tasks": records}), encoding="utf-8")


def _write_history(path, records):
    path.write_text(json.dumps({"jobs": records}), encoding="utf-8")


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
    events = await store.list_events("job-1")
    assert [event.event_type for event in events] == [
        "job_created",
        "state_changed",
    ]
    assert events[1].data == {
        "previous_state": "accepted",
        "new_state": "queued",
        "previous_revision": 0,
        "new_revision": 1,
    }

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
        "store_owners",
        "legacy_imports",
        "job_deletions",
    } <= tables
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert (
        connection.execute(
            "SELECT revision FROM jobs WHERE job_id = 'job-1'"
        ).fetchone()[0]
        == 1
    )
    connection.close()
    await store.close()


@pytest.mark.asyncio
async def test_save_increments_revision(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job.create(_request(), job_id="job-1")
    await store.create(job)

    assert job.revision == 0
    job.transition(JobState.QUEUED)
    saved = await store.save(job)

    assert saved.revision == 1
    assert job.revision == 1
    loaded = await store.get("job-1")
    assert loaded is not None
    assert loaded.revision == 1
    await store.close()


@pytest.mark.asyncio
async def test_stale_save_raises(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    await store.create(Job.create(_request(), job_id="job-1"))
    actor_a = await store.get("job-1")
    actor_b = await store.get("job-1")
    assert actor_a is not None and actor_b is not None

    actor_a.transition(JobState.QUEUED)
    await store.save(actor_a)
    actor_b.transition(JobState.RUNNING)
    with pytest.raises(StaleJobError) as raised:
        await store.save(actor_b)

    assert raised.value.code == "stale_job"
    assert actor_b.revision == 0
    events = await store.list_events("job-1")
    assert [event.event_type for event in events] == [
        "job_created",
        "state_changed",
    ]
    await store.close()


@pytest.mark.asyncio
async def test_stale_save_cannot_regress_state(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    running = Job.create(_request("running"), job_id="job-1")
    running.transition(JobState.QUEUED)
    await store.create(running)
    running.transition(JobState.RUNNING)
    await store.save(running)
    actor_a = await store.get("job-1")
    actor_b = await store.get("job-1")
    assert actor_a is not None and actor_b is not None

    actor_a.set_result(GenerationResult(artifacts=("gallery/new.png",)))
    actor_a.transition(JobState.RESULT_READY)
    await store.save(actor_a)
    with pytest.raises(StaleJobError):
        await store.save(actor_b)

    loaded = await store.get("job-1")
    assert loaded is not None
    assert loaded.state is JobState.RESULT_READY
    assert loaded.result is not None
    assert loaded.result.artifacts == ("gallery/new.png",)
    await store.close()


@pytest.mark.asyncio
async def test_illegal_transition_rejected_by_store(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job.create(_request(), job_id="job-1")
    await store.create(job)
    job.transition(JobState.QUEUED)
    await store.save(job)
    job.transition(JobState.RUNNING)
    await store.save(job)
    job.transition(JobState.SUCCEEDED)
    await store.save(job)

    old = await store.get("job-1")
    assert old is not None
    old.state = JobState.RUNNING
    with pytest.raises(StateTransitionError):
        await store.save(old)

    loaded = await store.get("job-1")
    assert loaded is not None
    assert loaded.state is JobState.SUCCEEDED
    await store.close()


@pytest.mark.asyncio
async def test_revision_survives_roundtrip(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job(
        job_id="job-1",
        request=_request(),
        state=JobState.SUCCEEDED,
        revision=7,
    )
    await store.create(job)
    await store.close()

    restored_store = SQLiteJobStore(tmp_path, import_legacy=False)
    restored = await restored_store.get("job-1")
    assert restored is not None
    assert restored.revision == 7
    await restored_store.close()


def test_v1_schema_migrates_revision_column_without_recreating_database(
    tmp_path,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    request_json = json.dumps(
        {"prompt": "old", "source": "test", "reference_images": []}
    )
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations(version, applied_at)
            VALUES (1, '2026-01-01T00:00:00+00:00');
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                parent_job_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deadline_at TEXT,
                request_json TEXT NOT NULL,
                result_json TEXT,
                error_code TEXT,
                error_message TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, state, parent_job_id, created_at, updated_at,
                deadline_at, request_json, result_json, error_code,
                error_message, cancel_requested, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old-job",
                "succeeded",
                None,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                None,
                request_json,
                None,
                None,
                None,
                0,
                "{}",
            ),
        )

    store = SQLiteJobStore(tmp_path, import_legacy=False)
    restored = asyncio.run(store.get("old-job"))
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]

    assert restored is not None
    assert restored.revision == 0
    assert "revision" in columns
    assert versions == [1, 2, 3, 4, 5]
    store.close_sync()


@pytest.mark.asyncio
async def test_background_task_migration_preserves_image_urls(tmp_path) -> None:
    _write_background_tasks(
        tmp_path / "background_tasks.json",
        {
            "legacy-url": {
                "task_id": "legacy-url",
                "status": "succeeded",
                "image_urls": ["https://images.example/a.png?token=secret"],
                "provider": "google",
                "model": "image-model",
                "stats": {"provider": "google", "model": "image-model"},
            }
        },
    )
    store = SQLiteJobStore(tmp_path)
    try:
        job = await store.get("legacy-url")
        assert job is not None
        assert job.state is JobState.SUCCEEDED
        assert job.result is not None
        assert job.result.artifacts == ("https://images.example/a.png",)
        assert job.result.provider == "google"
        assert job.result.model == "image-model"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_background_task_migration_preserves_image_paths(tmp_path) -> None:
    _write_background_tasks(
        tmp_path / "background_tasks.json",
        {
            "legacy-path": {
                "task_id": "legacy-path",
                "status": "succeeded",
                "image_paths": ["/tmp/generated.png"],
            }
        },
    )
    store = SQLiteJobStore(tmp_path)
    try:
        job = await store.get("legacy-path")
        assert job is not None
        assert job.result is not None
        assert job.result.artifacts[0].startswith("artifact:")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_background_task_migration_preserves_text_content(tmp_path) -> None:
    _write_background_tasks(
        tmp_path / "background_tasks.json",
        {
            "legacy-text": {
                "task_id": "legacy-text",
                "status": "succeeded",
                "text_content": "generated description",
            }
        },
    )
    store = SQLiteJobStore(tmp_path)
    try:
        job = await store.get("legacy-text")
        assert job is not None
        assert job.result is not None
        assert job.result.text == "generated description"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_background_task_migration_populates_artifacts(tmp_path) -> None:
    _write_background_tasks(
        tmp_path / "background_tasks.json",
        {
            "legacy-artifacts": {
                "task_id": "legacy-artifacts",
                "status": "partial_success",
                "items": [
                    {
                        "image_urls": ["https://images.example/a.png"],
                        "image_paths": ["/tmp/b.png"],
                        "text_content": "item text",
                        "provider": "google",
                        "model": "model-a",
                    }
                ],
                "stats": {"provider": "google", "model": "model-a"},
            }
        },
    )
    store = SQLiteJobStore(tmp_path)
    try:
        job = await store.get("legacy-artifacts")
        artifacts = await store.list_artifacts("legacy-artifacts")
        assert job is not None and job.result is not None
        assert job.result.partial is True
        assert "https://images.example/a.png" in job.result.artifacts
        assert any(item.startswith("artifact:") for item in job.result.artifacts)
        assert {artifact.artifact_id for artifact in artifacts} == set(
            job.result.artifacts
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_history_import_preserves_partial_result(tmp_path) -> None:
    _write_history(
        tmp_path / "generation_history.json",
        [
            {
                "job_id": "history-partial",
                "status": "partial_success",
                "source": "webui",
                "prompt": "partial prompt",
                "images": ["gallery/partial.png"],
                "source_urls": ["https://images.example/partial.png?sig=secret"],
                "text_content": "partial text",
                "stats": {"provider": "google", "model": "model-a"},
            }
        ],
    )
    store = SQLiteJobStore(tmp_path)
    try:
        job = await store.get("history-partial")
        assert job is not None and job.result is not None
        assert job.state is JobState.PARTIAL
        assert job.result.partial is True
        assert job.result.text == "partial text"
        assert job.result.provider == "google"
        assert "?sig=" not in " ".join(job.result.artifacts)
        assert "https://images.example/partial.png" in job.result.artifacts
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_legacy_import_is_idempotent(tmp_path) -> None:
    _write_background_tasks(
        tmp_path / "background_tasks.json",
        {
            "legacy-once": {
                "task_id": "legacy-once",
                "status": "succeeded",
                "image_paths": ["/tmp/once.png"],
            }
        },
    )
    first = SQLiteJobStore(tmp_path)
    await first.close()
    second = SQLiteJobStore(tmp_path)
    try:
        jobs = await second.list_jobs()
        events = await second.list_events("legacy-once")
        with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
            marker = connection.execute(
                "SELECT imported_count FROM legacy_imports WHERE source = 'background_tasks.json'"
            ).fetchone()
        assert [job.job_id for job in jobs] == ["legacy-once"]
        assert [event.event_type for event in events] == ["legacy_import"]
        assert marker[0] == 1
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_one_bad_record_does_not_abort_other_records(tmp_path) -> None:
    _write_background_tasks(
        tmp_path / "background_tasks.json",
        {
            "bad": {"task_id": "bad", "status": "succeeded", "items": "bad"},
            "good": {
                "task_id": "good",
                "status": "succeeded",
                "image_urls": ["https://images.example/good.png"],
            },
        },
    )
    store = SQLiteJobStore(tmp_path)
    try:
        jobs = await store.list_jobs()
        assert [job.job_id for job in jobs] == ["good"]
        with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
            marker = connection.execute(
                "SELECT record_count, imported_count, skipped_count FROM legacy_imports WHERE source = 'background_tasks.json'"
            ).fetchone()
        assert marker == (2, 1, 1)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_storage_does_not_persist_signed_url(tmp_path) -> None:
    request = GenerationRequest(
        prompt="draw",
        source="test",
        reference_images=("https://images.example/a.png?token=signed-secret",),
    )
    job = Job(
        job_id="signed-url",
        request=request,
        result=GenerationResult(
            artifacts=("https://images.example/out.png?sig=secret",)
        ),
    )
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    try:
        await store.create(job)
        with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
            row = connection.execute(
                "SELECT request_json, result_json FROM jobs WHERE job_id = ?",
                (job.job_id,),
            ).fetchone()
            raw = " ".join(str(value or "") for value in row)
        assert "signed-secret" not in raw
        assert "sig=secret" not in raw
        assert "reference_id" in raw
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_storage_does_not_persist_authorization(tmp_path) -> None:
    job = Job(
        job_id="authorization",
        request=GenerationRequest(
            prompt="draw",
            source="test",
            metadata={"api_key": "api-secret"},
        ),
        error_message="Authorization: Bearer provider-secret",
    )
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    try:
        await store.create(job)
        with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
            row = connection.execute(
                "SELECT request_json, error_message FROM jobs WHERE job_id = ?",
                (job.job_id,),
            ).fetchone()
            raw = " ".join(str(value or "") for value in row)
        assert "api-secret" not in raw
        assert "provider-secret" not in raw
        assert "Authorization: Bearer ***" in raw
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_event_data_is_redacted(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job.create(_request(), job_id="event-redaction")
    try:
        await store.create(job)
        await store.append_event(
            JobEvent(
                "event-redaction",
                job.job_id,
                "test",
                data={
                    "url": "https://example.test/a?token=event-secret",
                    "Authorization": "Bearer event-secret",
                },
            )
        )
        with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
            raw = connection.execute(
                "SELECT data_json FROM job_events WHERE event_id = 'event-redaction'"
            ).fetchone()[0]
        assert "event-secret" not in raw
        assert "token=" not in raw
    finally:
        await store.close()


def test_error_message_is_redacted() -> None:
    error = CoreError(
        "GET https://example.test/a?token=error-secret Authorization: Bearer bearer-secret"
    )

    assert "error-secret" not in error.message
    assert "bearer-secret" not in error.message
    assert error.as_dict()["message"] == error.message


def test_database_name_cannot_escape_data_dir(tmp_path) -> None:
    with pytest.raises(ValueError, match="data_dir"):
        SQLiteJobStore(tmp_path, database_name="../outside.sqlite3")
    with pytest.raises(ValueError, match="data_dir"):
        SQLiteJobStore(tmp_path, database_name=str(tmp_path / "outside.sqlite3"))


@pytest.mark.asyncio
async def test_store_files_are_private(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    try:
        await store.create(Job.create(_request(), job_id="permissions"))
        assert os.stat(tmp_path).st_mode & 0o777 == 0o700
        assert os.stat(tmp_path / "jobs.sqlite3").st_mode & 0o777 == 0o600
        for sidecar in (
            tmp_path / "jobs.sqlite3-wal",
            tmp_path / "jobs.sqlite3-shm",
        ):
            if sidecar.exists():
                assert os.stat(sidecar).st_mode & 0o777 == 0o600
    finally:
        await store.close()


def _old_job(job_id: str, state: JobState = JobState.SUCCEEDED) -> Job:
    old = datetime.now(timezone.utc) - timedelta(days=3)
    return Job(
        job_id=job_id,
        request=_request(job_id),
        state=state,
        created_at=old,
        updated_at=old,
    )


@pytest.mark.asyncio
async def test_running_job_cannot_be_pruned(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    try:
        job = _old_job("running", JobState.RUNNING)
        await store.create(job)
        assert (
            await store.prune_jobs(
                datetime.now(timezone.utc) + timedelta(days=1), {JobState.RUNNING}
            )
            == 0
        )
        assert await store.get(job.job_id) is not None
        with pytest.raises(JobDeletionError):
            await store.delete_job(job.job_id)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_delivery_lease_blocks_delete(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    try:
        job = _old_job("delivery", JobState.SUCCEEDED)
        await store.create(job)
        await store.add_lease(
            LeaseRecord("delivery-lease", job.job_id, "delivery", status="active")
        )
        with pytest.raises(JobDeletionError):
            await store.delete_job(job.job_id)
        assert await store.get(job.job_id) is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_terminal_job_can_be_pruned(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    try:
        job = _old_job("terminal", JobState.SUCCEEDED)
        await store.create(job)
        removed = await store.prune_jobs(
            datetime.now(timezone.utc) + timedelta(days=1), {JobState.SUCCEEDED}
        )
        assert removed == 1
        assert await store.get(job.job_id) is None
        with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
            assert (
                connection.execute(
                    "SELECT job_id, reason FROM job_deletions WHERE job_id = ?",
                    (job.job_id,),
                ).fetchone()[1]
                == "retention_prune"
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_prune_removes_related_rows(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    try:
        job = _old_job("cascade", JobState.FAILED)
        await store.create(job)
        await store.add_attempt(Attempt("cascade-attempt", job.job_id, 1))
        await store.add_artifact(job.job_id, Artifact("cascade-artifact"))
        await store.add_lease(
            LeaseRecord("cascade-lease", job.job_id, "quota", status="released")
        )
        await store.append_event(JobEvent("cascade-event", job.job_id, "audit"))
        assert (
            await store.prune_jobs(
                datetime.now(timezone.utc) + timedelta(days=1), {JobState.FAILED}
            )
            == 1
        )
        with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
            for table in ("attempts", "artifacts", "leases", "job_events"):
                assert (
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE job_id = ?",
                        (job.job_id,),
                    ).fetchone()[0]
                    == 0
                )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM job_deletions WHERE job_id = ?",
                    (job.job_id,),
                ).fetchone()[0]
                == 1
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_checkpoint_does_not_break_store(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    try:
        await store.create(Job.create(_request("before"), job_id="before"))
        await store.checkpoint()
        await store.create(Job.create(_request("after"), job_id="after"))
        assert await store.get("before") is not None
        assert await store.get("after") is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_expire_leases_marks_due_lease_and_keeps_cleanup_guard(tmp_path) -> None:
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    now = datetime.now(timezone.utc)
    try:
        job = _old_job("expiring", JobState.SUCCEEDED)
        await store.create(job)
        await store.add_lease(
            LeaseRecord(
                "expiring-lease",
                job.job_id,
                "artifact",
                metadata={"expires_at": (now - timedelta(seconds=1)).isoformat()},
            )
        )
        assert await store.expire_leases(now) == 1
        with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
            assert connection.execute(
                "SELECT status, released_at FROM leases WHERE lease_id = 'expiring-lease'"
            ).fetchone() == ("expired", None)
        with pytest.raises(JobDeletionError):
            await store.delete_job(job.job_id)
    finally:
        await store.close()


def test_oversized_request_is_rejected() -> None:
    with pytest.raises(InvalidRequestError, match="prompt"):
        GenerationRequest(prompt="x" * 10_001, source="test")
    with pytest.raises(InvalidRequestError, match="metadata"):
        GenerationRequest(prompt="draw", source="test", metadata={"x": "y" * 70_000})


def _make_owner_stale(database, owner_id: str) -> None:
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE store_owners SET heartbeat_at = ? WHERE owner_id = ?",
            (stale, owner_id),
        )


@pytest.mark.asyncio
async def test_second_store_does_not_interrupt_live_owner(tmp_path) -> None:
    first = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job(
        job_id="live-job",
        request=_request(),
        state=JobState.RUNNING,
    )
    await first.create(job)

    second = SQLiteJobStore(tmp_path, import_legacy=False)
    try:
        loaded = await second.get(job.job_id)
        assert loaded is not None
        assert loaded.state is JobState.RUNNING
        with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
            active = connection.execute(
                "SELECT COUNT(*) FROM store_owners WHERE released_at IS NULL"
            ).fetchone()[0]
        assert active == 2
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_stale_owner_can_be_recovered(tmp_path) -> None:
    first = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job(
        job_id="stale-job",
        request=_request(),
        state=JobState.RUNNING,
    )
    await first.create(job)
    _make_owner_stale(tmp_path / "jobs.sqlite3", first.owner_id)

    second = SQLiteJobStore(tmp_path, import_legacy=False)
    try:
        loaded = await second.get(job.job_id)
        assert loaded is not None
        assert loaded.state is JobState.INTERRUPTED
        with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
            released = connection.execute(
                "SELECT released_at FROM store_owners WHERE owner_id = ?",
                (first.owner_id,),
            ).fetchone()[0]
        assert released is not None
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_recovery_marks_running_attempt_interrupted(tmp_path) -> None:
    first = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job(job_id="attempt-job", request=_request(), state=JobState.RUNNING)
    await first.create(job)
    await first.add_attempt(Attempt("attempt-1", job.job_id, 1))
    _make_owner_stale(tmp_path / "jobs.sqlite3", first.owner_id)

    second = SQLiteJobStore(tmp_path, import_legacy=False)
    try:
        with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
            attempt = connection.execute(
                "SELECT status, finished_at FROM attempts WHERE attempt_id = 'attempt-1'"
            ).fetchone()
        events = await second.list_events(job.job_id)
        assert attempt[0] == "interrupted"
        assert attempt[1] is not None
        assert any(event.event_type == "attempt_recovery" for event in events)
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_recovery_does_not_leave_active_leases(tmp_path) -> None:
    first = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job(job_id="lease-job", request=_request(), state=JobState.RUNNING)
    await first.create(job)
    await first.add_lease(LeaseRecord("lease-1", job.job_id, "quota"))
    _make_owner_stale(tmp_path / "jobs.sqlite3", first.owner_id)

    second = SQLiteJobStore(tmp_path, import_legacy=False)
    try:
        with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
            lease = connection.execute(
                "SELECT status, released_at FROM leases WHERE lease_id = 'lease-1'"
            ).fetchone()
        assert lease[0] == "recovery_pending"
        assert lease[1] is not None
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_result_ready_becomes_delivery_pending(tmp_path) -> None:
    first = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job(
        job_id="ready-job",
        request=_request(),
        state=JobState.RESULT_READY,
        result=GenerationResult(artifacts=("gallery/ready.png",)),
    )
    await first.create(job)
    _make_owner_stale(tmp_path / "jobs.sqlite3", first.owner_id)

    second = SQLiteJobStore(tmp_path, import_legacy=False)
    try:
        loaded = await second.get(job.job_id)
        assert loaded is not None
        assert loaded.state is JobState.DELIVERY_PENDING
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_recovery_preserves_result_artifacts(tmp_path) -> None:
    first = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job(
        job_id="result-job",
        request=_request(),
        state=JobState.RUNNING,
        result=GenerationResult(artifacts=("gallery/result.png",)),
    )
    await first.create(job)
    _make_owner_stale(tmp_path / "jobs.sqlite3", first.owner_id)

    second = SQLiteJobStore(tmp_path, import_legacy=False)
    try:
        loaded = await second.get(job.job_id)
        artifacts = await second.list_artifacts(job.job_id)
        assert loaded is not None
        assert loaded.state is JobState.INTERRUPTED
        assert loaded.result is not None
        assert loaded.result.artifacts == ("gallery/result.png",)
        assert [artifact.artifact_id for artifact in artifacts] == [
            "gallery/result.png"
        ]
    finally:
        await second.close()
        await first.close()


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
        "ready": JobState.DELIVERY_PENDING,
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


class _FailingConnection:
    """Inject SQL failures while keeping actual SQLite transactions underneath."""

    def __init__(self, connection, failures):
        self.connection = connection
        self.failures = set(failures)
        self.statements = []

    def execute(self, sql, *args):
        self.statements.append(sql)
        if sql in self.failures:
            self.failures.remove(sql)
            raise sqlite3.OperationalError(f"injected {sql} failure")
        return self.connection.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def _assert_store_lock_available(store):
    acquired = []

    def probe():
        # A fresh thread is essential: a pooled worker could own the leaked
        # RLock and successfully reacquire it, hiding the original regression.
        available = store._lock.acquire(blocking=False)
        acquired.append(available)
        if available:
            store._lock.release()

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    thread.join(timeout=2)
    assert acquired == [True]


@pytest.mark.asyncio
async def test_begin_failure_does_not_poison_store_lock(tmp_path, monkeypatch):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    faulty = _FailingConnection(store._connection, {"BEGIN IMMEDIATE"})
    monkeypatch.setattr(store, "_connection", faulty)
    job = Job.create(_request(), job_id="begin-failure")

    try:
        with pytest.raises(sqlite3.OperationalError, match="BEGIN IMMEDIATE"):
            await store.create(job)
        _assert_store_lock_available(store)
        assert not store._broken
        assert not faulty.in_transaction
        assert await store.get(job.job_id) is None
        await store.create(job)
        assert (await store.get(job.job_id)).job_id == job.job_id
    finally:
        # Avoid hanging test teardown on the very lock leak under test.
        faulty.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["get", "list_jobs", "list_events", "list_artifacts"]
)
async def test_closed_store_reads_raise_service_closed_error(tmp_path, method):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    await store.close()
    args = () if method == "list_jobs" else ("job-1",)

    with pytest.raises(ServiceClosedError) as raised:
        await getattr(store, method)(*args)
    assert raised.value.code == "service_closed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [
        "create",
        "save",
        "append_event",
        "add_attempt",
        "add_artifact",
        "add_lease",
        "release_lease",
    ],
)
async def test_closed_store_writes_raise_service_closed_error(tmp_path, method):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job.create(_request(), job_id="job-1")
    await store.create(job)
    await store.close()
    args = {
        "create": (job,),
        "save": (job,),
        "append_event": (JobEvent("event-1", job.job_id, "test"),),
        "add_attempt": (Attempt("attempt-1", job.job_id, 1),),
        "add_artifact": (job.job_id, Artifact("artifact-1")),
        "add_lease": (LeaseRecord("lease-1", job.job_id, "quota"),),
        "release_lease": ("lease-1",),
    }

    with pytest.raises(ServiceClosedError) as raised:
        await getattr(store, method)(*args[method])
    assert raised.value.code == "service_closed"
    _assert_store_lock_available(store)


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_commit_failure_does_not_leave_open_transaction(
    tmp_path, monkeypatch, rollback_fails
):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job.create(_request(), job_id="job-1")
    await store.create(job)
    job.transition(JobState.QUEUED)
    failures = {"COMMIT", "ROLLBACK"} if rollback_fails else {"COMMIT"}
    faulty = _FailingConnection(store._connection, failures)
    monkeypatch.setattr(store, "_connection", faulty)

    with pytest.raises(sqlite3.OperationalError, match="COMMIT"):
        await store.save(job)
    assert "ROLLBACK" in faulty.statements
    assert store._broken
    assert store._closed
    assert job.revision == 0
    _assert_store_lock_available(store)
    with pytest.raises(ServiceClosedError):
        await store.save(job)
    with pytest.raises(ServiceClosedError):
        await store.get(job.job_id)
    await store.close()

    # A fresh connection must be able to take the write lock immediately, and
    # neither the job update nor its state_changed event may have survived.
    with sqlite3.connect(store.path, timeout=0.1) as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert connection.execute(
            "SELECT state, revision FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone() == ("accepted", 0)
        assert connection.execute(
            "SELECT event_type FROM job_events WHERE job_id = ?", (job.job_id,)
        ).fetchall() == [("job_created",)]


@pytest.mark.asyncio
async def test_rollback_failure_preserves_body_error_and_disables_connection(
    tmp_path, monkeypatch
):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    faulty = _FailingConnection(store._connection, {"ROLLBACK"})
    monkeypatch.setattr(store, "_connection", faulty)
    original_error = ValueError("transaction body failed")

    def operation():
        with store._transaction():
            raise original_error

    with pytest.raises(ValueError) as raised:
        await store._run_write(operation)
    assert raised.value is original_error
    assert store._broken and store._closed
    _assert_store_lock_available(store)
    await store.close()


async def _cancel_gated_write(store, monkeypatch, operation, *, fail, repeat_cancel):
    started = threading.Event()
    finish = threading.Event()
    insert_event = store._insert_event_locked

    def gated_event(connection, event):
        started.set()
        if not finish.wait(timeout=5):
            raise TimeoutError("test did not release the database worker")
        if fail:
            raise sqlite3.OperationalError("injected write failure")
        insert_event(connection, event)

    monkeypatch.setattr(store, "_insert_event_locked", gated_event)
    task = asyncio.create_task(operation())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel("caller cancelled write")
        await asyncio.sleep(0)
        assert not task.done(), "cancellation returned before transaction settled"
        if repeat_cancel:
            task.cancel("second cancellation")
            await asyncio.sleep(0)
            assert not task.done()
        finish.set()
        with pytest.raises(JobStoreWriteCancelledError) as raised:
            await task
        assert isinstance(raised.value, asyncio.CancelledError)
        assert task.cancelled()
        assert raised.value.args == ("caller cancelled write",)
        return raised.value
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_cancelled_create_has_observable_commit_result(
    tmp_path, monkeypatch, fail, repeat_cancel
):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job.create(_request(), job_id="cancelled-create")
    try:
        outcome = await _cancel_gated_write(
            store,
            monkeypatch,
            lambda: store.create(job),
            fail=fail,
            repeat_cancel=repeat_cancel,
        )
        loaded = await store.get(job.job_id)
        events = await store.list_events(job.job_id)
        if fail:
            assert outcome.outcome == "failed"
            assert outcome.result is None
            assert isinstance(outcome.error, sqlite3.OperationalError)
            assert loaded is None
            assert events == []
        else:
            assert outcome.outcome == "committed"
            assert outcome.error is None
            assert outcome.result.as_dict() == loaded.as_dict()
            assert [event.event_type for event in events] == ["job_created"]
        assert not store._connection.in_transaction
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_cancelled_save_has_observable_commit_result(
    tmp_path, monkeypatch, fail, repeat_cancel
):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job.create(_request(), job_id="cancelled-save")
    await store.create(job)
    job.transition(JobState.QUEUED)
    try:
        outcome = await _cancel_gated_write(
            store,
            monkeypatch,
            lambda: store.save(job),
            fail=fail,
            repeat_cancel=repeat_cancel,
        )
        loaded = await store.get(job.job_id)
        events = await store.list_events(job.job_id)
        if fail:
            assert outcome.outcome == "failed"
            assert outcome.result is None
            assert isinstance(outcome.error, sqlite3.OperationalError)
            assert loaded.state is JobState.ACCEPTED
            assert loaded.revision == job.revision == 0
            assert [event.event_type for event in events] == ["job_created"]
        else:
            assert outcome.outcome == "committed"
            assert outcome.error is None
            assert loaded.state is JobState.QUEUED
            assert loaded.revision == job.revision == 1
            assert outcome.result.as_dict() == loaded.as_dict()
            assert [event.event_type for event in events] == [
                "job_created",
                "state_changed",
            ]
        assert not store._connection.in_transaction
    finally:
        await store.close()
