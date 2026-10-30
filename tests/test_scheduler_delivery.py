"""调度器投递阶段：DELIVERY_PENDING、投递失败收尾、登记判定点与失败兜底。"""

from __future__ import annotations

import asyncio
import inspect
from io import BytesIO

import pytest
from PIL import Image

from tests.test_scheduler import (
    FakeProvider,
    StaticArtifactsProvider,
    make_scheduler,
    request,
    scheduler_module_create_task,
)
from tl.core.models import Job
from tl.core.path_policy import PathPolicy
from tl.core.reference_service import ReferenceService
from tl.core.remote_fetch import RemoteFetchService
from tl.core.results import GenerationResult
from tl.core.scheduler import JobScheduler
from tl.core.service import GenerationService
from tl.core.states import JobState


class RecordingDelivery:
    """记录 deliver / deliver_failure 调用；可按需抛错或停在闸门上。"""

    def __init__(
        self,
        store,
        log=None,
        *,
        deliver_error=None,
        failure_error=None,
        gate=None,
        failure_gate=None,
    ):
        self.store = store
        self.log = log if log is not None else []
        self.deliver_error = deliver_error
        self.failure_error = failure_error
        self.gate = gate
        self.failure_gate = failure_gate
        self.deliveries: list[tuple] = []
        self.failures: list[tuple] = []
        self.entered = asyncio.Event()
        self.cancelled = False

    async def deliver(self, job, result):
        persisted = await self.store.get(job.job_id)
        self.deliveries.append((job.state, persisted.state, result.artifacts))
        self.log.append(("deliver",))
        self.entered.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.deliver_error is not None:
            raise self.deliver_error

    async def deliver_failure(self, job):
        self.failures.append((job.state, job.error_code))
        self.log.append(("deliver_failure", job.state))
        if self.failure_gate is not None:
            await self.failure_gate.wait()
        if self.failure_error is not None:
            raise self.failure_error


class LogObserver:
    """把观察者钩子写进与投递方共用的日志，核对相对顺序。"""

    def __init__(self, log):
        self.log = log

    async def job_accepted(self, job):
        self.log.append(("observer", "accepted"))

    async def job_result(self, job, result):
        self.log.append(("observer", "result"))

    async def job_finished(self, job):
        self.log.append(("observer", "finished", job.state))


class PartialProvider(StaticArtifactsProvider):
    async def parse_response(self, response, *, request, attempt):
        return GenerationResult(artifacts=self.artifacts, partial=True)


def assert_registry_empty(scheduler):
    assert scheduler._deliveries == {}
    assert scheduler._delivery_decided == set()


# ===== 投递阶段 =====


@pytest.mark.asyncio
async def test_delivery_runs_in_delivery_pending_after_result_observers(tmp_path):
    log = []
    scheduler, store = make_scheduler(
        tmp_path, FakeProvider(), observers=[LogObserver(log)]
    )
    delivery = RecordingDelivery(store, log)
    try:
        handle = await scheduler.submit(request(), delivery=delivery)
        result = await scheduler.wait(handle.job_id)

        assert result.state is JobState.SUCCEEDED
        assert result.error_code is None
        # deliver 看到的 Job 与已落库状态都是 DELIVERY_PENDING，且拿到未脱敏结果。
        assert delivery.deliveries == [
            (JobState.DELIVERY_PENDING, "delivery_pending", ("artifact-1",))
        ]
        assert delivery.failures == []
        # job_result 观察者先于投递，job_finished 在投递之后。
        assert log == [
            ("observer", "accepted"),
            ("observer", "result"),
            ("deliver",),
            ("observer", "finished", JobState.SUCCEEDED),
        ]
        events = await store.list_events(handle.job_id)
        assert [e.state for e in events if e.event_type == "state_changed"] == [
            JobState.QUEUED,
            JobState.RUNNING,
            JobState.RESULT_READY,
            JobState.DELIVERY_PENDING,
            JobState.SUCCEEDED,
        ]
        assert_registry_empty(scheduler)
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_partial_result_stays_partial_after_successful_delivery(tmp_path):
    scheduler, store = make_scheduler(tmp_path, PartialProvider(("a.png",)))
    delivery = RecordingDelivery(store)
    try:
        handle = await scheduler.submit(request(), delivery=delivery)
        result = await scheduler.wait(handle.job_id)

        assert result.state is JobState.PARTIAL
        assert result.error_code is None
        assert len(delivery.deliveries) == 1
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_without_delivery_the_flow_skips_delivery_pending(tmp_path):
    scheduler, store = make_scheduler(tmp_path, FakeProvider())
    try:
        handle = await scheduler.submit(request())
        result = await scheduler.wait(handle.job_id)

        assert result.state is JobState.SUCCEEDED
        events = await store.list_events(handle.job_id)
        assert JobState.DELIVERY_PENDING not in [e.state for e in events]
    finally:
        await scheduler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_delivery_failure_ends_partial_and_keeps_result(tmp_path, failure):
    log = []
    gate = asyncio.Event() if failure == "timeout" else None
    scheduler, store = make_scheduler(
        tmp_path,
        StaticArtifactsProvider(("/tmp/runtime/out.png",)),
        observers=[LogObserver(log)],
        delivery_timeout=0.05,
    )
    delivery = RecordingDelivery(
        store,
        log,
        gate=gate,
        deliver_error=RuntimeError("boom") if failure == "error" else None,
    )
    try:
        handle = await scheduler.submit(request(), delivery=delivery)
        result = await scheduler.wait(handle.job_id)

        assert result.state is JobState.PARTIAL
        assert result.error_code == "delivery_failed"
        expected_reason = "RuntimeError: boom" if failure == "error" else "超时"
        assert expected_reason in result.error_message
        # 结果不因投递失败丢失：运行时原文与落库的脱敏形态都在。
        assert result.result is not None
        assert result.result.artifacts == ("/tmp/runtime/out.png",)
        stored = await store.get(handle.job_id)
        assert stored.state is JobState.PARTIAL
        assert stored.error_code == "delivery_failed"
        assert stored.result is not None and stored.result.artifacts
        # 投递已交接：失败不再触发 deliver_failure，观察者仍收到终态。
        assert delivery.failures == []
        assert log[-1] == ("observer", "finished", JobState.PARTIAL)
        assert_registry_empty(scheduler)
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_cancel_during_delivery_cancels_job_without_failure_callback(tmp_path):
    gate = asyncio.Event()
    scheduler, store = make_scheduler(tmp_path, FakeProvider())
    delivery = RecordingDelivery(store, gate=gate)
    try:
        handle = await scheduler.submit(request(), delivery=delivery)
        await asyncio.wait_for(delivery.entered.wait(), 2)

        snapshot = await scheduler.cancel(handle.job_id)

        assert snapshot.state is JobState.CANCELLED
        assert delivery.cancelled
        # deliver 已接手：取消不会再走 deliver_failure。
        assert delivery.failures == []
        stored = await store.get(handle.job_id)
        assert stored.state is JobState.CANCELLED
        assert stored.result is not None
        assert_registry_empty(scheduler)
    finally:
        gate.set()
        await scheduler.close()


class DeliveryPendingGateStore:
    """DELIVERY_PENDING 落库提交后、返回 _run 之前停在闸门上。"""

    def __init__(self, inner):
        self._inner = inner
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def save(self, job, *, expected_revision=None):
        saved = await self._inner.save(job, expected_revision=expected_revision)
        if job.state is JobState.DELIVERY_PENDING:
            self.entered.set()
            await self.gate.wait()
        return saved


@pytest.mark.asyncio
async def test_cancel_before_deliver_starts_still_gets_deliver_failure(tmp_path):
    scheduler, store = make_scheduler(tmp_path, FakeProvider())
    gated = DeliveryPendingGateStore(store)
    scheduler.store = gated
    delivery = RecordingDelivery(store)
    try:
        handle = await scheduler.submit(request(), delivery=delivery)
        await asyncio.wait_for(gated.entered.wait(), 2)

        snapshot = await scheduler.cancel(handle.job_id)

        # 已越过判定点但 deliver 尚未接手：不能让投递方既收不到结果也收不到失败。
        assert snapshot.state is JobState.CANCELLED
        assert delivery.deliveries == []
        assert delivery.failures == [(JobState.CANCELLED, None)]
        assert_registry_empty(scheduler)
    finally:
        gated.gate.set()
        await scheduler.close()


def test_set_error_keeps_result_and_set_result_clears_error():
    job = Job.create(request(), job_id="job-1")
    job.set_error("boom", "first")
    result = GenerationResult(artifacts=("a.png",))
    job.set_result(result)
    assert job.error_code is None and job.error_message is None

    job.set_error("delivery_failed", "later")

    assert job.result is result
    assert (job.error_code, job.error_message) == ("delivery_failed", "later")


@pytest.mark.asyncio
async def test_service_forwards_trusted_local_to_reference_ingest(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.png"
    buffer = BytesIO()
    Image.new("RGB", (8, 8), (4, 5, 6)).save(buffer, format="PNG")
    outside.write_bytes(buffer.getvalue())
    policy = PathPolicy((root,))
    references = ReferenceService(
        policy, RemoteFetchService(policy), tmp_path / "cache"
    )
    service = GenerationService(None, references)
    reference = outside.resolve().as_uri()

    # 默认不读取本地路径；入口声明已校验后才换成句柄。
    assert await service.prepare_references([reference], requester={}) == (reference,)
    prepared = await service.prepare_references(
        [reference], requester={}, trusted_local=True
    )

    assert prepared[0].startswith("ref:tmp:")


# ===== 登记判定点 =====


class ResultGateObserver:
    """job_result 钩子停在闸门上：此时 Job 尚未越过投递判定点。"""

    def __init__(self):
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def job_result(self, job, result):
        self.entered.set()
        await self.gate.wait()


class AttachProbeObserver:
    """job_finished 钩子里尝试登记：Job 的 task 仍在，但早已越过判定点。"""

    def __init__(self, scheduler, delivery):
        self.scheduler = scheduler
        self.delivery = delivery
        self.outcome = None

    async def job_finished(self, job):
        assert job.job_id in self.scheduler._tasks
        self.outcome = self.scheduler.attach_delivery(job.job_id, self.delivery)


@pytest.mark.asyncio
async def test_attach_delivery_before_the_decision_point_is_honored(tmp_path):
    observer = ResultGateObserver()
    scheduler, store = make_scheduler(tmp_path, FakeProvider(), observers=[observer])
    delivery = RecordingDelivery(store)
    try:
        handle = await scheduler.submit(request())
        await asyncio.wait_for(observer.entered.wait(), 2)

        assert scheduler.attach_delivery(handle.job_id, delivery) is True
        # 重复登记被拒绝，不覆盖第一个投递方。
        other = RecordingDelivery(store)
        assert scheduler.attach_delivery(handle.job_id, other) is False
        observer.gate.set()
        result = await scheduler.wait(handle.job_id)

        assert result.state is JobState.SUCCEEDED
        assert len(delivery.deliveries) == 1
        assert other.deliveries == [] and other.failures == []
        # Job 结束后不再可登记。
        assert scheduler.attach_delivery(handle.job_id, other) is False
    finally:
        observer.gate.set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_attach_delivery_after_the_decision_point_is_rejected(tmp_path):
    scheduler, store = make_scheduler(tmp_path, FakeProvider())
    late = RecordingDelivery(store)
    probe = AttachProbeObserver(scheduler, late)
    scheduler.observers = (probe,)
    try:
        handle = await scheduler.submit(request())
        result = await scheduler.wait(handle.job_id)

        assert result.state is JobState.SUCCEEDED
        # 判定点已过且当时没有投递：结果归调用方处理，晚到的登记被拒绝。
        assert probe.outcome is False
        assert late.deliveries == [] and late.failures == []
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_attach_delivery_is_rejected_while_delivery_is_in_progress(tmp_path):
    gate = asyncio.Event()
    scheduler, store = make_scheduler(tmp_path, FakeProvider())
    delivery = RecordingDelivery(store, gate=gate)
    other = RecordingDelivery(store)
    try:
        handle = await scheduler.submit(request(), delivery=delivery)
        await asyncio.wait_for(delivery.entered.wait(), 2)

        assert scheduler.attach_delivery(handle.job_id, other) is False

        gate.set()
        await scheduler.wait(handle.job_id)
        assert other.deliveries == [] and other.failures == []
    finally:
        gate.set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_attach_delivery_unknown_job_and_closed_scheduler(tmp_path):
    block = asyncio.Event()
    provider = FakeProvider(block=block)
    scheduler, store = make_scheduler(tmp_path, provider)
    delivery = RecordingDelivery(store)
    handle = await scheduler.submit(request(timeout=30.0))
    await provider.started.wait()

    assert scheduler.attach_delivery("job-missing", delivery) is False
    await scheduler.close()

    # 关闭后的调度器不再接受登记，登记表为空。
    assert scheduler.attach_delivery(handle.job_id, delivery) is False
    assert_registry_empty(scheduler)


@pytest.mark.asyncio
async def test_attach_delivery_is_rejected_while_closing(tmp_path):
    block = asyncio.Event()
    provider = FakeProvider(block=block)
    scheduler, store = make_scheduler(tmp_path, provider)
    delivery = RecordingDelivery(store)
    try:
        handle = await scheduler.submit(request(timeout=30.0))
        await provider.started.wait()
        # close() 进行中：task 尚在，但不再接受新的登记。
        scheduler._closed = True

        assert scheduler.attach_delivery(handle.job_id, delivery) is False
    finally:
        scheduler._closed = False
        block.set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_close_clears_registrations_without_runtime_task(tmp_path):
    scheduler, store = make_scheduler(tmp_path, FakeProvider())
    scheduler._deliveries["ghost"] = RecordingDelivery(store)
    scheduler._delivery_decided.add("ghost")

    await scheduler.close()

    assert_registry_empty(scheduler)


def test_attach_delivery_is_synchronous():
    # 与判定点之间不允许交错：必须是普通函数，不能是协程函数。
    assert not inspect.iscoroutinefunction(JobScheduler.attach_delivery)
    assert not inspect.iscoroutinefunction(GenerationService.attach_delivery)


@pytest.mark.asyncio
async def test_submit_delivery_equals_attach_right_after_submit(tmp_path):
    scheduler, store = make_scheduler(tmp_path, FakeProvider())
    via_submit = RecordingDelivery(store)
    via_attach = RecordingDelivery(store)
    try:
        first = await scheduler.submit(request(), delivery=via_submit)
        # 提交时已登记：之后再登记同一个 Job 与"已登记过"同样被拒绝。
        assert scheduler.attach_delivery(first.job_id, via_attach) is False
        second = await scheduler.submit(request())
        assert scheduler.attach_delivery(second.job_id, via_attach) is True

        assert (await scheduler.wait(first.job_id)).state is JobState.SUCCEEDED
        assert (await scheduler.wait(second.job_id)).state is JobState.SUCCEEDED

        expected = [(JobState.DELIVERY_PENDING, "delivery_pending", ("artifact-1",))]
        assert via_submit.deliveries == expected
        assert via_attach.deliveries == expected
        assert_registry_empty(scheduler)
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_service_forwards_delivery_registration(tmp_path):
    block = asyncio.Event()
    scheduler, store = make_scheduler(tmp_path, FakeProvider(block=block))
    service = GenerationService(scheduler)
    via_submit = RecordingDelivery(store)
    via_attach = RecordingDelivery(store)
    try:
        first = await service.submit(request(), delivery=via_submit)
        second = await service.submit(request())
        assert service.attach_delivery(second.job_id, via_attach) is True
        assert service.attach_delivery(second.job_id, via_attach) is False
        block.set()
        await service.wait(first.job_id)
        await service.wait(second.job_id)

        assert len(via_submit.deliveries) == 1
        assert len(via_attach.deliveries) == 1
    finally:
        block.set()
        await service.close()


# ===== 失败兜底 =====


@pytest.mark.asyncio
async def test_failed_job_calls_deliver_failure_once_before_job_finished(tmp_path):
    log = []
    scheduler, store = make_scheduler(
        tmp_path,
        FakeProvider(failures=100),
        observers=[LogObserver(log)],
        retry_backoff=0.01,
    )
    delivery = RecordingDelivery(store, log)
    try:
        handle = await scheduler.submit(request(timeout=0.05), delivery=delivery)
        result = await scheduler.wait(handle.job_id)

        assert result.state is JobState.FAILED
        assert delivery.deliveries == []
        assert delivery.failures == [(JobState.FAILED, result.error_code)]
        # 投递方先于 job_finished 观察者得到通知。
        assert log == [
            ("observer", "accepted"),
            ("deliver_failure", JobState.FAILED),
            ("observer", "finished", JobState.FAILED),
        ]
        assert_registry_empty(scheduler)
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_cancelled_running_job_calls_deliver_failure_once(tmp_path):
    block = asyncio.Event()
    provider = FakeProvider(block=block)
    scheduler, store = make_scheduler(tmp_path, provider)
    delivery = RecordingDelivery(store)
    try:
        handle = await scheduler.submit(request(timeout=30.0), delivery=delivery)
        await provider.started.wait()

        snapshot = await scheduler.cancel(handle.job_id)

        assert snapshot.state is JobState.CANCELLED
        assert delivery.deliveries == []
        assert delivery.failures == [(JobState.CANCELLED, None)]
        assert_registry_empty(scheduler)
    finally:
        block.set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_attach_delivery_on_running_job_gets_deliver_failure_on_cancel(tmp_path):
    block = asyncio.Event()
    provider = FakeProvider(block=block)
    scheduler, store = make_scheduler(tmp_path, provider)
    delivery = RecordingDelivery(store)
    try:
        handle = await scheduler.submit(request(timeout=30.0))
        await provider.started.wait()
        assert scheduler.attach_delivery(handle.job_id, delivery) is True

        await scheduler.cancel(handle.job_id)

        assert delivery.failures == [(JobState.CANCELLED, None)]
    finally:
        block.set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_close_cancels_running_job_and_clears_registry(tmp_path):
    block = asyncio.Event()
    provider = FakeProvider(block=block)
    scheduler, store = make_scheduler(tmp_path, provider)
    delivery = RecordingDelivery(store)
    await scheduler.submit(request(timeout=30.0), delivery=delivery)
    await provider.started.wait()
    assert scheduler._deliveries

    await scheduler.close()

    # 关闭取消运行中的 Job：投递方收到失败通知，登记表清空。
    assert [state for state, _ in delivery.failures] == [JobState.CANCELLED]
    assert_registry_empty(scheduler)


@pytest.mark.asyncio
async def test_close_during_delivery_clears_registry(tmp_path):
    gate = asyncio.Event()
    scheduler, store = make_scheduler(tmp_path, FakeProvider())
    delivery = RecordingDelivery(store, gate=gate)
    await scheduler.submit(request(), delivery=delivery)
    await asyncio.wait_for(delivery.entered.wait(), 2)

    await scheduler.close()

    assert delivery.cancelled
    assert delivery.failures == []
    assert_registry_empty(scheduler)


@pytest.mark.asyncio
async def test_orphaned_submit_calls_deliver_failure(tmp_path, monkeypatch):
    log = []
    scheduler, store = make_scheduler(
        tmp_path, FakeProvider(), observers=[LogObserver(log)]
    )
    delivery = RecordingDelivery(store, log)
    module, original = scheduler_module_create_task()

    def failing_create_task(coro, **kwargs):
        if str(kwargs.get("name", "")).startswith("job:"):
            raise RuntimeError("create_task failed")
        return original(coro, **kwargs)

    try:
        monkeypatch.setattr(module, "create_task", failing_create_task)
        with pytest.raises(RuntimeError, match="create_task failed"):
            await scheduler.submit(request(), delivery=delivery)

        assert delivery.deliveries == []
        assert delivery.failures == [(JobState.ORPHANED, None)]
        assert log[-2:] == [
            ("deliver_failure", JobState.ORPHANED),
            ("observer", "finished", JobState.ORPHANED),
        ]
        assert_registry_empty(scheduler)
    finally:
        monkeypatch.setattr(module, "create_task", original)
        await scheduler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "notified"),
    [
        (JobState.FAILED, True),
        (JobState.CANCELLED, True),
        (JobState.INTERRUPTED, True),
        (JobState.ORPHANED, True),
        (JobState.SUCCEEDED, False),
        (JobState.PARTIAL, False),
    ],
)
async def test_deliver_failure_only_for_terminal_states_without_result(
    tmp_path, state, notified
):
    scheduler, store = make_scheduler(tmp_path, FakeProvider())
    delivery = RecordingDelivery(store)
    try:
        job = Job(job_id=f"job-{state.value}", request=request(), state=state)
        await store.create(job)

        await scheduler._deliver_failure(job.job_id, delivery)

        assert delivery.failures == ([(state, None)] if notified else [])
        # 没有登记投递方（None）时什么都不做。
        await scheduler._deliver_failure(job.job_id, None)
        assert len(delivery.failures) == (1 if notified else 0)
    finally:
        await scheduler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_deliver_failure_errors_are_only_logged(tmp_path, failure):
    log = []
    scheduler, store = make_scheduler(
        tmp_path,
        FakeProvider(failures=100),
        observers=[LogObserver(log)],
        retry_backoff=0.01,
        delivery_timeout=0.05,
    )
    delivery = RecordingDelivery(
        store,
        log,
        failure_error=RuntimeError("boom") if failure == "error" else None,
        failure_gate=asyncio.Event() if failure == "timeout" else None,
    )
    try:
        handle = await scheduler.submit(request(timeout=0.05), delivery=delivery)
        result = await scheduler.wait(handle.job_id)

        # 通知失败/超时既不改 Job 状态，也不挡住 job_finished 观察者。
        assert result.state is JobState.FAILED
        assert log[-1] == ("observer", "finished", JobState.FAILED)
        assert len(delivery.failures) == 1
    finally:
        await scheduler.close()
