"""Real AstrBot 4.29 send tool and context types, synthetic runner and filesystem.

Run with --noconftest in run_checks.py's temporary, network-blocked environment.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.append("/AstrBot/data/site-packages")

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.star.context import Context
from astrbot.core.tools.message_tools import SendMessageToUserTool
from astrbot.core.utils.active_event_registry import active_event_registry

from tl import background_notify
from tl.core.states import JobState


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_during", ["session", "llm", "event", "after_send"])
async def test_native_tool_pause_before_and_after_send(
    monkeypatch, tmp_path, pause_during
):
    # Construct native contexts with synthetic services; no Core lifecycle runs.
    core = object.__new__(Context)
    core.send_message = AsyncMock(return_value=True)
    core.conversation_manager = object()
    core.get_config = lambda umo: {"provider_settings": {}, "agent_runner": {}}
    source = CronMessageEvent(
        context=core,
        session=MessageSession.from_str("synthetic:FriendMessage:peer"),
        message="synthetic request",
    )
    services = SimpleNamespace(
        is_session_enabled=AsyncMock(return_value=True),
        is_llm_enabled_for_session=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(background_notify, "SessionServiceManager", services)
    monkeypatch.setattr(background_notify, "PlainInput", None)
    monkeypatch.setattr(background_notify, "_OFFICIAL_API_READY", True)
    monkeypatch.setattr(background_notify, "get_session_tool_timeout", lambda *args: 15)
    monkeypatch.setattr(
        background_notify,
        "_get_session_conv",
        AsyncMock(return_value=SimpleNamespace(history="[]")),
    )
    persist = AsyncMock()
    monkeypatch.setattr(background_notify, "persist_agent_history", persist)
    media_entered, media_release = asyncio.Event(), asyncio.Event()
    source_file = tmp_path / "synthetic.png"
    source_file.write_bytes(b"synthetic-media-content")
    shared_tool = SendMessageToUserTool()

    async def resolve(*args, **kwargs):
        media_entered.set()
        await media_release.wait()
        return str(source_file), False

    original_call = shared_tool.call
    shared_tool._resolve_path_from_sandbox = AsyncMock(side_effect=resolve)
    core.get_llm_tool_manager = lambda: SimpleNamespace(
        get_builtin_tool=lambda tool_type: shared_tool
    )
    captured = {}

    async def build(**kwargs):
        captured.update(kwargs)
        # Real Core re-adds the builtin for proactive platforms during build.
        kwargs["req"].func_tool.add_tool(shared_tool)

        class SyntheticRunner:
            async def step_until_done(self, _steps):
                native = AstrAgentContext(context=core, event=kwargs["event"])
                wrapped = ContextWrapper(context=native)
                tool = kwargs["req"].func_tool.get_tool("send_message_to_user")
                captured["tool_result"] = await tool.call(
                    wrapped,
                    messages=[{"type": "image", "path": str(source_file)}],
                )
                captured["wrapped"] = wrapped
                if pause_during == "after_send":
                    services.is_session_enabled.return_value = False
                yield None

        return SimpleNamespace(agent_runner=SyntheticRunner())

    monkeypatch.setattr(background_notify, "build_main_agent", build)
    manager = SimpleNamespace(record_delivery=AsyncMock())
    plugin = SimpleNamespace(context=core, background_task_manager=manager)
    callback = asyncio.create_task(
        background_notify.notify_llm_background_result(
            plugin,
            source,
            {"task_id": "synthetic-job", "image_paths": [str(source_file)]},
            notice="synthetic completed image",
            scene="native-guard-probe",
        )
    )
    await asyncio.wait_for(media_entered.wait(), timeout=5)
    if pause_during == "session":
        services.is_session_enabled.return_value = False
    elif pause_during == "llm":
        services.is_llm_enabled_for_session.return_value = False
    elif pause_during == "event":
        source.stop_event()
    media_release.set()
    if pause_during == "after_send":
        assert await callback is True
        assert captured["tool_result"].startswith("Message sent")
        assert captured["event"]._has_send_oper is True
        core.send_message.assert_awaited_once()
        manager.record_delivery.assert_awaited_once_with("synthetic-job")
        persist.assert_awaited_once()
    else:
        assert await callback is False
        assert captured["tool_result"].startswith("error:")
        assert captured["event"]._has_send_oper is False
        core.send_message.assert_not_awaited()
        manager.record_delivery.assert_not_awaited()
        persist.assert_not_awaited()
    assert captured["wrapped"].context.context is core
    assert shared_tool.call == original_call


@pytest.mark.asyncio
async def test_native_agent_stop_cancels_registered_generation(monkeypatch):
    core = object.__new__(Context)
    source = CronMessageEvent(
        context=core,
        session=MessageSession.from_str("synthetic:FriendMessage:native-stop"),
        message="synthetic request",
    )
    started = asyncio.Event()

    async def wait(_job_id):
        started.set()
        await asyncio.Event().wait()

    service = SimpleNamespace(
        wait=wait,
        get_status=AsyncMock(return_value=SimpleNamespace(state=JobState.RUNNING)),
        cancel=AsyncMock(),
    )
    monkeypatch.setattr(
        background_notify, "active_event_registry", active_event_registry
    )
    task = await background_notify.register_background_job_stop(
        source, service, "native-stop-job"
    )
    await asyncio.wait_for(started.wait(), timeout=5)

    assert active_event_registry.request_agent_stop_all(source.unified_msg_origin) == 1
    with pytest.raises(asyncio.CancelledError):
        await task

    service.cancel.assert_awaited_once_with("native-stop-job")
    assert background_notify._event_stopped(source)
    assert not source.is_stopped()
