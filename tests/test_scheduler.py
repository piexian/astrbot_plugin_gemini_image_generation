from __future__ import annotations

import asyncio
import contextlib
import contextvars
from datetime import datetime, timedelta, timezone

import pytest

from tl.core.dispatch import ProviderDispatchError
from tl.core.errors import (
    LegacyProviderError,
    ProviderRouteError,
    ReferenceTimeoutError,
)
from tl.core.job_store import SQLiteJobStore
from tl.core.models import Job
from tl.core.requests import GenerationRequest
from tl.core.results import GenerationResult
from tl.core.router import ProviderCandidate, ProviderRouter
from tl.core.scheduler import (
    _ROUTER_EXECUTE_GRACE_SECONDS,
    JobScheduler,
)
from tl.core.service import GenerationService
from tl.core.states import JobState


class FakeProvider:
    def __init__(self, *, failures: int = 0, block: asyncio.Event | None = None):
        self.failures = failures
        self.attempts = 0
        self.started = asyncio.Event()
        self.block = block

    async def supports(self, request):
        return True

    async def normalize(self, request):
        return request

    async def build_request(self, request, attempt):
        self.attempts += 1
        self.started.set()
        if self.block is not None:
            await self.block.wait()
        return "https://provider.test/generate", {}, lambda: {"attempt": attempt.number}

    async def send(self, request, attempt, *, url, headers, body_factory):
        if self.failures:
            self.failures -= 1
            raise ProviderDispatchError("upstream 5xx", status_code=503)
        return {"attempt": attempt.number}

    async def parse_response(self, response, *, request, attempt):
        return GenerationResult(artifacts=(f"artifact-{attempt.number}",))


class FakeReservation:
    def __init__(self, *, release_gate: asyncio.Event | None = None):
        self.releases = 0
        self.release_gate = release_gate

    async def release(self):
        if self.release_gate is not None:
            await self.release_gate.wait()
        self.releases += 1


class FakeLeaseSource:
    def __init__(self, *, release_gate: asyncio.Event | None = None):
        self.reservations: list[FakeReservation] = []
        self.release_gate = release_gate

    async def reserve(self, request):
        reservation = FakeReservation(release_gate=self.release_gate)
        self.reservations.append(reservation)
        return reservation

    async def resolve(self, references, *, deadline_at=None, requester=None):
        return tuple(f"resolved:{item}" for item in references)


def request(*, timeout: float = 2.0, references: tuple[str, ...] = ()):
    return GenerationRequest(
        prompt="draw",
        source="test",
        reference_images=references,
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=timeout),
    )


def make_scheduler(tmp_path, provider=None, **kwargs):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    scheduler = JobScheduler(store, provider=provider or FakeProvider(), **kwargs)
    return scheduler, store


@pytest.mark.asyncio
async def test_submit_persists_before_runtime_task(tmp_path):
    provider = FakeProvider()
    scheduler, store = make_scheduler(tmp_path, provider)
    try:
        handle = await scheduler.submit(request())
        assert await store.get(handle.job_id) is not None
        await provider.started.wait()
        assert (await store.get(handle.job_id)).state in {
            JobState.RUNNING,
            JobState.RESULT_READY,
            JobState.SUCCEEDED,
        }
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_create_attach_failure_has_no_orphan_task(tmp_path, monkeypatch):
    provider = FakeProvider()
    scheduler, store = make_scheduler(tmp_path, provider)
    try:

        async def fail_create(job):
            raise RuntimeError("create failed")

        monkeypatch.setattr(store, "create", fail_create)
        with pytest.raises(RuntimeError):
            await scheduler.submit(request())
        assert scheduler._tasks == {}
    finally:
        await scheduler.close()


def scheduler_module_create_task():
    import tl.core.scheduler as module

    return module.asyncio, module.asyncio.create_task


@pytest.mark.asyncio
async def test_attach_failure_has_no_orphan_task(tmp_path, monkeypatch):
    scheduler, store = make_scheduler(tmp_path)
    module, original = scheduler_module_create_task()

    def fail_create_task(*args, **kwargs):
        raise RuntimeError("attach failed")

    monkeypatch.setattr(module, "create_task", fail_create_task)
    try:
        with pytest.raises(RuntimeError):
            await scheduler.submit(request())
        assert scheduler._tasks == {}
        snapshot = await scheduler.get_status(next(iter(scheduler._done), "missing"))
        assert snapshot is None or snapshot.state is JobState.ORPHANED
    finally:
        monkeypatch.setattr(module, "create_task", original)
        await scheduler.close()


@pytest.mark.asyncio
async def test_cancel_waits_for_cleanup(tmp_path):
    block = asyncio.Event()
    provider = FakeProvider(block=block)
    gate = asyncio.Event()
    quota = FakeLeaseSource(release_gate=gate)
    scheduler, store = make_scheduler(tmp_path, provider, quota=quota)
    try:
        handle = await scheduler.submit(request())
        await provider.started.wait()
        cancelling = asyncio.create_task(scheduler.cancel(handle.job_id))
        await asyncio.sleep(0)
        assert not cancelling.done()
        gate.set()
        snapshot = await cancelling
        assert snapshot.state is JobState.CANCELLED
        assert quota.reservations[0].releases == 1
    finally:
        block.set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_close_rejects_new_jobs(tmp_path):
    scheduler, _ = make_scheduler(tmp_path)
    await scheduler.close()
    with pytest.raises(Exception, match="关闭"):
        await scheduler.submit(request())


@pytest.mark.asyncio
async def test_provider_5xx_retries_by_deadline(tmp_path):
    provider = FakeProvider(failures=2)
    scheduler, _ = make_scheduler(tmp_path, provider, retry_backoff=0.01)
    try:
        result = await scheduler.wait((await scheduler.submit(request())).job_id)
        assert result.state is JobState.SUCCEEDED
        assert provider.attempts == 3
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_retry_does_not_exceed_deadline(tmp_path):
    provider = FakeProvider(failures=100)
    scheduler, _ = make_scheduler(tmp_path, provider, retry_backoff=0.05)
    try:
        result = await scheduler.wait(
            (await scheduler.submit(request(timeout=0.03))).job_id
        )
        assert result.state is JobState.FAILED
        assert provider.attempts <= 2
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_all_resource_leases_are_released(tmp_path):
    provider = FakeProvider()
    sources = [FakeLeaseSource() for _ in range(5)]
    scheduler, _ = make_scheduler(
        tmp_path,
        provider,
        quota=sources[0],
        limiter=sources[1],
        reference_store=sources[2],
        artifact_store=sources[3],
        delivery_store=sources[4],
    )
    try:
        result = await scheduler.wait(
            (await scheduler.submit(request(references=("reference-1",)))).job_id
        )
        assert result.state is JobState.SUCCEEDED
        assert all(source.reservations[0].releases == 1 for source in sources)
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_restart_recovery_status_is_observable(tmp_path):
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    job = Job(
        job_id="restart-job",
        request=request(),
        state=JobState.RUNNING,
    )
    await store.create(job)
    await store.close()
    restored_store = SQLiteJobStore(tmp_path, import_legacy=False)
    scheduler = JobScheduler(restored_store, provider=FakeProvider())
    try:
        snapshot = await scheduler.get_status(job.job_id)
        assert snapshot is not None
        assert snapshot.state is JobState.INTERRUPTED
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_stale_worker_cannot_overwrite_new_result(tmp_path):
    block = asyncio.Event()
    provider = FakeProvider(block=block)
    scheduler, store = make_scheduler(tmp_path, provider)
    try:
        handle = await scheduler.submit(request())
        await provider.started.wait()
        winner = await store.get(handle.job_id)
        winner.set_result(GenerationResult(artifacts=("winner",)))
        winner.transition(JobState.RESULT_READY)
        await store.save(winner)
        winner.transition(JobState.SUCCEEDED)
        await store.save(winner)
        block.set()
        result = await scheduler.wait(handle.job_id)
        assert result.result is not None
        assert result.result.artifacts == ("winner",)
    finally:
        block.set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_heartbeat_keeps_long_provider_owner_live(tmp_path):
    block = asyncio.Event()
    provider = FakeProvider(block=block)
    scheduler, store = make_scheduler(
        tmp_path,
        provider,
        heartbeat_interval=0.05,
    )
    second = None
    try:
        handle = await scheduler.submit(request(timeout=2.0))
        await provider.started.wait()
        await asyncio.sleep(0.3)
        second = SQLiteJobStore(tmp_path, import_legacy=False, stale_owner_timeout=0.2)
        snapshot = await second.get(handle.job_id)
        assert snapshot is not None
        assert snapshot.state is JobState.RUNNING
    finally:
        block.set()
        if second is not None:
            await second.close()
        await scheduler.close()


class RouterCandidateProvider:
    """Router 候选 contract 的最小 fake。"""

    def __init__(
        self,
        name: str,
        *,
        send_error: Exception | None = None,
        hang: bool = False,
        artifacts: tuple[str, ...] = ("artifact-ok",),
    ):
        self.name = name
        self.send_error = send_error
        self.hang = hang
        self.artifacts = artifacts
        self.send_count = 0

    async def supports(self, request):
        return True

    async def normalize(self, request):
        return request

    async def build_request(self, request, attempt):
        return "https://provider.test/generate", {}, lambda: {"attempt": attempt.number}

    async def send(self, request, attempt, *, url, headers, body_factory):
        self.send_count += 1
        if self.hang:
            await asyncio.sleep(30)
        if self.send_error is not None:
            raise self.send_error
        return {"attempt": body_factory()["attempt"]}

    async def parse_response(self, response, *, request, attempt):
        return GenerationResult(artifacts=self.artifacts)


@pytest.mark.asyncio
async def test_router_single_candidate_succeeds(tmp_path):
    provider = RouterCandidateProvider("alpha")
    router = ProviderRouter([ProviderCandidate(provider, "c1")])
    scheduler, _ = make_scheduler(tmp_path, router)
    try:
        result = await scheduler.wait((await scheduler.submit(request())).job_id)
        assert result.state is JobState.SUCCEEDED
        assert result.result is not None
        assert result.result.provider == "alpha"
        assert result.result.candidate_id == "c1"
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_router_falls_back_to_next_candidate(tmp_path):
    first = RouterCandidateProvider("first", send_error=ValueError("bad"))
    second = RouterCandidateProvider("second")
    router = ProviderRouter(
        [ProviderCandidate(first, "c1"), ProviderCandidate(second, "c2")],
        backoff=0.0,
    )
    scheduler, _ = make_scheduler(tmp_path, router)
    try:
        result = await scheduler.wait((await scheduler.submit(request())).job_id)
        assert result.state is JobState.SUCCEEDED
        assert result.result is not None
        assert result.result.provider == "second"
        assert first.send_count == 1
        assert second.send_count == 1
        # attempts 记录：Router 在结果上聚合全部候选的总尝试数。
        assert result.result.attempt_count == 2
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_router_all_candidates_failed(tmp_path):
    first = RouterCandidateProvider("first", send_error=ValueError("first failed"))
    second = RouterCandidateProvider("second", send_error=ValueError("second failed"))
    router = ProviderRouter(
        [ProviderCandidate(first, "c1"), ProviderCandidate(second, "c2")],
        backoff=0.0,
    )
    scheduler, store = make_scheduler(tmp_path, router)
    try:
        job_id = (await scheduler.submit(request())).job_id
        result = await scheduler.wait(job_id)
        assert result.state is JobState.FAILED
        assert result.error_code == "provider_route_failed"
        assert "所有候选" in (result.error_message or "")
        events = await store.list_events(job_id)
        assert any(event.event_type == "job_runtime_error" for event in events)
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_router_deadline_truncation(tmp_path):
    provider = RouterCandidateProvider("slow", hang=True)
    router = ProviderRouter([provider], backoff=0.0)
    scheduler, _ = make_scheduler(tmp_path, router)
    try:
        result = await scheduler.wait(
            (await scheduler.submit(request(timeout=0.2))).job_id
        )
        assert result.state is JobState.FAILED
        assert result.error_code == "deadline_exceeded"
    finally:
        await scheduler.close()


class _LegacyE2EClient:
    """端到端用例的鸭子类型 legacy client。"""

    def __init__(self):
        self.configs = []

    async def generate_image(
        self, config, *, max_retries=3, total_timeout=120, **kwargs
    ):
        self.configs.append(config)
        return (["https://img.test/generated.png"], [], "ok", None)


class _ReferenceStoreHarness:
    """为 ReferenceService 补上引用租约（reserve），resolve 原样转发。"""

    def __init__(self, service):
        self.service = service
        self.resolved_locations = []

    async def reserve(self, references):
        return FakeReservation()

    async def resolve(self, references, *, deadline_at=None, requester=None):
        artifacts = [item async for item in self.service.resolve(references)]
        self.resolved_locations.extend(item.location for item in artifacts)
        return artifacts


@pytest.mark.asyncio
async def test_legacy_adapter_end_to_end_with_reference_service(tmp_path):
    from io import BytesIO
    from pathlib import Path as RefPath

    from PIL import Image as PILImage

    from tl.core.legacy_adapter import LegacyClientAdapter
    from tl.core.path_policy import PathPolicy
    from tl.core.reference_service import ReferenceService
    from tl.core.remote_fetch import RemoteFetchService

    buf = BytesIO()
    PILImage.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="PNG")
    root = tmp_path / "allowed"
    root.mkdir()
    ref = root / "ref.png"
    ref.write_bytes(buf.getvalue())
    cache_dir = tmp_path / "cache"
    policy = PathPolicy((root,), allow_private_networks=True)
    service = ReferenceService(policy, RemoteFetchService(policy), cache_dir)

    client = _LegacyE2EClient()
    adapter = LegacyClientAdapter(
        client, provider="gemini", model="gemini-x", candidate_id="cand-1"
    )
    router = ProviderRouter([ProviderCandidate(adapter, "cand-1")])
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    harness = _ReferenceStoreHarness(service)
    scheduler = JobScheduler(store, provider=router, reference_store=harness)
    try:
        request = GenerationRequest(
            prompt="draw with reference",
            source="test",
            reference_images=(str(ref),),
        )
        result = await scheduler.wait((await scheduler.submit(request)).job_id)
        assert result.state is JobState.SUCCEEDED
        assert result.result is not None
        assert result.result.provider == "gemini"
        # 传给 legacy client 的参考图就是 ReferenceService 落盘的 location。
        assert len(client.configs) == 1
        assert client.configs[0].reference_images == harness.resolved_locations
        assert client.configs[0].reference_images
        for location in client.configs[0].reference_images:
            assert location.startswith(str(cache_dir))
            assert RefPath(location).is_file()
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_original_references_cleared_after_jobs(tmp_path):
    provider = FakeProvider()
    scheduler, _ = make_scheduler(tmp_path, provider, reference_store=FakeLeaseSource())
    try:
        # 无引用与有引用各跑一个，任务完成回调都应清掉登记。
        await scheduler.wait((await scheduler.submit(request())).job_id)
        await scheduler.wait(
            (await scheduler.submit(request(references=("raw-ref.png",)))).job_id
        )
        # 断言用途直接访问内部 dict。
        assert scheduler._original_references == {}
    finally:
        await scheduler.close()
    assert scheduler._original_references == {}


@pytest.mark.asyncio
async def test_recovery_excludes_tmp_reference_jobs() -> None:
    from tl.core.recovery import recoverable_jobs

    handle = f"ref:tmp:{'a' * 32}"
    tmp_payload = Job.create(
        request(references=(handle,)), job_id="tmp-job"
    ).to_storage_dict()
    assert tmp_payload["metadata"]["has_tmp_references"] is True
    # 旧记录没有 metadata 标记，从描述符 display 推断，也不能进入重放列表。
    tmp_payload["metadata"] = {}
    tmp_job = Job.from_dict(tmp_payload)
    durable_job = Job.from_dict(
        Job.create(
            request(references=("/tmp/durable.png",)), job_id="durable-job"
        ).to_storage_dict()
    )

    class Store:
        async def list_jobs(self, states):
            return [tmp_job, durable_job]

    recovered = await recoverable_jobs(Store())

    assert recovered == [durable_job]
    assert tmp_job.metadata["has_tmp_references"] is True


@pytest.mark.asyncio
async def test_reference_lease_receives_original_references(tmp_path) -> None:
    handle = f"ref:tmp:{'b' * 32}"

    class Source:
        def __init__(self):
            self.reserved = []

        async def reserve(self, references):
            self.reserved.append(tuple(references))
            return FakeReservation()

        async def resolve(self, references, *, deadline_at=None, requester=None):
            return tuple(f"resolved:{item}" for item in references)

    source = Source()
    scheduler, _ = make_scheduler(tmp_path, reference_store=source)
    try:
        await scheduler.wait(
            (await scheduler.submit(request(references=(handle,)))).job_id
        )
    finally:
        await scheduler.close()

    assert source.reserved == [(handle,)]


@pytest.mark.asyncio
async def test_run_without_registration_resolves_store_descriptors(tmp_path):
    """重启恢复：无内存登记时回退 store 里的脱敏描述符。"""

    class SpyResolver:
        def __init__(self):
            self.inputs = []

        async def reserve(self, references):
            return FakeReservation()

        async def resolve(self, references, *, deadline_at=None, requester=None):
            self.inputs.append(tuple(references))
            return tuple(f"resolved:{item}" for item in references)

    resolver = SpyResolver()
    scheduler, store = make_scheduler(tmp_path, reference_store=resolver)
    try:
        source_request = request(references=("/tmp/raw-ref.png",))
        job = Job.create(source_request, job_id="recovered")
        # 模拟 JobStore 持久化往返：原始引用被替换为 ref: 描述符。
        restored = Job.from_dict(job.to_storage_dict())
        await store.create(restored)
        await scheduler._run(restored.job_id)
        assert len(resolver.inputs) == 1
        received = resolver.inputs[0]
        assert received and all(item.startswith("ref:") for item in received)
        final = await store.get(restored.job_id)
        assert final.state is JobState.SUCCEEDED
    finally:
        await scheduler.close()


class StaticArtifactsProvider:
    """返回固定 artifacts 的最小 provider（走 dispatch 路径）。"""

    def __init__(self, artifacts: tuple[str, ...]):
        self.artifacts = artifacts
        self.attempts = 0

    async def supports(self, request):
        return True

    async def normalize(self, request):
        return request

    async def build_request(self, request, attempt):
        self.attempts += 1
        return "https://provider.test/generate", {}, lambda: {"attempt": attempt.number}

    async def send(self, request, attempt, *, url, headers, body_factory):
        return {}

    async def parse_response(self, response, *, request, attempt):
        return GenerationResult(artifacts=self.artifacts)


@pytest.mark.asyncio
async def test_wait_returns_runtime_artifacts_while_store_stays_redacted(tmp_path):
    artifacts = (
        "/tmp/runtime/session-1/image-001.png",
        "https://cdn.test/img.png?sig=secret&exp=42",
    )
    scheduler, store = make_scheduler(tmp_path, StaticArtifactsProvider(artifacts))
    try:
        job_id = (await scheduler.submit(request())).job_id
        result = await scheduler.wait(job_id)
        assert result.state is JobState.SUCCEEDED
        # wait() 读回运行时原文：本地绝对路径与带 query 的 URL 原样保留。
        assert result.result is not None
        assert result.result.artifacts == artifacts
        # 多个 waiter 读到同一运行时结果，读取不消耗登记。
        repeat = await scheduler.wait(job_id)
        assert repeat.result is not None
        assert repeat.result.artifacts == artifacts
        stored = await store.get(job_id)
        assert stored is not None and stored.result is not None
        stored_artifacts = stored.result.artifacts
        assert stored_artifacts != artifacts
        # 存储侧脱敏：本地路径替换为 artifact:<hash>，URL 丢弃 query。
        assert stored_artifacts[0].startswith("artifact:")
        assert stored_artifacts[1] == "https://cdn.test/img.png"
    finally:
        await scheduler.close()


class _GatedSaveStore:
    """包一层 store：成功终态 save 提交后、返回 _run 前挂在闸门上。"""

    def __init__(self, inner, gate):
        self._inner = inner
        self._gate = gate

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def save(self, job, *, expected_revision=None):
        saved = await self._inner.save(job, expected_revision=expected_revision)
        if job.state in (JobState.SUCCEEDED, JobState.PARTIAL):
            await self._gate.wait()
        return saved


@pytest.mark.asyncio
async def test_wait_waits_for_registration_after_terminal_commit(tmp_path):
    """终态已提交而登记未发生时，wait() 等任务收尾再读运行时结果。"""
    gate = asyncio.Event()
    artifacts = ("/tmp/runtime/race.png",)
    store = SQLiteJobStore(tmp_path, import_legacy=False)
    scheduler = JobScheduler(store, provider=StaticArtifactsProvider(artifacts))
    scheduler.store = _GatedSaveStore(store, gate)
    try:
        job_id = (await scheduler.submit(request())).job_id
        # 轮询直到 SUCCEEDED save 已提交、_run 挂在闸门上。
        for _ in range(200):
            stored = await scheduler.store.get(job_id)
            if stored.state is JobState.SUCCEEDED:
                break
            await asyncio.sleep(0.01)
        assert stored.state is JobState.SUCCEEDED
        waiting = asyncio.create_task(scheduler.wait(job_id))
        await asyncio.sleep(0.05)
        gate.set()
        result = await waiting
        assert result.state is JobState.SUCCEEDED
        # 读回运行时原文而非脱敏的 artifact:<hash>。
        assert result.result is not None
        assert result.result.artifacts == artifacts
    finally:
        gate.set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_wait_falls_back_to_redacted_result_after_eviction(tmp_path, monkeypatch):
    monkeypatch.setattr("tl.core.scheduler._RUNTIME_RESULT_CAPACITY", 1)
    artifacts = ("/tmp/runtime/a.png", "https://cdn.test/b.png?sig=x")
    scheduler, store = make_scheduler(tmp_path, StaticArtifactsProvider(artifacts))
    try:
        first = (await scheduler.submit(request())).job_id
        assert (await scheduler.wait(first)).result.artifacts == artifacts
        second = (await scheduler.submit(request())).job_id
        assert (await scheduler.wait(second)).result.artifacts == artifacts
        # 容量 1：second 登记淘汰 first，读回回退存储里的脱敏结果。
        fallback = await scheduler.wait(first)
        assert fallback.result is not None
        assert fallback.result.artifacts != artifacts
        stored = await store.get(first)
        assert stored is not None and stored.result is not None
        assert fallback.result.artifacts == stored.result.artifacts
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_close_clears_runtime_results(tmp_path):
    provider = StaticArtifactsProvider(("/tmp/runtime/a.png",))
    scheduler, _ = make_scheduler(tmp_path, provider)
    job_id = (await scheduler.submit(request())).job_id
    await scheduler.wait(job_id)
    assert scheduler._runtime_results
    await scheduler.close()
    assert scheduler._runtime_results == {}


@pytest.mark.asyncio
async def test_cancelled_and_failed_jobs_are_not_registered(tmp_path):
    block = asyncio.Event()
    provider = FakeProvider(block=block, failures=100)
    scheduler, _ = make_scheduler(tmp_path, provider, retry_backoff=0.01)
    try:
        cancelled = await scheduler.submit(request())
        await provider.started.wait()
        await scheduler.cancel(cancelled.job_id)
        assert scheduler._runtime_results == {}
        block.set()
        failed = await scheduler.wait(
            (await scheduler.submit(request(timeout=0.05))).job_id
        )
        assert failed.state is JobState.FAILED
        assert scheduler._runtime_results == {}
    finally:
        block.set()
        await scheduler.close()


class DeadlineRecordingResolver:
    """记录收到的 deadline_at，符合 ReferenceServiceProtocol.resolve 签名。"""

    def __init__(self):
        self.deadlines = []

    async def reserve(self, references):
        return FakeReservation()

    async def resolve(self, references, *, deadline_at=None, requester=None):
        self.deadlines.append(deadline_at)
        return tuple(f"resolved:{item}" for item in references)


@pytest.mark.asyncio
async def test_reference_resolve_receives_job_deadline(tmp_path):
    resolver = DeadlineRecordingResolver()
    scheduler, store = make_scheduler(
        tmp_path, FakeProvider(), reference_store=resolver
    )
    try:
        job_id = (
            await scheduler.submit(request(timeout=3.0, references=("ref-1",)))
        ).job_id
        result = await scheduler.wait(job_id)
        assert result.state is JobState.SUCCEEDED
        stored = await store.get(job_id)
        assert stored is not None
        # scheduler 把 job 的 deadline 原样透传给 reference store。
        assert resolver.deadlines == [stored.deadline_at]
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_reference_resolve_receives_requester(tmp_path):
    class RequesterRecordingResolver:
        def __init__(self):
            self.calls = []

        async def reserve(self, references):
            return FakeReservation()

        async def resolve(self, references, *, deadline_at=None, requester=None):
            self.calls.append((deadline_at, dict(requester or {})))
            return tuple(f"resolved:{item}" for item in references)

    resolver = RequesterRecordingResolver()
    scheduler, store = make_scheduler(
        tmp_path, FakeProvider(), reference_store=resolver
    )
    try:
        job_id = (
            await scheduler.submit(
                GenerationRequest(
                    prompt="draw",
                    source="test",
                    reference_images=("ref-1",),
                    deadline_at=datetime.now(timezone.utc) + timedelta(seconds=3.0),
                    requester={"user_id": "u1", "umo": "g1"},
                )
            )
        ).job_id
        result = await scheduler.wait(job_id)
        assert result.state is JobState.SUCCEEDED
        stored = await store.get(job_id)
        # scheduler 把 job.requester 与 deadline 一起透传给 reference store。
        assert resolver.calls == [(stored.deadline_at, {"user_id": "u1", "umo": "g1"})]
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_reference_deadline_exhaustion_fails_job(tmp_path):
    class ExhaustedDeadlineResolver:
        """模拟 ReferenceService：deadline 耗尽时抛 ReferenceTimeoutError。"""

        async def reserve(self, references):
            return FakeReservation()

        async def resolve(self, references, *, deadline_at=None, requester=None):
            # 真实服务由 fetch 按 deadline 截断后抛 ReferenceTimeoutError，
            # 此处收到 deadline 即模拟耗尽场景。
            if deadline_at is not None:
                raise ReferenceTimeoutError("参考图解析 deadline 已耗尽")
            return tuple(references)

    provider = FakeProvider()
    scheduler, _ = make_scheduler(
        tmp_path, provider, reference_store=ExhaustedDeadlineResolver()
    )
    try:
        result = await scheduler.wait(
            (await scheduler.submit(request(timeout=1.0, references=("ref-1",)))).job_id
        )
        assert result.state is JobState.FAILED
        assert result.error_code == "reference_timeout"
        # 参考图解析失败在 provider 分发之前终止。
        assert provider.attempts == 0
    finally:
        await scheduler.close()


class RecordingObserver:
    """记录钩子事件；可按钩子名抛错或挂起。"""

    def __init__(self, name="obs", *, raise_in=None, hang_in=None):
        self.name = name
        self.events: list[tuple] = []
        self._raise_in = raise_in
        self._hang_in = hang_in

    async def _maybe_break(self, hook, *args):
        if hook == self._raise_in:
            raise RuntimeError(f"{hook} broken")
        if hook == self._hang_in:
            await asyncio.sleep(5)

    async def job_accepted(self, job):
        self.events.append(("accepted", job.job_id, job.state))
        await self._maybe_break("job_accepted", job)

    async def job_result(self, job, result):
        self.events.append(("result", job.job_id, len(result.artifacts)))
        await self._maybe_break("job_result", job, result)

    async def job_finished(self, job):
        self.events.append(("finished", job.job_id, job.state))
        await self._maybe_break("job_finished", job)


_SCOPE_VAR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "observer_scope_test", default=None
)


class ScopeObserver:
    """runtime_scope 设置 contextvar；provider 执行期间读取，退出后复原。"""

    def __init__(self):
        self.seen_in_provider: list[str | None] = []

    def runtime_scope(self, job):
        @contextlib.contextmanager
        def scope():
            token = _SCOPE_VAR.set("scoped")
            try:
                yield
            finally:
                _SCOPE_VAR.reset(token)

        return scope()


class ScopeReadingProvider(FakeProvider):
    def __init__(self, observer: ScopeObserver):
        super().__init__()
        self._observer = observer

    async def build_request(self, request, attempt):
        self._observer.seen_in_provider.append(_SCOPE_VAR.get())
        return await super().build_request(request, attempt)


@pytest.mark.asyncio
async def test_observer_hooks_run_in_accepted_result_finished_order(tmp_path):
    observer = RecordingObserver()
    scheduler, _ = make_scheduler(tmp_path, FakeProvider(), observers=[observer])
    try:
        handle = await scheduler.submit(request())
        result = await scheduler.wait(handle.job_id)
        assert result.state is JobState.SUCCEEDED
        assert observer.events == [
            ("accepted", handle.job_id, JobState.QUEUED),
            ("result", handle.job_id, 1),
            ("finished", handle.job_id, JobState.SUCCEEDED),
        ]
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_observer_runtime_scope_wraps_provider_execution(tmp_path):
    observer = ScopeObserver()
    provider = ScopeReadingProvider(observer)
    scheduler, _ = make_scheduler(tmp_path, provider, observers=[observer])
    try:
        handle = await scheduler.submit(request())
        await scheduler.wait(handle.job_id)
        # provider 执行期间 contextvar 可见，退出后复原。
        assert observer.seen_in_provider == ["scoped"]
        assert _SCOPE_VAR.get() is None
    finally:
        await scheduler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["raise", "hang"])
async def test_observer_failure_does_not_affect_job_or_other_observers(tmp_path, mode):
    broken = RecordingObserver(
        "broken",
        raise_in="job_result" if mode == "raise" else None,
        hang_in="job_result" if mode == "hang" else None,
    )
    healthy = RecordingObserver("healthy")
    scheduler, _ = make_scheduler(
        tmp_path, FakeProvider(), observers=[broken, healthy], observer_timeout=0.05
    )
    try:
        handle = await scheduler.submit(request())
        result = await scheduler.wait(handle.job_id)
        # 异常/超时的观察者不影响 Job 状态，也不影响其他观察者。
        assert result.state is JobState.SUCCEEDED
        assert broken.events[0][0] == "accepted"
        assert healthy.events == [
            ("accepted", handle.job_id, JobState.QUEUED),
            ("result", handle.job_id, 1),
            ("finished", handle.job_id, JobState.SUCCEEDED),
        ]
    finally:
        await scheduler.close()


class _RouteErrorProvider:
    """Router 路径：execute 抛 ProviderRouteError，cause 带具体 code。"""

    async def execute(self, request, *, deadline_at=None):
        cause = LegacyProviderError("boom", code="provider_http_429", retryable=False)
        raise ProviderRouteError("所有候选 provider 均失败", retryable=False) from cause


@pytest.mark.asyncio
async def test_observer_failed_error_code_taken_from_route_cause(tmp_path):
    observer = RecordingObserver()
    scheduler, _ = make_scheduler(tmp_path, _RouteErrorProvider(), observers=[observer])
    try:
        handle = await scheduler.submit(request())
        result = await scheduler.wait(handle.job_id)
        assert result.state is JobState.FAILED
        assert result.error_code == "provider_http_429"
        assert observer.events[-1] == ("finished", handle.job_id, JobState.FAILED)
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_observer_receives_cancelled_terminal_state(tmp_path):
    observer = RecordingObserver()
    block = asyncio.Event()
    provider = FakeProvider(block=block)
    scheduler, _ = make_scheduler(tmp_path, provider, observers=[observer])
    try:
        handle = await scheduler.submit(request(timeout=30.0))
        await provider.started.wait()
        await scheduler.cancel(handle.job_id)
        block.set()
        assert observer.events[-1] == (
            "finished",
            handle.job_id,
            JobState.CANCELLED,
        )
    finally:
        block.set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_observer_receives_orphaned_when_attach_fails(tmp_path, monkeypatch):
    observer = RecordingObserver()
    scheduler, _ = make_scheduler(tmp_path, FakeProvider(), observers=[observer])
    module, original = scheduler_module_create_task()

    def failing_create_task(coro, **kwargs):
        if name := kwargs.get("name", ""):
            if str(name).startswith("job:"):
                raise RuntimeError("create_task failed")
        return original(coro, **kwargs)

    try:
        monkeypatch.setattr(module, "create_task", failing_create_task)
        with pytest.raises(RuntimeError):
            await scheduler.submit(request())
        # 建 task 失败的 Job 已触发 job_accepted，孤儿终态也要通知。
        assert observer.events[0][0] == "accepted"
        assert observer.events[-1][2] is JobState.ORPHANED
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_close_notifies_observers_with_cancelled(tmp_path):
    observer = RecordingObserver()
    block = asyncio.Event()
    provider = FakeProvider(block=block)
    scheduler, _ = make_scheduler(tmp_path, provider, observers=[observer])
    await scheduler.submit(request(timeout=30.0))
    await provider.started.wait()
    await scheduler.close()
    block.set()
    assert observer.events[-1][2] is JobState.CANCELLED


def test_result_wait_timeout_includes_observer_hook_time(tmp_path):
    """等待上限 = deadline + provider 兜底 + 两个观察者钩子 + 固定余量。"""

    scheduler, _ = make_scheduler(tmp_path, FakeProvider(), observer_timeout=7.0)
    assert scheduler.result_wait_timeout(100) == pytest.approx(
        100 + _ROUTER_EXECUTE_GRACE_SECONDS + 2 * 7.0 + 10
    )
    service = GenerationService(scheduler)
    assert service.result_wait_timeout(100) == scheduler.result_wait_timeout(100)
