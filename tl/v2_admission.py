"""插件层准入组件：把共享额度与旧 FIFO 生成队列接到 v2 submit 准入。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

from .api_types import APIError
from .core.errors import (
    InvalidRequestError,
    QueueFullError,
    QuotaExceededError,
    ServiceClosedError,
)
from .generation_scheduler import generation_reservation

if TYPE_CHECKING:
    from .core.requests import GenerationRequest
    from .rate_limiter import RateLimiter

_QUOTA_SCOPES = frozenset({"session", "global", "plugin"})


class _QuotaReservation:
    """已扣除的额度：Job 被接受后失败或取消都不退款，仅 submit 回滚时退款。"""

    def __init__(self, rate_limiter: RateLimiter, token: str | None) -> None:
        self._rate_limiter = rate_limiter
        self.token = token

    def release(self) -> None:
        """Job 结束时调用；额度不回补。"""

    async def refund(self) -> None:
        await self._rate_limiter.refund(self.token)


class RateLimiterQuota:
    """把共享 RateLimiter 适配为 submit 准入的额度组件。

    ``request.metadata["quota"]`` 决定扣减方式：``False`` 表示本次不计额度；
    缺省按会话扣 1。可用字段：``cost``（正整数，由 RateLimiter 校验）、
    ``scope``（``session`` 默认 / ``global`` / ``plugin``）、``plugin_id``
    （scope 为 plugin 时必填）。会话额度取 ``request.requester["umo"]``。
    """

    def __init__(self, get_rate_limiter: Callable[[], RateLimiter]) -> None:
        self._get_rate_limiter = get_rate_limiter

    async def reserve(
        self, request: GenerationRequest, admission: Mapping[str, Any] | None = None
    ) -> _QuotaReservation | None:
        quota = request.metadata.get("quota")
        if quota is False:
            return None
        if not isinstance(quota, Mapping):
            quota = {}
        scope = quota.get("scope", "session")
        if scope not in _QUOTA_SCOPES:
            raise InvalidRequestError("quota.scope 必须是 session、global 或 plugin")
        plugin_id = quota.get("plugin_id") if scope == "plugin" else None
        if scope == "plugin" and not plugin_id:
            raise InvalidRequestError("quota.scope 为 plugin 时必须提供 plugin_id")
        umo: str | None = None
        if scope == "session":
            umo = request.requester.get("umo")
            if not umo:
                raise QuotaExceededError("无法识别完整会话 UMO，已拒绝生成")
        rate_limiter = self._get_rate_limiter()
        decision = await rate_limiter.acquire(
            umo,
            cost=quota.get("cost", 1),
            event=(admission or {}).get("event"),
            plugin_id=plugin_id,
        )
        if not decision.allowed:
            raise QuotaExceededError(
                decision.message or "",
                details={
                    "scope": decision.scope,
                    "retry_after": decision.retry_after,
                    # 没有消息对应旧路径群名单不通过时的静默拒绝。
                    "silent": not decision.message,
                },
            )
        return _QuotaReservation(rate_limiter, decision.token)


class _FifoReservation:
    """旧 FIFO 队列票据：provider 执行期间经 contextvar 让被装饰调用直接使用。"""

    def __init__(self, ticket: Any) -> None:
        self._ticket = ticket

    def runtime_scope(self) -> AbstractContextManager[None]:
        return generation_reservation(self._ticket)

    def release(self) -> None:
        # 幂等：被装饰的调用用完票据后自己也会 release。
        self._ticket.release()


class LegacyFifoLimiter:
    """把旧 GenerationScheduler 的 FIFO 队列适配为 submit 准入的并发组件。"""

    def __init__(self, get_generation_scheduler: Callable[[], Any]) -> None:
        self._get_generation_scheduler = get_generation_scheduler

    def reserve(
        self, request: GenerationRequest, admission: Mapping[str, Any] | None = None
    ) -> _FifoReservation | None:
        scheduler = self._get_generation_scheduler()
        if scheduler is None:
            return None
        try:
            ticket = scheduler.reserve()
        except APIError as error:
            if error.error_type == "queue_full":
                raise QueueFullError(error.message) from error
            if error.error_type == "service_closed":
                raise ServiceClosedError(error.message) from error
            raise
        return _FifoReservation(ticket)
