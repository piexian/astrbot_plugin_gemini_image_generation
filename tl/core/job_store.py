"""SQLite-backed persistence for scheduler jobs.

The store is deliberately independent from AstrBot and from the legacy JSON
trackers.  SQLite transactions provide the serialization and crash recovery
boundary; no shared ``.tmp`` file is used for concurrent saves.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import JobNotFoundError
from .models import Artifact, Attempt, Job, JobEvent, LeaseRecord, utc_now
from .requests import GenerationRequest
from .states import JobState, coerce_state

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_LEGACY_TERMINAL = {
    "succeeded": JobState.SUCCEEDED,
    "partial_success": JobState.PARTIAL,
    "partial": JobState.PARTIAL,
    "failed": JobState.FAILED,
    "cancelled": JobState.CANCELLED,
    "interrupted": JobState.INTERRUPTED,
}


def _timestamp(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return copy.deepcopy(default)
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return copy.deepcopy(default)


def _safe_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _legacy_state(value: Any) -> JobState:
    raw = _safe_text(value, JobState.INTERRUPTED.value).lower()
    if raw in _LEGACY_TERMINAL:
        return _LEGACY_TERMINAL[raw]
    if raw == JobState.ACCEPTED.value:
        return JobState.ORPHANED
    if raw in {JobState.QUEUED.value, JobState.RUNNING.value}:
        return JobState.INTERRUPTED
    try:
        return coerce_state(raw)
    except ValueError:
        return JobState.INTERRUPTED


class SQLiteJobStore:
    """Serialized async facade over a WAL SQLite database.

    Each public operation runs under the same process lock and one SQLite
    transaction.  This keeps concurrent scheduler calls ordered while SQLite's
    WAL journal makes committed state recoverable after a process crash.
    """

    def __init__(
        self,
        data_dir: str | Path,
        *,
        database_name: str = "jobs.sqlite3",
        import_legacy: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / database_name
        self._lock = threading.RLock()
        self._closed = False
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure_connection()
        self._migrate()
        if import_legacy:
            self._import_legacy_files()
        self._recover_after_crash()

    def _configure_connection(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=30000")

    def _transaction(self):
        class Transaction:
            def __init__(self, store: SQLiteJobStore) -> None:
                self.store = store

            def __enter__(self):
                self.store._lock.acquire()
                if self.store._closed:
                    self.store._lock.release()
                    raise RuntimeError("JobStore 已关闭")
                self.store._connection.execute("BEGIN IMMEDIATE")
                return self.store._connection

            def __exit__(self, exc_type, exc, traceback):
                try:
                    if exc_type is None:
                        self.store._connection.execute("COMMIT")
                    else:
                        self.store._connection.execute("ROLLBACK")
                finally:
                    self.store._lock.release()
                return False

        return Transaction(self)

    def _migrate(self) -> None:
        with self._lock:
            connection = self._connection
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
            current = int(row["version"] or 0)
            if current < 1:
                # executescript owns the explicit transaction.  Python's
                # sqlite3 wrapper commits any already-open transaction before
                # executescript, so calling it from _transaction() would make
                # the outer COMMIT fail and weaken migration atomicity.
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS jobs (
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
                    CREATE INDEX IF NOT EXISTS idx_jobs_state_updated
                        ON jobs(state, updated_at);
                    CREATE INDEX IF NOT EXISTS idx_jobs_parent
                        ON jobs(parent_job_id);

                    CREATE TABLE IF NOT EXISTS attempts (
                        attempt_id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                        number INTEGER NOT NULL,
                        provider TEXT,
                        model TEXT,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        error_code TEXT,
                        error_message TEXT,
                        UNIQUE(job_id, number)
                    );

                    CREATE TABLE IF NOT EXISTS artifacts (
                        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                        artifact_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        location TEXT,
                        mime_type TEXT,
                        size_bytes INTEGER,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        PRIMARY KEY(job_id, artifact_id)
                    );

                    CREATE TABLE IF NOT EXISTS leases (
                        lease_id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        acquired_at TEXT NOT NULL,
                        released_at TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE INDEX IF NOT EXISTS idx_leases_job_status
                        ON leases(job_id, status);

                    CREATE TABLE IF NOT EXISTS job_events (
                        event_id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                        event_type TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        state TEXT,
                        data_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE INDEX IF NOT EXISTS idx_job_events_job_created
                        ON job_events(job_id, created_at);
                    INSERT INTO schema_migrations(version, applied_at)
                        VALUES (1, datetime('now'));
                    COMMIT;
                    """
                )
                current = 1
            if current != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"不支持的 JobStore schema 版本: {current}，期望 {_SCHEMA_VERSION}"
                )

    def _read_json_file(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as exc:
            self._backup_corrupt(path)
            logger.warning("JobStore 忽略损坏的旧 JSON %s: %s", path, exc)
            return None

    @staticmethod
    def _backup_corrupt(path: Path) -> None:
        if not path.exists():
            return
        backup = path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex}")
        try:
            os.replace(path, backup)
        except OSError:
            logger.warning("JobStore 无法备份损坏文件 %s", path, exc_info=True)

    def _import_legacy_files(self) -> None:
        history = self._read_json_file(self.data_dir / "generation_history.json")
        background = self._read_json_file(self.data_dir / "background_tasks.json")
        records: dict[str, Job] = {}
        if isinstance(history, Mapping) and isinstance(history.get("jobs"), list):
            for record in history["jobs"]:
                try:
                    job = self._job_from_history(record)
                except Exception as exc:
                    logger.warning("JobStore 跳过损坏的历史记录: %s", exc)
                    job = None
                if job is not None:
                    records.setdefault(job.job_id, job)
        if isinstance(background, Mapping):
            tasks = background.get("tasks", background)
            if isinstance(tasks, Mapping):
                for task_id, record in tasks.items():
                    try:
                        job = self._job_from_background(str(task_id), record)
                    except Exception as exc:
                        logger.warning("JobStore 跳过损坏的后台任务记录: %s", exc)
                        job = None
                    if job is not None:
                        records.setdefault(job.job_id, job)
        if not records:
            return
        with self._transaction() as connection:
            for job in records.values():
                inserted = connection.execute(
                    """
                    INSERT OR IGNORE INTO jobs(
                        job_id, state, parent_job_id, created_at, updated_at,
                        deadline_at, request_json, result_json, error_code,
                        error_message, cancel_requested, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._job_values(job),
                ).rowcount
                if inserted:
                    self._insert_event_locked(
                        connection,
                        JobEvent(
                            event_id=f"event-{uuid.uuid4().hex}",
                            job_id=job.job_id,
                            event_type="legacy_import",
                            state=job.state,
                            data={
                                "source": job.metadata.get("legacy_source", "unknown")
                            },
                        ),
                    )

    def _job_from_history(self, record: Any) -> Job | None:
        if not isinstance(record, Mapping):
            return None
        job_id = _safe_text(record.get("job_id"))
        if not job_id:
            return None
        params = (
            record.get("params") if isinstance(record.get("params"), Mapping) else {}
        )
        references = record.get("reference_names") or []
        if not isinstance(references, (list, tuple)):
            references = []
        request = GenerationRequest.from_values(
            prompt=_safe_text(record.get("prompt"), "(legacy generation)"),
            source=_safe_text(record.get("source"), "plugin"),
            image_count=max(int(record.get("requested_images") or 1), 1),
            provider=params.get("provider"),
            model=params.get("model"),
            candidate_id=params.get("candidate_id"),
            resolution=params.get("resolution"),
            aspect_ratio=params.get("aspect_ratio"),
            negative_prompt=params.get("negative_prompt"),
            quality=params.get("quality"),
            reference_images=references,
            requester=record.get("requester") or {},
            metadata={"legacy_source": "generation_history.json"},
        )
        state = _legacy_state(record.get("status"))
        images = record.get("images") or []
        result = None
        if images or record.get("text_content"):
            from .results import GenerationResult

            result = GenerationResult(
                artifacts=tuple(str(item) for item in images if item),
                text=record.get("text_content") or None,
                provider=(record.get("stats") or {}).get("provider"),
                model=(record.get("stats") or {}).get("model"),
                partial=state is JobState.PARTIAL,
            )
        created = _parse_time(record.get("created_at")) or utc_now()
        updated = _parse_time(record.get("finished_at")) or created
        return Job(
            job_id=job_id,
            request=request,
            state=state,
            parent_job_id=record.get("parent_job_id"),
            created_at=created,
            updated_at=updated,
            result=result,
            error_message=record.get("error"),
            metadata={
                "legacy_source": "generation_history.json",
                "item_name": record.get("item_name"),
            },
        )

    def _job_from_background(self, task_id: str, record: Any) -> Job | None:
        if not isinstance(record, Mapping):
            return None
        job_id = _safe_text(record.get("task_id"), task_id)
        if not job_id:
            return None
        try:
            image_count = max(int(record.get("total_items") or 1), 1)
        except (TypeError, ValueError):
            image_count = 1
        created = _parse_time(record.get("created_at")) or utc_now()
        updated = _parse_time(record.get("updated_at")) or created
        request = GenerationRequest.from_values(
            prompt=_safe_text(record.get("message"), "(legacy background task)"),
            source="llm_tool",
            image_count=image_count,
            requester={"session_id": _safe_text(record.get("session_id"))},
            metadata={
                "legacy_source": "background_tasks.json",
                "kind": record.get("kind"),
            },
        )
        return Job(
            job_id=job_id,
            request=request,
            state=_legacy_state(record.get("status")),
            created_at=created,
            updated_at=updated,
            error_message=record.get("message")
            if record.get("status") == "failed"
            else None,
            metadata={
                "legacy_source": "background_tasks.json",
                "routing_mode": record.get("routing_mode"),
            },
        )

    def _recover_after_crash(self) -> None:
        now = _timestamp()
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT job_id, state, result_json FROM jobs WHERE state IN (?, ?, ?, ?, ?)",
                tuple(
                    state.value
                    for state in (
                        JobState.ACCEPTED,
                        JobState.QUEUED,
                        JobState.RUNNING,
                        JobState.RESULT_READY,
                        JobState.DELIVERY_PENDING,
                    )
                ),
            ).fetchall()
            for row in rows:
                state = (
                    JobState.ORPHANED
                    if row["state"] == "accepted"
                    else JobState.INTERRUPTED
                )
                if row["state"] in {"result_ready", "delivery_pending"}:
                    state = JobState.ORPHANED
                connection.execute(
                    "UPDATE jobs SET state = ?, updated_at = ?, error_code = ?, error_message = ? WHERE job_id = ?",
                    (
                        state.value,
                        now,
                        "crash_recovery",
                        "插件重启，Job runtime ownership 已丢失",
                        row["job_id"],
                    ),
                )
                self._insert_event_locked(
                    connection,
                    JobEvent(
                        event_id=f"event-{uuid.uuid4().hex}",
                        job_id=row["job_id"],
                        event_type="crash_recovery",
                        state=state,
                        data={"previous_state": row["state"]},
                    ),
                )

    @staticmethod
    def _job_values(job: Job) -> tuple[Any, ...]:
        payload = job.as_dict()
        return (
            job.job_id,
            job.state.value,
            job.parent_job_id,
            payload["created_at"],
            payload["updated_at"],
            payload["deadline_at"],
            _json(payload["request"]),
            _json(payload["result"]) if payload["result"] is not None else None,
            job.error_code,
            job.error_message,
            int(job.cancel_requested),
            _json(payload["metadata"]),
        )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        request_values = _json_loads(row["request_json"], {})
        result_values = _json_loads(row["result_json"], None)
        payload = {
            "job_id": row["job_id"],
            "state": row["state"],
            "parent_job_id": row["parent_job_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deadline_at": row["deadline_at"],
            "request": request_values,
            "result": result_values,
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "cancel_requested": bool(row["cancel_requested"]),
            "metadata": _json_loads(row["metadata_json"], {}),
        }
        return Job.from_dict(payload)

    def _insert_event_locked(
        self, connection: sqlite3.Connection, event: JobEvent
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO job_events(
                event_id, job_id, event_type, created_at, state, data_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.job_id,
                event.event_type,
                _timestamp(event.created_at),
                event.state.value if event.state else None,
                _json(event.data),
            ),
        )

    async def create(self, job: Job) -> Job:
        def operation() -> Job:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, state, parent_job_id, created_at, updated_at,
                        deadline_at, request_json, result_json, error_code,
                        error_message, cancel_requested, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._job_values(job),
                )
                self._sync_result_artifacts_locked(connection, job)
                self._insert_event_locked(
                    connection,
                    JobEvent(
                        event_id=f"event-{uuid.uuid4().hex}",
                        job_id=job.job_id,
                        event_type="job_created",
                        state=job.state,
                    ),
                )
            return copy.deepcopy(job)

        return await asyncio.to_thread(operation)

    async def get(self, job_id: str) -> Job | None:
        def operation() -> Job | None:
            with self._lock:
                if self._closed:
                    raise RuntimeError("JobStore 已关闭")
                row = self._connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (str(job_id),)
                ).fetchone()
                return self._row_to_job(row) if row else None

        return await asyncio.to_thread(operation)

    async def save(self, job: Job) -> Job:
        def operation() -> Job:
            with self._transaction() as connection:
                previous = connection.execute(
                    "SELECT state FROM jobs WHERE job_id = ?", (job.job_id,)
                ).fetchone()
                if previous is None:
                    raise JobNotFoundError(f"Job 不存在: {job.job_id}")
                connection.execute(
                    """
                    UPDATE jobs SET state = ?, parent_job_id = ?, created_at = ?,
                        updated_at = ?, deadline_at = ?, request_json = ?,
                        result_json = ?, error_code = ?, error_message = ?,
                        cancel_requested = ?, metadata_json = ?
                    WHERE job_id = ?
                    """,
                    (*self._job_values(job)[1:], job.job_id),
                )
                if previous["state"] != job.state.value:
                    self._insert_event_locked(
                        connection,
                        JobEvent(
                            event_id=f"event-{uuid.uuid4().hex}",
                            job_id=job.job_id,
                            event_type="state_changed",
                            state=job.state,
                            data={"previous_state": previous["state"]},
                        ),
                    )
                self._sync_result_artifacts_locked(connection, job)
            return copy.deepcopy(job)

        return await asyncio.to_thread(operation)

    @staticmethod
    def _sync_result_artifacts_locked(connection: sqlite3.Connection, job: Job) -> None:
        if job.result is None:
            return
        for artifact_id in job.result.artifacts:
            connection.execute(
                """
                INSERT OR IGNORE INTO artifacts(
                    job_id, artifact_id, kind, location, mime_type,
                    size_bytes, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job.job_id, artifact_id, "image", artifact_id, None, None, "{}"),
            )

    async def append_event(self, event: JobEvent) -> None:
        def operation() -> None:
            with self._transaction() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM jobs WHERE job_id = ?", (event.job_id,)
                ).fetchone()
                if exists is None:
                    raise JobNotFoundError(f"Job 不存在: {event.job_id}")
                self._insert_event_locked(connection, event)

        await asyncio.to_thread(operation)

    async def list_events(self, job_id: str) -> list[JobEvent]:
        def operation() -> list[JobEvent]:
            with self._lock:
                rows = self._connection.execute(
                    "SELECT * FROM job_events WHERE job_id = ? ORDER BY created_at, event_id",
                    (str(job_id),),
                ).fetchall()
            events: list[JobEvent] = []
            for row in rows:
                state = row["state"]
                events.append(
                    JobEvent(
                        event_id=row["event_id"],
                        job_id=row["job_id"],
                        event_type=row["event_type"],
                        created_at=_parse_time(row["created_at"]) or utc_now(),
                        state=coerce_state(state) if state else None,
                        data=_json_loads(row["data_json"], {}),
                    )
                )
            return events

        return await asyncio.to_thread(operation)

    async def list_jobs(
        self, states: Iterable[JobState | str] | None = None
    ) -> list[Job]:
        def operation() -> list[Job]:
            with self._lock:
                if states:
                    values = [coerce_state(state).value for state in states]
                    placeholders = ",".join("?" for _ in values)
                    rows = self._connection.execute(
                        f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY created_at",
                        values,
                    ).fetchall()
                else:
                    rows = self._connection.execute(
                        "SELECT * FROM jobs ORDER BY created_at"
                    ).fetchall()
            return [self._row_to_job(row) for row in rows]

        return await asyncio.to_thread(operation)

    async def add_attempt(self, attempt: Attempt) -> Attempt:
        def operation() -> Attempt:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO attempts(
                        attempt_id, job_id, number, provider, model, status,
                        started_at, finished_at, error_code, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt.attempt_id,
                        attempt.job_id,
                        attempt.number,
                        attempt.provider,
                        attempt.model,
                        attempt.status,
                        _timestamp(attempt.started_at),
                        _timestamp(attempt.finished_at)
                        if attempt.finished_at
                        else None,
                        attempt.error_code,
                        attempt.error_message,
                    ),
                )
            return copy.deepcopy(attempt)

        return await asyncio.to_thread(operation)

    async def add_artifact(self, job_id: str, artifact: Artifact) -> Artifact:
        def operation() -> Artifact:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO artifacts(
                        job_id, artifact_id, kind, location, mime_type,
                        size_bytes, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        artifact.artifact_id,
                        artifact.kind,
                        artifact.location,
                        artifact.mime_type,
                        artifact.size_bytes,
                        _json(artifact.metadata),
                    ),
                )
            return copy.deepcopy(artifact)

        return await asyncio.to_thread(operation)

    async def add_lease(self, lease: LeaseRecord) -> LeaseRecord:
        def operation() -> LeaseRecord:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO leases(
                        lease_id, job_id, kind, status, acquired_at,
                        released_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lease.lease_id,
                        lease.job_id,
                        lease.kind,
                        lease.status,
                        _timestamp(lease.acquired_at),
                        _timestamp(lease.released_at) if lease.released_at else None,
                        _json(lease.metadata),
                    ),
                )
            return copy.deepcopy(lease)

        return await asyncio.to_thread(operation)

    async def release_lease(
        self, lease_id: str, *, released_at: datetime | None = None
    ) -> None:
        def operation() -> None:
            with self._transaction() as connection:
                connection.execute(
                    "UPDATE leases SET status = 'released', released_at = ? WHERE lease_id = ?",
                    (_timestamp(released_at), str(lease_id)),
                )

        await asyncio.to_thread(operation)

    async def list_artifacts(self, job_id: str) -> list[Artifact]:
        def operation() -> list[Artifact]:
            with self._lock:
                rows = self._connection.execute(
                    "SELECT * FROM artifacts WHERE job_id = ? ORDER BY artifact_id",
                    (str(job_id),),
                ).fetchall()
            return [
                Artifact(
                    artifact_id=row["artifact_id"],
                    kind=row["kind"],
                    location=row["location"],
                    mime_type=row["mime_type"],
                    size_bytes=row["size_bytes"],
                    metadata=_json_loads(row["metadata_json"], {}),
                )
                for row in rows
            ]

        return await asyncio.to_thread(operation)

    async def close(self) -> None:
        def operation() -> None:
            with self._lock:
                if self._closed:
                    return
                self._connection.close()
                self._closed = True

        await asyncio.to_thread(operation)

    def close_sync(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def __enter__(self) -> SQLiteJobStore:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close_sync()


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


JobStore = SQLiteJobStore
