"""公开 SDK 的 v2 提交、投递与生命周期测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.test_plugin_service import make_service
from tests.test_web_studio_service import _png
from tl.api_types import APIError
from tl.core.errors import (
    CoreError,
    QueueFullError,
    QuotaExceededError,
    ServiceClosedError,
)
from tl.core.stack import build_generation_stack
from tl.plugin_service import PluginServiceError
from tl.v2_admission import LegacyFifoLimiter, RateLimiterQuota
from tl.v2_tracking import TrackerProjection


async def make_v2_service(tmp_path, archive=None):
    service, gate = make_service(tmp_path)
    if archive is not None:
        service.plugin.web_studio_service.archive_images = archive
    stack = build_generation_stack(
        service.plugin.api_client,
        [{"provider": "legacy", "model": "auto"}],
        tmp_path / "v2",
        (),
        quota=RateLimiterQuota(lambda: service.plugin.rate_limiter),
        limiter=LegacyFifoLimiter(lambda: service.plugin.generation_scheduler),
        observers=[
            TrackerProjection(
                service.plugin.generation_tracker,
                service.plugin.web_studio_service.archive_images,
            )
        ],
    )
    service.plugin._v2_service_for = lambda umo: stack
    return service, stack, gate


async def close_service(service, stack=None, *, facade_closed=False):
    if not facade_closed:
        await service.close()
    if stack is not None:
        await stack.close()
    await service.plugin.generation_scheduler.close()
    await service.plugin.background_task_manager.close()
    await service.plugin.generation_tracker.close()
    await service.plugin.rate_limiter.close()


async def wait_v2_delivery(service, task_id):
    for _ in range(200):
        if task_id not in service._v2_jobs:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("v2 投递未结束")


@pytest.mark.asyncio
async def test_v2_end_to_end_fill_archive_callback_and_wait(tmp_path):
    async def archive(urls, paths, **kwargs):
        return [f"archived-{index}.png" for index, _ in enumerate(urls + paths, 1)]

    service, stack, _ = await make_v2_service(tmp_path, archive=archive)
    callback_records = []
    callback_done = asyncio.Event()

    async def on_complete(record):
        callback_records.append(record)
        callback_done.set()

    try:
        accepted = await service.submit(
            plugin_id="a",
            plugin_name="插件 A",
            prompt="draw",
            image_count=2,
            on_complete=on_complete,
        )
        assert accepted == {
            "task_id": accepted["task_id"],
            "status": "queued",
            "instance_id": service.instance_id,
        }
        result = await service.wait_task(accepted["task_id"], plugin_id="a", timeout=5)
        await asyncio.wait_for(callback_done.wait(), 2)
        await wait_v2_delivery(service, accepted["task_id"])
        final = await service.get_task(accepted["task_id"], plugin_id="a")

        assert result["status"] == "succeeded"
        assert result["job_id"]
        assert result["generated_images"] == 2
        assert result["image_urls"] == []
        assert [path.rsplit("/", 1)[-1] for path in result["image_paths"]] == [
            "archived-1.png",
            "archived-2.png",
        ]
        assert final["callback_status"] == "succeeded"
        assert callback_records[0]["task_id"] == accepted["task_id"]
        assert service.plugin.api_client.calls == 2
        assert service._tasks == {}
        assert service._v2_jobs == {}
        history = service.plugin.generation_tracker.get(result["job_id"])
        assert history["caller"] == {"plugin_id": "a", "plugin_name": "插件 A"}
        assert history["images"] == ["archived-1.png", "archived-2.png"]
        assert history["callback_status"] == "succeeded"
    finally:
        await close_service(service, stack)


class RecordingV2Service:
    def __init__(self, error=None):
        self.error = error
        self.references = None
        self.request = None
        self.delivery = None
        self.cancelled = []

    async def prepare_references(self, references, **kwargs):
        self.references = list(references)
        return tuple(
            "ref:tmp:" + "a" * 32 if value.startswith("file:") else value
            for value in references
        )

    async def submit(self, request, *, delivery):
        self.request = request
        self.delivery = delivery
        if self.error:
            raise self.error
        return SimpleNamespace(job_id="v2-job-1")

    async def cancel(self, job_id):
        self.cancelled.append(job_id)


@pytest.mark.asyncio
async def test_v2_submit_preserves_urls_ingests_local_files_and_builds_record(tmp_path):
    service, _ = make_service(tmp_path)
    fake = RecordingV2Service()
    service.plugin._v2_service_for = lambda umo: fake
    local = _png(tmp_path / "reference.png")
    url = "https://example.org/reference.png"
    try:
        accepted = await service.submit(
            plugin_id="a",
            plugin_name="插件 A",
            prompt="edit",
            reference_images=[url, str(local)],
            image_count=2,
            use_rate_limit=True,
        )
        assert accepted["status"] == "queued"
        assert fake.references == [url, local.as_uri()]
        request = fake.request
        assert request.source == "plugin"
        assert request.reference_images == (url, "ref:tmp:" + "a" * 32)
        assert request.image_count == 2
        assert request.metadata["require_images"] is True
        assert request.metadata["fill_image_count"] is True
        assert request.metadata["caller"] == {
            "plugin_id": "a",
            "plugin_name": "插件 A",
        }
        assert request.metadata["quota"] == {
            "cost": 1,
            "scope": "plugin",
            "plugin_id": "a",
        }
        record = await service.get_task(accepted["task_id"], plugin_id="a")
        assert record["job_id"] == "v2-job-1"
        assert record["requested_images"] == 2
        assert record["caller"] == {"plugin_id": "a", "plugin_name": "插件 A"}
        assert fake.delivery._task_id == accepted["task_id"]
    finally:
        await close_service(service)


@pytest.mark.asyncio
async def test_v2_session_quota_scope(tmp_path):
    service, _ = make_service(tmp_path)
    fake = RecordingV2Service()
    service.plugin._v2_service_for = lambda umo: fake
    try:
        await service.submit(
            plugin_id="a",
            prompt="draw",
            umo="xai:FriendMessage:user-1",
            use_rate_limit=True,
        )
        assert fake.request.metadata["quota"] == {"cost": 1, "scope": "session"}
    finally:
        await close_service(service)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (QuotaExceededError("额度不足"), "rate_limited"),
        (QueueFullError("队列已满"), "queue_full"),
        (ServiceClosedError("服务关闭"), "service_closed"),
        (CoreError("请求无效"), "invalid_request"),
    ],
)
async def test_v2_submit_error_codes(tmp_path, error, code):
    service, _ = make_service(tmp_path)
    service.plugin._v2_service_for = lambda umo: RecordingV2Service(error)
    try:
        with pytest.raises(PluginServiceError) as caught:
            await service.submit(plugin_id="a", prompt="draw")
        assert caught.value.code == code
    finally:
        await close_service(service)


@pytest.mark.asyncio
async def test_v2_partial_fill_returns_original_sources(tmp_path):
    service, stack, _ = await make_v2_service(tmp_path)
    calls = 0

    async def duplicate(config, **kwargs):
        nonlocal calls
        calls += 1
        config.successful_provider = "xai"
        config.successful_model = "grok-imagine-image"
        return ["https://example.org/same.png"], [], None, None

    service.plugin.api_client.generate_image = duplicate
    try:
        accepted = await service.submit(plugin_id="a", prompt="draw", image_count=2)
        result = await service.wait_task(accepted["task_id"], plugin_id="a", timeout=5)
        assert result["status"] == "partial_success"
        assert result["generated_images"] == 1
        assert result["image_urls"] == ["https://example.org/same.png"]
        assert result["image_paths"] == []
        assert result["error"] == "供应商未返回新的图片"
        assert calls == 2
    finally:
        await close_service(service, stack)


@pytest.mark.asyncio
async def test_v2_failure_updates_record_and_runs_callback(tmp_path):
    service, stack, _ = await make_v2_service(tmp_path)
    callback_records = []
    callback_done = asyncio.Event()

    async def fail(config, **kwargs):
        raise APIError("上游失败", 500, "server")

    async def on_complete(record):
        callback_records.append(record)
        callback_done.set()

    service.plugin.api_client.generate_image = fail
    try:
        accepted = await service.submit(
            plugin_id="a", prompt="draw", on_complete=on_complete
        )
        result = await service.wait_task(accepted["task_id"], plugin_id="a", timeout=5)
        assert result["status"] == "failed"
        assert result["error"]
        assert result["generated_images"] == 0
        await asyncio.wait_for(callback_done.wait(), 2)
        await wait_v2_delivery(service, accepted["task_id"])
        assert callback_records[0]["task_id"] == accepted["task_id"]
        final = await service.get_task(accepted["task_id"], plugin_id="a")
        assert final["callback_status"] == "succeeded"
    finally:
        await close_service(service, stack)


@pytest.mark.asyncio
async def test_v2_cancel_task_returns_interrupted(tmp_path):
    service, stack, gate = await make_v2_service(tmp_path)
    gate.clear()
    try:
        accepted = await service.submit(plugin_id="a", prompt="draw")
        await asyncio.wait_for(service.plugin.api_client.entered.wait(), 2)
        result = await service.cancel_task(accepted["task_id"], plugin_id="a")
        assert result["status"] == "interrupted"
        assert result["error"] == "任务已取消或服务已关闭"
    finally:
        gate.set()
        await close_service(service, stack)


@pytest.mark.asyncio
async def test_close_cancels_v2_and_skips_callback(tmp_path):
    service, stack, gate = await make_v2_service(tmp_path)
    gate.clear()
    called = False

    async def on_complete(record):
        nonlocal called
        called = True

    try:
        accepted = await service.submit(
            plugin_id="a", prompt="draw", on_complete=on_complete
        )
        await asyncio.wait_for(service.plugin.api_client.entered.wait(), 2)
        await service.close()
        record = await service.plugin.background_task_manager.get_for_plugin(
            accepted["task_id"], "a"
        )
        assert record["status"] == "interrupted"
        assert record["callback_status"] == "interrupted"
        assert called is False
    finally:
        gate.set()
        await close_service(service, stack, facade_closed=True)


@pytest.mark.asyncio
async def test_none_v2_service_uses_legacy_task_path(tmp_path):
    service, _ = make_service(tmp_path)
    calls = []
    service.plugin._v2_service_for = lambda umo: calls.append(umo) or None
    try:
        accepted = await service.submit(plugin_id="a", prompt="draw")
        assert accepted["task_id"] in service._tasks
        result = await service.wait_task(accepted["task_id"], plugin_id="a")
        assert result["status"] == "succeeded"
        assert calls == [None]
    finally:
        await close_service(service)


@pytest.mark.asyncio
async def test_v2_callback_timeout_marks_only_callback_failed(tmp_path, monkeypatch):
    import tl.v2_sdk_delivery as delivery_module

    monkeypatch.setattr(delivery_module, "_CALLBACK_TIMEOUT_SECONDS", 0)
    service, stack, _ = await make_v2_service(tmp_path)

    async def on_complete(record):
        await asyncio.Event().wait()

    try:
        accepted = await service.submit(
            plugin_id="a", prompt="draw", on_complete=on_complete
        )
        result = await service.wait_task(accepted["task_id"], plugin_id="a", timeout=5)
        await wait_v2_delivery(service, accepted["task_id"])
        final = await service.get_task(accepted["task_id"], plugin_id="a")
        assert result["status"] == "succeeded"
        assert final["status"] == "succeeded"
        assert final["callback_status"] == "failed"
    finally:
        await close_service(service, stack)


@pytest.mark.asyncio
async def test_v2_record_creation_failure_cancels_submitted_job(tmp_path, monkeypatch):
    service, _ = make_service(tmp_path)
    fake = RecordingV2Service()
    service.plugin._v2_service_for = lambda umo: fake

    async def fail_create(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(service.plugin.background_task_manager, "create", fail_create)
    try:
        with pytest.raises(RuntimeError, match="disk full"):
            await service.submit(plugin_id="a", prompt="draw")
        assert fake.cancelled == ["v2-job-1"]
        assert service._v2_jobs == {}
    finally:
        await close_service(service)
