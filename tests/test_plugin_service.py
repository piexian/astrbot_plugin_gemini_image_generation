import asyncio
from types import SimpleNamespace

import pytest

from tl.background_tasks import BackgroundTaskManager
from tl.generation_scheduler import GenerationScheduler, scheduled_generation
from tl.generation_tracker import GenerationTracker
from tl.image_generator import ImageGenerator
from tl.plugin_config import PluginConfig, ProviderCandidate
from tl.plugin_service import ImageGenerationService, PluginServiceError
from tl.provider_runtime import ProviderRuntime
from tl.rate_limiter import RateLimiter


@pytest.mark.asyncio
async def test_wait_uses_configured_timeout_and_explicit_override(tmp_path):
    service, gate = make_service(tmp_path)
    gate.clear()
    service.plugin.cfg.total_timeout = 0.01
    accepted = await service.submit(plugin_id="a", prompt="draw")
    task_id = accepted["task_id"]
    with pytest.raises(TimeoutError):
        await service.wait_task(task_id, plugin_id="a")
    assert (await service.get_task(task_id, plugin_id="a"))["status"] == "running"
    waiting = asyncio.create_task(service.wait_task(task_id, plugin_id="a", timeout=2))
    await asyncio.sleep(0.03)
    assert not waiting.done()
    gate.set()
    assert (await waiting)["status"] == "succeeded"
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["history", "task", "both"])
async def test_finish_storage_failure_releases_waiters_and_runs_callback(
    tmp_path, failure
):
    service, gate = make_service(tmp_path)
    gate.clear()
    callbacks = []

    async def callback(result):
        callbacks.append(result)

    accepted = await service.submit(plugin_id="a", prompt="draw", on_complete=callback)
    task_id = accepted["task_id"]
    task = service._tasks[task_id]
    await service.plugin.api_client.entered.wait()
    waiting = asyncio.create_task(service.wait_task(task_id, plugin_id="a", timeout=2))
    await asyncio.sleep(0)

    async def broken_save():
        raise OSError("disk full")

    manager = service.plugin.background_task_manager
    tracker = service.plugin.generation_tracker
    if failure in {"task", "both"}:
        manager._save = broken_save
    if failure in {"history", "both"}:
        tracker._save = broken_save
    gate.set()
    result = await waiting
    await task
    assert result["status"] == "succeeded"
    assert result["image_urls"] == ["https://example.org/1.png"]
    assert len(callbacks) == 1
    assert callbacks[0]["image_urls"] == result["image_urls"]
    assert (await service.get_task(task_id, plugin_id="a"))[
        "callback_status"
    ] == "succeeded"
    assert task_id not in service._events
    await service.close()


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
async def test_ready_config_recovery_queue_full_and_old_instance(tmp_path):
    service, _ = make_service(tmp_path)
    assert service.get_status()["ready"]
    capability = service.capabilities()["candidates"][0]
    assert capability["provider"] == "xai"
    assert capability["model"] == "grok-imagine-image"
    assert "api_keys" not in capability and "settings" not in capability
    service.plugin.provider_runtime.updating = True
    assert service.get_status()["reason"] == "configuration_updating"
    with pytest.raises(TimeoutError):
        await service.wait_ready(timeout=0.01)
    service.plugin.provider_runtime.updating = False
    service.plugin.api_client = None
    assert service.get_status()["reason"] == "not_configured"
    service.plugin.api_client = service.plugin.image_generator.api_client
    assert (await service.wait_ready(timeout=1))["instance_id"] == service.instance_id
    tickets = [service.plugin.generation_scheduler.reserve() for _ in range(4)]
    assert service.get_status()["ready"] and not service.get_status()["accepting_tasks"]
    with pytest.raises(PluginServiceError) as exc:
        await service.submit(plugin_id="a", prompt="draw")
    assert exc.value.code == "queue_full"
    for ticket in tickets:
        ticket.release()
    await service.close()
    assert service.get_status()["state"] == "closed"
    with pytest.raises(PluginServiceError):
        await service.wait_ready()


@pytest.mark.asyncio
async def test_identity_callback_multi_image_and_history(tmp_path):
    service, _ = make_service(tmp_path)
    callbacks = []

    async def callback(result):
        callbacks.append(result)
        raise RuntimeError("consumer failed")

    accepted = await service.submit(
        plugin_id="story",
        plugin_name="故事插件",
        prompt="draw",
        image_count=2,
        on_complete=callback,
    )
    task_id = accepted["task_id"]
    task = service._tasks[task_id]
    result = await service.wait_task(task_id, plugin_id="story", timeout=2)
    await task
    assert result["status"] == "succeeded"
    assert result["generated_images"] == 2
    assert callbacks[0]["image_urls"] == result["image_urls"]
    result = await service.get_task(task_id, plugin_id="story")
    assert result["callback_status"] == "failed"
    with pytest.raises(PermissionError):
        await service.get_task(task_id, plugin_id="other")
    with pytest.raises(PermissionError):
        await service.plugin.background_task_manager.get(task_id, "")
    records = service.plugin.generation_tracker.records_snapshot()
    assert len(records) == 1
    assert records[0]["caller"] == {"plugin_id": "story", "plugin_name": "故事插件"}
    assert records[0]["source"] == "plugin"
    assert records[0]["callback_status"] == "failed"
    await service.close()


@pytest.mark.asyncio
async def test_wait_timeout_immediate_cancel_and_shutdown(tmp_path):
    service, gate = make_service(tmp_path)
    gate.clear()
    accepted = await service.submit(plugin_id="a", prompt="draw")
    task_id = accepted["task_id"]
    with pytest.raises(TimeoutError):
        await service.wait_task(task_id, plugin_id="a", timeout=0.01)
    assert (await service.get_task(task_id, plugin_id="a"))["status"] == "running"
    queued = await service.submit(plugin_id="b", prompt="draw")
    result = await service.cancel_task(queued["task_id"], plugin_id="b")
    assert result["status"] == "interrupted"
    await service.close()
    with pytest.raises(PluginServiceError):
        await service.get_task(task_id, plugin_id="a")
    recovered = BackgroundTaskManager(tmp_path)
    assert (await recovered.get_for_plugin(task_id, "a"))["status"] == "interrupted"
    assert not service.plugin.generation_scheduler.busy


@pytest.mark.asyncio
async def test_default_limits_without_global_and_optional_umo(tmp_path):
    service, _ = make_service(tmp_path, history=False)
    service.plugin.cfg.default_rate_limit = {
        "enabled": True,
        "period_seconds": 60,
        "max_requests": 1,
    }
    for name in ["a", "b"]:
        accepted = await service.submit(
            plugin_id=name, prompt="draw", use_rate_limit=True
        )
        await service.wait_task(accepted["task_id"], plugin_id=name)
    with pytest.raises(PluginServiceError) as exc:
        await service.submit(
            plugin_id="a", plugin_name="renamed", prompt="draw", use_rate_limit=True
        )
    assert exc.value.code == "rate_limited"
    accepted = await service.submit(plugin_id="a", prompt="draw")
    await service.wait_task(accepted["task_id"], plugin_id="a")
    umo = "qq:GroupMessage:123"
    accepted = await service.submit(
        plugin_id="a", prompt="draw", umo=umo, use_rate_limit=True
    )
    await service.wait_task(accepted["task_id"], plugin_id="a")
    with pytest.raises(PluginServiceError):
        await service.submit(plugin_id="b", prompt="draw", umo=umo, use_rate_limit=True)
    with pytest.raises(PluginServiceError):
        await service.submit(plugin_id="b", prompt="draw", umo="invalid")
    assert service.plugin.generation_tracker.records_snapshot() == []
    await service.close()


@pytest.mark.asyncio
async def test_callback_removed_and_shutdown_during_submission(tmp_path):
    service, gate = make_service(tmp_path)
    gate.clear()
    called = []

    async def callback(result):
        called.append(result)

    accepted = await service.submit(plugin_id="a", prompt="draw", on_complete=callback)
    task_id = accepted["task_id"]
    assert await service.remove_callback(task_id, plugin_id="a")
    gate.set()
    assert (await service.wait_task(task_id, plugin_id="a"))[
        "callback_status"
    ] == "removed"
    assert not called

    entered = asyncio.Event()
    release = asyncio.Event()
    acquire = service.plugin.rate_limiter.acquire

    async def slow_acquire(*args, **kwargs):
        entered.set()
        await release.wait()
        return await acquire(*args, **kwargs)

    service.plugin.rate_limiter.acquire = slow_acquire
    submission = asyncio.create_task(
        service.submit(plugin_id="b", prompt="draw", use_rate_limit=True)
    )
    await entered.wait()
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(PluginServiceError) as exc:
        await submission
    assert exc.value.code == "service_closed"
    await closing
    assert not service.plugin.generation_scheduler.busy


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
async def test_concurrent_sources_history_filter_and_archived_result(tmp_path):
    service, _ = make_service(tmp_path)

    async def archive(urls, paths, **kwargs):
        gallery = service.plugin.generation_tracker.gallery_dir
        gallery.mkdir(exist_ok=True)
        name = kwargs["job_id"] + ".png"
        (gallery / name).write_bytes(b"test artifact")
        return [name]

    service.plugin.web_studio_service.archive_images = archive
    submissions = await asyncio.gather(
        *(
            service.submit(
                plugin_id=name,
                plugin_name=f"名称{name}",
                prompt="draw",
                requester={"user_id": name},
                umo=f"qq:FriendMessage:{name}",
            )
            for name in ["a", "b"]
        )
    )
    for name, submission in zip(["a", "b"], submissions):
        result = await service.wait_task(submission["task_id"], plugin_id=name)
        assert not result["image_urls"]
        assert result["image_paths"] == [
            str(
                service.plugin.generation_tracker.gallery_dir
                / (result["job_id"] + ".png")
            )
        ]
        matches = service.plugin.generation_tracker.query_history(
            page=1,
            size=20,
            keyword=f"名称{name}",
            source="plugin",
            group_id="",
            user_id="",
            plugin_id=name,
        )
        assert matches["total"] == 1
        assert matches["items"][0]["requester"]["user_id"] == name
        assert matches["items"][0]["requester"]["umo"] == f"qq:FriendMessage:{name}"
    await service.close()


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
