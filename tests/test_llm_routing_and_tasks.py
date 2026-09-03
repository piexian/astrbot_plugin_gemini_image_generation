from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

if "mcp" not in sys.modules:
    mcp_module = types.ModuleType("mcp")
    mcp_types_module = types.ModuleType("mcp.types")
    mcp_module.types = mcp_types_module
    sys.modules["mcp"] = mcp_module
    sys.modules["mcp.types"] = mcp_types_module

for module_name in (
    "astrbot.core",
    "astrbot.core.agent",
    "astrbot.core.agent.run_context",
    "astrbot.core.agent.tool",
    "astrbot.core.astr_agent_context",
):
    sys.modules.setdefault(module_name, types.ModuleType(module_name))


class _FunctionTool:
    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, *args, **kwargs):
        return None


sys.modules["astrbot.core.agent.run_context"].ContextWrapper = type(
    "ContextWrapper", (), {}
)
sys.modules["astrbot.core.agent.tool"].FunctionTool = _FunctionTool
sys.modules["astrbot.core.agent.tool"].ToolExecResult = type("ToolExecResult", (), {})
sys.modules["astrbot.core.astr_agent_context"].AstrAgentContext = type(
    "AstrAgentContext", (), {}
)

from tl import batch_generation  # noqa: E402
from tl.background_tasks import BackgroundTaskManager  # noqa: E402
from tl.batch_generation import run_batch_job  # noqa: E402
from tl.llm_query_tools import (  # noqa: E402
    BackgroundTaskStatusTool,
    ProviderModelQueryTool,
)
from tl.llm_tools import (  # noqa: E402
    GeminiImageGenerationTool,
    _await_generation_task_and_send,
)
from tl.plugin_config import ProviderCandidate  # noqa: E402


def test_tool_schemas_are_valid_draft_2020_12() -> None:
    plugin = SimpleNamespace(
        cfg=SimpleNamespace(
            provider_candidates=[],
            batch_max_images_per_task=10,
        )
    )
    tools = (
        GeminiImageGenerationTool(plugin=plugin),
        ProviderModelQueryTool(plugin=plugin),
        BackgroundTaskStatusTool(plugin=plugin),
    )
    tools[0].refresh_from_plugin()

    for tool in tools:
        Draft202012Validator.check_schema(tool.parameters)


def _context(event):
    return SimpleNamespace(context=SimpleNamespace(event=event))


class _Event:
    unified_msg_origin = "platform:friend:user-1"

    def __init__(self) -> None:
        self.sent: list[object] = []

    def plain_result(self, text: str):
        return text

    async def send(self, value) -> None:
        self.sent.append(value)


class _MessageSender:
    def __init__(self) -> None:
        self.deliveries: list[dict] = []

    @staticmethod
    def merge_available_images(urls, paths):
        return list(dict.fromkeys(list(urls or []) + list(paths or [])))

    @staticmethod
    def prepare_text_content(text, images=None):
        return text or ""

    async def send_results_with_stream_retry(self, **kwargs) -> None:
        self.deliveries.append(kwargs)


class _AvatarManager:
    pass


@pytest.mark.asyncio
async def test_provider_query_defaults_to_modes_and_hides_detail() -> None:
    candidate = ProviderCandidate(
        id="xai#1",
        api_type="xai",
        settings={"model": "grok-imagine-image", "api_keys": ["key"]},
        model_alias="fast",
    )
    plugin = SimpleNamespace(
        cfg=SimpleNamespace(
            provider_candidates=[candidate],
            batch_max_images_per_task=12,
        )
    )
    tool = ProviderModelQueryTool(plugin=plugin)

    default_result = json.loads(await tool.call(_context(_Event())))
    detail_result = json.loads(await tool.call(_context(_Event()), detail=True))

    assert default_result["models"] == [
        {
            "provider": "xai",
            "model": "grok-imagine-image",
            "alias": "fast",
            "generation_modes": ["text_to_image", "image_to_image"],
        }
    ]
    assert "parameters" not in default_result["models"][0]
    assert detail_result["models"][0]["parameters"]["quality"]["enum"] == [
        "low",
        "medium",
    ]
    image_count = detail_result["models"][0]["parameters"]["image_count"]
    assert image_count["maximum"] == 12
    assert image_count["maximum_scope"] == "batch_task_item"
    assert image_count["native_request_maximum"] == 10
    assert image_count["native_request_maximum_scope"] == "provider_request"
    assert "批量任务" in image_count["description"]
    assert "单次上游请求" in image_count["description"]
    assert "api_keys" not in json.dumps(detail_result)


@pytest.mark.asyncio
async def test_task_status_tool_enforces_session_ownership(tmp_path) -> None:
    manager = BackgroundTaskManager(tmp_path)
    record = await manager.create(
        session_id="platform:friend:user-1",
        kind="single",
        routing_mode="full_polling",
        message="running",
    )
    tool = BackgroundTaskStatusTool(
        plugin=SimpleNamespace(background_task_manager=manager)
    )

    owned = json.loads(await tool.call(_context(_Event()), task_id=record["task_id"]))
    foreign_event = _Event()
    foreign_event.unified_msg_origin = "platform:friend:user-2"
    foreign = json.loads(
        await tool.call(_context(foreign_event), task_id=record["task_id"])
    )

    assert owned["task_id"] == record["task_id"]
    assert "session_id" not in owned
    assert foreign == {"error": "任务不存在或不属于当前会话"}


@pytest.mark.asyncio
async def test_single_tool_returns_task_id_only_after_backgrounding(tmp_path) -> None:
    manager = BackgroundTaskManager(tmp_path)
    event = _Event()

    class _Generator:
        @staticmethod
        def get_request_stats():
            return {
                "successful_provider": "xai",
                "successful_model": "grok-imagine-image",
                "successful_model_alias": "fast",
                "successful_candidate_id": "xai#1",
            }

    class _Plugin:
        api_client = object()
        image_generator = _Generator()
        message_sender = _MessageSender()
        avatar_manager = _AvatarManager()
        background_task_manager = manager
        cfg = SimpleNamespace(
            provider_candidates=[],
            llm_tool_timeout_reserve_percent=100,
            llm_tool_reference_path_mode="whitelist",
            llm_tool_reference_allowed_dirs=[],
            preserve_reference_image_size=False,
        )

        @staticmethod
        async def _check_and_consume_limit(_event):
            return True, None

        @staticmethod
        async def _fetch_images_from_event(_event, include_at_avatars=False):
            return [], []

        @staticmethod
        def get_tool_timeout(_event):
            return 1

        @staticmethod
        async def _generate_image_core_internal(**kwargs):
            await asyncio.sleep(0)
            return True, ([], ["generated.png"], None, None)

    plugin = _Plugin()
    tool = GeminiImageGenerationTool(plugin=plugin)

    response = json.loads(await tool.call(_context(event), prompt="draw"))
    running = list(manager._runtime_tasks.values())
    await asyncio.gather(*running)
    record = await manager.get(response["task_id"], event.unified_msg_origin)

    assert response["status"] == "running"
    assert response["routing_mode"] == "full_polling"
    assert record is not None
    assert record["status"] == "succeeded"
    assert record["items"][0]["provider"] == "xai"


@pytest.mark.asyncio
async def test_batch_job_refills_native_under_return(tmp_path) -> None:
    manager = BackgroundTaskManager(tmp_path)
    event = _Event()

    class _Generator:
        @staticmethod
        def get_request_stats():
            return {
                "successful_provider": "minimax",
                "successful_model": "image-01",
                "successful_model_alias": None,
                "successful_candidate_id": "minimax#1",
            }

    class _Plugin:
        cfg = SimpleNamespace(batch_concurrency=2)
        background_task_manager = manager
        message_sender = _MessageSender()
        avatar_manager = _AvatarManager()
        image_generator = _Generator()
        calls: list[int] = []
        serial = 0

        async def _generate_image_core_internal(self, **kwargs):
            requested = int(kwargs["image_count"])
            self.calls.append(requested)
            count = min(requested, 2)
            paths = []
            for _ in range(count):
                self.serial += 1
                paths.append(f"image-{self.serial}.png")
            return True, ([], paths, None, None)

    plugin = _Plugin()
    record = await manager.create(
        session_id=event.unified_msg_origin,
        kind="batch",
        routing_mode="provider_retry",
        message="running",
    )
    item = {
        "name": "set-a",
        "prompt": "draw",
        "image_count": 5,
        "provider": "minimax",
        "model": None,
    }

    await run_batch_job(plugin, event, record["task_id"], [item])
    result = await manager.get(record["task_id"], event.unified_msg_origin)

    assert plugin.calls == [5, 3, 1]
    assert result is not None
    assert result["status"] == "succeeded"
    assert result["items"][0]["generated_images"] == 5
    assert len(plugin.message_sender.deliveries) == 1


@pytest.mark.asyncio
async def test_batch_job_waits_for_siblings_and_attributes_item_exceptions(
    tmp_path,
) -> None:
    class _FailOnceManager(BackgroundTaskManager):
        progress_failure_raised = False

        async def update(self, task_id: str, **changes):
            if "completed_items" in changes and not self.progress_failure_raised:
                self.progress_failure_raised = True
                raise RuntimeError("progress update failed")
            return await super().update(task_id, **changes)

    manager = _FailOnceManager(tmp_path)
    event = _Event()
    slow_started = asyncio.Event()
    slow_finished = asyncio.Event()

    class _Generator:
        @staticmethod
        def get_request_stats():
            return {
                "successful_provider": "xai",
                "successful_model": "grok-imagine-image",
                "successful_model_alias": None,
                "successful_candidate_id": "xai#1",
            }

    class _Plugin:
        cfg = SimpleNamespace(batch_concurrency=2)
        background_task_manager = manager
        message_sender = _MessageSender()
        avatar_manager = _AvatarManager()
        image_generator = _Generator()
        partial_calls = 0

        async def _generate_image_core_internal(self, **kwargs):
            if kwargs["prompt"] == "partial":
                self.partial_calls += 1
                if self.partial_calls == 1:
                    return True, ([], ["partial.png"], None, None)
                await slow_started.wait()
                raise RuntimeError("provider exploded")

            slow_started.set()
            await asyncio.sleep(0.02)
            slow_finished.set()
            return True, ([], ["slow.png"], None, None)

    plugin = _Plugin()
    record = await manager.create(
        session_id=event.unified_msg_origin,
        kind="batch",
        routing_mode="mixed",
        message="running",
        total_items=2,
    )
    items = [
        {
            "name": "partial-item",
            "prompt": "partial",
            "image_count": 2,
            "provider": "xai",
            "model": None,
        },
        {
            "name": "slow-item",
            "prompt": "slow",
            "image_count": 1,
            "provider": "xai",
            "model": None,
        },
    ]

    await run_batch_job(plugin, event, record["task_id"], items)
    result = await manager.get(record["task_id"], event.unified_msg_origin)

    assert result is not None
    assert result["status"] == "partial_success"
    assert result["completed_items"] == 2
    assert result["succeeded_items"] == 1
    assert result["failed_items"] == 1
    by_name = {item["name"]: item for item in result["items"]}
    assert by_name["partial-item"]["generated_images"] == 1
    assert "provider exploded" in by_name["partial-item"]["error"]
    assert by_name["slow-item"]["success"] is True
    assert len(plugin.message_sender.deliveries) == 2
    assert manager.progress_failure_raised is True


@pytest.mark.asyncio
async def test_batch_job_cancellation_interrupts_children(tmp_path) -> None:
    manager = BackgroundTaskManager(tmp_path)
    event = _Event()
    generation_started = asyncio.Event()
    blocker = asyncio.Event()

    class _Plugin:
        cfg = SimpleNamespace(batch_concurrency=1)
        background_task_manager = manager
        message_sender = _MessageSender()
        avatar_manager = _AvatarManager()
        image_generator = SimpleNamespace(get_request_stats=lambda: {})

        @staticmethod
        async def _generate_image_core_internal(**kwargs):
            generation_started.set()
            await blocker.wait()

    plugin = _Plugin()
    record = await manager.create(
        session_id=event.unified_msg_origin,
        kind="batch",
        routing_mode="provider_retry",
        message="running",
    )
    item = {
        "name": "blocked-item",
        "prompt": "draw",
        "image_count": 1,
        "provider": "xai",
        "model": None,
    }
    task = asyncio.create_task(run_batch_job(plugin, event, record["task_id"], [item]))
    await generation_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    result = await manager.get(record["task_id"], event.unified_msg_origin)
    assert result is not None
    assert result["status"] == "interrupted"


@pytest.mark.asyncio
async def test_single_background_task_marks_delivery_failure_partial(tmp_path) -> None:
    manager = BackgroundTaskManager(tmp_path)
    event = _Event()

    class _FailingSender(_MessageSender):
        async def send_results_with_stream_retry(self, **kwargs) -> None:
            raise RuntimeError("send failed")

    plugin = SimpleNamespace(
        background_task_manager=manager,
        message_sender=_FailingSender(),
        avatar_manager=_AvatarManager(),
        api_client=None,
    )
    record = await manager.create(
        session_id=event.unified_msg_origin,
        kind="single",
        routing_mode="full_polling",
        message="running",
    )

    async def completed_generation():
        return (
            True,
            ([], ["generated.png"], None, None),
            {
                "successful_provider": "xai",
                "successful_model": "grok-imagine-image",
            },
        )

    await _await_generation_task_and_send(
        plugin,
        event,
        asyncio.create_task(completed_generation()),
        scene="test",
        task_id=record["task_id"],
    )
    result = await manager.get(record["task_id"], event.unified_msg_origin)

    assert result is not None
    assert result["status"] == "partial_success"
    assert result["items"][0]["delivery_success"] is False


def _notify_plugin(
    manager, event, *, llm_text="图片生成失败，请稍后重试", llm_error=None
):
    """构造带可 mock 聊天模型 context 的插件替身，返回 (plugin, llm_prompts)。"""
    llm_prompts: list[str] = []

    class _Context:
        async def get_current_chat_provider_id(self, umo: str) -> str:
            return "chat-provider"

        async def llm_generate(
            self, *, chat_provider_id, prompt, contexts=None, **kwargs
        ):
            if llm_error is not None:
                raise llm_error
            llm_prompts.append(prompt)
            return SimpleNamespace(completion_text=llm_text)

    plugin = SimpleNamespace(
        background_task_manager=manager,
        message_sender=_MessageSender(),
        avatar_manager=_AvatarManager(),
        api_client=None,
        context=_Context(),
        cfg=SimpleNamespace(background_failure_notify_llm=True),
    )
    return plugin, llm_prompts


@pytest.mark.asyncio
async def test_background_failure_feeds_back_to_llm(tmp_path) -> None:
    manager = BackgroundTaskManager(tmp_path)
    event = _Event()
    plugin, llm_prompts = _notify_plugin(manager, event)
    record = await manager.create(
        session_id=event.unified_msg_origin,
        kind="single",
        routing_mode="full_polling",
        message="running",
    )

    async def failed_generation():
        return False, "供应商 5xx", {}

    await _await_generation_task_and_send(
        plugin,
        event,
        asyncio.create_task(failed_generation()),
        scene="test",
        task_id=record["task_id"],
        notify_llm=True,
    )
    result = await manager.get(record["task_id"], event.unified_msg_origin)

    assert llm_prompts and "供应商 5xx" in llm_prompts[0]
    assert event.sent == ["图片生成失败，请稍后重试"]
    assert result is not None
    assert result["status"] == "failed"
    assert result["items"][0]["delivery_success"] is True


@pytest.mark.asyncio
async def test_background_exception_feeds_back_to_llm(tmp_path) -> None:
    manager = BackgroundTaskManager(tmp_path)
    event = _Event()
    plugin, llm_prompts = _notify_plugin(manager, event)
    record = await manager.create(
        session_id=event.unified_msg_origin,
        kind="single",
        routing_mode="full_polling",
        message="running",
    )

    async def raising_generation():
        raise RuntimeError("APIError boom")

    await _await_generation_task_and_send(
        plugin,
        event,
        asyncio.create_task(raising_generation()),
        scene="test",
        task_id=record["task_id"],
        notify_llm=True,
    )
    result = await manager.get(record["task_id"], event.unified_msg_origin)

    assert event.sent == ["图片生成失败，请稍后重试"]
    assert result is not None
    assert result["status"] == "failed"
    assert result["message"] == "后台生成异常: APIError boom"


@pytest.mark.asyncio
async def test_background_failure_notify_degrades_silently(tmp_path) -> None:
    manager = BackgroundTaskManager(tmp_path)
    event = _Event()
    plugin, _ = _notify_plugin(manager, event, llm_error=RuntimeError("provider down"))
    record = await manager.create(
        session_id=event.unified_msg_origin,
        kind="single",
        routing_mode="full_polling",
        message="running",
    )

    async def failed_generation():
        return False, "供应商 5xx", {}

    await _await_generation_task_and_send(
        plugin,
        event,
        asyncio.create_task(failed_generation()),
        scene="test",
        task_id=record["task_id"],
        notify_llm=True,
    )
    result = await manager.get(record["task_id"], event.unified_msg_origin)

    assert event.sent == []
    assert result is not None
    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_background_failure_default_keeps_direct_send(tmp_path) -> None:
    manager = BackgroundTaskManager(tmp_path)
    event = _Event()
    plugin, llm_prompts = _notify_plugin(manager, event)
    record = await manager.create(
        session_id=event.unified_msg_origin,
        kind="single",
        routing_mode="full_polling",
        message="running",
    )

    async def failed_generation():
        return False, "供应商 5xx", {}

    await _await_generation_task_and_send(
        plugin,
        event,
        asyncio.create_task(failed_generation()),
        scene="test",
        task_id=record["task_id"],
    )
    result = await manager.get(record["task_id"], event.unified_msg_origin)

    assert llm_prompts == []
    assert event.sent == ["供应商 5xx"]
    assert result is not None
    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_background_failure_switch_off_keeps_direct_send(tmp_path) -> None:
    manager = BackgroundTaskManager(tmp_path)
    event = _Event()
    plugin, llm_prompts = _notify_plugin(manager, event)
    plugin.cfg.background_failure_notify_llm = False
    record = await manager.create(
        session_id=event.unified_msg_origin,
        kind="single",
        routing_mode="full_polling",
        message="running",
    )

    async def failed_generation():
        return False, "供应商 5xx", {}

    await _await_generation_task_and_send(
        plugin,
        event,
        asyncio.create_task(failed_generation()),
        scene="test",
        task_id=record["task_id"],
        notify_llm=True,
    )

    assert llm_prompts == []
    assert event.sent == ["供应商 5xx"]


@pytest.mark.asyncio
async def test_batch_failure_summary_feeds_back_to_llm(tmp_path, monkeypatch) -> None:
    manager = BackgroundTaskManager(tmp_path)
    event = _Event()
    plugin, llm_prompts = _notify_plugin(manager, event)
    plugin.cfg = SimpleNamespace(
        batch_concurrency=2,
        background_failure_notify_llm=True,
    )
    plugin.image_generator = SimpleNamespace(get_request_stats=lambda: {})
    record = await manager.create(
        session_id=event.unified_msg_origin,
        kind="batch",
        routing_mode="provider_retry",
        message="running",
    )
    item = {"name": "set-a", "prompt": "draw", "image_count": 2}

    async def _fail(plugin=None, **kwargs):
        return False, "boom"

    monkeypatch.setattr(batch_generation, "invoke_generation_core", _fail)

    await run_batch_job(plugin, event, record["task_id"], [item])
    result = await manager.get(record["task_id"], event.unified_msg_origin)

    assert llm_prompts and "boom" in llm_prompts[0]
    assert event.sent == ["图片生成失败，请稍后重试"]
    assert plugin.message_sender.deliveries == []
    assert result is not None
    assert result["status"] == "failed"
