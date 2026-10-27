"""入口适配器的唯一提交面（参数/权限校验留在入口，本层保持薄）。"""

from __future__ import annotations

from .models import Job
from .requests import GenerationRequest
from .scheduler import JobHandle, JobResult, JobScheduler, JobSnapshot


class GenerationService:
    """组合一个 JobScheduler 并透传提交/等待/取消/状态/恢复/关闭。"""

    def __init__(self, scheduler: JobScheduler) -> None:
        self._scheduler = scheduler

    async def submit(self, request: GenerationRequest) -> JobHandle:
        return await self._scheduler.submit(request)

    async def wait(self, job_id: str, timeout: float | None = None) -> JobResult:
        return await self._scheduler.wait(job_id, timeout=timeout)

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
