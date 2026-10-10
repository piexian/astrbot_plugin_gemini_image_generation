from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tl.core.dispatch import ProviderDispatchError
from tl.core.job_store import SQLiteJobStore
from tl.core.models import Job
from tl.core.requests import GenerationRequest
from tl.core.results import GenerationResult
from tl.core.router import ProviderCandidate, ProviderRouter
from tl.core.scheduler import JobScheduler
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

    async def resolve(self, references):
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

    async def generate_image(self, config, *, max_retries=3, total_timeout=120):
        self.configs.append(config)
        return (["https://img.test/generated.png"], [], "ok", None)


class _ReferenceStoreHarness:
    """为 ReferenceService 补上引用租约（reserve），并把 JobStore 持久化
    往返产出的 ref: 描述符翻译回可解析输入。"""

    def __init__(self, service):
        self.service = service
        self.by_descriptor = {}
        self.resolved_locations = []

    def register(self, *raw_inputs):
        from tl.core.requests import reference_descriptor

        for item in raw_inputs:
            descriptor = reference_descriptor(item)
            self.by_descriptor[descriptor["reference_id"]] = item

    async def reserve(self, references):
        return FakeReservation()

    async def resolve(self, references):
        inputs = tuple(self.by_descriptor.get(item, item) for item in references)
        artifacts = [item async for item in self.service.resolve(inputs)]
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
    harness.register(str(ref))
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
