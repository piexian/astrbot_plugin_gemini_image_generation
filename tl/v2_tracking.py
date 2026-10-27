"""v2 调度器生命周期到生成历史（GenerationTracker）的投影层。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .core.results import GenerationResult
from .core.states import JobState
from .generation_scheduler import generation_progress
from .generation_tracker import _KNOWN_SOURCES, record_generation_success
from .v2_bridge import split_artifacts


class TrackerProjection:
    """以 JobObserver 钩子把调度器生命周期写回生成历史。

    记录创建失败只记日志，不影响生成（与旧入口口径一致）；job_finished
    覆盖 FAILED/CANCELLED/INTERRUPTED/ORPHANED，SUCCEEDED/PARTIAL 由
    job_result 完成记录。
    """

    def __init__(self, tracker: Any, archive_images_fn: Any = None) -> None:
        self._tracker = tracker
        self._archive_images_fn = archive_images_fn

    async def job_accepted(self, job: Any) -> None:
        request = job.request
        metadata = request.metadata or {}
        source = request.source if request.source in _KNOWN_SOURCES else "command"
        try:
            await self._tracker.begin(
                job_id=job.job_id,
                source=source,
                prompt=request.prompt,
                params={
                    "resolution": request.resolution,
                    "aspect_ratio": request.aspect_ratio,
                    "provider": request.provider,
                    "model": request.model,
                    "image_count": request.image_count,
                    "quality": request.quality,
                    "seed": None,
                    "negative_prompt": request.negative_prompt,
                },
                requester=dict(request.requester),
                parent_job_id=job.parent_job_id,
                item_name=metadata.get("item_name"),
                requested_images=request.image_count,
                caller=metadata.get("caller"),
                reference_names=metadata.get("reference_names"),
            )
        except Exception as exc:
            logger.warning(f"[生成追踪] v2 创建记录失败，不影响本次生成: {exc}")

    def runtime_scope(self, job: Any):
        async def progress(status: str) -> None:
            await self._tracker.update(job.job_id, status=status)

        def preview(data: str, fmt: str):
            return self._tracker.preview(job.job_id, data, fmt)

        return generation_progress(progress, preview=preview)

    async def job_result(self, job: Any, result: GenerationResult) -> None:
        if result is None:
            return
        stats_source = result.stats or {}
        image_urls, image_paths = split_artifacts(result.artifacts)
        await record_generation_success(
            self._tracker,
            job.job_id,
            image_urls=image_urls,
            image_paths=image_paths,
            text_content=result.text,
            stats={
                "retry_count": stats_source.get("retry_count"),
                "token_usage": stats_source.get("token_usage"),
                "retry_note": stats_source.get("retry_note"),
                "successful_candidate_id": result.candidate_id,
                "successful_provider": result.provider,
                "successful_model": result.model,
                "successful_model_alias": stats_source.get("successful_model_alias"),
            },
            archive_images_fn=self._archive_images_fn,
            candidate_id=result.candidate_id,
        )

    async def job_finished(self, job: Any) -> None:
        try:
            if job.state == JobState.FAILED:
                error = job.error_message or job.error_code or "生成失败"
                await self._tracker.fail(job.job_id, error=error)
            elif job.state == JobState.CANCELLED:
                await self._tracker.update(
                    job.job_id, status="interrupted", error="生成任务已取消"
                )
            elif job.state in (JobState.INTERRUPTED, JobState.ORPHANED):
                await self._tracker.update(
                    job.job_id,
                    status="interrupted",
                    error="调度器关闭或重启，任务已中断",
                )
        except Exception as exc:
            logger.warning(
                f"[生成追踪] v2 终态写入失败: job_id={job.job_id}, "
                f"异常类型={type(exc).__name__}"
            )
