"""插件层准入适配器：RateLimiterQuota、LegacyFifoLimiter 及其与调度器的衔接。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_rate_limiter import A, B, count, event
from tests.test_rate_limiter import config as limiter_config
from tl.api_types import APIError
from tl.core.errors import (
    InvalidRequestError,
    QueueFullError,
    QuotaExceededError,
    ServiceClosedError,
)
from tl.core.requests import GenerationRequest
from tl.core.stack import build_generation_stack
from tl.core.states import JobState
from tl.generation_scheduler import GenerationScheduler, scheduled_generation
from tl.rate_limiter import RateLimiter
from tl.v2_admission import LegacyFifoLimiter, RateLimiterQuota

_UNSET = object()


def quota_request(*, umo=A, quota=_UNSET) -> GenerationRequest:
    return GenerationRequest(
        prompt="draw",
        source="command",
        requester={"umo": umo} if umo else {},
        metadata={} if quota is _UNSET else {"quota": quota},
    )


def make_quota(**limits):
    limiter = RateLimiter(limiter_config(**limits))
    return limiter, RateLimiterQuota(lambda: limiter)


# ===== RateLimiterQuota =====


@pytest.mark.asyncio
async def test_default_quota_charges_session_and_global_once():
    limiter, quota = make_quota(global_max=5, session_max=2)
    reservation = await quota.reserve(quota_request(), None)
    assert reservation is not None and reservation.token
    assert count(limiter, A) == count(limiter, "global") == 1


@pytest.mark.asyncio
async def test_cost_is_charged_as_one_weighted_entry_and_over_limit_is_denied():
    limiter, quota = make_quota(session_max=3)
    await quota.reserve(quota_request(quota={"cost": 2}), None)
    assert count(limiter, A) == 2
    with pytest.raises(QuotaExceededError) as caught:
        await quota.reserve(quota_request(quota={"cost": 2}), None)
    error = caught.value
    assert "已达上限" in error.message
    assert error.details["scope"] == "session"
    assert error.details["retry_after"] >= 1
    assert error.details["silent"] is False
    # 被拒的请求不占用额度。
    assert count(limiter, A) == 2


@pytest.mark.asyncio
async def test_invalid_cost_is_rejected_by_the_limiter():
    limiter, quota = make_quota(session_max=3)
    with pytest.raises(QuotaExceededError, match="无效的生成任务数量"):
        await quota.reserve(quota_request(quota={"cost": 0}), None)
    assert not limiter._rate_limit_buckets


@pytest.mark.asyncio
async def test_quota_false_means_no_reservation():
    limiter, quota = make_quota(global_max=1, session_max=1)
    assert await quota.reserve(quota_request(quota=False), None) is None
    assert not limiter._rate_limit_buckets


@pytest.mark.asyncio
async def test_session_scope_requires_full_umo():
    limiter, quota = make_quota(session_max=1)
    with pytest.raises(QuotaExceededError, match="无法识别完整会话 UMO，已拒绝生成"):
        await quota.reserve(quota_request(umo=None), None)
    # 不完整的 UMO 由 RateLimiter 同口径拒绝。
    with pytest.raises(QuotaExceededError, match="无法识别完整会话 UMO，已拒绝生成"):
        await quota.reserve(quota_request(umo="10001"), None)
    assert not limiter._rate_limit_buckets


@pytest.mark.asyncio
async def test_global_scope_ignores_umo_and_session_bucket():
    limiter, quota = make_quota(global_max=2, session_max=1)
    scoped = {"scope": "global"}
    # 没有 UMO 也可预留；只占全局桶，不占会话桶。
    await quota.reserve(quota_request(umo=None, quota=scoped), None)
    await quota.reserve(quota_request(umo=A, quota=scoped), None)
    assert count(limiter, "global") == 2
    assert count(limiter, A) == 0
    with pytest.raises(QuotaExceededError) as caught:
        await quota.reserve(quota_request(quota=scoped), None)
    assert caught.value.details["scope"] == "global"


@pytest.mark.asyncio
async def test_plugin_scope_uses_plugin_bucket_and_requires_plugin_id():
    limiter, quota = make_quota(global_max=5, session_max=1)
    scoped = {"scope": "plugin", "plugin_id": "demo"}
    await quota.reserve(quota_request(umo=None, quota=scoped), None)
    assert count(limiter, "plugin/demo") == 1
    assert count(limiter, "global") == 1
    with pytest.raises(QuotaExceededError) as caught:
        await quota.reserve(quota_request(umo=None, quota=scoped), None)
    assert caught.value.details["scope"] == "plugin"
    for invalid in ({"scope": "plugin"}, {"scope": "tenant"}):
        with pytest.raises(InvalidRequestError):
            await quota.reserve(quota_request(quota=invalid), None)
    assert count(limiter, "plugin/demo") == 1


@pytest.mark.asyncio
async def test_group_access_denial_is_silent():
    cfg = limiter_config(global_max=10)
    cfg.group_limit_mode = "whitelist"
    cfg.group_limit_list = {"10002"}
    limiter = RateLimiter(cfg)
    quota = RateLimiterQuota(lambda: limiter)
    with pytest.raises(QuotaExceededError) as caught:
        await quota.reserve(quota_request(), {"event": event()})
    # 与旧路径 check_and_consume 一致：没有消息 = 静默拒绝，也不扣额度。
    assert caught.value.message == ""
    assert caught.value.details["silent"] is True
    assert not limiter._rate_limit_buckets
    # 白名单内的群与不带事件的准入都不受影响。
    assert await quota.reserve(quota_request(), {"event": event(group_id="10002")})
    assert await quota.reserve(quota_request(umo=B), None)


@pytest.mark.asyncio
async def test_release_keeps_charge_and_refund_restores_it():
    limiter, quota = make_quota(global_max=5, session_max=2)
    reservation = await quota.reserve(quota_request(), None)
    # Job 被接受后的 release 不退款。
    assert reservation.release() is None
    assert count(limiter, A) == count(limiter, "global") == 1
    await reservation.refund()
    assert count(limiter, A) == count(limiter, "global") == 0
    # 重复退款无副作用。
    await reservation.refund()
    assert not limiter._rate_limit_buckets


@pytest.mark.asyncio
async def test_refund_without_token_is_a_noop():
    limiter = RateLimiter(limiter_config(global_max=None, session_max=None))
    reservation = await RateLimiterQuota(lambda: limiter).reserve(quota_request(), None)
    assert reservation is not None and reservation.token is None
    await reservation.refund()
    assert not limiter._rate_limit_buckets


# ===== LegacyFifoLimiter =====


class DecoratedClient:
    """被 @scheduled_generation 装饰的假旧客户端，记录调用时队列状态。"""

    def __init__(self, scheduler, *, error=None):
        self.generation_scheduler = scheduler
        self.error = error
        self.snapshots = []

    @scheduled_generation
    async def generate_image(self, config=None, **kwargs):
        scheduler = self.generation_scheduler
        self.snapshots.append((scheduler.active, len(scheduler.waiters)))
        if self.error is not None:
            raise self.error
        return (["https://img.test/out.png"], [], "ok", None)


def provider_failure():
    # APIError 经适配层转为不可重试错误，Job 只会调用一次客户端。
    return APIError("provider failed", 400, "api_error")


def fifo_limiter(scheduler):
    return LegacyFifoLimiter(lambda: scheduler)


@pytest.mark.asyncio
async def test_reserved_ticket_is_consumed_by_first_decorated_call():
    scheduler = GenerationScheduler(concurrency=2, queue_size=5)
    reserve_calls = []
    original = scheduler.reserve

    def counting_reserve():
        reserve_calls.append(None)
        return original()

    scheduler.reserve = counting_reserve
    reservation = fifo_limiter(scheduler).reserve(quota_request(), None)
    assert (scheduler.active, len(scheduler.waiters)) == (1, 0)
    assert len(reserve_calls) == 1
    client = DecoratedClient(scheduler)
    with reservation.runtime_scope():
        await client.generate_image()
        # 第一次调用直接使用预留的票据：没有第二次排队，active 仍只有 1。
        assert client.snapshots == [(1, 0)]
        assert len(reserve_calls) == 1
        assert scheduler.active == 0
        # 票据已被领取，后续调用（如重试）重新排队。
        await client.generate_image()
        assert len(reserve_calls) == 2
    assert client.snapshots == [(1, 0), (1, 0)]
    # 装饰器已 release 过票据，调度器侧的再次 release 幂等。
    reservation.release()
    reservation.release()
    assert (scheduler.active, len(scheduler.waiters)) == (0, 0)


@pytest.mark.asyncio
async def test_failed_decorated_call_still_frees_slot_once():
    scheduler = GenerationScheduler(concurrency=1, queue_size=1)
    reservation = fifo_limiter(scheduler).reserve(quota_request(), None)
    client = DecoratedClient(scheduler, error=RuntimeError("provider failed"))
    with reservation.runtime_scope(), pytest.raises(RuntimeError):
        await client.generate_image()
    reservation.release()
    assert scheduler.active == 0


@pytest.mark.asyncio
async def test_release_frees_unused_slot_and_promotes_waiter():
    scheduler = GenerationScheduler(concurrency=1, queue_size=2)
    limiter = fifo_limiter(scheduler)
    first = limiter.reserve(quota_request(), None)
    second = limiter.reserve(quota_request(), None)
    assert (scheduler.active, len(scheduler.waiters)) == (1, 1)
    first.release()
    first.release()
    # 槽位让给排队中的预留，active 不会被重复扣减。
    assert (scheduler.active, len(scheduler.waiters)) == (1, 0)
    second.release()
    assert (scheduler.active, len(scheduler.waiters)) == (0, 0)


@pytest.mark.asyncio
async def test_waiting_reservation_blocks_decorated_call_until_slot_frees():
    scheduler = GenerationScheduler(concurrency=1, queue_size=2)
    limiter = fifo_limiter(scheduler)
    first = limiter.reserve(quota_request(), None)
    second = limiter.reserve(quota_request(), None)
    client = DecoratedClient(scheduler)

    async def run_second():
        with second.runtime_scope():
            return await client.generate_image()

    running = asyncio.create_task(run_second())
    await asyncio.sleep(0.01)
    assert not running.done() and client.snapshots == []
    first.release()
    await asyncio.wait_for(running, timeout=1)
    assert client.snapshots == [(1, 0)]
    assert scheduler.active == 0


@pytest.mark.asyncio
async def test_queue_full_maps_to_queue_full_error_with_legacy_message():
    scheduler = GenerationScheduler(concurrency=1, queue_size=1)
    limiter = fifo_limiter(scheduler)
    held = [limiter.reserve(quota_request(), None) for _ in range(2)]
    with pytest.raises(APIError) as legacy:
        scheduler.reserve()
    with pytest.raises(QueueFullError) as caught:
        limiter.reserve(quota_request(), None)
    assert caught.value.message == legacy.value.message == "生成队列已满，请稍后重试"
    assert caught.value.code == "queue_full"
    assert caught.value.__cause__ is not None
    # 拒绝的预留不占队列。
    assert (scheduler.active, len(scheduler.waiters)) == (1, 1)
    for reservation in held:
        reservation.release()


@pytest.mark.asyncio
async def test_closed_scheduler_maps_to_service_closed_error():
    scheduler = GenerationScheduler(concurrency=1, queue_size=1)
    await scheduler.close()
    with pytest.raises(ServiceClosedError) as caught:
        fifo_limiter(scheduler).reserve(quota_request(), None)
    assert caught.value.message == "生成服务正在关闭"


def test_missing_scheduler_means_no_reservation():
    assert LegacyFifoLimiter(lambda: None).reserve(quota_request(), None) is None


def test_unrelated_api_errors_pass_through():
    class Broken:
        def reserve(self):
            raise APIError("boom", None, "unknown")

    with pytest.raises(APIError, match="boom"):
        LegacyFifoLimiter(lambda: Broken()).reserve(quota_request(), None)


# ===== 与调度器、真实栈的衔接 =====


def stack_request(*, umo=A) -> GenerationRequest:
    return GenerationRequest(
        prompt="draw",
        source="command",
        requester={"umo": umo},
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )


def make_stack(tmp_path, client, limiter, fifo):
    return build_generation_stack(
        client,
        [{"provider": "legacy", "model": "auto"}],
        tmp_path / "store",
        (),
        quota=RateLimiterQuota(lambda: limiter),
        limiter=LegacyFifoLimiter(lambda: fifo),
    )


@pytest.mark.asyncio
async def test_stack_charges_quota_once_and_hands_reserved_ticket_to_client(tmp_path):
    limiter = RateLimiter(limiter_config(global_max=5, session_max=2))
    fifo = GenerationScheduler(concurrency=2, queue_size=5)
    client = DecoratedClient(fifo)
    service = make_stack(tmp_path, client, limiter, fifo)
    try:
        handle = await service.submit(stack_request())
        result = await service.wait(handle.job_id)
        assert result.state is JobState.SUCCEEDED
        assert count(limiter, A) == count(limiter, "global") == 1
        # 客户端调用直接使用 submit 时预留的票据：没有第二个队列条目。
        assert client.snapshots == [(1, 0)]
        assert (fifo.active, len(fifo.waiters)) == (0, 0)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_stack_failed_job_keeps_quota_charged_and_frees_slot(tmp_path):
    limiter = RateLimiter(limiter_config(session_max=2))
    fifo = GenerationScheduler(concurrency=1, queue_size=1)
    service = make_stack(
        tmp_path, DecoratedClient(fifo, error=provider_failure()), limiter, fifo
    )
    try:
        result = await service.wait((await service.submit(stack_request())).job_id)
        assert result.state is JobState.FAILED
        # 已接受的 Job 失败不退款（与旧路径一致），队列槽位照常释放。
        assert count(limiter, A) == 1
        assert fifo.active == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_stack_quota_denial_creates_no_job_and_skips_queue(tmp_path):
    limiter = RateLimiter(limiter_config(session_max=1))
    fifo = GenerationScheduler(concurrency=1, queue_size=1)
    service = make_stack(tmp_path, DecoratedClient(fifo), limiter, fifo)
    try:
        await service.wait((await service.submit(stack_request())).job_id)
        with pytest.raises(QuotaExceededError) as caught:
            await service.submit(stack_request())
        assert caught.value.details["silent"] is False
        assert len(await service._scheduler.store.list_jobs()) == 1
        assert count(limiter, A) == 1
        assert (fifo.active, len(fifo.waiters)) == (0, 0)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_stack_queue_full_refunds_quota_and_creates_no_job(tmp_path):
    limiter = RateLimiter(limiter_config(global_max=5, session_max=2))
    fifo = GenerationScheduler(concurrency=1, queue_size=0)
    held = fifo.reserve()
    service = make_stack(tmp_path, DecoratedClient(fifo), limiter, fifo)
    try:
        with pytest.raises(QueueFullError, match="生成队列已满"):
            await service.submit(stack_request())
        # 额度先于队列预留，队列被拒后退款：桶回到提交前。
        assert not limiter._rate_limit_buckets
        assert await service._scheduler.store.list_jobs() == []
        assert fifo.active == 1
    finally:
        held.release()
        await service.close()


@pytest.mark.asyncio
async def test_stack_cancel_running_job_frees_slot_without_refund(tmp_path):
    limiter = RateLimiter(limiter_config(session_max=2))
    fifo = GenerationScheduler(concurrency=1, queue_size=1)
    started = asyncio.Event()

    class HangingClient(DecoratedClient):
        @scheduled_generation
        async def generate_image(self, config=None, **kwargs):
            started.set()
            await asyncio.sleep(30)

    service = make_stack(tmp_path, HangingClient(fifo), limiter, fifo)
    try:
        handle = await service.submit(stack_request())
        await started.wait()
        assert fifo.active == 1
        snapshot = await service.cancel(handle.job_id)
        assert snapshot.state is JobState.CANCELLED
        assert fifo.active == 0
        assert count(limiter, A) == 1
    finally:
        await service.close()
