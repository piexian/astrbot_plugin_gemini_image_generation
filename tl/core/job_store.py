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
from collections.abc import Callable, Iterable, Mapping
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

from .errors import (
    CheckpointBusyError,
    JobDeletionError,
    JobNotFoundError,
    JobStoreWriteCancelledError,
    ServiceClosedError,
    StaleJobError,
    StateTransitionError,
    redact_sensitive,
)
from .models import Artifact, Attempt, Job, JobEvent, LeaseRecord, utc_now
from .requests import GenerationRequest
from .results import GenerationResult
from .states import TERMINAL_STATES, JobState, can_transition, coerce_state

logger = logging.getLogger(__name__)
_WriteResult = TypeVar("_WriteResult")

_SCHEMA_VERSION = 5
_DEFAULT_STALE_OWNER_TIMEOUT = 60.0
_DEFAULT_RETENTION = timedelta(hours=24)
# Reject an oversized source intact, without a completion marker. An operator
# can reduce/split it and retry; never silently mark truncated input complete.
_MAX_LEGACY_JSON_BYTES = 16 * 1024 * 1024
_MAX_LEGACY_RECORDS = 10_000
_MAX_LEGACY_ITEMS = 1_000
_MAX_LEGACY_ARTIFACTS = 1_000
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


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field_name} 必须是带时区的 datetime")
    return value.astimezone(timezone.utc)


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


def _validate_database_name(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("database_name 必须是文件名")
    path = Path(value)
    if path.is_absolute() or len(path.parts) != 1 or path.parts[0] in {".", ".."}:
        raise ValueError("database_name 不得逃出 data_dir")
    return value


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


def _legacy_url(value: str) -> str:
    """Retain a source locator, never URL credentials or signed query data."""
    if not value.lower().startswith(("http://", "https://")):
        return value
    parsed = urlsplit(value)
    if not parsed.hostname:
        raise ValueError("legacy URL 缺少 hostname")
    return urlunsplit(
        (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", "")
    )


def _legacy_safe_value(value: Any, depth: int = 0) -> Any:
    if depth > 32:
        raise ValueError("legacy 元数据嵌套过深")
    if isinstance(value, str):
        return _legacy_url(value)
    if isinstance(value, Mapping):
        return {key: _legacy_safe_value(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_legacy_safe_value(item, depth + 1) for item in value]
    return value


def _legacy_refs(record: Mapping[str, Any], field: str) -> list[str]:
    values = record.get(field) or []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list) or len(values) > _MAX_LEGACY_ARTIFACTS:
        raise ValueError("legacy 图片列表无效或超过数量限制")
    if any(not isinstance(value, str) for value in values):
        raise ValueError("legacy 图片引用必须是字符串")
    return [_legacy_url(value.strip()) for value in values if value.strip()]


def _legacy_result(
    record: Mapping[str, Any], state: JobState, items: list[dict[str, Any]]
) -> GenerationResult | None:
    references: dict[str, None] = {}
    texts: list[str] = []
    for source in [record, *items]:
        for field in ("image_urls", "image_paths", "images", "source_urls"):
            references.update(dict.fromkeys(_legacy_refs(source, field)))
            if len(references) > _MAX_LEGACY_ARTIFACTS:
                raise ValueError("legacy 图片总数超过限制")
        text = source.get("text_content")
        if text is not None and not isinstance(text, str):
            raise ValueError("legacy text_content 必须是字符串")
        if text and text not in texts:
            texts.append(text)

    stats = record.get("stats") or {}
    if not isinstance(stats, Mapping):
        raise ValueError("legacy stats 必须是对象")
    params = record.get("params") or {}
    if not isinstance(params, Mapping):
        params = {}

    def identity(name: str) -> str | None:
        value = (
            record.get(name)
            or stats.get(name)
            or stats.get(f"successful_{name}")
            or params.get(name)
        )
        if not value:
            # Mixed-provider batches retain per-item identity in metadata, not a
            # misleading single provider/model on the aggregate result.
            values = {item[name] for item in items if isinstance(item.get(name), str)}
            value = next(iter(values)) if len(values) == 1 else None
        if value is not None and not isinstance(value, str):
            raise ValueError("legacy provider/model 必须是字符串")
        return value or None

    provider, model = identity("provider"), identity("model")
    if not (references or texts or stats or provider or model) and state not in {
        JobState.SUCCEEDED,
        JobState.PARTIAL,
    }:
        return None
    return GenerationResult(
        # Match list_artifacts' ordering and collapse duplicate references.
        artifacts=tuple(sorted(references)),
        text="\n".join(texts) or None,
        provider=provider,
        model=model,
        partial=state is JobState.PARTIAL,
        stats=redact_sensitive(_legacy_safe_value(stats)),
    )


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
        stale_owner_timeout: float = _DEFAULT_STALE_OWNER_TIMEOUT,
        retention: timedelta = _DEFAULT_RETENTION,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.database_name = _validate_database_name(database_name)
        self.path = self.data_dir / self.database_name
        self._lock = threading.RLock()
        self._closed = False
        self._broken = False
        if not isinstance(retention, timedelta) or retention < timedelta(0):
            raise ValueError("retention 必须是非负 timedelta")
        self.retention = retention
        if stale_owner_timeout <= 0:
            raise ValueError("stale_owner_timeout 必须是正数")
        self.stale_owner_timeout = float(stale_owner_timeout)
        self.owner_id = uuid.uuid4().hex
        self.process_id = os.getpid()
        self.started_at = _timestamp()
        self.heartbeat_at = self.started_at
        self.released_at: str | None = None
        self._owner_registered = False
        self._should_recover = False
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._harden_permissions()
        self._connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._configure_connection()
            self._harden_permissions()
            self._migrate()
            self._should_recover = self._register_owner()
            if import_legacy:
                self._import_legacy_files()
            if self._should_recover:
                self._recover_after_crash()
        except BaseException:
            self._release_owner_best_effort()
            self._rollback_best_effort()
            self._mark_broken()
            raise

    def _ensure_open(self) -> None:
        if self._broken:
            raise ServiceClosedError("JobStore 连接已损坏，请重新打开")
        if self._closed:
            raise ServiceClosedError("JobStore 已关闭")

    def _harden_permissions(self) -> None:
        """Keep the database and SQLite sidecars private to this account."""
        try:
            os.chmod(self.data_dir, 0o700)
            for path in (
                self.path,
                Path(f"{self.path}-wal"),
                Path(f"{self.path}-shm"),
            ):
                if path.exists():
                    os.chmod(path, 0o600)
        except OSError as exc:
            raise ServiceClosedError("JobStore 数据文件权限设置失败") from exc

    def _mark_broken(self) -> None:
        # Set before close: even if close fails, this connection cannot be reused.
        self._broken = True
        try:
            self.close_sync()
        except BaseException:
            logger.error("JobStore 损坏连接关闭失败，连接已停用")

    def _release_owner_best_effort(self) -> None:
        if not self._owner_registered or self._closed:
            return
        try:
            with self._lock:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    "UPDATE store_owners SET released_at = ? WHERE owner_id = ?",
                    (_timestamp(), self.owner_id),
                )
                self._connection.execute("COMMIT")
                self.released_at = _timestamp()
                self._owner_registered = False
        except BaseException:
            self._rollback_best_effort()

    def _rollback_best_effort(self) -> None:
        try:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
        except BaseException:
            self._mark_broken()

    async def _run_write(self, operation: Callable[[], _WriteResult]) -> _WriteResult:
        """Own the worker until it finishes, including repeated cancellation.

        run_in_executor is the thread primitive behind to_thread; retaining its
        Future allows shielding without spawning a detached asyncio Task. Keep
        to_thread's context propagation. No cancellation is treated as rollback.
        """
        worker = asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, operation
        )
        cancellation: asyncio.CancelledError | None = None
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
            except BaseException:
                # Retrieve the worker exception below, including if cancellation
                # was already received while the transaction was still running.
                break
        try:
            result = worker.result()
        except BaseException as exc:
            if cancellation is not None:
                raise JobStoreWriteCancelledError(
                    *cancellation.args, outcome="failed", error=exc
                ) from exc
            raise
        if cancellation is not None:
            raise JobStoreWriteCancelledError(
                *cancellation.args, outcome="committed", result=result
            ) from cancellation
        return result

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
                try:
                    self.store._ensure_open()
                    self.store._connection.execute("BEGIN IMMEDIATE")
                    self.store._heartbeat_locked(self.store._connection)
                except BaseException:
                    self.store._rollback_best_effort()
                    self.store._lock.release()
                    raise
                return self.store._connection

            def __exit__(self, exc_type, exc, traceback):
                try:
                    if exc_type is None:
                        try:
                            self.store._connection.execute("COMMIT")
                            self.store._harden_permissions()
                        except BaseException:
                            self.store._rollback_best_effort()
                            self.store._mark_broken()
                            raise
                    else:
                        self.store._rollback_best_effort()
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
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            needs_revision_column = "revision" not in columns
            if needs_revision_column or current < 2:
                with self._transaction():
                    if needs_revision_column:
                        connection.execute(
                            "ALTER TABLE jobs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
                        )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                        VALUES (2, ?)
                        """,
                        (_timestamp(),),
                    )
                current = 2
            owner_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_owners'"
            ).fetchone()
            if owner_table is None or current < 3:
                with self._transaction() as migration_connection:
                    migration_connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS store_owners (
                            owner_id TEXT PRIMARY KEY,
                            process_id INTEGER NOT NULL,
                            started_at TEXT NOT NULL,
                            heartbeat_at TEXT NOT NULL,
                            released_at TEXT
                        )
                        """
                    )
                    migration_connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_store_owners_active
                        ON store_owners(released_at, heartbeat_at)
                        """
                    )
                    migration_connection.execute(
                        """
                        INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                        VALUES (3, ?)
                        """,
                        (_timestamp(),),
                    )
                current = 3
            legacy_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'legacy_imports'"
            ).fetchone()
            if legacy_table is None or current < 4:
                with self._transaction() as migration_connection:
                    migration_connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS legacy_imports (
                            source TEXT PRIMARY KEY,
                            completed_at TEXT NOT NULL,
                            record_count INTEGER NOT NULL,
                            imported_count INTEGER NOT NULL,
                            skipped_count INTEGER NOT NULL
                        )
                        """
                    )
                    migration_connection.execute(
                        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (4, ?)",
                        (_timestamp(),),
                    )
                current = 4
            if current < 5:
                with self._transaction() as migration_connection:
                    # No FK: this minimal deletion record must outlive the Job
                    # and its cascaded events, without retaining prompts/results.
                    migration_connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS job_deletions (
                            deletion_id TEXT PRIMARY KEY,
                            job_id TEXT NOT NULL,
                            state TEXT NOT NULL,
                            revision INTEGER NOT NULL,
                            deleted_at TEXT NOT NULL,
                            reason TEXT NOT NULL
                        )
                        """
                    )
                    migration_connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_job_deletions_job ON job_deletions(job_id)"
                    )
                    migration_connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (5, ?)",
                        (_timestamp(),),
                    )
                current = 5
            if current != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"不支持的 JobStore schema 版本: {current}，期望 {_SCHEMA_VERSION}"
                )

    def _register_owner(self) -> bool:
        """Register this process and return whether stale jobs may recover.

        ``accepted`` is deliberately recovered as ``orphaned`` because no
        runtime task can be proven to own it after a process boundary.
        """
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - self.stale_owner_timeout
        now_text = _timestamp(now)
        with self._transaction() as connection:
            live_owner = False
            rows = connection.execute(
                "SELECT owner_id, heartbeat_at FROM store_owners WHERE released_at IS NULL"
            ).fetchall()
            for row in rows:
                heartbeat = _parse_time(row["heartbeat_at"])
                is_stale = heartbeat is None or heartbeat.timestamp() < cutoff
                if is_stale:
                    connection.execute(
                        "UPDATE store_owners SET released_at = ? WHERE owner_id = ?",
                        (now_text, row["owner_id"]),
                    )
                else:
                    live_owner = True
            connection.execute(
                """
                INSERT INTO store_owners(
                    owner_id, process_id, started_at, heartbeat_at, released_at
                ) VALUES (?, ?, ?, ?, NULL)
                """,
                (
                    self.owner_id,
                    self.process_id,
                    self.started_at,
                    now_text,
                ),
            )
        self._owner_registered = True
        self.heartbeat_at = now_text
        return not live_owner

    def _heartbeat_locked(self, connection: sqlite3.Connection) -> None:
        if not self._owner_registered:
            return
        now_text = _timestamp()
        result = connection.execute(
            """
            UPDATE store_owners SET heartbeat_at = ?
            WHERE owner_id = ? AND released_at IS NULL
            """,
            (now_text, self.owner_id),
        )
        if result.rowcount != 1:
            raise ServiceClosedError("JobStore owner 已失效，请重新打开")
        self.heartbeat_at = now_text

    def _read_json_file(self, path: Path) -> Any:
        try:
            with path.open("rb") as source:
                raw = source.read(_MAX_LEGACY_JSON_BYTES + 1)
            if len(raw) > _MAX_LEGACY_JSON_BYTES:
                logger.warning("JobStore 跳过超大旧 JSON %s，未标记迁移完成", path.name)
                return None
            return json.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            return None
        except Exception as exc:
            self._backup_corrupt(path)
            logger.warning(
                "JobStore 忽略损坏的旧 JSON %s: %s", path.name, type(exc).__name__
            )
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
        for source in ("generation_history.json", "background_tasks.json"):
            with self._lock:
                if self._connection.execute(
                    "SELECT 1 FROM legacy_imports WHERE source = ?", (source,)
                ).fetchone():
                    continue
            payload = self._read_json_file(self.data_dir / source)
            if not isinstance(payload, Mapping):
                continue
            if source == "generation_history.json":
                records = payload.get("jobs")
                if not isinstance(records, list):
                    continue
                entries = enumerate(records)
            else:
                records = payload.get("tasks", payload)
                if not isinstance(records, Mapping):
                    continue
                entries = records.items()
            if len(records) > _MAX_LEGACY_RECORDS:
                logger.warning("JobStore 跳过记录数超限的 %s，未标记迁移完成", source)
                continue

            # Jobs, artifacts, events and the source marker commit together.
            # Recheck the marker inside the write transaction for concurrent opens.
            with self._transaction() as connection:
                if connection.execute(
                    "SELECT 1 FROM legacy_imports WHERE source = ?", (source,)
                ).fetchone():
                    continue
                imported = skipped = 0
                for key, record in entries:
                    try:
                        job = (
                            self._job_from_history(record)
                            if source == "generation_history.json"
                            else self._job_from_background(str(key), record)
                        )
                        if job is None:
                            skipped += 1
                            continue
                        values = self._job_values(job)
                        # Validate encoded fields before SQL so one invalid JSON
                        # record cannot roll back its valid siblings.
                        for value in values:
                            if value is not None and not isinstance(value, (str, int)):
                                raise ValueError("legacy Job 字段类型无效")
                            if isinstance(value, str):
                                value.encode("utf-8")
                    except (
                        ValueError,
                        TypeError,
                        OverflowError,
                        RecursionError,
                    ) as exc:
                        skipped += 1
                        logger.warning(
                            "JobStore 跳过 %s 中的坏记录: %s",
                            source,
                            type(exc).__name__,
                        )
                        continue
                    inserted = connection.execute(
                        """
                        INSERT OR IGNORE INTO jobs(
                            job_id, state, revision, parent_job_id, created_at, updated_at,
                            deadline_at, request_json, result_json, error_code,
                            error_message, cancel_requested, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    ).rowcount
                    if not inserted:
                        skipped += 1
                        continue
                    self._sync_result_artifacts_locked(connection, job)
                    self._insert_event_locked(
                        connection,
                        JobEvent(
                            event_id=f"event-{uuid.uuid4().hex}",
                            job_id=job.job_id,
                            event_type="legacy_import",
                            state=job.state,
                            data={"source": source},
                        ),
                    )
                    imported += 1
                connection.execute(
                    """
                    INSERT INTO legacy_imports(
                        source, completed_at, record_count, imported_count, skipped_count
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (source, _timestamp(), len(records), imported, skipped),
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
        result = _legacy_result(record, state, [])
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
                "source_urls": _legacy_refs(record, "source_urls"),
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
        raw_items = record.get("items") or []
        if not isinstance(raw_items, list) or len(raw_items) > _MAX_LEGACY_ITEMS:
            raise ValueError("legacy items 无效或数量超限")
        items = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            try:
                clean_item = redact_sensitive(_legacy_safe_value(item))
                # A bad batch item must not hide output from its valid siblings.
                _legacy_result(clean_item, JobState.SUCCEEDED, [])
            except (ValueError, TypeError, RecursionError):
                continue
            items.append(clean_item)
        state = _legacy_state(record.get("status"))
        result = _legacy_result(record, state, items)
        request = GenerationRequest.from_values(
            prompt=_safe_text(record.get("message"), "(legacy background task)"),
            source="llm_tool",
            image_count=image_count,
            provider=result.provider if result else None,
            model=result.model if result else None,
            requester={"session_id": _safe_text(record.get("session_id"))},
            metadata={
                "legacy_source": "background_tasks.json",
                "kind": record.get("kind"),
            },
        )
        return Job(
            job_id=job_id,
            request=request,
            state=state,
            created_at=created,
            updated_at=updated,
            result=result,
            error_message=record.get("message")
            if record.get("status") == "failed"
            else None,
            metadata={
                "legacy_source": "background_tasks.json",
                "routing_mode": record.get("routing_mode"),
                "items": items,
            },
        )

    def _recover_after_crash(self) -> None:
        now = _timestamp()
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT job_id, state, revision FROM jobs WHERE state IN (?, ?, ?, ?, ?)",
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
                if row["state"] == "result_ready":
                    state = JobState.DELIVERY_PENDING
                elif row["state"] == "delivery_pending":
                    state = JobState.DELIVERY_PENDING
                previous_revision = int(row["revision"])
                new_revision = previous_revision + 1
                connection.execute(
                    """
                    UPDATE jobs SET state = ?, revision = ?, updated_at = ?,
                        error_code = ?, error_message = ?
                    WHERE job_id = ?
                    """,
                    (
                        state.value,
                        new_revision,
                        now,
                        "crash_recovery",
                        "插件重启，Job runtime ownership 已丢失",
                        row["job_id"],
                    ),
                )
                attempts = connection.execute(
                    """
                    SELECT attempt_id FROM attempts
                    WHERE job_id = ? AND status = 'started' AND finished_at IS NULL
                    """,
                    (row["job_id"],),
                ).fetchall()
                for attempt in attempts:
                    connection.execute(
                        """
                        UPDATE attempts SET status = 'interrupted', finished_at = ?
                        WHERE attempt_id = ?
                        """,
                        (now, attempt["attempt_id"]),
                    )
                    self._insert_event_locked(
                        connection,
                        JobEvent(
                            event_id=f"event-{uuid.uuid4().hex}",
                            job_id=row["job_id"],
                            event_type="attempt_recovery",
                            state=state,
                            data={
                                "attempt_id": attempt["attempt_id"],
                                "status": "interrupted",
                                "finished_at": now,
                            },
                        ),
                    )
                active_leases = connection.execute(
                    """
                    SELECT lease_id FROM leases
                    WHERE job_id = ? AND status = 'active'
                    """,
                    (row["job_id"],),
                ).fetchall()
                for lease in active_leases:
                    connection.execute(
                        """
                        UPDATE leases SET status = 'recovery_pending', released_at = ?
                        WHERE lease_id = ?
                        """,
                        (now, lease["lease_id"]),
                    )
                    self._insert_event_locked(
                        connection,
                        JobEvent(
                            event_id=f"event-{uuid.uuid4().hex}",
                            job_id=row["job_id"],
                            event_type="lease_recovery",
                            state=state,
                            data={
                                "lease_id": lease["lease_id"],
                                "status": "recovery_pending",
                                "released_at": now,
                            },
                        ),
                    )
                self._insert_event_locked(
                    connection,
                    JobEvent(
                        event_id=f"event-{uuid.uuid4().hex}",
                        job_id=row["job_id"],
                        event_type="crash_recovery",
                        state=state,
                        data={
                            "previous_state": row["state"],
                            "new_state": state.value,
                            "previous_revision": previous_revision,
                            "new_revision": new_revision,
                            "attempts_recovered": len(attempts),
                            "leases_recovered": len(active_leases),
                        },
                    ),
                )

    @staticmethod
    def _job_values(job: Job) -> tuple[Any, ...]:
        payload = job.to_storage_dict()
        return (
            job.job_id,
            job.state.value,
            job.revision,
            job.parent_job_id,
            payload["created_at"],
            payload["updated_at"],
            payload["deadline_at"],
            _json(payload["request"]),
            _json(payload["result"]) if payload["result"] is not None else None,
            payload["error_code"],
            payload["error_message"],
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
            "revision": row["revision"],
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
        payload = event.to_storage_dict()
        connection.execute(
            """
            INSERT OR IGNORE INTO job_events(
                event_id, job_id, event_type, created_at, state, data_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload["event_id"],
                payload["job_id"],
                payload["event_type"],
                payload["created_at"],
                payload["state"],
                _json(payload["data"]),
            ),
        )

    async def create(self, job: Job) -> Job:
        self._ensure_open()
        job = copy.deepcopy(job)

        def operation() -> Job:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, state, revision, parent_job_id, created_at, updated_at,
                        deadline_at, request_json, result_json, error_code,
                        error_message, cancel_requested, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            return job

        return await self._run_write(operation)

    async def get(self, job_id: str) -> Job | None:
        def operation() -> Job | None:
            with self._lock:
                self._ensure_open()
                self._heartbeat_locked(self._connection)
                row = self._connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (str(job_id),)
                ).fetchone()
                return self._row_to_job(row) if row else None

        return await asyncio.to_thread(operation)

    async def save(self, job: Job, *, expected_revision: int | None = None) -> Job:
        self._ensure_open()
        expected = job.revision if expected_revision is None else expected_revision
        if type(expected) is not int or expected < 0:
            raise ValueError("expected_revision 必须是非负整数")
        caller_job = job
        job = copy.deepcopy(job)

        def operation() -> Job:
            with self._transaction() as connection:
                previous = connection.execute(
                    "SELECT state, revision FROM jobs WHERE job_id = ?",
                    (job.job_id,),
                ).fetchone()
                if previous is None:
                    raise JobNotFoundError(f"Job 不存在: {job.job_id}")
                previous_revision = int(previous["revision"])
                if previous_revision != expected:
                    raise StaleJobError(
                        f"Job 快照已过期: {job.job_id}",
                        details={
                            "job_id": job.job_id,
                            "expected_revision": expected,
                            "current_revision": previous_revision,
                        },
                    )

                previous_state = coerce_state(previous["state"])
                target_state = coerce_state(job.state)
                if target_state != previous_state and not can_transition(
                    previous_state, target_state
                ):
                    raise StateTransitionError(
                        f"Job {job.job_id} 不能从 {previous_state.value} 转为 {target_state.value}",
                        details={
                            "job_id": job.job_id,
                            "from": previous_state.value,
                            "to": target_state.value,
                            "revision": previous_revision,
                        },
                    )

                values = self._job_values(job)
                result = connection.execute(
                    """
                    UPDATE jobs SET state = ?, parent_job_id = ?, created_at = ?,
                        updated_at = ?, deadline_at = ?, request_json = ?,
                        result_json = ?, error_code = ?, error_message = ?,
                        cancel_requested = ?, metadata_json = ?,
                        revision = revision + 1
                    WHERE job_id = ? AND revision = ?
                    """,
                    (
                        values[1],
                        values[3],
                        values[4],
                        values[5],
                        values[6],
                        values[7],
                        values[8],
                        values[9],
                        values[10],
                        values[11],
                        values[12],
                        job.job_id,
                        expected,
                    ),
                )
                if result.rowcount == 0:
                    raise StaleJobError(
                        f"Job 快照已过期: {job.job_id}",
                        details={
                            "job_id": job.job_id,
                            "expected_revision": expected,
                        },
                    )

                new_revision = expected + 1
                if target_state != previous_state:
                    self._insert_event_locked(
                        connection,
                        JobEvent(
                            event_id=f"event-{uuid.uuid4().hex}",
                            job_id=job.job_id,
                            event_type="state_changed",
                            state=target_state,
                            data={
                                "previous_state": previous_state.value,
                                "new_state": target_state.value,
                                "previous_revision": previous_revision,
                                "new_revision": new_revision,
                            },
                        ),
                    )
                self._sync_result_artifacts_locked(connection, job)
                job.revision = new_revision
            return job

        try:
            saved = await self._run_write(operation)
        except JobStoreWriteCancelledError as exc:
            if exc.outcome == "committed":
                caller_job.revision = exc.result.revision
            raise
        caller_job.revision = saved.revision
        return saved

    @staticmethod
    def _sync_result_artifacts_locked(connection: sqlite3.Connection, job: Job) -> None:
        if job.result is None:
            return
        for artifact_id in job.result.to_storage_dict()["artifacts"]:
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
        self._ensure_open()
        event = copy.deepcopy(event)

        def operation() -> None:
            with self._transaction() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM jobs WHERE job_id = ?", (event.job_id,)
                ).fetchone()
                if exists is None:
                    raise JobNotFoundError(f"Job 不存在: {event.job_id}")
                self._insert_event_locked(connection, event)

        await self._run_write(operation)

    async def list_events(self, job_id: str) -> list[JobEvent]:
        def operation() -> list[JobEvent]:
            with self._lock:
                self._ensure_open()
                self._heartbeat_locked(self._connection)
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
                self._ensure_open()
                self._heartbeat_locked(self._connection)
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
        self._ensure_open()
        attempt = copy.deepcopy(attempt)

        def operation() -> Attempt:
            values = attempt.to_storage_dict()
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO attempts(
                        attempt_id, job_id, number, provider, model, status,
                        started_at, finished_at, error_code, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        values["attempt_id"],
                        values["job_id"],
                        values["number"],
                        values["provider"],
                        values["model"],
                        values["status"],
                        values["started_at"],
                        values["finished_at"],
                        values["error_code"],
                        values["error_message"],
                    ),
                )
            return attempt

        return await self._run_write(operation)

    async def add_artifact(self, job_id: str, artifact: Artifact) -> Artifact:
        self._ensure_open()
        artifact = copy.deepcopy(artifact)

        def operation() -> Artifact:
            values = artifact.to_storage_dict()
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
                        values["artifact_id"],
                        values["kind"],
                        values["location"],
                        values["mime_type"],
                        values["size_bytes"],
                        _json(values["metadata"]),
                    ),
                )
            return artifact

        return await self._run_write(operation)

    async def add_lease(self, lease: LeaseRecord) -> LeaseRecord:
        self._ensure_open()
        lease = copy.deepcopy(lease)

        def operation() -> LeaseRecord:
            values = lease.to_storage_dict()
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO leases(
                        lease_id, job_id, kind, status, acquired_at,
                        released_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        values["lease_id"],
                        values["job_id"],
                        values["kind"],
                        values["status"],
                        values["acquired_at"],
                        values["released_at"],
                        _json(values["metadata"]),
                    ),
                )
            return lease

        return await self._run_write(operation)

    async def release_lease(
        self, lease_id: str, *, released_at: datetime | None = None
    ) -> None:
        self._ensure_open()

        def operation() -> None:
            with self._transaction() as connection:
                connection.execute(
                    "UPDATE leases SET status = 'released', released_at = ? WHERE lease_id = ?",
                    (_timestamp(released_at), str(lease_id)),
                )

        await self._run_write(operation)

    async def list_artifacts(self, job_id: str) -> list[Artifact]:
        def operation() -> list[Artifact]:
            with self._lock:
                self._ensure_open()
                self._heartbeat_locked(self._connection)
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

    @staticmethod
    def _deletion_blocker(
        connection: sqlite3.Connection, row: sqlite3.Row, cutoff: datetime
    ) -> str | None:
        if row["state"] not in TERMINAL_STATES:
            return "non_terminal"
        updated = _parse_time(row["updated_at"])
        if updated is None or updated >= cutoff:
            return "retention"
        # Expiry or a recovery marker is not proof of external resource release.
        # Retain compensation records, including quota/limiter reservations.
        if connection.execute(
            "SELECT 1 FROM leases WHERE job_id = ? AND status <> 'released' LIMIT 1",
            (row["job_id"],),
        ).fetchone():
            return "unreleased_lease"
        return None

    @staticmethod
    def _delete_job_locked(
        connection: sqlite3.Connection, row: sqlite3.Row, *, now: datetime, reason: str
    ) -> None:
        connection.execute(
            """
            INSERT INTO job_deletions(deletion_id, job_id, state, revision, deleted_at, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"deletion-{uuid.uuid4().hex}", row["job_id"], row["state"],
                row["revision"], _timestamp(now), reason,
            ),
        )
        # foreign_keys=ON: attempts/artifacts/leases/job_events cascade within
        # this transaction. This API never unlinks physical artifact files.
        connection.execute("DELETE FROM jobs WHERE job_id = ?", (row["job_id"],))

    async def delete_job(self, job_id: str) -> None:
        """Delete an eligible Job; missing ids are an idempotent no-op.

        Eligibility uses terminal state and updated_at older than retention
        (default 24 hours). JobDeletionError identifies a blocking constraint.
        """
        self._ensure_open()

        def operation() -> None:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT job_id, state, revision, updated_at FROM jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    return
                now = utc_now()
                reason = self._deletion_blocker(connection, row, now - self.retention)
                if reason:
                    raise JobDeletionError(
                        "Job 尚不可删除",
                        details={"job_id": job_id, "reason": reason},
                    )
                self._delete_job_locked(connection, row, now=now, reason="delete_job")

        await self._run_write(operation)

    async def prune_jobs(self, before: datetime, states: set[JobState]) -> int:
        """Prune requested terminal states, respecting both cutoff and retention.

        Active states in the filter are ignored. All eligibility checks, audit
        inserts and cascading deletes share one write transaction.
        """
        self._ensure_open()
        before = _aware_utc(before, "before")
        selected = sorted({coerce_state(state) for state in states} & TERMINAL_STATES)

        def operation() -> int:
            with self._transaction() as connection:
                if not selected:
                    return 0
                now = utc_now()
                cutoff = min(before, now - self.retention)
                placeholders = ",".join("?" for _ in selected)
                rows = connection.execute(
                    f"""
                    SELECT job_id, state, revision, updated_at FROM jobs
                    WHERE state IN ({placeholders})
                        AND julianday(updated_at) < julianday(?)
                    ORDER BY updated_at, job_id
                    """,
                    (*selected, _timestamp(cutoff)),
                ).fetchall()
                count = 0
                for row in rows:
                    if self._deletion_blocker(connection, row, cutoff) is not None:
                        continue
                    self._delete_job_locked(
                        connection, row, now=now, reason="retention_prune"
                    )
                    count += 1
                return count

        return await self._run_write(operation)

    async def expire_leases(self, now: datetime) -> int:
        """Expire active leases whose metadata.expires_at (aware ISO time) is due.

        Missing/invalid deadlines never authorize expiry. An expired lease still
        protects its Job until release_lease confirms external cleanup; expiry
        is recorded in a Job event, without falsely setting released_at.
        """
        self._ensure_open()
        now = _aware_utc(now, "now")

        def operation() -> int:
            with self._transaction() as connection:
                rows = connection.execute(
                    "SELECT lease_id, job_id, metadata_json FROM leases WHERE status = 'active'"
                ).fetchall()
                count = 0
                for row in rows:
                    metadata = _json_loads(row["metadata_json"], {})
                    if not isinstance(metadata, dict):
                        continue
                    expiry = metadata.get("expires_at")
                    if not isinstance(expiry, str):
                        continue
                    try:
                        expiry = _aware_utc(datetime.fromisoformat(expiry), "expires_at")
                    except ValueError:
                        continue
                    if expiry > now:
                        continue
                    connection.execute(
                        "UPDATE leases SET status = 'expired' WHERE lease_id = ?",
                        (row["lease_id"],),
                    )
                    self._insert_event_locked(
                        connection,
                        JobEvent(
                            event_id=f"event-{uuid.uuid4().hex}",
                            job_id=row["job_id"],
                            event_type="lease_expired",
                            created_at=now,
                            data={
                                "lease_id": row["lease_id"],
                                "expires_at": _timestamp(expiry),
                                "status": "expired",
                            },
                        ),
                    )
                    count += 1
                return count

        return await self._run_write(operation)

    async def checkpoint(self) -> None:
        """Flush and truncate WAL outside a transaction, preserving cancellation.

        SQLite reports readers blocking TRUNCATE in its result row, not always
        as an exception. A busy checkpoint does not invalidate the connection.
        """
        self._ensure_open()

        def operation() -> None:
            with self._lock:
                self._ensure_open()
                self._heartbeat_locked(self._connection)
                busy, _, _ = self._connection.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
                if busy:
                    raise CheckpointBusyError("WAL checkpoint 被其他连接占用，请稍后重试")

        await self._run_write(operation)

    async def close(self) -> None:
        await self._run_write(self.close_sync)

    def close_sync(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._owner_registered and not self._broken:
                try:
                    if self._connection.in_transaction:
                        self._connection.execute("ROLLBACK")
                    self._connection.execute("BEGIN IMMEDIATE")
                    released = _timestamp()
                    self._connection.execute(
                        "UPDATE store_owners SET released_at = ? WHERE owner_id = ?",
                        (released, self.owner_id),
                    )
                    self._connection.execute("COMMIT")
                    self.released_at = released
                    self._owner_registered = False
                except BaseException:
                    self._rollback_best_effort()
            try:
                self._harden_permissions()
                self._connection.close()
            finally:
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
