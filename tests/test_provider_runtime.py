from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tl.background_tasks import BackgroundTaskManager
from tl.generation_tracker import GenerationTracker
from tl.plugin_config import PluginConfig
from tl.provider_runtime import ProviderRuntime, ProviderRuntimeBusy, provider_operation
from tl.tl_api import GeminiAPIClient
from tl.web_studio_service import StudioServiceError, WebStudioService


class Event:
    def plain_result(self, message: str) -> dict[str, str]:
        return {"text": message}


def load_llm_tools(monkeypatch):
    """Extend conftest's logger stub only at the SDK boundary, not tool code."""
    if "tl.llm_tools" in sys.modules:
        return sys.modules["tl.llm_tools"]

    if "mcp" not in sys.modules:
        mcp = types.ModuleType("mcp")
        mcp.types = types.ModuleType("mcp.types")
        monkeypatch.setitem(sys.modules, "mcp", mcp)
        monkeypatch.setitem(sys.modules, "mcp.types", mcp.types)

    class GenericHostType:
        @classmethod
        def __class_getitem__(cls, item):
            return cls

    for name in (
        "astrbot.core",
        "astrbot.core.agent",
        "astrbot.core.agent.run_context",
        "astrbot.core.agent.tool",
        "astrbot.core.astr_agent_context",
    ):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    for module, name in (
        ("astrbot.core.agent.run_context", "ContextWrapper"),
        ("astrbot.core.agent.tool", "FunctionTool"),
        ("astrbot.core.agent.tool", "ToolExecResult"),
        ("astrbot.core.astr_agent_context", "AstrAgentContext"),
    ):
        if not hasattr(sys.modules[module], name):
            monkeypatch.setattr(
                sys.modules[module], name, GenericHostType, raising=False
            )
    return importlib.import_module("tl.llm_tools")


def command_class():
    """Run main's real admission methods without AstrBot registration/lifecycle."""
    path = Path(__file__).resolve().parents[1] / "main.py"
    source = ast.parse(path.read_text(encoding="utf-8"))
    plugin = next(node for node in source.body if isinstance(node, ast.ClassDef))
    plugin.bases = []
    plugin.decorator_list = []
    plugin.body = [
        node
        for node in plugin.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_provider_jobs_busy", "generate_image"}
    ]
    for method in plugin.body:
        method.decorator_list = [
            decorator
            for decorator in method.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "provider_operation"
        ]
    source.body = [
        ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0
        ),
        plugin,
    ]
    namespace = {
        "provider_operation": provider_operation,
        "AstrMessageEvent": Event,
        "Any": Any,
    }
    exec(compile(ast.fix_missing_locations(source), str(path), "exec"), namespace)
    return namespace[plugin.name]


def test_operation_and_update_are_synchronous_mutually_exclusive_leases():
    runtime = ProviderRuntime()
    assert not runtime.busy
    with runtime.operation():
        assert runtime.active == 1
        with runtime.operation():
            assert runtime.active == 2
            with pytest.raises(ProviderRuntimeBusy, match="生成任务") as error:
                with runtime.update():
                    pytest.fail("update entered while operations are active")
            assert error.value.reason == "busy"
        assert runtime.active == 1
    assert runtime.active == 0
    with runtime.update():
        assert runtime.updating
        with pytest.raises(ProviderRuntimeBusy) as error:
            with runtime.operation():
                pytest.fail("operation entered during update")
        assert error.value.reason == "updating"
        with pytest.raises(ProviderRuntimeBusy):
            with runtime.update():
                pytest.fail("nested update entered")
    assert not runtime.updating
    assert not runtime.busy


@pytest.mark.parametrize("lease", ["operation", "update"])
def test_exception_releases_lease_and_terminal_states_reject_admission(lease):
    runtime = ProviderRuntime()
    with pytest.raises(ValueError, match="test failure"):
        with getattr(runtime, lease)():
            raise ValueError("test failure")
    assert runtime.active == 0 and not runtime.updating
    for state in ("failed", "closed"):
        setattr(runtime, state, True)
        with pytest.raises(ProviderRuntimeBusy) as error:
            with getattr(runtime, lease)():
                pytest.fail("terminal state admitted work")
        assert error.value.reason == state
        setattr(runtime, state, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_mode", ["aclose", "cancel", "exhaust"])
async def test_async_generator_retains_lease_and_closes_underlying_generator(exit_mode):
    runtime = ProviderRuntime()
    entered = asyncio.Event()
    release = asyncio.Event()
    closed = []

    class Owner:
        provider_runtime = runtime

        @provider_operation("command")
        async def generate(self, event: Event, count: int = 2):
            try:
                yield event.plain_result(str(count))
                entered.set()
                await release.wait()
                yield event.plain_result("last")
            finally:
                await asyncio.sleep(0)
                closed.append(runtime.active)

    stream = Owner().generate(Event())
    assert runtime.active == 0
    assert await anext(stream) == {"text": "2"}
    assert runtime.active == 1
    with pytest.raises(ProviderRuntimeBusy):
        with runtime.update():
            pytest.fail("suspended generator lost its lease")
    if exit_mode == "aclose":
        await stream.aclose()
    elif exit_mode == "cancel":
        pending = asyncio.create_task(anext(stream))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await stream.aclose()
    else:
        release.set()
        assert [result async for result in stream] == [{"text": "last"}]
    assert closed == [1]
    assert runtime.active == 0
    with runtime.update():
        pass


@pytest.mark.asyncio
async def test_coroutine_cancellation_releases_lease():
    runtime = ProviderRuntime()
    entered = asyncio.Event()

    @provider_operation("api")
    async def wait(owner):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(wait(SimpleNamespace(provider_runtime=runtime)))
    await entered.wait()
    assert runtime.active == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runtime.busy


def test_wrappers_preserve_evaluated_annotations_and_host_command_signature():
    @provider_operation("api")
    async def typed(owner, event: Event, *, count: int = 3) -> dict[str, str]:
        return event.plain_result(str(count))

    signature = inspect.signature(typed, eval_str=True)
    assert signature == inspect.signature(typed.__wrapped__, eval_str=True)
    assert signature.parameters["event"].annotation is Event
    assert signature.parameters["count"].default == 3
    assert signature.return_annotation == dict[str, str]
    command = command_class().generate_image
    assert inspect.isasyncgenfunction(command)
    signature = inspect.signature(command, eval_str=True)
    assert list(signature.parameters) == ["self", "event", "prompt"]
    assert signature.parameters["event"].annotation is Event
    assert signature.parameters["prompt"].annotation is str


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["updating", "closed", "failed"])
async def test_real_entrypoints_return_their_busy_contract(
    reason, monkeypatch, tmp_path
):
    llm = load_llm_tools(monkeypatch)
    runtime = ProviderRuntime()
    setattr(runtime, reason, True)
    message = ProviderRuntimeBusy(reason).message
    plugin = command_class()()
    plugin.provider_runtime = runtime
    tool = llm.GeminiImageGenerationTool(plugin=plugin)
    client = GeminiAPIClient(["fake-runtime-key"])
    client.provider_runtime = runtime
    monkeypatch.setattr(
        client,
        "_get_session",
        AsyncMock(side_effect=AssertionError("network forbidden in admission tests")),
    )
    studio = WebStudioService(client, None, PluginConfig(), tmp_path)
    studio.provider_runtime = runtime
    # Inputs deliberately cannot reach provider code; admission precedes validation.
    with pytest.raises(StudioServiceError) as error:
        await studio.generate({})
    assert error.value.status_code == 503
    assert error.value.data == {"reason": reason}
    assert error.value.message == message
    assert await tool.call(None) == message
    assert await llm.execute_image_generation_tool(plugin, Event(), "test") == [message]
    with pytest.raises(ProviderRuntimeBusy) as error:
        await client.generate_image("test")
    assert error.value.reason == reason
    assert [item async for item in plugin.generate_image(Event(), "test")] == [
        {"text": message}
    ]
    assert runtime.active == 0
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["studio", "background"])
async def test_main_busy_source_blocks_update_before_background_coroutine_starts(
    source, tmp_path
):
    plugin = command_class()()
    config = PluginConfig()
    tracker = GenerationTracker(tmp_path, max_records=20)
    studio = WebStudioService(None, tracker, config, tmp_path)
    manager = BackgroundTaskManager(tmp_path)
    plugin.web_studio_service = studio
    plugin.background_task_manager = manager
    plugin.generation_tracker = tracker
    runtime = ProviderRuntime(plugin._provider_jobs_busy)
    started = False

    async def pending():
        nonlocal started
        started = True
        await asyncio.Event().wait()

    if source == "studio":
        studio._attach("test-job", pending(), [])
        task = studio._runtime_tasks["test-job"]
    else:
        task = manager.attach("test-job", pending())
    try:
        assert not started
        assert runtime.active == 0
        assert runtime.busy
        with pytest.raises(ProviderRuntimeBusy) as error:
            with runtime.update():
                pytest.fail("unstarted task was not counted")
        assert error.value.reason == "busy"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await studio.close()
        await manager.close()
        await tracker.close()
    assert not started
    assert not runtime.busy
    with runtime.update():
        pass


@pytest.mark.asyncio
async def test_main_tracker_running_record_blocks_update_without_runtime_tasks(
    tmp_path,
):
    plugin = command_class()()
    tracker = GenerationTracker(tmp_path, max_records=20, enabled=False)
    plugin.generation_tracker = tracker
    plugin.web_studio_service = WebStudioService(
        None, tracker, PluginConfig(), tmp_path
    )
    plugin.background_task_manager = BackgroundTaskManager(tmp_path)
    runtime = ProviderRuntime(plugin._provider_jobs_busy)
    record = await tracker.begin(
        source="llm_tool", prompt="test", params={}, requester={}
    )
    try:
        assert runtime.active == 0
        assert not plugin.web_studio_service._runtime_tasks
        assert not plugin.background_task_manager._runtime_tasks
        with pytest.raises(ProviderRuntimeBusy):
            with runtime.update():
                pytest.fail("running tracker record was ignored")
        await tracker.fail(record["job_id"], error="fake test failure")
        with runtime.update():
            pass
    finally:
        await plugin.web_studio_service.close()
        await plugin.background_task_manager.close()
        await tracker.close()
