"""LLM 工具单图的 v2 后台投递：把调度器的投递阶段接到后台记录与主 Agent 回灌。"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from .background_dispatch import dispatch_generation_result, single_task_record_changes
from .background_notify import report_background_failure
from .batch_generation import (
    _batch_item_result,
    _batch_summary,
    _public_item,
    _send_batch_results,
)
from .core.states import JobState
from .v2_bridge import split_artifacts, v2_failure_message

# 工具建好后台记录后才调用 bind_record；投递最多等这么久，超时就不带任务号继续。
_RECORD_BIND_TIMEOUT_SECONDS = 30.0
_CANCELLED_MESSAGE = "图片生成已取消"
_INTERRUPTED_MESSAGE = "图片生成已中断"


class ToolBackgroundDelivery:
    """调度器投递方（DeliveryProtocol）：结果经主 Agent 回灌送达，并收尾后台记录。

    后台记录由工具在 ``attach_delivery`` 成功之后才创建，所以记录晚于投递方登记；
    ``bind_record`` 把任务号交给适配器，``deliver`` / ``deliver_failure`` 先等它。
    """

    def __init__(self, plugin: Any, event: Any, *, scene: str) -> None:
        self._plugin = plugin
        self._event = event
        self._scene = scene
        self._task_id: str | None = None
        self._bound = asyncio.Event()

    def bind_record(self, task_id: str | None) -> None:
        """绑定后台记录；task_id 为 None 表示记录没建成，投递不必再等。"""

        self._task_id = task_id
        self._bound.set()

    async def deliver(self, job: Any, result: Any) -> None:
        task_id = await self._wait_record()
        image_urls, image_paths = split_artifacts(result.artifacts)
        result_data = (image_urls, image_paths, result.text, None)
        try:
            delivered = await dispatch_generation_result(
                self._plugin,
                self._event,
                True,
                result_data,
                scene=self._scene,
                notify_llm=True,
                task_id=task_id,
            )
        except Exception as exc:
            logger.error(f"[{self._scene}] 后台结果分发异常: {exc}", exc_info=True)
            delivered = False
        stats = result.stats
        await self._update_record(
            task_id,
            job,
            single_task_record_changes(
                success=True,
                delivered=delivered,
                result_data=result_data,
                stats={
                    "successful_provider": result.provider,
                    "successful_model": result.model,
                    "successful_model_alias": stats.get("successful_model_alias"),
                    "successful_candidate_id": result.candidate_id,
                },
            ),
        )
        if not delivered:
            # 让调度器把 Job 收尾为 PARTIAL + delivery_failed，结果仍在 Job 上。
            raise RuntimeError("后台结果未送达")

    async def deliver_failure(self, job: Any) -> None:
        task_id = await self._wait_record()
        if job.state is JobState.FAILED:
            message = v2_failure_message(job, total_timeout=self._total_timeout())
            delivered = await report_background_failure(
                self._plugin,
                self._event,
                message,
                scene=self._scene,
                task_id=task_id,
            )
            changes = single_task_record_changes(
                success=False, delivered=delivered, result_data=message, stats={}
            )
        else:
            # 取消、关闭或重启不是生成失败，只收尾记录，不打扰用户。
            changes = {
                "status": "interrupted",
                "message": (
                    _CANCELLED_MESSAGE
                    if job.state is JobState.CANCELLED
                    else _INTERRUPTED_MESSAGE
                ),
            }
        await self._update_record(task_id, job, changes)

    async def _wait_record(self) -> str | None:
        if not self._bound.is_set():
            try:
                await asyncio.wait_for(
                    self._bound.wait(), timeout=_RECORD_BIND_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                logger.warning(f"[{self._scene}] 等待后台记录绑定超时，不带任务号继续")
        return self._task_id

    async def _update_record(
        self, task_id: str | None, job: Any, changes: dict[str, Any]
    ) -> None:
        if not task_id:
            return
        # 记录落盘失败只记日志（best_effort）：不能把已送达的结果改判为投递失败。
        await self._plugin.background_task_manager.update(
            task_id, best_effort=True, job_id=job.job_id, **changes
        )

    def _total_timeout(self) -> int:
        return self._plugin.image_generator.total_timeout


class ToolBatchDelivery(ToolBackgroundDelivery):
    """批量父 Job 的投递方：累计子项进度，并最终唤醒主 Agent 发送汇总。"""

    def __init__(self, plugin: Any, event: Any) -> None:
        super().__init__(plugin, event, scene="批量任务")
        self._child_order: list[str] = []
        self._results: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _item_name(job: Any) -> str:
        return str((job.request.metadata or {}).get("item_name") or job.job_id)

    def _remember_child(self, child_job: Any) -> None:
        if child_job.job_id not in self._child_order:
            self._child_order.append(child_job.job_id)

    def _ordered_results(self) -> list[dict[str, Any]]:
        return [
            self._results[job_id]
            for job_id in self._child_order
            if job_id in self._results
        ]

    async def child_started(self, parent_job: Any, child_job: Any) -> None:
        self._remember_child(child_job)
        task_id = await self._wait_record()
        await self._update_record(
            task_id, parent_job, {"current_item": self._item_name(child_job)}
        )

    async def child_finished(
        self, parent_job: Any, child_job: Any, result: Any
    ) -> None:
        self._remember_child(child_job)
        stats = dict(result.stats or {}) if result is not None else {}
        if result is not None:
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
        error = (
            child_job.error_message
            or child_job.error_code
            or stats.get("fill_error")
            or ("批量项已取消" if child_job.state is JobState.CANCELLED else None)
        )
        item = {
            "name": self._item_name(child_job),
            "image_count": child_job.request.image_count,
        }
        self._results[child_job.job_id] = _batch_item_result(
            item,
            collected=list(result.artifacts) if result is not None else [],
            text_parts=[result.text] if result is not None and result.text else [],
            last_stats=stats,
            error=error,
        )
        await self._update_progress(parent_job)

    async def _update_progress(self, parent_job: Any) -> None:
        task_id = await self._wait_record()
        results = self._ordered_results()
        succeeded = sum(bool(item["success"]) for item in results)
        await self._update_record(
            task_id,
            parent_job,
            {
                "completed_items": len(results),
                "succeeded_items": succeeded,
                "failed_items": len(results) - succeeded,
                "items": [_public_item(item) for item in results],
                "current_item": None,
            },
        )

    async def deliver(self, job: Any, result: Any) -> None:
        task_id = await self._wait_record()
        try:
            delivered = await _send_batch_results(
                self._plugin, self._event, task_id or "", self._ordered_results()
            )
        except Exception as exc:
            logger.error(f"[批量任务/{task_id}] 结果分发异常: {exc}", exc_info=True)
            delivered = False
        await self._finalize_record(task_id, job, delivered=delivered)
        if not delivered:
            raise RuntimeError("批量结果未送达")

    async def deliver_failure(self, job: Any) -> None:
        task_id = await self._wait_record()
        if job.state is JobState.FAILED:
            summary = _batch_summary(task_id or "", self._ordered_results())
            try:
                delivered = await report_background_failure(
                    self._plugin,
                    self._event,
                    summary,
                    scene=f"批量任务/{task_id}",
                    task_id=task_id,
                )
            except Exception as exc:
                logger.error(f"[批量任务/{task_id}] 失败通知异常: {exc}", exc_info=True)
                delivered = False
            await self._finalize_record(
                task_id, job, delivered=delivered, force_failed=True
            )
            return
        await self._update_record(
            task_id,
            job,
            {
                "status": "interrupted",
                "message": "批量生成已中断",
                "current_item": None,
            },
        )

    async def _finalize_record(
        self,
        task_id: str | None,
        job: Any,
        *,
        delivered: bool,
        force_failed: bool = False,
    ) -> None:
        results = self._ordered_results()
        succeeded = sum(bool(item["success"]) for item in results)
        generated = sum(int(item["generated_images"]) for item in results)
        if force_failed:
            status = "failed"
        elif succeeded == len(results):
            status = "succeeded"
        elif generated:
            status = "partial_success"
        else:
            status = "failed"
        if not delivered and status == "succeeded":
            status = "partial_success"
        await self._update_record(
            task_id,
            job,
            {
                "status": status,
                "message": (
                    "批量生成已完成，Agent 已调用发送工具"
                    if delivered
                    else "批量生成已完成，但结果发送未成功；可查询任务获取图片 URL/路径"
                ),
                "current_item": None,
                "completed_items": len(results),
                "succeeded_items": succeeded,
                "failed_items": len(results) - succeeded,
                "items": [_public_item(item) for item in results],
            },
        )
