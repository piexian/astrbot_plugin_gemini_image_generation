"""入口适配器的唯一提交面（参数/权限校验留在入口，本层保持薄）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .models import Job
from .reference_service import ReferenceService
from .requests import GenerationRequest
from .scheduler import JobHandle, JobResult, JobScheduler, JobSnapshot


class GenerationService:
    """组合一个 JobScheduler 并透传提交/等待/取消/状态/恢复/关闭。"""

    def __init__(
        self, scheduler: JobScheduler, reference_service: ReferenceService | None = None
    ) -> None:
        self._scheduler = scheduler
        self._reference_service = reference_service

    async def prepare_references(
        self, references: Sequence[str], *, requester: Mapping[str, str]
    ) -> tuple[str, ...]:
        """内联参考图换成短句柄；未注入 ReferenceService 说明装配缺失。"""

        if self._reference_service is None:
            raise RuntimeError(
                "GenerationService 未注入 ReferenceService，无法预处理参考图"
            )
        return await self._reference_service.ingest_inline(
            references, requester=requester
        )

    async def submit(self, request: GenerationRequest) -> JobHandle:
        return await self._scheduler.submit(request)

    async def wait(self, job_id: str, timeout: float | None = None) -> JobResult:
        return await self._scheduler.wait(job_id, timeout=timeout)

    def result_wait_timeout(self, deadline_seconds: float) -> float:
        """转发调度器的等待上限计算（含观察者钩子时间，见 JobScheduler）。"""

        return self._scheduler.result_wait_timeout(deadline_seconds)

    async def cancel(self, job_id: str) -> JobSnapshot:
        return await self._scheduler.cancel(job_id)

    async def get_status(self, job_id: str) -> JobSnapshot | None:
        return await self._scheduler.get_status(job_id)

    async def recover(self) -> list[Job]:
        """启动恢复用：返回 JobStore 的可恢复 Job 快照，不自动重放。"""

        return await self._scheduler.recover()

    async def close(self) -> None:
        # JobScheduler.close 已级联 store.close，此处无需重复关闭组件。
        await self._scheduler.close()
