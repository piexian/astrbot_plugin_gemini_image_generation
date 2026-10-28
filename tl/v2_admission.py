"""插件层准入组件：把共享额度与旧 FIFO 生成队列接到 v2 submit 准入。"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from typing import TYPE_CHECKING, Any

from .api_types import APIError
from .core.errors import (
    InvalidRequestError,
    QueueFullError,
    QuotaExceededError,
    ServiceClosedError,
    StudioBusyError,
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
    """把旧 GenerationScheduler 的 FIFO 队列适配为 submit 准入的并发组件。

    批量父 Job（``metadata["batch_parent"]`` 为真）不调用供应商，不占生成队列；
    它的子 Job 各自在启动时按普通请求预留。
    """

    def __init__(self, get_generation_scheduler: Callable[[], Any]) -> None:
        self._get_generation_scheduler = get_generation_scheduler

    def reserve(
        self, request: GenerationRequest, admission: Mapping[str, Any] | None = None
    ) -> _FifoReservation | None:
        if request.metadata.get("batch_parent"):
            return None
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


class _StudioReservation:
    def __init__(self, owner: StudioAdmissionLimiter) -> None:
        self._owner = owner
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._owner._active = max(self._owner._active - 1, 0)


class StudioAdmissionLimiter:
    """WebUI 入口级并发闸门；每个单图或批量父任务占一个名额。"""

    def __init__(self, get_maximum: Callable[[], int]) -> None:
        self._get_maximum = get_maximum
        self._active = 0

    def reserve(
        self, request: GenerationRequest, admission: Mapping[str, Any] | None = None
    ) -> _StudioReservation | None:
        if not request.metadata.get("studio_admission"):
            return None
        maximum = max(int(self._get_maximum()), 1)
        if self._active >= maximum:
            raise StudioBusyError("工作台生成任务已达到并发上限，请稍后重试")
        self._active += 1
        return _StudioReservation(self)


class _CompositeReservation:
    def __init__(self, reservations: Sequence[Any]) -> None:
        self._reservations = tuple(reservations)
        self._released = False

    @contextmanager
    def runtime_scope(self):
        with ExitStack() as stack:
            for reservation in self._reservations:
                factory = getattr(reservation, "runtime_scope", None)
                if callable(factory):
                    stack.enter_context(factory())
            yield

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        first_error: Exception | None = None
        for reservation in self._reservations:
            release = getattr(reservation, "release", None)
            if not callable(release):
                continue
            try:
                result = release()
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


class CompositeAdmissionLimiter:
    """按声明顺序组合多个准入组件，任一失败时释放已预留部分。"""

    def __init__(self, *components: Any) -> None:
        self._components = tuple(components)

    async def reserve(
        self, request: GenerationRequest, admission: Mapping[str, Any] | None = None
    ) -> _CompositeReservation | None:
        reservations: list[Any] = []
        try:
            for component in self._components:
                result = component.reserve(request, admission)
                reservation = await result if inspect.isawaitable(result) else result
                if reservation is not None:
                    reservations.append(reservation)
        except BaseException:
            if reservations:
                try:
                    await _CompositeReservation(reservations).release()
                except Exception:
                    pass
            raise
        return _CompositeReservation(reservations) if reservations else None
