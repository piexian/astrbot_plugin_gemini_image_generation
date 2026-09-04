"""图像生成任务追踪、持久化与 SSE 扇出。"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
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
_KNOWN_SOURCES = {"command", "llm_tool", "llm_batch", "webui"}
_SAFE_FILE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_PARAM_KEYS = (
    "resolution",
    "aspect_ratio",
    "provider",
    "model",
    "candidate_id",
    "image_count",
    "quality",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _bounded_text(value: Any, limit: int) -> str:
    try:
        return str(value or "")[:limit]
    except Exception:
        return ""


def _positive_int(value: Any, default: int = 1) -> int:
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return default


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class TrackingContext:
    source: str
    parent_job_id: str | None = None
    item_name: str | None = None


_TRACKING_CONTEXT: ContextVar[TrackingContext | None] = ContextVar(
    "gemini_image_generation_tracking_context",
    default=None,
)


@contextmanager
def tracking_context(
    source: str,
    parent_job_id: str | None = None,
    item_name: str | None = None,
) -> Iterator[TrackingContext]:
    """在当前异步调用链中传播任务来源和父子关系。"""
    current = _TRACKING_CONTEXT.get()
    value = TrackingContext(
        source=source if source in _KNOWN_SOURCES else "command",
        parent_job_id=_bounded_text(
            parent_job_id
            if parent_job_id is not None
            else getattr(current, "parent_job_id", None),
            128,
        )
        or None,
        item_name=_bounded_text(
            item_name if item_name is not None else getattr(current, "item_name", None),
            64,
        )
        or None,
    )
    token = _TRACKING_CONTEXT.set(value)
    try:
        yield value
    finally:
        _TRACKING_CONTEXT.reset(token)


def current_tracking_context() -> TrackingContext | None:
    return _TRACKING_CONTEXT.get()


def _event_value(event: Any, method_name: str) -> Any:
    try:
        method = getattr(event, method_name, None)
        return method() if callable(method) else None
    except Exception:
        return None


def requester_from_event(event: Any) -> dict[str, str]:
    """从不同平台事件中尽力提取最小化的请求者元数据。"""
    try:
        message_obj = getattr(event, "message_obj", None)
    except Exception:
        message_obj = None

    user_id = _event_value(event, "get_sender_id")
    user_name = _event_value(event, "get_sender_name")
    group_id = _event_value(event, "get_group_id")

    if message_obj is not None:
        try:
            user_id = user_id or getattr(message_obj, "sender_id", None)
            group_id = group_id or getattr(message_obj, "group_id", None)
            sender = getattr(message_obj, "sender", None)
            if sender is not None and not user_name:
                user_name = (
                    getattr(sender, "card", None)
                    or getattr(sender, "nickname", None)
                    or getattr(sender, "name", None)
                )
        except Exception:
            pass

    return {
        "user_id": _bounded_text(user_id, 128),
        "user_name": _bounded_text(user_name, 200),
        "group_id": _bounded_text(group_id, 128),
    }


class GenerationTracker:
    """维护可恢复的生成记录，并向多个 SSE 订阅者广播快照。"""

    def __init__(
        self,
        data_dir: str | Path,
        max_records: int,
        *,
        enabled: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "generation_history.json"
        self.gallery_dir = self.data_dir / "gallery"
        self.max_records = max(int(max_records), 1)
        self.enabled = bool(enabled)
        self.gallery_lock = threading.RLock()
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._closed = False
        self._jobs = self._load_jobs() if self.enabled else {}
        changed_records = self._mark_running_interrupted()
        pruned = self._prune_locked()
        if self.enabled and (changed_records or pruned):
            self._save_sync()

    @staticmethod
    def _snapshot(value: Any) -> Any:
        return copy.deepcopy(value)

    def _load_jobs(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            jobs = payload.get("jobs") if isinstance(payload, dict) else None
            if not isinstance(jobs, list):
                raise ValueError("jobs 字段不是列表")
        except FileNotFoundError:
            return {}
        except Exception as exc:
            self._backup_corrupt_history()
            logger.warning(f"[生成历史] 历史文件损坏，已从空记录恢复: {exc}")
            return {}

        records: dict[str, dict[str, Any]] = {}
        for record in jobs:
            if not isinstance(record, dict):
                continue
            job_id = str(record.get("job_id") or "").strip()
            if job_id:
                records[job_id] = record
        return records

    def _backup_corrupt_history(self) -> None:
        if not self.path.exists():
            return
        stamp = _now().strftime("%Y%m%dT%H%M%S%fZ")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            os.replace(self.path, backup)
        except OSError as exc:
            logger.warning(f"[生成历史] 损坏文件备份失败: {exc}")

    def _save_sync(self) -> None:
        if not self.enabled:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".json.tmp")
        payload = json.dumps(
            {"version": 1, "jobs": list(self._jobs.values())},
            ensure_ascii=False,
            indent=2,
        )
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, self.path)

    async def _save(self) -> None:
        if not self.enabled:
            return
        await asyncio.to_thread(self._save_sync)

    def import_legacy(self, records: list[dict[str, Any]]) -> int:
        """启动时批量补录历史图片记录（同步、仅在初始化期调用）。

        以记录内图片文件名去重，已存在的文件不重复建档。
        """
        if not self.enabled or not records:
            return 0
        known_images = {
            str(name)
            for record in self._jobs.values()
            for name in (record.get("images") or [])
        }
        imported = 0
        for record in records:
            images = [str(name) for name in (record.get("images") or [])]
            if not images or any(name in known_images for name in images):
                continue
            job_id = str(record.get("job_id") or "").strip()
            if not job_id or job_id in self._jobs:
                continue
            self._jobs[job_id] = record
            known_images.update(images)
            imported += 1
        if imported:
            self._prune_locked()
            self._save_sync()
        return imported

    def _mark_running_interrupted(self) -> list[dict[str, Any]]:
        changed: list[dict[str, Any]] = []
        finished_at = _timestamp()
        for record in self._jobs.values():
            if record.get("status") != "running":
                continue
            record["status"] = "interrupted"
            record["finished_at"] = finished_at
            record["error"] = "插件重启，生成任务已中断"
            record["duration_ms"] = self._duration_ms(record, finished_at)
            changed.append(record)
        return changed

    @staticmethod
    def _group_id(record: dict[str, Any]) -> str:
        return str(record.get("parent_job_id") or record.get("job_id") or "")

    def _prune_locked(self) -> list[dict[str, Any]]:
        removed: list[dict[str, Any]] = []
        if len(self._jobs) <= self.max_records:
            return removed

        groups: dict[str, list[dict[str, Any]]] = {}
        for record in self._jobs.values():
            groups.setdefault(self._group_id(record), []).append(record)

        ordered_groups = sorted(
            groups.values(),
            key=lambda group: str(group[0].get("created_at") or ""),
        )
        for group in ordered_groups:
            if len(self._jobs) <= self.max_records:
                break
            if any(record.get("status") == "running" for record in group):
                continue
            for record in group:
                removed_record = self._jobs.pop(str(record.get("job_id")), None)
                if removed_record is not None:
                    removed.append(removed_record)
        if removed:
            names = {
                str(name) for record in removed for name in (record.get("images") or [])
            }
            self._delete_gallery_files(names)
        return removed

    @staticmethod
    def _clean_params(params: Any) -> dict[str, Any]:
        source = params if isinstance(params, dict) else {}
        cleaned = {key: source.get(key) for key in _PARAM_KEYS}
        cleaned["image_count"] = _positive_int(cleaned.get("image_count"), 1)
        for key in (
            "resolution",
            "aspect_ratio",
            "provider",
            "model",
            "candidate_id",
            "quality",
        ):
            value = cleaned.get(key)
            cleaned[key] = _bounded_text(value, 128) or None
        return cleaned

    @staticmethod
    def _clean_requester(requester: Any) -> dict[str, str]:
        source = requester if isinstance(requester, dict) else {}
        return {
            "user_id": _bounded_text(source.get("user_id"), 128),
            "user_name": _bounded_text(source.get("user_name"), 200),
            "group_id": _bounded_text(source.get("group_id"), 128),
        }

    @staticmethod
    def _clean_stats(stats: Any) -> dict[str, Any]:
        source = stats if isinstance(stats, dict) else {}
        try:
            retry_count = max(int(source.get("retry_count") or 0), 0)
        except (TypeError, ValueError):
            retry_count = 0
        return {
            "provider": _bounded_text(
                source.get("provider") or source.get("successful_provider"), 128
            ),
            "model": _bounded_text(
                source.get("model") or source.get("successful_model"), 256
            ),
            "alias": _bounded_text(
                source.get("alias") or source.get("successful_model_alias"), 128
            ),
            "retry_count": retry_count,
        }

    async def begin(
        self,
        *,
        source: str,
        prompt: str,
        params: dict[str, Any],
        requester: dict[str, Any],
        parent_job_id: str | None = None,
        item_name: str | None = None,
        requested_images: int = 1,
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("生成追踪器已关闭")
        async with self._lock:
            job_id = f"job-{uuid.uuid4().hex[:12]}"
            record = {
                "job_id": job_id,
                "parent_job_id": _bounded_text(parent_job_id, 128) or None,
                "item_name": _bounded_text(item_name, 64) or None,
                "source": source if source in _KNOWN_SOURCES else "command",
                "status": "running",
                "prompt": _bounded_text(prompt, 2000),
                "params": self._clean_params(params),
                "requester": self._clean_requester(requester),
                "created_at": _timestamp(),
                "finished_at": None,
                "duration_ms": 0,
                "requested_images": _positive_int(requested_images, 1),
                "generated_images": 0,
                "images": [],
                "text_content": "",
                "error": None,
                "stats": self._clean_stats({}),
            }
            self._jobs[job_id] = record
            self._prune_locked()
            await self._save()
            self._broadcast(record)
            params_summary = record["params"]
            logger.debug(
                f"[生成历史] 开始记录: job_id={job_id}, 状态=无->running, "
                f"来源={record['source']}, 目标张数={record['requested_images']}, "
                f"供应商={params_summary.get('provider') or '未指定'}, "
                f"模型={params_summary.get('model') or '未指定'}, "
                f"prompt={_bounded_text(prompt, 30)!r}, 记录总数={len(self._jobs)}"
            )
            return self._snapshot(record)

    async def update(self, job_id: str, **changes: Any) -> None:
        async with self._lock:
            record = self._jobs.get(str(job_id))
            if record is None:
                return
            allowed = {
                "status",
                "images",
                "generated_images",
                "requested_images",
                "text_content",
                "error",
                "stats",
            }
            for key, value in changes.items():
                if key not in allowed:
                    continue
                if key in {"generated_images", "requested_images"}:
                    try:
                        record[key] = max(int(value), 0)
                    except (TypeError, ValueError):
                        continue
                elif key == "text_content":
                    record[key] = _bounded_text(value, 500)
                elif key == "error":
                    record[key] = _bounded_text(value, 1000) or None
                elif key == "stats":
                    record[key] = self._clean_stats(value)
                elif key == "images":
                    record[key] = self._clean_images(value)
                elif key == "status" and value in TERMINAL_STATUSES | {"running"}:
                    record[key] = value
            if record.get("status") in TERMINAL_STATUSES:
                finished_at = str(record.get("finished_at") or _timestamp())
                record["finished_at"] = finished_at
                record["duration_ms"] = self._duration_ms(record, finished_at)
            self._prune_locked()
            await self._save()
            self._broadcast(record)

    async def complete(
        self,
        job_id: str,
        *,
        image_files: list[str],
        text_content: str | None,
        stats: dict[str, Any],
        status: str = "succeeded",
    ) -> None:
        if status not in {"succeeded", "partial_success"}:
            status = "succeeded"
        async with self._lock:
            record = self._jobs.get(str(job_id))
            if record is None:
                return
            previous_status = str(record.get("status") or "未知")
            images = self._clean_images(image_files)
            finished_at = _timestamp()
            record.update(
                {
                    "status": status,
                    "finished_at": finished_at,
                    "duration_ms": self._duration_ms(record, finished_at),
                    "generated_images": len(images),
                    "images": images,
                    "text_content": _bounded_text(text_content, 500),
                    "error": None,
                    "stats": self._clean_stats(stats),
                }
            )
            self._prune_locked()
            await self._save()
            self._broadcast(record)
            logger.debug(
                f"[生成历史] 完成记录: job_id={job_id}, "
                f"状态={previous_status}->{status}, 产出张数={len(images)}, "
                f"记录总数={len(self._jobs)}"
            )

    async def fail(self, job_id: str, *, error: Any) -> None:
        async with self._lock:
            record = self._jobs.get(str(job_id))
            if record is None:
                return
            previous_status = str(record.get("status") or "未知")
            finished_at = _timestamp()
            record.update(
                {
                    "status": "failed",
                    "finished_at": finished_at,
                    "duration_ms": self._duration_ms(record, finished_at),
                    "error": _bounded_text(error, 1000) or "生成失败",
                }
            )
            self._prune_locked()
            await self._save()
            self._broadcast(record)
            logger.debug(
                f"[生成历史] 失败记录: job_id={job_id}, "
                f"状态={previous_status}->failed, 记录总数={len(self._jobs)}"
            )

    @staticmethod
    def _duration_ms(record: dict[str, Any], finished_at: Any) -> int:
        created = _parse_timestamp(record.get("created_at"))
        finished = _parse_timestamp(finished_at)
        if created is None or finished is None:
            return 0
        return max(int((finished - created).total_seconds() * 1000), 0)

    def active_and_recent(self, hours: int = 24) -> list[dict[str, Any]]:
        cutoff = _now() - timedelta(hours=max(int(hours), 1))
        records = []
        for record in self._jobs.values():
            if record.get("status") == "running":
                records.append(record)
                continue
            finished = _parse_timestamp(
                record.get("finished_at") or record.get("created_at")
            )
            if finished is not None and finished >= cutoff:
                records.append(record)
        records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return self._snapshot(records)

    def records_snapshot(self) -> list[dict[str, Any]]:
        return self._snapshot(list(self._jobs.values()))

    def query_history(
        self,
        *,
        page: int,
        size: int,
        keyword: str,
        source: str,
        group_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        page = min(max(int(page), 1), 100)
        size = min(max(int(size), 1), 100)
        keyword = _bounded_text(keyword, 100).casefold()
        source = _bounded_text(source, 32)
        group_id = _bounded_text(group_id, 128)
        user_id = _bounded_text(user_id, 128)
        records = []
        for record in self._jobs.values():
            requester = record.get("requester") or {}
            if source and record.get("source") != source:
                continue
            if group_id and str(requester.get("group_id") or "") != group_id:
                continue
            if user_id and str(requester.get("user_id") or "") != user_id:
                continue
            if keyword:
                haystack = "\n".join(
                    str(value or "")
                    for value in (
                        record.get("prompt"),
                        record.get("item_name"),
                        requester.get("user_name"),
                        record.get("error"),
                    )
                ).casefold()
                if keyword not in haystack:
                    continue
            records.append(record)
        records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        total = len(records)
        start = (page - 1) * size
        return {
            "items": self._snapshot(records[start : start + size]),
            "page": page,
            "size": size,
            "total": total,
        }

    def get(self, job_id: str) -> dict[str, Any] | None:
        record = self._jobs.get(str(job_id))
        return self._snapshot(record) if record is not None else None

    async def delete(self, job_ids: list[str]) -> dict[str, Any]:
        deleted: list[str] = []
        failed: list[dict[str, str]] = []
        files: set[str] = set()
        async with self._lock:
            for raw_job_id in job_ids:
                job_id = str(raw_job_id or "").strip()
                if not job_id:
                    continue
                targets = [
                    record
                    for record in self._jobs.values()
                    if record.get("job_id") == job_id
                    or record.get("parent_job_id") == job_id
                ]
                targets = [
                    record
                    for record in targets
                    if str(record.get("job_id")) not in deleted
                ]
                if not targets:
                    failed.append({"job_id": job_id, "error": "记录不存在"})
                    continue
                if any(record.get("status") == "running" for record in targets):
                    failed.append({"job_id": job_id, "error": "运行中的任务不能删除"})
                    continue
                for record in targets:
                    record_id = str(record.get("job_id"))
                    self._jobs.pop(record_id, None)
                    deleted.append(record_id)
                    files.update(str(name) for name in record.get("images") or [])
            await self._save()
            if deleted:
                self._broadcast_event({"type": "resync"})

        await asyncio.to_thread(self._delete_gallery_files, files)
        return {"deleted": deleted, "failed": failed}

    @staticmethod
    def _clean_images(values: Any) -> list[str]:
        return [
            name
            for name in (values or [])
            if isinstance(name, str)
            and name not in {".", ".."}
            and _SAFE_FILE_NAME.fullmatch(name)
        ]

    def _delete_gallery_files(self, names: set[str]) -> None:
        with self.gallery_lock:
            self._delete_gallery_files_locked(names)

    def _delete_gallery_files_locked(self, names: set[str]) -> None:
        gallery_root = self.gallery_dir.resolve()
        for name in names:
            if not _SAFE_FILE_NAME.fullmatch(name):
                continue
            target = (self.gallery_dir / name).resolve()
            if target.parent != gallery_root:
                continue
            try:
                target.unlink(missing_ok=True)
                thumbnail = self.gallery_dir / ".thumbs" / f"{name}.jpg"
                if thumbnail.parent.resolve() == gallery_root / ".thumbs":
                    thumbnail.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(f"[生成历史] 删除 gallery 文件失败 {name}: {exc}")

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        logger.debug(f"[WebUI] SSE 订阅已建立: 当前订阅数={len(self._subscribers)}")
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)
        logger.debug(f"[WebUI] SSE 订阅已移除: 当前订阅数={len(self._subscribers)}")

    def _broadcast(self, record: dict[str, Any]) -> None:
        self._broadcast_event({"type": "job", "data": self._snapshot(record)})

    def _broadcast_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "resync":
            logger.debug(
                f"[WebUI] SSE resync 已触发: 当前订阅数={len(self._subscribers)}"
            )
        for queue in tuple(self._subscribers):
            if queue.full():
                logger.debug(
                    "[WebUI] SSE 队列已满，触发 resync: "
                    f"当前订阅数={len(self._subscribers)}"
                )
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait({"type": "resync"})
                except asyncio.QueueFull:
                    pass
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(self._snapshot(event))
            except asyncio.QueueFull:
                pass

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            changed = self._mark_running_interrupted()
            await self._save()
            for record in changed:
                self._broadcast(record)
            self._broadcast_event({"type": "resync"})
            self._subscribers.clear()
