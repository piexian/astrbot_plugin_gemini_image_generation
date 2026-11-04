import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_web_studio_service import _png
from tl.api_types import APIError
from tl.background_tasks import BackgroundTaskManager
from tl.core.stack import build_generation_stack
from tl.generation_scheduler import GenerationScheduler, scheduled_generation
from tl.generation_tracker import GenerationTracker
from tl.image_generator import ImageGenerator
from tl.plugin_config import PluginConfig, ProviderCandidate
from tl.plugin_service import ImageGenerationService
from tl.provider_runtime import ProviderRuntime
from tl.rate_limiter import RateLimiter
from tl.v2_admission import LegacyFifoLimiter, RateLimiterQuota
from tl.v2_tracking import TrackerProjection


def make_service(tmp_path, *, history=True):
    cfg = PluginConfig(
        provider_candidates=[
            ProviderCandidate(
                id="xai#1",
                api_type="xai",
                settings={"model": "grok-imagine-image", "api_keys": ["test"]},
            )
        ]
    )
    scheduler = GenerationScheduler(1, 3)
    gate = asyncio.Event()
    gate.set()

    class Client:
        generation_scheduler = scheduler
        calls = 0
        entered = asyncio.Event()

        @scheduled_generation
        async def generate_image(self, config, **kwargs):
            self.entered.set()
            await gate.wait()
            self.calls += 1
            config.successful_provider = "xai"
            config.successful_model = "grok-imagine-image"
            return [f"https://example.org/{self.calls}.png"], [], None, None

    tracker = GenerationTracker(tmp_path, 30, enabled=history)
    client = Client()

    async def archive(urls, paths, **kwargs):
        return []

    plugin = SimpleNamespace(
        cfg=cfg,
        api_client=client,
        provider_runtime=ProviderRuntime(),
        generation_scheduler=scheduler,
        generation_tracker=tracker,
        background_task_manager=BackgroundTaskManager(tmp_path),
        rate_limiter=RateLimiter(cfg),
        image_generator=ImageGenerator(None, client, tracker=tracker),
        web_studio_service=SimpleNamespace(archive_images=archive),
    )
    plugin._v2_service = build_generation_stack(
        # 取客户端用回调：用例会在提交前替换 plugin.api_client.generate_image。
        client=lambda: plugin.api_client,
        candidates=[{"provider": "legacy", "model": "auto"}],
        # 与 v2 用例自建的栈分开：同一 SQLite 目录不挂两个 JobStore。
        data_dir=tmp_path / "v2-facade",
        # 与宿主装配一致：会话工作区内的本地参考图按已校验路径读取。
        allowed_local_roots=(tmp_path,),
        quota=RateLimiterQuota(lambda: plugin.rate_limiter),
        limiter=LegacyFifoLimiter(lambda: plugin.generation_scheduler),
        observers=[
            TrackerProjection(tracker, plugin.web_studio_service.archive_images)
        ],
    )
    plugin._ensure_v2_service = lambda: plugin._v2_service
    service = ImageGenerationService(plugin)
    service.initialized()
    return service, gate


async def close_service(service):
    """关闭 facade 与 v2 栈，再收口用例自建的组件。"""

    await service.close()
    await service.plugin._v2_service.close()
    await service.plugin.generation_scheduler.close()
    await service.plugin.background_task_manager.close()
    await service.plugin.generation_tracker.close()
    await service.plugin.rate_limiter.close()


@pytest.mark.asyncio
async def test_plugin_limit_buckets_persist_and_do_not_collide_with_umo():
    import copy

    saved = {}

    async def get_kv(key, default):
        return copy.deepcopy(saved.get(key, default))

    async def put_kv(key, value):
        saved[key] = copy.deepcopy(value)

    config = PluginConfig(
        default_rate_limit={"enabled": True, "period_seconds": 60, "max_requests": 1}
    )
    first = RateLimiter(config, get_kv=get_kv, put_kv=put_kv)
    assert (await first.acquire(None, plugin_id="GroupMessage:x")).allowed
    assert (await first.acquire("plugin:GroupMessage:x")).allowed
    await first.close()
    second = RateLimiter(config, get_kv=get_kv, put_kv=put_kv)
    assert not (await second.acquire(None, plugin_id="GroupMessage:x")).allowed
    assert (await second.acquire(None, plugin_id="other")).allowed
    await second.close()


@pytest.mark.asyncio
async def test_partial_generation_preserves_images_and_restart_does_not_replay_callback(
    tmp_path,
):
    service, _ = make_service(tmp_path)
    original = service.plugin.api_client.generate_image
    calls = 0

    async def generate(config, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise APIError("供应商拒绝后续请求", 500, "server")
        return await original(config, **kwargs)

    service.plugin.api_client.generate_image = generate
    accepted = await service.submit(plugin_id="a", prompt="draw", image_count=2)
    result = await service.wait_task(accepted["task_id"], plugin_id="a")
    assert result["status"] == "partial_success" and result["generated_images"] == 1
    await service.close()
    # Simulate a process exit between generation completion and callback delivery.
    await service.plugin.background_task_manager.update(
        accepted["task_id"], callback_status="pending"
    )
    reloaded = BackgroundTaskManager(tmp_path)
    restored = await reloaded.get_for_plugin(accepted["task_id"], "a")
    assert restored["status"] == "partial_success"
    assert restored["callback_status"] == "interrupted"
    await close_service(service)


@pytest.mark.asyncio
@pytest.mark.parametrize("use_file_uri", [False, True])
async def test_local_reference_reaches_client_as_resolved_cache_path(
    tmp_path, use_file_uri
):
    service, _ = make_service(tmp_path)
    path = _png(tmp_path / "reference.png")
    captured = []
    original = service.plugin.api_client.generate_image

    async def generate(config, **kwargs):
        captured.append(config)
        return await original(config, **kwargs)

    service.plugin.api_client.generate_image = generate
    try:
        accepted = await service.submit(
            plugin_id="a",
            prompt="edit",
            reference_images=[path.as_uri() if use_file_uri else str(path)],
        )
        result = await service.wait_task(accepted["task_id"], plugin_id="a")
        assert result["status"] == "succeeded"
        assert len(captured) == 1
        # 本地图经 v2 句柄落成缓存文件，内容与原图一致而非 base64 串。
        supplier_reference = Path(captured[0].reference_images[0])
        assert supplier_reference != path
        assert supplier_reference.read_bytes() == path.read_bytes()
    finally:
        await close_service(service)
