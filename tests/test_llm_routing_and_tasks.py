from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace

import pytest

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

from tl.background_tasks import BackgroundTaskManager  # noqa: E402
from tl.batch_generation import run_batch_job  # noqa: E402
from tl.llm_query_tools import BackgroundTaskStatusTool  # noqa: E402
from tl.llm_tools import _resolve_foreground_wait_seconds  # noqa: E402
from tl.plugin_config import get_session_tool_timeout  # noqa: E402


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"agent_runner": {"config": {"misc": {"tool_call_timeout": 300}}}}, 300),
        (
            {
                "agent_runner": {"config": {"misc": {"tool_call_timeout": 300}}},
                "provider_settings": {"tool_call_timeout": 60},
            },
            300,
        ),
        ({"provider_settings": {"tool_call_timeout": 240}}, 240),
        ({}, 120),
    ],
)
def test_session_tool_timeout_config_layouts(config, expected):
    context = SimpleNamespace(get_config=lambda: config)
    assert get_session_tool_timeout(context) == expected


def _make_timeout_context(get_config, conf_ids):
    class _ConfigMgr:
        def get_conf_info(self, umo):
            return {"id": conf_ids.get(umo, "default")}

    return SimpleNamespace(get_config=get_config, astrbot_config_mgr=_ConfigMgr())


def _session_isolated_event(group_id="10001", *, isolated=True):
    return SimpleNamespace(
        unified_msg_origin="qq:GroupMessage:8801_10001",
        get_extra=lambda key: True if isolated and key == "_session_isolated" else None,
        get_group_id=lambda: group_id,
    )


def test_session_tool_timeout_falls_back_to_group_umo_under_unique_session():
    rewritten = "qq:GroupMessage:8801_10001"
    group_umo = "qq:GroupMessage:10001"

    def get_config(umo=None):
        if umo == group_umo:
            return {"agent_runner": {"config": {"misc": {"tool_call_timeout": 300}}}}
        return {"provider_settings": {"tool_call_timeout": 120}}

    conf_ids = {rewritten: "default", group_umo: "group_conf"}
    context = _make_timeout_context(get_config, conf_ids)
    event = _session_isolated_event()

    assert get_session_tool_timeout(context, rewritten, event=event) == 300


def test_session_tool_timeout_no_fallback_when_umo_resolves_session_config():
    rewritten = "qq:GroupMessage:8801_10001"

    def get_config(umo=None):
        return {"agent_runner": {"config": {"misc": {"tool_call_timeout": 200}}}}

    conf_ids = {rewritten: "session_conf"}
    context = _make_timeout_context(get_config, conf_ids)
    event = _session_isolated_event()

    assert get_session_tool_timeout(context, rewritten, event=event) == 200


@pytest.mark.parametrize(
    ("group_id", "isolated"),
    [
        (None, True),
        ("10001", False),
        ("8801_10001", True),
    ],
)
def test_session_tool_timeout_no_fallback_cases(group_id, isolated):
    rewritten = "qq:GroupMessage:8801_10001"

    def get_config(umo=None):
        return {"provider_settings": {"tool_call_timeout": 120}}

    conf_ids = {"qq:GroupMessage:10001": "group_conf"}
    context = _make_timeout_context(get_config, conf_ids)
    event = _session_isolated_event(group_id=group_id, isolated=isolated)

    assert get_session_tool_timeout(context, rewritten, event=event) == 120


def test_session_tool_timeout_without_event_keeps_original_umo():
    rewritten = "qq:GroupMessage:8801_10001"

    def get_config(umo=None):
        return {"provider_settings": {"tool_call_timeout": 120}}

    conf_ids = {"qq:GroupMessage:10001": "group_conf"}
    context = _make_timeout_context(get_config, conf_ids)

    assert get_session_tool_timeout(context, rewritten) == 120


def test_foreground_wait_uses_session_runner_timeout():
    event = SimpleNamespace(unified_msg_origin="qq:GroupMessage:test")
    calls = []

    def get_config(umo=None):
        calls.append(umo)
        timeout = 300 if umo == event.unified_msg_origin else 120
        return {"agent_runner": {"config": {"misc": {"tool_call_timeout": timeout}}}}

    context = SimpleNamespace(get_config=get_config)
    plugin = SimpleNamespace(
        cfg=SimpleNamespace(llm_tool_timeout_reserve_percent=75),
        get_tool_timeout=lambda event: get_session_tool_timeout(
            context, event.unified_msg_origin
        ),
    )
    assert _resolve_foreground_wait_seconds(plugin, event) == 75
    assert calls == [event.unified_msg_origin]


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
    async def send(self, *args, **kwargs):
        return None


class _AvatarManager:
    async def fetch(self, *args, **kwargs):
        return None


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
