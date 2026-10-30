"""入口适配器的唯一提交面（参数/权限校验留在入口，本层保持薄）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .models import Job
from .reference_service import ReferenceService
from .requests import GenerationRequest
from .scheduler import BatchHandle, JobHandle, JobResult, JobScheduler, JobSnapshot


class GenerationService:
    """组合一个 JobScheduler 并透传提交/等待/取消/状态/恢复/关闭。"""

    def __init__(
        self, scheduler: JobScheduler, reference_service: ReferenceService | None = None
    ) -> None:
        self._scheduler = scheduler
        self._reference_service = reference_service

    async def prepare_references(
        self,
        references: Sequence[str],
        *,
        requester: Mapping[str, str],
        trusted_local: bool = False,
    ) -> tuple[str, ...]:
        """内联参考图换成短句柄；未注入 ReferenceService 说明装配缺失。

        ``trusted_local`` 表示调用方已按自己的规则校验过本地路径（如 LLM 工具
        的 tool_path_guard），local / file: 形态的参考图也会读取并换成句柄。
        """

        if self._reference_service is None:
            raise RuntimeError(
                "GenerationService 未注入 ReferenceService，无法预处理参考图"
            )
        return await self._reference_service.ingest_inline(
            references, requester=requester, trusted_local=trusted_local
        )

    async def submit(
        self,
        request: GenerationRequest,
        *,
        admission: Mapping[str, Any] | None = None,
        delivery: Any = None,
    ) -> JobHandle:
        """提交 Job；admission 为不持久化的准入上下文（如命令 event）。

        ``delivery`` 是结果投递方（见 DeliveryProtocol），Job 被接受后登记。
        """
        return await self._scheduler.submit(
            request, admission=admission, delivery=delivery
        )

    async def submit_batch(
        self,
        parent: GenerationRequest,
        children: Sequence[GenerationRequest],
        *,
        concurrency: int,
        admission: Mapping[str, Any] | None = None,
        delivery: Any = None,
    ) -> BatchHandle:
        """提交批量父子 Job；提交时就创建全部子 Job，语义见 JobScheduler.submit_batch。

        等待批量结束用 ``wait(handle.job_id)``（父 Job）；``result_wait_timeout``
        不适用于批量，各子 Job 的执行时限由子请求自己的 ``deadline_seconds`` 决定。
        """
        return await self._scheduler.submit_batch(
            parent,
            children,
            concurrency=concurrency,
            admission=admission,
            delivery=delivery,
        )

    def attach_delivery(self, job_id: str, delivery: Any) -> bool:
        """给运行中的 Job 登记结果投递方；False 表示未登记，结果由调用方处理。

        同步方法：与调度器的投递判定点不会交错，语义见 JobScheduler.attach_delivery。
        """
        return self._scheduler.attach_delivery(job_id, delivery)

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
