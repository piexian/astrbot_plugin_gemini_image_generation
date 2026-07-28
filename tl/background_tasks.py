"""Persistent background task records for LLM image generation."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Coroutine
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger

TERMINAL_STATUSES = {
    "succeeded",
    "partial_success",
    "failed",
    "interrupted",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


class BackgroundTaskManager:
    """Keep session-scoped task metadata across plugin reloads."""

    def __init__(self, data_dir: str | Path, retention_hours: int = 24):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "background_tasks.json"
        self.retention_hours = max(int(retention_hours), 1)
        self._lock = asyncio.Lock()
        self._records: dict[str, dict[str, Any]] = self._load_records()
        self._runtime_tasks: dict[str, asyncio.Task[Any]] = {}
        changed = self._mark_stale_tasks_interrupted()
        changed = self._prune_expired() or changed
        if changed:
            self._save_sync()

    def _load_records(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning(f"[后台任务] 读取任务记录失败，将使用空记录: {exc}")
            return {}
        if not isinstance(raw, dict):
            return {}
        records = raw.get("tasks", raw)
        return records if isinstance(records, dict) else {}

    def _save_sync(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".json.tmp")
        payload = json.dumps(
            {"version": 1, "tasks": self._records},
            ensure_ascii=False,
            indent=2,
        )
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, self.path)

    async def _save(self) -> None:
        await asyncio.to_thread(self._save_sync)

    def _mark_stale_tasks_interrupted(self) -> bool:
        changed = False
        now_text = _timestamp()
        for record in self._records.values():
            if record.get("status") in {"queued", "running"}:
                record["status"] = "interrupted"
                record["updated_at"] = now_text
                record["message"] = "插件重启，后台任务已中断"
                changed = True
        return changed

    def _prune_expired(self) -> bool:
        cutoff = _now() - timedelta(hours=self.retention_hours)
        expired: list[str] = []
        for task_id, record in self._records.items():
            value = record.get("updated_at") or record.get("created_at")
            try:
                updated_at = datetime.fromisoformat(str(value))
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                expired.append(task_id)
                continue
            if updated_at < cutoff:
                expired.append(task_id)
        for task_id in expired:
            self._records.pop(task_id, None)
        return bool(expired)

    async def create(
        self,
        *,
        session_id: str,
        kind: str,
        routing_mode: str,
        message: str,
        total_items: int = 1,
    ) -> dict[str, Any]:
        async with self._lock:
            self._prune_expired()
            task_id = f"img-{uuid.uuid4().hex[:12]}"
            now_text = _timestamp()
            record = {
                "task_id": task_id,
                "session_id": str(session_id or "unknown"),
                "kind": kind,
                "status": "running",
                "routing_mode": routing_mode,
                "message": message,
                "created_at": now_text,
                "updated_at": now_text,
                "total_items": max(int(total_items), 1),
                "completed_items": 0,
                "succeeded_items": 0,
                "failed_items": 0,
                "current_item": None,
                "items": [],
            }
            self._records[task_id] = record
            await self._save()
            return dict(record)

    async def update(self, task_id: str, **changes: Any) -> dict[str, Any] | None:
        async with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return None
            record.update(changes)
            record["updated_at"] = _timestamp()
            await self._save()
            return dict(record)

    async def get(self, task_id: str, session_id: str) -> dict[str, Any] | None:
        async with self._lock:
            changed = self._prune_expired()
            if changed:
                await self._save()
            record = self._records.get(str(task_id or "").strip())
            if record is None:
                return None
            if record.get("session_id") != str(session_id or "unknown"):
                raise PermissionError("任务不存在或不属于当前会话")
            return dict(record)

    def attach(
        self,
        task_id: str,
        coroutine: Coroutine[Any, Any, Any],
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._runtime_tasks[task_id] = task

        def _done(done_task: asyncio.Task[Any]) -> None:
            self._runtime_tasks.pop(task_id, None)
            if done_task.cancelled():
                return
            exc = done_task.exception()
            if exc:
                logger.error(
                    f"[后台任务] {task_id} 异常终止: {exc}",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_done)
        return task

    async def close(self) -> None:
        tasks = list(self._runtime_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            self._mark_stale_tasks_interrupted()
            await self._save()
