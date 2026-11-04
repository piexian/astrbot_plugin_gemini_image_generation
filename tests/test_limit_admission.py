from __future__ import annotations

import ast
import asyncio
import json
import os
import shlex
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
from tl.core.errors import (
    CoreError,
    QueueFullError,
    QuotaExceededError,
    ServiceClosedError,
)
from tl.core.stack import build_generation_stack
from tl.core.states import JobState
from tl.format_error import format_error_message
from tl.generation_errors import api_failure_message
from tl.generation_tracker import (
    GenerationTracker,
    ReferenceImageUnavailableError,
)
from tl.image_generator import ImageGenerator
from tl.llm_tools import GeminiImageBatchTool, GeminiImageGenerationTool
from tl.prompt_templates import builtin_template, render_template
from tl.provider_capabilities import parse_model_selector, select_candidates
from tl.rate_limiter import RateLimiter
from tl.reference_image import REFERENCE_IMAGE_CACHE_DIR
from tl.v2_admission import (
    CompositeAdmissionLimiter,
    LegacyFifoLimiter,
    RateLimiterQuota,
    StudioAdmissionLimiter,
)
from tl.v2_bridge import (
    apply_result_to_config as v2_apply_result_to_config,
)
from tl.v2_bridge import (
    command_request as v2_command_request,
)
from tl.v2_bridge import (
    split_artifacts as v2_split_artifacts,
)
from tl.v2_bridge import (
    v2_failure_message,
    v2_failure_text,
)
from tl.v2_tracking import TrackerProjection
from tl.web_studio_service import StudioServiceError, WebStudioService

# 1x1 PNG：命令入口的参考图先经 ReferenceService 校验，必须是可解码的图片。
_PNG_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _Limiter:
    def __init__(self, *, allowed=True):
        self.allowed = allowed
        self.calls = []
        self.refunds = []
        self.closed = False

    def allows_group(self, event):
        return True

    async def acquire(self, umo, *, cost=1, event=None, plugin_id=None):
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
    env = await _install_v2(service, tracker)
    try:
        yield service
    finally:
        await env.v2.close()
        await service.close()
        await tracker.close()


async def _install_v2(service, tracker):
    """按宿主装配把共享 _Limiter 接入 v2 准入；空客户端让供应商执行失败。"""

    stack = build_generation_stack(
        lambda: service.api_client,
        [{"provider": "legacy", "model": "auto"}],
        service.data_dir / "v2",
        (),
        quota=RateLimiterQuota(lambda: service.rate_limiter),
        limiter=CompositeAdmissionLimiter(
            StudioAdmissionLimiter(
                lambda: int(getattr(service.config, "webui_max_concurrent_jobs", 2))
            ),
        ),
        observers=[TrackerProjection(tracker, service.archive_images)],
    )
    service._v2_service_provider = lambda: stack
    return SimpleNamespace(v2=stack, tracker=tracker)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["input", "api", "missing", "upload", "closed"])
async def test_studio_preflight_does_not_consume(studio, monkeypatch, failure):
    payload = {"prompt": "draw"}
    if failure == "input":
        payload["prompt"] = " "
    elif failure == "api":
        studio.api_client = None
    elif failure == "missing":
        studio._v2_service_provider = lambda: None
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
    await studio._v2_service_provider().wait(result["job_id"])
    assert studio.tracker.get(result["job_id"])["status"] == "failed"
    assert studio.rate_limiter.calls == [(None, 2 if batch else 1)]
    assert studio.rate_limiter.refunds == []
    await studio.close()
    assert not studio.rate_limiter.closed  # 共享 limiter 由插件统一关闭。


@pytest.mark.asyncio
async def test_studio_fill_requests_do_not_consume_again(studio):
    paths = [_png(studio.data_dir / f"out-{i}.png", i) for i in range(3)]
    studio.api_client = _SequenceClient([([], [str(p)], None, None) for p in paths])
    result = await studio.generate({"prompt": "draw", "image_count": 3})
    await studio._v2_service_provider().wait(result["job_id"])
    assert studio.api_client.counts == [3, 2, 1]
    assert studio.rate_limiter.calls == [(None, 1)]
    assert studio.rate_limiter.refunds == []


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("error_type", [RuntimeError, ReferenceImageUnavailableError])
async def test_studio_quota_rejection_never_creates_history(
    studio, monkeypatch, batch, error_type
):
    """准入拒绝发生在落库前：额度回滚、不建历史记录、不留上传租约。"""

    path = _png(studio.upload_dir / "ref.png")
    quota = studio.rate_limiter

    async def reject(umo, *, cost=1, event=None, plugin_id=None):
        quota.calls.append((umo, cost))
        raise error_type("start failed")

    monkeypatch.setattr(quota, "acquire", reject)
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
    with pytest.raises(expected):
        await studio.generate({**payload, "upload_names": [path.name]})
    assert quota.calls == [(None, 2 if batch else 1)]
    assert studio._upload_ref_counts == {}
    assert studio.tracker.active_and_recent() == []


@pytest.mark.asyncio
async def test_studio_forbidden_legacy_v2_is_rejected(studio):
    """灰度关闭时 Studio 不再回退旧运行时任务，直接返回 503。"""

    studio._v2_service_provider = lambda: None
    path = _png(studio.upload_dir / "ref.png")
    with pytest.raises(StudioServiceError) as caught:
        await studio.generate({"prompt": "draw", "upload_names": [path.name]})
    assert caught.value.status_code == 503
    assert studio.rate_limiter.calls == []
    assert studio._upload_ref_counts == {}


class _ToolClient:
    """LLM 工具用的假 legacy 客户端：默认成功返回一张图，可改为失败/停闸。"""

    def __init__(self, *, ok=True, gate=None):
        self.ok = ok
        self.gate = gate
        self.configs = []
        self.started = asyncio.Event()

    async def generate_image(self, config, **kwargs):
        self.configs.append(config)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if not self.ok:
            raise RuntimeError("provider failed")
        # 每轮返回不同 URL：补足张数按产出去重，重复 URL 会被合并成一张。
        serial = len(self.configs)
        return [f"https://img.test/out-{serial}.png"], [], "ok", None


async def _install_tool_v2(plugin, limiter):
    """按宿主装配把共享 limiter 接到工具用的 v2 栈，返回 (service, client)。"""

    client = _ToolClient()
    stack = build_generation_stack(
        client,
        [{"provider": "legacy", "model": "auto"}],
        plugin.v2_dir,
        (REFERENCE_IMAGE_CACHE_DIR,),
        quota=RateLimiterQuota(lambda: limiter),
    )
    plugin._v2_service = stack
    return stack, client


def _tool_plugin(tmp_path, *, allowed=True):
    config = _config()
    config.batch_max_images_per_task = 10
    config.preserve_reference_image_size = False
    config.llm_tool_timeout_reserve_percent = 100
    config.batch_concurrency = 2
    manager = BackgroundTaskManager(tmp_path)
    limiter = _Limiter(allowed=allowed)
    plugin = SimpleNamespace(
        api_client=object(),
        cfg=config,
        rate_limiter=limiter,
        background_task_manager=manager,
        _fetch_images_from_event=AsyncMock(return_value=([], [])),
        get_tool_timeout=lambda event: 1,
        v2_dir=tmp_path / "v2",
        message_sender=SimpleNamespace(
            merge_available_images=lambda urls, paths: [*(urls or []), *(paths or [])],
            prepare_text_content=lambda text, refs: text or "",
        ),
        generation_tracker=SimpleNamespace(enabled=False),
    )
    plugin.image_generator = ImageGenerator(
        context=None,
        api_client=plugin.api_client,
        filter_valid_fn=lambda images, source: images or [],
    )
    plugin._ensure_v2_service = lambda: plugin._v2_service
    return plugin


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["api", "empty", "route", "batch", "empty_batch", "malformed_batch"],
)
async def test_llm_invalid_requests_do_not_consume(tmp_path, failure):
    plugin = _tool_plugin(tmp_path)
    # 单图失败只走单图工具；批量失败只走独立批量工具。
    batch_failure = failure in {"batch", "empty_batch", "malformed_batch"}
    if failure == "api":
        plugin.api_client = None
        kwargs = {"prompt": "draw"}
    elif failure == "empty":
        kwargs = {"prompt": " "}
    elif failure == "route":
        kwargs = {"prompt": "draw", "model": "unknown"}
    elif failure == "empty_batch":
        kwargs = {"tasks": []}
    elif failure == "malformed_batch":
        kwargs = {"tasks": "bad"}
    else:
        kwargs = {"tasks": [{"prompt": "draw", "image_count": 2}]}
    tool_cls = GeminiImageBatchTool if batch_failure else GeminiImageGenerationTool
    result = await tool_cls(plugin=plugin).call(_context(_Event()), **kwargs)
    assert "❌" in result
    assert plugin.rate_limiter.calls == []
    await plugin.background_task_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
async def test_llm_batch_charges_prepared_task_count_once(tmp_path, allowed):
    plugin = _tool_plugin(tmp_path, allowed=allowed)
    service, client = await _install_tool_v2(plugin, plugin.rate_limiter)
    items = [
        {"name": name, "prompt": name, "image_count": count, "provider": "legacy"}
        for name, count in (("a", 3), ("b", 2))
    ]
    try:
        result = await GeminiImageBatchTool(plugin=plugin).call(
            _context(_Event()), tasks=items
        )
        if allowed:
            json.loads(result)
            await service.wait(_parent_job_id(await _jobs(service)), timeout=30)
            assert len(client.configs) == 5
        else:
            assert result == "global exhausted"
            assert client.configs == []
        assert plugin.rate_limiter.calls == [(_Event.unified_msg_origin, 2)]
    finally:
        await service.close()
        await plugin.background_task_manager.close()


async def _jobs(service):
    return await service._scheduler.store.list_jobs()


def _parent_job_id(jobs):
    return next(job.job_id for job in jobs if job.request.metadata.get("batch_parent"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["foreground", "background", "fallback", "forum"])
async def test_llm_single_charges_once_across_execution_modes(
    tmp_path, monkeypatch, mode
):
    plugin = _tool_plugin(tmp_path)
    service, client = await _install_tool_v2(plugin, plugin.rate_limiter)
    gate = asyncio.Event()
    client.gate = gate if mode == "fallback" else None
    foreground = {"background": 0, "fallback": 0.01}.get(mode, 30)
    monkeypatch.setattr(
        llm_tools, "_resolve_foreground_wait_seconds", lambda *_: foreground
    )

    async def call_tool_result(**kwargs):
        # 本模块没有真实 mcp 类型：前台结果改用可读文本断言。
        return "\n".join([*kwargs["image_urls"], *kwargs["image_paths"]])

    monkeypatch.setattr(llm_tools, "_build_call_tool_result", call_tool_result)
    try:
        result = await GeminiImageGenerationTool(plugin=plugin).call(
            _context(_Event()), prompt="draw", for_forum=mode == "forum"
        )
        if mode in {"background", "fallback"}:
            assert json.loads(result)["status"] == "running"
            gate.set()
            await _eventually(lambda: _terminal_record(plugin))
            # 生成成功但本模块没有官方回灌 API：投递不回执，记录按部分成功收尾。
            (record,) = plugin.background_task_manager._records.values()
            assert record["status"] == "partial_success"
            assert record["items"][0]["success"] is True
            assert record["items"][0]["delivery_success"] is False
        else:
            # 前台路径拿到结果后直接返回图片位置；不建后台记录。
            assert "out-1.png" in result
            assert plugin.background_task_manager._records == {}
        assert plugin.rate_limiter.calls == [(_Event.unified_msg_origin, 1)]
        assert len(client.configs) == 1
    finally:
        await service.close()
        await plugin.background_task_manager.close()


async def _terminal_record(plugin):
    records = list(plugin.background_task_manager._records.values())
    return bool(records) and records[0]["status"] in {
        "succeeded",
        "partial_success",
        "failed",
        "interrupted",
    }


async def _eventually(check, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if await check():
            return
        assert asyncio.get_running_loop().time() < deadline, "条件在限定时间内未成立"
        await asyncio.sleep(0.01)


def _command_class():
    """执行 main.py 原方法体，仅去掉依赖宿主注册器的装饰器与类基类。"""
    source_path = Path(__file__).resolve().parents[1] / "main.py"
    source = ast.parse(source_path.read_text(encoding="utf-8"))
    plugin = next(node for node in source.body if isinstance(node, ast.ClassDef))
    methods = {
        "_check_command_generation_limit",
        "_quick_generate_image",
        "_ensure_v2_service",
        "generate_image",
        "modify_image",
        "change_style",
        "_handle_quick_mode",
        "_quick_template",
        "_quick_dimensions",
        "quick_avatar",
        "quick_poster",
        "quick_wallpaper",
        "quick_card",
        "quick_mobile",
        "quick_figure",
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
        "render_template": render_template,
        "builtin_template": builtin_template,
        "asyncio": asyncio,
        "shlex": shlex,
        "time": time,
        "logger": logger,
        "ApiRequestConfig": ApiRequestConfig,
        "CoreError": CoreError,
        "QuotaExceededError": QuotaExceededError,
        "QueueFullError": QueueFullError,
        "ServiceClosedError": ServiceClosedError,
        "format_error_message": format_error_message,
        "select_candidates": select_candidates,
        "parse_model_selector": parse_model_selector,
        "requester_from_event": lambda event: {},
        "_build_no_ref_msg": lambda mode, suggestion: f"{mode}: {suggestion}",
        "JobState": JobState,
        "v2_failure_message": v2_failure_message,
        "v2_failure_text": v2_failure_text,
        "api_failure_message": api_failure_message,
        "v2_command_request": v2_command_request,
        "v2_split_artifacts": v2_split_artifacts,
        "v2_apply_result_to_config": v2_apply_result_to_config,
        # _ensure_v2_service 装配 v2 栈所需。
        "os": os,
        "build_generation_stack": build_generation_stack,
        "REFERENCE_IMAGE_CACHE_DIR": REFERENCE_IMAGE_CACHE_DIR,
        "RateLimiterQuota": RateLimiterQuota,
        "LegacyFifoLimiter": LegacyFifoLimiter,
        "CompositeAdmissionLimiter": CompositeAdmissionLimiter,
        "StudioAdmissionLimiter": StudioAdmissionLimiter,
        "TrackerProjection": TrackerProjection,
    }
    exec(
        compile(ast.fix_missing_locations(source), str(source_path), "exec"), namespace
    )
    return namespace[plugin.name]


@pytest_asyncio.fixture
async def command_plugin(tmp_path):
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
        fetch_images_from_event=AsyncMock(return_value=([_PNG_DATA_URI], [])),
        filter_valid_reference_images=lambda values, **kwargs: list(values),
    )
    plugin.image_generator = ImageGenerator(
        context=None,
        api_client=plugin.api_client,
        filter_valid_fn=lambda images, source: images or [],
    )
    plugin.generation_tracker = SimpleNamespace(
        begin=AsyncMock(return_value={"job_id": "job"}),
        complete=AsyncMock(),
        update=AsyncMock(),
        fail=AsyncMock(),
    )
    plugin.web_studio_service = SimpleNamespace(
        archive_images=AsyncMock(return_value=[])
    )
    service = build_generation_stack(
        lambda: plugin.api_client,
        [{"provider": "legacy", "model": "auto"}],
        tmp_path / "v2",
        (REFERENCE_IMAGE_CACHE_DIR,),
        quota=RateLimiterQuota(lambda: plugin.rate_limiter),
        observers=[
            TrackerProjection(
                plugin.generation_tracker, plugin.web_studio_service.archive_images
            )
        ],
    )
    plugin._v2_service = service
    plugin._ensure_v2_service = lambda: plugin._v2_service

    async def duration(*args, **kwargs):
        if False:
            yield None

    plugin.message_sender = SimpleNamespace(send_api_duration=duration)
    try:
        yield plugin
    finally:
        await service.close()


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
    # 额度恰好扣一次，且由 v2 submit 准入统一扣减。
    assert plugin.rate_limiter.calls == [(event.unified_msg_origin, 1)]
    assert plugin.api_client.generate_image.await_count == (1 if allowed else 0)
    assert any(
        ("所有候选 provider 均失败" if allowed else "global exhausted") in result
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
    plugin.api_client is None


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
    plugin.studio_providers = None
    plugin.provider_runtime = SimpleNamespace(closed=False)
    plugin.configuration_lock = asyncio.Lock()
    plugin._public_service = module("service")
    plugin.generation_scheduler = module("scheduler")
    plugin.web_studio_service = module("studio")
    plugin.generation_tracker = module("tracker")
    plugin.background_task_manager = module("background")
    plugin.api_client = module("api")
    plugin.rate_limiter = module("limiter")
    plugin._v2_service = module("v2")
    await plugin.terminate()
    assert plugin._web_closed
    assert closed[-1] == "limiter"
    assert set(closed) == {
        "service",
        "scheduler",
        "studio",
        "tracker",
        "background",
        "v2",
        "api",
        "limiter",
    }
    # v2 栈在后台任务之后、生成历史之前关闭（投影取消 Job 时 tracker 仍开放），
    # 并早于 legacy client。
    assert closed.index("background") < closed.index("v2")
    assert closed.index("v2") < closed.index("tracker")
    assert closed.index("v2") < closed.index("api")


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
    plugin.rate_limiter = limiter
    service, tool_client = await _install_tool_v2(plugin, limiter)
    tool_client.ok = False
    try:
        batch = await studio.generate(
            {"batch": [{"name": name, "prompt": name} for name in ("a", "b")]}
        )
        await studio._v2_service_provider().wait(batch["job_id"])
        # Studio 两条任务不占任一会话额度，LLM 的第一条任务仍可执行。
        llm_event = _Event()
        llm_event.unified_msg_origin = "platform:FriendMessage:user-1"
        result = await GeminiImageGenerationTool(plugin=plugin).call(
            _context(llm_event), prompt="draw", deliver="return"
        )
        # LLM 工具按会话额度扣 1，与 Studio 的全局额度互不干扰。
        assert "❌" in result
        assert tool_client.configs
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
        await service.close()
        await plugin.background_task_manager.close()
        await limiter.close()


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


def _quick_harness(tmp_path, plugin, result, *, archive_error=None, archived=None):
    """装配真实 v2 栈与生成历史：返回 (sent, tracker, service)。"""

    async def generate_image(config, **kwargs):
        config.successful_provider = "openai_images"
        config.successful_model = "gpt-image-2"
        config.successful_candidate_id = "cand-1"
        config.retry_count = 0
        return result

    async def archive_images(urls, paths, **kwargs):
        if archive_error is not None:
            raise archive_error
        return list(archived or [])

    tracker = GenerationTracker(tmp_path / "tracker", 30)
    plugin.api_client = SimpleNamespace(generate_image=generate_image)
    plugin.image_generator.api_client = plugin.api_client
    plugin.image_handler.fetch_images_from_event = AsyncMock(return_value=([], []))
    service = build_generation_stack(
        lambda: plugin.api_client,
        [{"provider": "legacy", "model": "auto"}],
        tmp_path / "v2",
        (REFERENCE_IMAGE_CACHE_DIR,),
        quota=RateLimiterQuota(lambda: plugin.rate_limiter),
        observers=[TrackerProjection(tracker, archive_images)],
    )
    plugin._v2_service = service
    plugin.generation_tracker = tracker

    sent = []

    async def send_results(**kwargs):
        sent.append(kwargs)

    async def duration(*args, **kwargs):
        if False:
            yield None

    plugin.message_sender = SimpleNamespace(
        send_results_with_stream_retry=send_results, send_api_duration=duration
    )
    return sent, tracker, service


async def _run_quick(plugin):
    event = _Event()
    event.message_str = ""
    return [
        message
        async for message in plugin._handle_quick_mode(
            event, "draw", "2K", "3:2", "手办化", mode_key="figure"
        )
    ]


@pytest.mark.asyncio
async def test_quick_mode_archive_timeout_records_partial_success(
    command_plugin, tmp_path
):
    """归档下载超时（空消息 TimeoutError）不能把已发送结果记为失败。"""
    plugin = command_plugin
    sent, tracker, service = _quick_harness(
        tmp_path,
        plugin,
        (["https://cdn.example/img.png"], [], None, None),
        archive_error=TimeoutError(),
    )
    try:
        outputs = await _run_quick(plugin)

        assert "🎨 使用手办化模式生成图像..." in outputs
        assert sent and sent[0]["image_urls"] == ["https://cdn.example/img.png"]
        (record,) = tracker.records_snapshot()
        assert record["status"] == "partial_success"
        assert record["images"] == []
        assert record["source_urls"] == ["https://cdn.example/img.png"]
        assert record["stats"]["provider"] == "openai_images"
        assert record["stats"]["model"] == "gpt-image-2"
        assert "画廊归档不完整（0/1）" in record["error"]
        assert "TimeoutError" in record["error"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_quick_mode_full_archive_records_succeeded(command_plugin, tmp_path):
    plugin = command_plugin
    sent, tracker, service = _quick_harness(
        tmp_path,
        plugin,
        (["https://cdn.example/img.png"], [], None, None),
        archived=["gallery-a.png"],
    )
    try:
        await _run_quick(plugin)

        assert sent
        (record,) = tracker.records_snapshot()
        assert record["status"] == "succeeded"
        assert record["images"] == ["gallery-a.png"]
        assert record["source_urls"] == ["https://cdn.example/img.png"]
        assert not record["error"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_quick_mode_text_only_result_is_partial_success(command_plugin, tmp_path):
    plugin = command_plugin
    _sent, tracker, service = _quick_harness(
        tmp_path, plugin, ([], [], "模型仅返回文本", None)
    )
    try:
        await _run_quick(plugin)

        (record,) = tracker.records_snapshot()
        assert record["status"] == "partial_success"
        assert record["images"] == []
        assert record["text_content"] == "模型仅返回文本"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_quick_mode_empty_result_fails_with_clear_error(command_plugin, tmp_path):
    plugin = command_plugin
    _sent, tracker, service = _quick_harness(tmp_path, plugin, ([], [], None, None))
    try:
        await _run_quick(plugin)

        (record,) = tracker.records_snapshot()
        assert record["status"] == "failed"
        assert record["error"] == "供应商未返回图片或文本内容"
    finally:
        await service.close()
