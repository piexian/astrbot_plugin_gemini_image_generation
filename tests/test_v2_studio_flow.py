"""WebUI 经真实 v2 栈提交、投影与归档的端到端测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_web_studio_service import _config, _png
from tl.core.errors import (
    CoreError,
    QueueFullError,
    QuotaExceededError,
    ReferenceResolveError,
    ServiceClosedError,
)
from tl.core.stack import build_generation_stack
from tl.generation_tracker import TERMINAL_STATUSES, GenerationTracker
from tl.v2_admission import (
    CompositeAdmissionLimiter,
    LegacyFifoLimiter,
    RateLimiterQuota,
    StudioAdmissionLimiter,
)
from tl.v2_tracking import TrackerProjection
from tl.web_studio_service import StudioServiceError, WebStudioService


class OutputClient:
    def __init__(self, data_dir: Path, *, gate: asyncio.Event | None = None):
        self.data_dir = data_dir
        self.gate = gate
        self.started = asyncio.Event()
        self.configs = []

    async def generate_image(self, config, **kwargs):
        self.configs.append(config)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        output = _png(
            self.data_dir / f"output-{len(self.configs)}.png", len(self.configs)
        )
        return [], [str(output)], "done", None


class RecordingQuota:
    def __init__(self):
        self.calls = []
        self.refunds = []

    async def acquire(self, umo, *, cost=1, event=None, plugin_id=None):
        scope = "plugin" if plugin_id else ("global" if umo is None else "session")
        self.calls.append({"umo": umo, "cost": cost, "scope": scope})
        return SimpleNamespace(allowed=True, token=f"token-{len(self.calls)}")

    async def refund(self, token):
        self.refunds.append(token)


class ForbiddenLegacyLimiter:
    async def acquire(self, *args, **kwargs):
        raise AssertionError("v2 不得调用旧限流器")


async def make_env(
    tmp_path,
    client,
    *,
    config=None,
    quota=None,
    studio_maximum=2,
):
    tracker = GenerationTracker(tmp_path, 30)
    studio = WebStudioService(client, tracker, config or _config(), tmp_path)
    rate_limiter = quota or RecordingQuota()
    stack = build_generation_stack(
        client,
        [{"provider": "legacy", "model": "auto"}],
        tmp_path / "v2",
        (),
        quota=RateLimiterQuota(lambda: rate_limiter),
        limiter=CompositeAdmissionLimiter(
            StudioAdmissionLimiter(lambda: studio_maximum),
            LegacyFifoLimiter(lambda: None),
        ),
        observers=[TrackerProjection(tracker, studio.archive_images)],
    )
    studio._v2_service_provider = lambda: stack
    studio.rate_limiter = ForbiddenLegacyLimiter()
    return SimpleNamespace(tracker=tracker, studio=studio, v2=stack, client=client)


async def close_env(env):
    await env.v2.close()
    await env.studio.close()
    await env.tracker.close()


def forbid_legacy_execution(monkeypatch, studio):
    async def fail_async(*args, **kwargs):
        raise AssertionError("v2 不得调用旧生成路径")

    def fail_attach(*args, **kwargs):
        raise AssertionError("v2 不得创建旧运行时任务")

    monkeypatch.setattr(studio, "_execute_generation", fail_async)
    monkeypatch.setattr(studio, "_start_single", fail_async)
    monkeypatch.setattr(studio, "_start_batch", fail_async)
    monkeypatch.setattr(studio, "_attach", fail_attach)


@pytest.mark.asyncio
async def test_single_v2_fills_archives_and_bypasses_legacy_entry(
    tmp_path, monkeypatch
):
    client = OutputClient(tmp_path)
    quota = RecordingQuota()
    env = await make_env(tmp_path, client, quota=quota)
    forbid_legacy_execution(monkeypatch, env.studio)
    try:
        accepted = await env.studio.generate({"prompt": "draw", "image_count": 2})
        assert accepted["job_id"]
        assert env.studio._runtime_tasks == {}
        assert env.studio._admitted_jobs == 0

        snapshot = await env.v2.wait(accepted["job_id"])
        assert snapshot.state.value == "succeeded"
        record = env.tracker.get(accepted["job_id"])
        assert record["status"] == "succeeded"
        assert record["requested_images"] == 2
        assert record["generated_images"] == 2
        assert len(record["images"]) == 2
        assert all(
            (env.studio.gallery_dir / name).is_file() for name in record["images"]
        )
        assert [config.image_count for config in client.configs] == [2, 1]
        assert all(config.route_all_candidates for config in client.configs)
        assert all(
            config.requested_candidate_id == "google#1" for config in client.configs
        )
        assert quota.calls == [{"umo": None, "cost": 1, "scope": "global"}]
    finally:
        await close_env(env)


@pytest.mark.asyncio
async def test_batch_v2_links_children_and_aggregates_studio_history(
    tmp_path, monkeypatch
):
    client = OutputClient(tmp_path)
    quota = RecordingQuota()
    env = await make_env(tmp_path, client, quota=quota)
    forbid_legacy_execution(monkeypatch, env.studio)
    try:
        accepted = await env.studio.generate(
            {
                "batch": [
                    {"name": "a", "prompt": "a", "image_count": 1},
                    {"name": "b", "prompt": "b", "image_count": 2},
                ]
            }
        )
        assert len(accepted["item_job_ids"]) == 2
        snapshot = await env.v2.wait(accepted["job_id"])
        assert snapshot.state.value == "succeeded"

        parent = env.tracker.get(accepted["job_id"])
        children = [env.tracker.get(job_id) for job_id in accepted["item_job_ids"]]
        assert parent["status"] == "succeeded"
        assert parent["generated_images"] == 3
        assert parent["images"] == []
        assert [child["parent_job_id"] for child in children] == [
            accepted["job_id"]
        ] * 2
        assert [child["item_name"] for child in children] == ["a", "b"]
        assert [child["status"] for child in children] == ["succeeded", "succeeded"]
        assert [child["generated_images"] for child in children] == [1, 2]
        assert sum(len(child["images"]) for child in children) == 3
        assert quota.calls == [{"umo": None, "cost": 2, "scope": "global"}]
        assert env.studio._runtime_tasks == {}
    finally:
        await close_env(env)


@pytest.mark.asyncio
async def test_upload_lease_is_released_after_reference_ingest(tmp_path):
    gate = asyncio.Event()
    client = OutputClient(tmp_path, gate=gate)
    env = await make_env(tmp_path, client)
    env.studio.upload_dir.mkdir()
    upload = _png(env.studio.upload_dir / "ref.png")
    try:
        accepted = await env.studio.generate(
            {"prompt": "edit", "upload_names": [upload.name]}
        )
        assert env.studio._upload_ref_counts == {}
        await asyncio.wait_for(client.started.wait(), 2)
        reference = Path(client.configs[0].reference_images[0])
        assert reference != upload
        assert reference.is_file()
        gate.set()
        await env.v2.wait(accepted["job_id"])
        assert env.tracker.get(accepted["job_id"])["status"] == "succeeded"
    finally:
        gate.set()
        await close_env(env)


@pytest.mark.asyncio
async def test_reference_ingest_read_failure_maps_to_404_and_releases(tmp_path):
    client = OutputClient(tmp_path)
    env = await make_env(tmp_path, client)
    env.studio.upload_dir.mkdir()
    upload = _png(env.studio.upload_dir / "ref.png")

    async def fail_ingest(*args, **kwargs):
        raise ReferenceResolveError("读取本地参考图失败: ref.png")

    env.studio._v2_service_provider = lambda: SimpleNamespace(
        prepare_references=fail_ingest
    )
    try:
        with pytest.raises(StudioServiceError) as caught:
            await env.studio.generate({"prompt": "edit", "upload_names": [upload.name]})
        assert caught.value.status_code == 404
        assert caught.value.message == "参考图片不存在或正在清理，请重新选择"
        assert env.studio._upload_ref_counts == {}
    finally:
        await close_env(env)


@pytest.mark.asyncio
async def test_quota_rejection_maps_to_429_with_details(tmp_path):
    class DeniedQuota:
        async def acquire(self, umo, *, cost=1, event=None, plugin_id=None):
            raise QuotaExceededError(
                "额度不足", details={"scope": "global", "retry_after": 7}
            )

    env = await make_env(tmp_path, OutputClient(tmp_path), quota=DeniedQuota())
    try:
        with pytest.raises(StudioServiceError) as caught:
            await env.studio.generate({"prompt": "draw"})
        assert caught.value.status_code == 429
        assert caught.value.data == {"scope": "global", "retry_after": 7}
        assert env.tracker.active_and_recent() == []
    finally:
        await close_env(env)


@pytest.mark.asyncio
async def test_studio_capacity_rejection_maps_to_429(tmp_path):
    gate = asyncio.Event()
    client = OutputClient(tmp_path, gate=gate)
    env = await make_env(
        tmp_path,
        client,
        config=_config(webui_max_concurrent_jobs=1),
        studio_maximum=1,
    )
    try:
        accepted = await env.studio.generate({"prompt": "first"})
        await asyncio.wait_for(client.started.wait(), 2)
        with pytest.raises(StudioServiceError, match="并发上限") as caught:
            await env.studio.generate({"prompt": "second"})
        assert caught.value.status_code == 429
        gate.set()
        await env.v2.wait(accepted["job_id"])
        assert len(client.configs) == 1
    finally:
        gate.set()
        await close_env(env)


@pytest.mark.asyncio
async def test_none_v2_service_keeps_legacy_runtime_path(tmp_path):
    client = OutputClient(tmp_path)
    tracker = GenerationTracker(tmp_path, 20)
    studio = WebStudioService(
        client, tracker, _config(), tmp_path, v2_service_provider=lambda: None
    )
    try:
        accepted = await studio.generate({"prompt": "draw"})
        task = studio._runtime_tasks[accepted["job_id"]]
        await task
        assert tracker.get(accepted["job_id"])["status"] == "succeeded"
    finally:
        await studio.close()
        await tracker.close()


@pytest.mark.asyncio
async def test_v2_close_marks_studio_history_interrupted(tmp_path):
    gate = asyncio.Event()
    client = OutputClient(tmp_path, gate=gate)
    env = await make_env(tmp_path, client)
    try:
        accepted = await env.studio.generate({"prompt": "draw"})
        await asyncio.wait_for(client.started.wait(), 2)
        await env.v2.close()
        record = env.tracker.get(accepted["job_id"])
        assert record["status"] == "interrupted"
        assert record["status"] in TERMINAL_STATUSES
    finally:
        gate.set()
        await close_env(env)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (QueueFullError("队列已满"), 429),
        (ServiceClosedError("服务关闭"), 503),
        (CoreError("请求无效"), 400),
    ],
)
def test_v2_submit_error_mapping(error, status):
    mapped = WebStudioService._map_v2_error(error)
    assert isinstance(mapped, StudioServiceError)
    assert mapped.status_code == status
    assert mapped.message == error.message
