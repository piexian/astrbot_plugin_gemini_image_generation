import asyncio
from types import SimpleNamespace

import pytest

from tl.background_tasks import BackgroundTaskManager
from tl.generation_scheduler import GenerationScheduler, scheduled_generation
from tl.generation_tracker import GenerationTracker
from tl.image_generator import ImageGenerator
from tl.plugin_config import PluginConfig, ProviderCandidate
from tl.plugin_service import ImageGenerationService
from tl.provider_runtime import ProviderRuntime
from tl.rate_limiter import RateLimiter


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
    service = ImageGenerationService(plugin)
    service.initialized()
    return service, gate


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
    original = service.plugin.image_generator.generate_image_core
    calls = 0

    async def generate(**kwargs):
        nonlocal calls
        calls += 1
        return await original(**kwargs) if calls == 1 else (False, "供应商拒绝后续请求")

    service.plugin.image_generator.generate_image_core = generate
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
