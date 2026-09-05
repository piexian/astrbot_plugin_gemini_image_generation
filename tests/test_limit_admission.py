from __future__ import annotations

import ast
import asyncio
import json
import shlex
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from astrbot.api import logger

# 复用现有宿主 stub；测试执行真实工具和命令方法，不加载 AstrBot 服务。
from tests.test_llm_routing_and_tasks import _context, _Event
from tests.test_web_studio_service import _config, _png, _SequenceClient
from tl import enhanced_prompts, llm_tools
from tl.api_types import ApiRequestConfig
from tl.background_tasks import BackgroundTaskManager
from tl.generation_tracker import GenerationTracker, ReferenceImageUnavailableError
from tl.llm_tools import GeminiImageGenerationTool
from tl.provider_capabilities import select_candidates
from tl.rate_limiter import RateLimiter
from tl.web_studio_service import StudioServiceError, WebStudioService


class _Limiter:
    def __init__(self, *, allowed=True):
        self.allowed = allowed
        self.calls = []
        self.refunds = []
        self.closed = False

    def allows_group(self, event):
        return True

    async def acquire(self, umo, *, cost=1):
        self.calls.append((umo, cost))
        return SimpleNamespace(
            allowed=self.allowed,
            message=None if self.allowed else "global exhausted",
            scope=None if self.allowed else "global",
            retry_after=0 if self.allowed else 17,
            token=f"token-{len(self.calls)}" if self.allowed else None,
        )

    async def check_and_consume(self, event, *, cost=1):
        self.calls.append((event.unified_msg_origin, cost))
        return self.allowed, None if self.allowed else "chat exhausted"

    async def refund(self, token):
        self.refunds.append(token)

    async def close(self):
        self.closed = True


@pytest_asyncio.fixture
async def studio(tmp_path):
    tracker = GenerationTracker(tmp_path, 30)
    service = WebStudioService(
        _SequenceClient([]), tracker, _config(), tmp_path, rate_limiter=_Limiter()
    )
    service.upload_dir.mkdir()
    try:
        yield service
    finally:
        await service.close()
        await tracker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["input", "api", "concurrency", "upload", "closed"])
async def test_studio_preflight_does_not_consume(studio, monkeypatch, failure):
    payload = {"prompt": "draw"}
    if failure == "input":
        payload["prompt"] = " "
    elif failure == "api":
        studio.api_client = None
    elif failure == "concurrency":
        studio._admitted_jobs = studio.config.webui_max_concurrent_jobs
    elif failure == "upload":
        path = _png(studio.upload_dir / "ref.png")
        payload["upload_names"] = [path.name]

        def disappeared(names):
            raise StudioServiceError("upload disappeared")

        monkeypatch.setattr(studio, "_acquire_uploads", disappeared)
    else:
        await studio.close()
    with pytest.raises(StudioServiceError):
        await studio.generate(payload)
    assert studio.rate_limiter.calls == []
    assert studio.rate_limiter.refunds == []
    assert studio._upload_ref_counts == {}
    assert not studio._runtime_tasks
    if failure != "concurrency":
        assert studio._admitted_jobs == 0


@pytest.mark.asyncio
async def test_studio_rejection_releases_upload_and_admission(studio, monkeypatch):
    studio.rate_limiter.allowed = False
    path = _png(studio.upload_dir / "ref.png")
    begin = AsyncMock()
    monkeypatch.setattr(studio.tracker, "begin", begin)
    with pytest.raises(StudioServiceError) as caught:
        await studio.generate({"prompt": "draw", "upload_names": [path.name]})
    assert caught.value.status_code == 429
    assert caught.value.message == "global exhausted"
    assert caught.value.data == {"scope": "global", "retry_after": 17}
    assert studio.rate_limiter.calls == [(None, 1)]
    assert studio.rate_limiter.refunds == []
    begin.assert_not_called()
    assert studio._upload_ref_counts == {}
    assert studio._admitted_jobs == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_studio_cost_counts_tasks_not_images_and_provider_failure_is_charged(
    studio, batch
):
    payload = (
        {
            "batch": [
                {"name": name, "prompt": name, "image_count": 3} for name in ("a", "b")
            ]
        }
        if batch
        else {"prompt": "draw", "image_count": 5}
    )
    # 空序列令供应商执行失败，而不是启动失败。
    result = await studio.generate(payload)
    await studio._runtime_tasks[result["job_id"]]
    assert studio.tracker.get(result["job_id"])["status"] == "failed"
    assert studio.rate_limiter.calls == [(None, 2 if batch else 1)]
    assert studio.rate_limiter.refunds == []
    assert studio._admitted_jobs == 0
    await studio.close()
    assert not studio.rate_limiter.closed  # 共享 limiter 由插件统一关闭。


@pytest.mark.asyncio
async def test_studio_fill_requests_do_not_consume_again(studio):
    paths = [_png(studio.data_dir / f"out-{i}.png", i) for i in range(3)]
    studio.api_client = _SequenceClient([([], [str(p)], None, None) for p in paths])
    result = await studio.generate({"prompt": "draw", "image_count": 3})
    await studio._runtime_tasks[result["job_id"]]
    assert studio.api_client.counts == [3, 2, 1]
    assert studio.rate_limiter.calls == [(None, 1)]
    assert studio.rate_limiter.refunds == []


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(
    "error_type", [RuntimeError, asyncio.CancelledError, ReferenceImageUnavailableError]
)
async def test_studio_start_failure_refunds_and_releases(
    studio, monkeypatch, batch, error_type
):
    path = _png(studio.upload_dir / "ref.png")
    begin = studio.tracker.begin
    calls = 0

    async def fail_begin(**kwargs):
        nonlocal calls
        calls += 1
        if calls == (3 if batch else 1):
            raise error_type("start failed")
        return await begin(**kwargs)

    monkeypatch.setattr(studio.tracker, "begin", fail_begin)
    payload = (
        {"batch": [{"name": name, "prompt": name} for name in ("a", "b")]}
        if batch
        else {"prompt": "draw"}
    )
    expected = (
        StudioServiceError
        if error_type is ReferenceImageUnavailableError
        else error_type
    )
    with pytest.raises(expected) as caught:
        await studio.generate({**payload, "upload_names": [path.name]})
    if expected is StudioServiceError:
        assert caught.value.status_code == 404
    assert studio.rate_limiter.calls == [(None, 2 if batch else 1)]
    assert studio.rate_limiter.refunds == ["token-1"]
    assert studio._upload_ref_counts == {}
    assert studio._admitted_jobs == 0
    assert not studio._runtime_tasks


@pytest.mark.asyncio
async def test_studio_task_creation_failure_refunds(studio, monkeypatch):
    def fail_create(coroutine):
        raise RuntimeError("task creation failed")

    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "create_task", fail_create)
        with pytest.raises(RuntimeError, match="task creation failed"):
            await studio.generate({"prompt": "draw"})
    assert studio.rate_limiter.refunds == ["token-1"]
    assert studio._admitted_jobs == 0
    assert not studio._runtime_tasks


@pytest.mark.asyncio
async def test_studio_post_attach_failure_does_not_refund_or_release_early(
    studio, monkeypatch
):
    path = _png(studio.upload_dir / "ref.png")
    start = studio._start_single

    async def start_then_fail(*args):
        await start(*args)
        raise RuntimeError("response failed after attach")

    monkeypatch.setattr(studio, "_start_single", start_then_fail)
    with pytest.raises(RuntimeError):
        await studio.generate({"prompt": "draw", "upload_names": [path.name]})
    assert studio.rate_limiter.refunds == []
    assert studio._admitted_jobs == 1
    assert studio._upload_ref_counts == {path.name: 1}
    await asyncio.gather(*studio._runtime_tasks.values())
    assert studio._admitted_jobs == 0
    assert studio._upload_ref_counts == {}


@pytest.mark.asyncio
async def test_studio_close_during_start_refunds_before_shutdown(studio, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    begin = studio.tracker.begin

    async def blocked_begin(**kwargs):
        entered.set()
        await release.wait()
        return await begin(**kwargs)

    monkeypatch.setattr(studio.tracker, "begin", blocked_begin)
    pending = asyncio.create_task(studio.generate({"prompt": "draw"}))
    await asyncio.wait_for(entered.wait(), 2)
    closing = asyncio.create_task(studio.close())
    await asyncio.sleep(0)
    assert studio.closed
    release.set()
    with pytest.raises(StudioServiceError) as caught:
        await pending
    await closing
    assert caught.value.status_code == 503
    assert studio.rate_limiter.refunds == ["token-1"]
    assert not studio._runtime_tasks
    assert studio._admitted_jobs == 0


def _tool_plugin(tmp_path):
    config = _config()
    config.batch_max_images_per_task = 10
    config.preserve_reference_image_size = False
    config.llm_tool_timeout_reserve_percent = 100
    manager = BackgroundTaskManager(tmp_path)
    limiter = _Limiter()
    plugin = SimpleNamespace(
        api_client=object(),
        cfg=config,
        rate_limiter=limiter,
        background_task_manager=manager,
        _check_and_consume_limit=limiter.check_and_consume,
        _fetch_images_from_event=AsyncMock(return_value=([], [])),
        get_tool_timeout=lambda event: 1,
        image_generator=SimpleNamespace(get_request_stats=lambda: {}),
    )
    return plugin


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["api", "empty", "route", "conflict", "batch", "empty_batch", "malformed_batch"],
)
async def test_llm_invalid_requests_do_not_consume(tmp_path, failure):
    plugin = _tool_plugin(tmp_path)
    kwargs = {"prompt": "draw"}
    if failure == "api":
        plugin.api_client = None
    elif failure == "empty":
        kwargs["prompt"] = " "
    elif failure == "route":
        kwargs["model"] = "unknown"
    elif failure == "conflict":
        plugin._fetch_images_from_event.return_value = (["ref.png"], [])
        kwargs.update(
            use_reference_images=True,
            preserve_reference_image_size=True,
            resolution="1K",
        )
    elif failure == "empty_batch":
        kwargs["batch_tasks"] = []
    elif failure == "malformed_batch":
        kwargs["batch_tasks"] = "bad"
    else:
        kwargs["batch_tasks"] = [{"name": "a", "prompt": "draw", "image_count": 2}]
    result = await GeminiImageGenerationTool(plugin=plugin).call(
        _context(_Event()), **kwargs
    )
    assert "❌" in result
    assert plugin.rate_limiter.calls == []
    await plugin.background_task_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
async def test_llm_batch_charges_prepared_task_count_once(
    tmp_path, monkeypatch, allowed
):
    plugin = _tool_plugin(tmp_path)
    plugin.rate_limiter.allowed = allowed
    received = []

    async def run_batch(plugin, event, task_id, items, **kwargs):
        received.extend(items)

    monkeypatch.setattr(llm_tools, "run_batch_job", run_batch)
    items = [
        {"name": name, "prompt": name, "image_count": count, "provider": "google"}
        for name, count in (("a", 3), ("b", 2))
    ]
    result = await GeminiImageGenerationTool(plugin=plugin).call(
        _context(_Event()), batch_tasks=items
    )
    if allowed:
        task_id = json.loads(result)["task_id"]
        await plugin.background_task_manager._runtime_tasks[task_id]
        assert len(received) == 2
    else:
        assert result == "chat exhausted"
        assert received == []
        assert not plugin.background_task_manager._runtime_tasks
    assert plugin.rate_limiter.calls == [(_Event.unified_msg_origin, 2)]
    await plugin.background_task_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["foreground", "background", "fallback", "forum"])
async def test_llm_single_charges_once_across_execution_modes(
    tmp_path, monkeypatch, mode
):
    plugin = _tool_plugin(tmp_path)
    calls = []
    release = asyncio.Event()

    # 保留旧单参数 fake，确保单任务不要求调用方升级签名。
    async def allow(event):
        calls.append(event.unified_msg_origin)
        return True, None

    async def generate(*args, **kwargs):
        if mode == "fallback":
            await release.wait()
        return False, "provider failed"

    async def dispatch(**kwargs):
        return False

    plugin._check_and_consume_limit = allow
    monkeypatch.setattr(llm_tools, "invoke_generation_core", generate)
    monkeypatch.setattr(llm_tools, "_dispatch_generation_result", dispatch)
    monkeypatch.setattr(
        llm_tools,
        "_resolve_foreground_wait_seconds",
        lambda *_: 0 if mode == "background" else 0.01,
    )
    result = await GeminiImageGenerationTool(plugin=plugin).call(
        _context(_Event()), prompt="draw", for_forum=mode == "forum"
    )
    if mode in {"background", "fallback"}:
        release.set()
        task_id = json.loads(result)["task_id"]
        await plugin.background_task_manager._runtime_tasks[task_id]
    else:
        assert "provider failed" in result
    assert calls == [_Event.unified_msg_origin]
    await plugin.background_task_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty", "api", None])
async def test_legacy_tool_checks_input_and_api_before_charging(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setitem(
        sys.modules, "astrbot.api.message_components", SimpleNamespace(Image=object)
    )
    plugin = _tool_plugin(tmp_path)
    plugin._generate_image_core_internal = AsyncMock(
        return_value=(False, "provider failed")
    )
    if failure == "api":
        plugin.api_client = None
    result = await llm_tools.execute_image_generation_tool(
        plugin, _Event(), "" if failure == "empty" else "draw"
    )
    assert result
    assert plugin.rate_limiter.calls == (
        [] if failure else [(_Event.unified_msg_origin, 1)]
    )
    assert plugin._generate_image_core_internal.await_count == (0 if failure else 1)
    await plugin.background_task_manager.close()


def _command_class():
    """执行 main.py 原方法体，仅去掉依赖宿主注册器的装饰器与类基类。"""
    source_path = Path(__file__).resolve().parents[1] / "main.py"
    source = ast.parse(source_path.read_text(encoding="utf-8"))
    plugin = next(node for node in source.body if isinstance(node, ast.ClassDef))
    methods = {
        "_check_command_generation_limit",
        "_check_and_consume_limit",
        "_quick_generate_image",
        "generate_image",
        "modify_image",
        "change_style",
        "_handle_quick_mode",
        "quick_avatar",
        "quick_sticker",
        "_parse_generation_route",
        "_extract_prompt_from_message",
        "_resolve_quick_mode_params",
        "_resolve_quick_mode_custom_size_overrides",
        "terminate",
    }
    plugin.bases = []
    plugin.body = [
        node
        for node in plugin.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in methods
    ]
    for method in plugin.body:
        method.decorator_list = [
            item
            for item in method.decorator_list
            if isinstance(item, ast.Name) and item.id == "staticmethod"
        ]
    source.body = [
        ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0
        ),
        plugin,
    ]
    namespace = {
        **vars(enhanced_prompts),
        "asyncio": asyncio,
        "shlex": shlex,
        "time": time,
        "logger": logger,
        "ApiRequestConfig": ApiRequestConfig,
        "select_candidates": select_candidates,
        "requester_from_event": lambda event: {},
        "format_error_message": str,
        "_build_no_ref_msg": lambda mode, suggestion: f"{mode}: {suggestion}",
    }
    exec(
        compile(ast.fix_missing_locations(source), str(source_path), "exec"), namespace
    )
    return namespace[plugin.name]


@pytest.fixture
def command_plugin():
    plugin = _command_class()()
    plugin.cfg = _config()
    plugin.cfg.preserve_reference_image_size = False
    plugin.cfg.enable_smart_retry = True
    plugin.cfg.enable_sticker_split = False
    plugin.cfg.sticker_grid_rows = plugin.cfg.sticker_grid_cols = 4
    plugin.cfg.quick_mode_overrides = {}
    plugin.rate_limiter = _Limiter()
    plugin.api_client = SimpleNamespace(
        generate_image=AsyncMock(side_effect=RuntimeError("provider failed"))
    )
    plugin._ensure_api_client = lambda: plugin.api_client is not None
    plugin.avatar_handler = SimpleNamespace(
        should_use_avatar=AsyncMock(return_value=False),
        should_use_avatar_for_prompt=AsyncMock(return_value=False),
    )
    plugin.image_handler = SimpleNamespace(
        fetch_images_from_event=AsyncMock(return_value=(["ref.png"], [])),
        filter_valid_reference_images=lambda values, **kwargs: list(values),
    )
    plugin.image_generator = SimpleNamespace(
        generate_image_core=AsyncMock(return_value=(False, "provider failed")),
        get_request_stats=lambda: {},
    )
    plugin.generation_tracker = SimpleNamespace(
        begin=AsyncMock(return_value={"job_id": "job"}), fail=AsyncMock()
    )

    async def duration(*args, **kwargs):
        if False:
            yield None

    plugin.message_sender = SimpleNamespace(send_api_duration=duration)
    return plugin


_COMMANDS = [
    ("generate_image", ("draw",), False),
    ("modify_image", ("change",), False),
    ("quick_avatar", ("draw",), False),
    ("quick_sticker", ("draw",), False),
    ("quick_sticker", ("draw",), True),
    ("change_style", ("ink",), False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,split", _COMMANDS)
@pytest.mark.parametrize("allowed", [False, True])
async def test_command_entries_consume_exactly_once(
    command_plugin, name, args, split, allowed
):
    plugin = command_plugin
    plugin.cfg.enable_sticker_split = split
    plugin.rate_limiter.allowed = allowed
    event = _Event()
    event.message_str = ""
    results = [result async for result in getattr(plugin, name)(event, *args)]
    assert plugin.rate_limiter.calls == [(event.unified_msg_origin, 1)]
    started = (
        plugin.api_client.generate_image.await_count
        + plugin.image_generator.generate_image_core.await_count
    )
    assert started == (1 if allowed else 0)
    assert any(
        ("provider failed" if allowed else "chat exhausted") in result
        for result in results
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,split", _COMMANDS)
async def test_command_api_unavailable_does_not_consume(
    command_plugin, name, args, split
):
    plugin = command_plugin
    plugin.api_client = None
    plugin.cfg.enable_sticker_split = split
    event = _Event()
    event.message_str = ""
    results = [result async for result in getattr(plugin, name)(event, *args)]
    assert any("API 客户端未初始化" in result for result in results)
    assert plugin.rate_limiter.calls == []
    plugin.image_generator.generate_image_core.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args,missing_refs",
    [
        ("generate_image", (" ",), False),
        ("modify_image", (" ",), False),
        ("change_style", (" ",), False),
        ("modify_image", ("change",), True),
        ("change_style", ("ink",), True),
        ("quick_sticker", ("draw",), True),
        ("generate_image", ("google/unknown draw",), False),
    ],
)
async def test_command_invalid_input_does_not_consume(
    command_plugin, name, args, missing_refs
):
    plugin = command_plugin
    if missing_refs:
        plugin.image_handler.fetch_images_from_event.return_value = ([], [])
    event = _Event()
    event.message_str = ""
    results = [result async for result in getattr(plugin, name)(event, *args)]
    assert results
    assert plugin.rate_limiter.calls == []
    plugin.api_client.generate_image.assert_not_called()
    plugin.image_generator.generate_image_core.assert_not_called()


@pytest.mark.asyncio
async def test_plugin_closes_limiter_after_request_producers(command_plugin):
    plugin = command_plugin
    closed = []

    def module(name):
        async def close():
            closed.append(name)

        return SimpleNamespace(close=close)

    plugin._web_routes = []
    plugin._web_api = None
    plugin.web_studio_service = module("studio")
    plugin.generation_tracker = module("tracker")
    plugin.background_task_manager = module("background")
    plugin.api_client = module("api")
    plugin.rate_limiter = module("limiter")
    await plugin.terminate()
    assert plugin._web_closed
    assert closed[-1] == "limiter"
    assert set(closed) == {"studio", "tracker", "background", "api", "limiter"}


@pytest.mark.asyncio
async def test_studio_llm_and_command_share_real_global_limiter(
    studio, command_plugin, tmp_path, monkeypatch
):
    config = _config()
    config.global_rate_limit = {
        "enabled": True,
        "period_seconds": 60,
        "max_requests": 3,
    }
    config.default_rate_limit = {
        "enabled": True,
        "period_seconds": 60,
        "max_requests": 1,
    }
    config.rate_limit_rules = []
    config.group_limit_list = set()
    config.group_limit_mode = "none"
    limiter = RateLimiter(config)
    studio.rate_limiter = command_plugin.rate_limiter = limiter
    plugin = _tool_plugin(tmp_path / "llm")
    plugin._check_and_consume_limit = limiter.check_and_consume
    monkeypatch.setattr(
        llm_tools,
        "invoke_generation_core",
        AsyncMock(return_value=(False, "provider failed")),
    )
    try:
        batch = await studio.generate(
            {"batch": [{"name": name, "prompt": name} for name in ("a", "b")]}
        )
        await studio._runtime_tasks[batch["job_id"]]
        # Studio 两条任务不占任一会话额度，LLM 的第一条任务仍可执行。
        llm_event = _Event()
        llm_event.unified_msg_origin = "platform:FriendMessage:user-1"
        result = await GeminiImageGenerationTool(plugin=plugin).call(
            _context(llm_event), prompt="draw", for_forum=True
        )
        assert "provider failed" in result
        with pytest.raises(StudioServiceError) as caught:
            await studio.generate({"prompt": "draw"})
        assert caught.value.status_code == 429
        assert caught.value.data["scope"] == "global"
        event = _Event()
        event.unified_msg_origin = "platform:FriendMessage:other-user"
        event.message_str = ""
        results = [
            result async for result in command_plugin.generate_image(event, "draw")
        ]
        assert any("全局" in result for result in results)
        command_plugin.api_client.generate_image.assert_not_called()
    finally:
        await plugin.background_task_manager.close()
        await limiter.close()


@pytest.mark.asyncio
async def test_compatibility_wrapper_forwards_batch_cost(command_plugin):
    event = _Event()
    assert await command_plugin._check_and_consume_limit(event, cost=4) == (True, None)
    assert command_plugin.rate_limiter.calls == [(event.unified_msg_origin, 4)]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,split", _COMMANDS)
async def test_denied_groups_remain_silent_before_preparation(
    command_plugin, name, args, split
):
    command_plugin.cfg.enable_sticker_split = split
    command_plugin.rate_limiter.allows_group = lambda event: False
    event = _Event()
    event.message_str = ""
    results = [result async for result in getattr(command_plugin, name)(event, *args)]
    assert results == []
    assert command_plugin.rate_limiter.calls == []
    command_plugin.image_handler.fetch_images_from_event.assert_not_awaited()
