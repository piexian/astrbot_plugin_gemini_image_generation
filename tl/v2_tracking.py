"""v2 调度器生命周期到生成历史（GenerationTracker）的投影层。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .core.results import GenerationResult
from .core.states import JobState
from .generation_scheduler import generation_progress
from .generation_tracker import (
    _KNOWN_SOURCES,
    TERMINAL_STATUSES,
    ReferenceImageUnavailableError,
    record_generation_success,
)
from .v2_bridge import split_artifacts

# 这些终态没有（或不再有）可记入历史的产出，job_finished 才需要改写记录。
_ABORTED_STATES = frozenset(
    {
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.INTERRUPTED,
        JobState.ORPHANED,
    }
)


class TrackerProjection:
    """以 JobObserver 钩子把调度器生命周期写回生成历史。

    记录创建失败只记日志，不影响生成（与旧入口口径一致）；job_finished
    覆盖 FAILED/CANCELLED/INTERRUPTED/ORPHANED，SUCCEEDED/PARTIAL 由
    job_result 完成记录。Job 已有结果且 job_result 已把记录收尾时（之后才在
    投递阶段被取消或失败），记录保持原样，不被改写成 interrupted/failed。
    """

    def __init__(self, tracker: Any, archive_images_fn: Any = None) -> None:
        self._tracker = tracker
        self._archive_images_fn = archive_images_fn
        # 批量父记录的 generated_images 累计：{父 job_id: {子 job_id: 张数}}，
        # 父 Job 收尾时清理。
        self._batch_images: dict[str, dict[str, int]] = {}
        self._batch_statuses: dict[str, dict[str, str | None]] = {}
        self._batch_source_urls: dict[str, dict[str, list[str]]] = {}

    async def job_accepted(self, job: Any) -> None:
        request = job.request
        metadata = request.metadata or {}
        source = request.source if request.source in _KNOWN_SOURCES else "command"
        begin_kwargs = {
            "job_id": job.job_id,
            "source": source,
            "prompt": request.prompt,
            "params": {
                "resolution": request.resolution,
                "aspect_ratio": request.aspect_ratio,
                "provider": request.provider,
                "model": request.model,
                "image_count": request.image_count,
                "quality": request.quality,
                "seed": metadata.get("seed"),
                "negative_prompt": request.negative_prompt,
            },
            "requester": dict(request.requester),
            "parent_job_id": job.parent_job_id,
            "item_name": metadata.get("item_name"),
            "requested_images": request.image_count,
            "caller": metadata.get("caller"),
            "reference_names": metadata.get("reference_names"),
        }
        if "requested_candidate_id" in metadata:
            begin_kwargs["params"]["candidate_id"] = metadata["requested_candidate_id"]
        if "generation_settings" in metadata:
            begin_kwargs["params"]["generation_settings"] = metadata[
                "generation_settings"
            ]
        try:
            await self._tracker.begin(**begin_kwargs)
        except ReferenceImageUnavailableError as exc:
            if not metadata.get("reference_names"):
                logger.warning(f"[生成追踪] v2 创建记录失败，不影响本次生成: {exc}")
                return
            # 参考图已入库为临时句柄；画廊原件随后被清理时仍要保留任务记录。
            begin_kwargs["reference_names"] = []
            try:
                await self._tracker.begin(**begin_kwargs)
            except Exception as retry_exc:
                logger.warning(
                    f"[生成追踪] v2 去除失效画廊租约后创建记录失败: {retry_exc}"
                )
        except Exception as exc:
            logger.warning(f"[生成追踪] v2 创建记录失败，不影响本次生成: {exc}")

    def runtime_scope(self, job: Any):
        async def progress(status: str) -> None:
            await self._tracker.update(job.job_id, status=status)

        async def count(value: int, stats: dict[str, Any]) -> None:
            await self._tracker.update(job.job_id, generated_images=value, stats=stats)

        def preview(data: str, fmt: str):
            return self._tracker.preview(job.job_id, data, fmt)

        return generation_progress(progress, preview=preview, count=count)

    @staticmethod
    def _is_batch_parent(job: Any) -> bool:
        request = getattr(job, "request", None)
        return bool((getattr(request, "metadata", None) or {}).get("batch_parent"))

    async def job_result(self, job: Any, result: GenerationResult) -> None:
        if result is None:
            return
        if self._is_batch_parent(job):
            await self._record_batch_parent(job, result)
            return
        stats_source = result.stats or {}
        request = getattr(job, "request", None)
        metadata = getattr(request, "metadata", None) or {}
        completion_policy = metadata.get("completion_policy")
        if completion_policy == "studio":
            stats = {
                "provider": result.provider,
                "model": result.model,
                "alias": stats_source.get("successful_model_alias"),
                "retry_count": stats_source.get("retry_count"),
            }
        else:
            stats = {
                "retry_count": stats_source.get("retry_count"),
                "token_usage": stats_source.get("token_usage"),
                "retry_note": stats_source.get("retry_note"),
                "successful_candidate_id": result.candidate_id,
                "successful_provider": result.provider,
                "successful_model": result.model,
                "successful_model_alias": stats_source.get("successful_model_alias"),
            }
        image_urls, image_paths = split_artifacts(result.artifacts)
        await record_generation_success(
            self._tracker,
            job.job_id,
            image_urls=image_urls,
            image_paths=image_paths,
            text_content=result.text,
            stats=stats,
            archive_images_fn=self._archive_images_fn,
            candidate_id=result.candidate_id,
            completion_policy=completion_policy,
            requested_images=getattr(request, "image_count", None),
            generation_error=(
                stats_source.get("fill_error")
                if completion_policy == "studio"
                else None
            ),
        )

    async def _record_batch_parent(self, job: Any, result: GenerationResult) -> None:
        """父记录只收尾状态与累计张数：图片已由各子 Job 的记录各自归档。"""

        counts = self._batch_images.pop(job.job_id, None)
        statuses = self._batch_statuses.pop(job.job_id, {})
        source_urls = self._batch_source_urls.pop(job.job_id, {})
        generated_images = (
            sum(counts.values()) if counts is not None else len(result.artifacts)
        )
        metadata = getattr(job.request, "metadata", None) or {}
        changes: dict[str, Any] = {
            "status": "partial_success" if result.partial else "succeeded",
            "generated_images": generated_images,
        }
        if metadata.get("completion_policy") == "studio":
            changes["status"] = (
                "succeeded"
                if statuses and set(statuses.values()) == {"succeeded"}
                else "partial_success"
            )
            order = [
                item.get("job_id") for item in (result.stats or {}).get("children", [])
            ]
            changes["source_urls"] = [
                url for child_id in order for url in source_urls.get(child_id, [])
            ]
        try:
            await self._tracker.update(job.job_id, **changes)
        except Exception as exc:
            logger.warning(
                f"[生成追踪] v2 批量父记录写入失败: job_id={job.job_id}, "
                f"异常类型={type(exc).__name__}"
            )

    async def child_finished(
        self, parent_job: Any, child_job: Any, result: Any
    ) -> None:
        """子 Job 收尾时把父记录的 generated_images 更新为各子 Job 产出的累计值。"""

        counts = self._batch_images.setdefault(parent_job.job_id, {})
        counts[child_job.job_id] = len(result.artifacts) if result is not None else 0
        record = self._tracker.get(child_job.job_id)
        statuses = self._batch_statuses.setdefault(parent_job.job_id, {})
        statuses[child_job.job_id] = record.get("status") if record else None
        sources = self._batch_source_urls.setdefault(parent_job.job_id, {})
        sources[child_job.job_id] = list((record or {}).get("source_urls") or [])
        try:
            await self._tracker.update(
                parent_job.job_id, generated_images=sum(counts.values())
            )
        except Exception as exc:
            logger.warning(
                f"[生成追踪] v2 批量父记录进度写入失败: job_id={parent_job.job_id}, "
                f"异常类型={type(exc).__name__}"
            )

    def _finished_with_result(self, job: Any) -> bool:
        """Job 带着结果结束，且生成历史记录已被 job_result 收尾。

        只看 job.result 不够：结果在 job_result 观察者运行之前就已落库，观察者
        中途被取消时记录仍在运行态，仍需按终态改写。
        """

        if getattr(job, "result", None) is None:
            return False
        record = self._tracker.get(job.job_id)
        return record is not None and record.get("status") in TERMINAL_STATUSES

    async def job_finished(self, job: Any) -> None:
        if job.state in (JobState.SUCCEEDED, JobState.PARTIAL):
            # job_result observers are best-effort and may time out while archiving.
            # Retry the projection at the terminal hook so a successful Job does not
            # leave its history permanently in the running state.
            if getattr(
                job, "result", None
            ) is not None and not self._finished_with_result(job):
                await self.job_result(job, job.result)
            self._batch_images.pop(job.job_id, None)
            self._batch_statuses.pop(job.job_id, None)
            self._batch_source_urls.pop(job.job_id, None)
            return
        self._batch_images.pop(job.job_id, None)
        self._batch_statuses.pop(job.job_id, None)
        self._batch_source_urls.pop(job.job_id, None)
        if job.state not in _ABORTED_STATES:
            return
        try:
            if self._finished_with_result(job):
                return
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
