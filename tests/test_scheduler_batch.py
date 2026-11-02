"""批量父子 Job：准入、并发、deadline、汇总、取消与关闭。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from tl.core.errors import LegacyProviderError, QueueFullError
from tl.core.job_store import SQLiteJobStore
from tl.core.requests import GenerationRequest
from tl.core.results import GenerationResult
from tl.core.scheduler import JobScheduler
from tl.core.states import JobState
from tl.v2_admission import LegacyFifoLimiter


class BatchProvider:
    def __init__(self, *, outputs=None, failures=(), gate=None):
        self.outputs = outputs or {}
        self.failures = set(failures)
        self.gate = gate
        self.active = 0
        self.max_active = 0
        self.started: list[str] = []

    async def supports(self, request):
        return True

    async def normalize(self, request):
        return request

    async def build_request(self, request, attempt):
        return "", {}, lambda: None

    async def send(self, request, attempt, *, url, headers, body_factory):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.append(request.prompt)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if request.prompt in self.failures:
                raise LegacyProviderError("生成失败", code="provider_failed")
            return request.prompt
        finally:
            self.active -= 1

    async def parse_response(self, response, *, request, attempt):
        return GenerationResult(
            artifacts=self.outputs.get(response, (f"{response}.png",))
        )


class Observer:
    def __init__(self):
        self.accepted: list[str] = []
        self.children_started: list[str] = []
        self.finished: list[tuple[str, JobState]] = []
        self.children_finished: list[str] = []

    async def job_accepted(self, job):
        self.accepted.append(job.job_id)

    async def child_started(self, parent_job, child_job):
        self.children_started.append(child_job.job_id)

    async def child_finished(self, parent_job, child_job, result):
        self.children_finished.append(child_job.job_id)

    async def job_finished(self, job):
        self.finished.append((job.job_id, job.state))


class Reservation:
    def __init__(self):
        self.releases = 0
        self.refunds = 0

    async def release(self):
        self.releases += 1

    async def refund(self):
        self.refunds += 1


class Admission:
    def __init__(self, *, reject=(), skip_parent=False):
        self.reject = set(reject)
        self.skip_parent = skip_parent
        self.calls: list[str] = []
        self.reservations: list[Reservation] = []

    async def reserve(self, request, admission=None):
        self.calls.append(request.prompt)
        if self.skip_parent and request.metadata.get("batch_parent"):
            return None
        if request.prompt in self.reject:
            raise QueueFullError("队列已满")
        reservation = Reservation()
        self.reservations.append(reservation)
        return reservation


class Delivery:
    def __init__(self):
        self.results = []
        self.failures = []

    async def deliver(self, job, result):
        self.results.append((job.job_id, result))

    async def deliver_failure(self, job):
        self.failures.append((job.job_id, job.state, job.error_code))


def child(name, count=1, seconds=5):
    return GenerationRequest(
        prompt=name,
        source="llm_batch",
        image_count=count,
        deadline_at=None,
        metadata={"deadline_seconds": seconds, "quota": False, "item_name": name},
    )


def parent(children, *, cost=1):
    return GenerationRequest(
        prompt="batch",
        source="llm_batch",
        image_count=sum(item.image_count for item in children),
        deadline_at=None,
        metadata={"batch_parent": True, "quota": {"cost": cost}},
    )


def make_scheduler(tmp_path, provider, **kwargs):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    return JobScheduler(store, provider=provider, **kwargs), store


async def eventually(check, timeout=2):
    for _ in range(int(timeout * 100)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("条件在限定时间内未成立")


@pytest.mark.asyncio
async def test_submit_batch_persists_jobs_and_aggregates_in_child_order(tmp_path):
    gate = asyncio.Event()
    provider = BatchProvider(outputs={"a": ("a-1.png", "a-2.png")}, gate=gate)
    observer = Observer()
    scheduler, store = make_scheduler(tmp_path, provider, observers=[observer])
    children = [child("a", 2), child("b"), child("c")]
    try:
        handle = await scheduler.submit_batch(
            parent(children, cost=3), children, concurrency=2
        )
        assert handle.child_job_ids == tuple(observer.accepted[1:])
        assert observer.accepted == [handle.job_id, *handle.child_job_ids]
        jobs = [await store.get(job_id) for job_id in observer.accepted]
        assert all(job is not None for job in jobs)
        assert jobs[0].deadline_at is None
        assert [job.parent_job_id for job in jobs[1:]] == [handle.job_id] * 3

        gate.set()
        result = await scheduler.wait(handle.job_id)
        assert result.state is JobState.SUCCEEDED
        assert result.result.artifacts == ("a-1.png", "a-2.png", "b.png", "c.png")
        assert result.result.stats["children"] == [
            {
                "job_id": handle.child_job_ids[0],
                "state": "succeeded",
                "requested_images": 2,
                "generated_images": 2,
                "error_code": None,
            },
            {
                "job_id": handle.child_job_ids[1],
                "state": "succeeded",
                "requested_images": 1,
                "generated_images": 1,
                "error_code": None,
            },
            {
                "job_id": handle.child_job_ids[2],
                "state": "succeeded",
                "requested_images": 1,
                "generated_images": 1,
                "error_code": None,
            },
        ]
        assert observer.children_finished == list(handle.child_job_ids)
    finally:
        gate.set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_batch_concurrency_and_deadline_start_with_child(tmp_path):
    gate = asyncio.Event()
    provider = BatchProvider(gate=gate)
    scheduler, store = make_scheduler(tmp_path, provider)
    children = [child("a"), child("b"), child("c")]
    try:
        handle = await scheduler.submit_batch(parent(children), children, concurrency=2)
        await eventually(lambda: provider.active == 2)
        assert set(provider.started) == {"a", "b"}
        late = await store.get(handle.child_job_ids[2])
        assert late.deadline_at is None and late.request.deadline_at is None

        gate.set()
        await eventually(lambda: "c" in provider.started)
        late = await store.get(handle.child_job_ids[2])
        assert late.deadline_at == late.request.deadline_at
        assert late.deadline_at > late.created_at + timedelta(seconds=4.9)
        result = await scheduler.wait(handle.job_id)
        assert result.state is JobState.SUCCEEDED
        assert provider.max_active == 2
    finally:
        gate.set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_child_limiter_rejection_isolated_and_quota_charged_once(tmp_path):
    provider = BatchProvider()
    quota = Admission(skip_parent=False)
    limiter = Admission(reject={"bad"}, skip_parent=True)
    scheduler, store = make_scheduler(tmp_path, provider, quota=quota, limiter=limiter)
    children = [child("good"), child("bad"), child("other")]
    try:
        handle = await scheduler.submit_batch(
            parent(children, cost=3), children, concurrency=1
        )
        result = await scheduler.wait(handle.job_id)
        assert result.state is JobState.PARTIAL
        assert result.result.artifacts == ("good.png", "other.png")
        bad = await store.get(handle.child_job_ids[1])
        assert bad.state is JobState.FAILED and bad.error_code == "queue_full"
        assert quota.calls == ["batch"]
        assert limiter.calls == ["batch", "good", "bad", "other"]
        assert len(quota.reservations) == 1
        assert quota.reservations[0].refunds == 0
        assert sum(item.releases > 0 for item in limiter.reservations) == 2
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_submit_batch_failure_refunds_parent_quota(tmp_path, monkeypatch):
    scheduler, store = make_scheduler(tmp_path, BatchProvider(), quota=Admission())
    children = [child("a")]
    quota = scheduler.quota

    async def fail_create(job):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "create", fail_create)
    try:
        with pytest.raises(RuntimeError, match="disk full"):
            await scheduler.submit_batch(parent(children), children, concurrency=1)
        assert quota.reservations[0].refunds == 1
        assert scheduler._tasks == {} and scheduler._batches == {}
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_all_children_failed_fails_parent_and_delivers_failure(tmp_path):
    provider = BatchProvider(failures={"a", "b"})
    delivery = Delivery()
    scheduler, store = make_scheduler(tmp_path, provider)
    children = [child("a"), child("b")]
    try:
        handle = await scheduler.submit_batch(
            parent(children), children, concurrency=2, delivery=delivery
        )
        result = await scheduler.wait(handle.job_id)
        assert result.state is JobState.FAILED
        assert result.error_code == "batch_all_failed"
        assert result.error_message == "所有批量任务均生成失败"
        assert delivery.results == []
        assert delivery.failures == [
            (handle.job_id, JobState.FAILED, "batch_all_failed")
        ]
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_cancel_parent_cascades_to_running_and_unstarted_children(tmp_path):
    gate = asyncio.Event()
    provider = BatchProvider(gate=gate)
    observer = Observer()
    scheduler, store = make_scheduler(tmp_path, provider, observers=[observer])
    children = [child("a"), child("b"), child("c")]
    try:
        handle = await scheduler.submit_batch(parent(children), children, concurrency=2)
        await eventually(lambda: provider.active == 2)
        snapshot = await scheduler.cancel(handle.job_id)
        assert snapshot.state is JobState.CANCELLED
        for child_id in handle.child_job_ids:
            assert (await store.get(child_id)).state is JobState.CANCELLED
        assert set(observer.children_finished) == set(handle.child_job_ids)
        await eventually(lambda: not scheduler._batches)
    finally:
        gate.set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_cancelling_one_child_does_not_cancel_siblings(tmp_path):
    gate = asyncio.Event()
    provider = BatchProvider(gate=gate)
    scheduler, store = make_scheduler(tmp_path, provider)
    children = [child("a"), child("b")]
    try:
        handle = await scheduler.submit_batch(parent(children), children, concurrency=2)
        await eventually(lambda: provider.active == 2)
        await scheduler.cancel(handle.child_job_ids[0])
        gate.set()
        result = await scheduler.wait(handle.job_id)
        assert result.state is JobState.PARTIAL
        assert result.result.artifacts == ("b.png",)
        assert (await store.get(handle.child_job_ids[0])).state is JobState.CANCELLED
        assert (await store.get(handle.child_job_ids[1])).state is JobState.SUCCEEDED
    finally:
        gate.set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_close_cancels_entire_batch_and_cleans_registries(tmp_path):
    gate = asyncio.Event()
    provider = BatchProvider(gate=gate)
    observer = Observer()
    scheduler, _store = make_scheduler(tmp_path, provider, observers=[observer])
    children = [child("a"), child("b"), child("c")]
    handle = await scheduler.submit_batch(parent(children), children, concurrency=2)
    await eventually(lambda: provider.active == 2)

    await scheduler.close()

    assert set(observer.children_finished) == set(handle.child_job_ids)
    assert scheduler._tasks == {}
    assert scheduler._batches == {}
    assert scheduler._admissions == {}
    assert scheduler._cleanup_tasks == set()


def test_legacy_fifo_limiter_skips_only_batch_parent():
    class Fifo:
        def __init__(self):
            self.calls = 0

        def reserve(self):
            self.calls += 1
            raise QueueFullError("stop here")

    fifo = Fifo()
    limiter = LegacyFifoLimiter(lambda: fifo)
    assert limiter.reserve(parent([child("a")])) is None
    assert fifo.calls == 0
    with pytest.raises(QueueFullError, match="stop here"):
        limiter.reserve(child("a"))
    assert fifo.calls == 1
