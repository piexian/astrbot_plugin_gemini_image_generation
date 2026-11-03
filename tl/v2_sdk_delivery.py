"""公开 SDK 的 v2 任务投递适配器。"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from .core.errors import redact_text
from .core.states import JobState
from .generation_tracker import without_session_origin
from .v2_bridge import split_artifacts, v2_failure_message

_CALLBACK_TIMEOUT_SECONDS = 30


class SdkDelivery:
    """把 v2 终态写回 SDK 后台记录，并保持回调与等待语义。"""

    def __init__(self, service: Any) -> None:
        self._service = service
        self._plugin = service.plugin
        self._task_id: str | None = None
        self._bound = False
        self._record_ready = asyncio.Event()

    def bind_record(self, task_id: str | None) -> None:
        self._task_id = task_id
        self._bound = True
        self._record_ready.set()

    async def _wait_record(self) -> str:
        if not self._bound:
            try:
                await asyncio.wait_for(self._record_ready.wait(), timeout=30)
            except asyncio.TimeoutError:
                logger.warning(
                    f"[插件接入] 等待 SDK 后台记录绑定超时: job_id={getattr(self, '_job_id', None)}, task_id={self._task_id}"
                )
                raise
        if not self._task_id:
            raise RuntimeError("SDK 后台记录未创建")
        return self._task_id

    async def job_started(self, job: Any) -> None:
        task_id = await self._wait_record()
        await self._plugin.background_task_manager.update(task_id, status="running")

    async def deliver(self, job: Any, result: Any) -> None:
        task_id = await self._wait_record()
        tracker = self._plugin.generation_tracker
        urls, paths = split_artifacts(result.artifacts)
        images = [*urls, *paths]
        history = tracker.get(job.job_id) or {}
        archived = list(history.get("images") or [])
        if archived and len(archived) == len(images):
            paths = [str(tracker.gallery_dir / name) for name in archived]
            urls = []
        status = (
            "succeeded"
            if len(images) >= job.request.image_count
            else ("partial_success" if images else "failed")
        )
        stats = dict(result.stats or {})
        stats.update(
            {
                "successful_provider": result.provider,
                "successful_model": result.model,
                "successful_model_alias": (result.stats or {}).get(
                    "successful_model_alias"
                ),
                "successful_candidate_id": result.candidate_id,
            }
        )
        error = stats.get("fill_error")
        logger.info(
            f"[插件接入] v2 投递完成: job_id={job.job_id}, task_id={task_id}, images={len(images)}"
        )
        await self._publish(
            task_id,
            job.job_id,
            status=status,
            message=error or "生成完成",
            error=error,
            generated_images=len(images),
            image_urls=urls,
            image_paths=paths,
            text_content=result.text or "",
            stats=stats,
        )

    async def deliver_failure(self, job: Any) -> None:
        task_id = await self._wait_record()
        if job.state is JobState.FAILED:
            status = "failed"
            error = v2_failure_message(
                job, total_timeout=getattr(self._plugin.cfg, "total_timeout", 120)
            )
        else:
            status = "interrupted"
            error = "任务已取消或服务已关闭"
        await self._publish(
            task_id,
            job.job_id,
            status=status,
            message=error,
            error=error,
            generated_images=0,
            image_urls=[],
            image_paths=[],
            text_content="",
            stats={},
        )
        logger.warning(
            f"[插件接入] v2 投递失败: job_id={getattr(job, 'job_id', None)}, "
            f"task_id={task_id}, state={getattr(job, 'state', None)}"
        )

    async def _publish(self, task_id: str, job_id: str, **changes: Any) -> None:
        manager = self._plugin.background_task_manager
        try:
            record = await manager.update(task_id, best_effort=True, **changes)
            await self._service._update_history(
                job_id,
                callback_status=record.get("callback_status", "none"),
            )
        finally:
            event = self._service._events.pop(task_id, None)
            if event:
                event.set()
        callback = self._service._callbacks.pop(task_id, None)
        if callback:
            await self._run_callback(task_id, job_id, callback, record)
        self._service._v2_jobs.pop(task_id, None)

    async def _run_callback(
        self, task_id: str, job_id: str, callback: Any, record: dict[str, Any]
    ) -> None:
        manager = self._plugin.background_task_manager
        if self._service._state != "ready":
            await manager.update(
                task_id, best_effort=True, callback_status="interrupted"
            )
            await self._service._update_history(job_id, callback_status="interrupted")
            return

        await manager.update(task_id, best_effort=True, callback_status="running")
        await self._service._update_history(job_id, callback_status="running")
        callback_task = asyncio.create_task(callback(without_session_origin(record)))
        self._service._callback_tasks[task_id] = callback_task
        callback_status = "succeeded"
        try:
            await asyncio.wait_for(callback_task, _CALLBACK_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            callback_status = "interrupted"
        except Exception as exc:
            callback_status = "failed"
            logger.warning(
                f"[插件接入] 任务 {task_id} 完成回调失败: {redact_text(str(exc))[:240]}",
                exc_info=True,
            )
        finally:
            self._service._callback_tasks.pop(task_id, None)
        await manager.update(task_id, best_effort=True, callback_status=callback_status)
        await self._service._update_history(job_id, callback_status=callback_status)
