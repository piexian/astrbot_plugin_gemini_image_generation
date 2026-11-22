from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tl import background_notify


@pytest.mark.asyncio
async def test_notify_uses_astrbot_430_wakeup(monkeypatch, tmp_path):
    image_path = tmp_path / "result.png"
    image_path.write_bytes(b"image")
    wake = AsyncMock()
    bump = AsyncMock()
    monkeypatch.setattr(background_notify, "wake_main_agent", wake)
    monkeypatch.setattr(background_notify, "_OFFICIAL_API_READY", True)
    monkeypatch.setattr(background_notify, "_bump_delivery_count", bump)

    context = object()
    agent_session = object()
    event = SimpleNamespace(
        unified_msg_origin="test:FriendMessage:user",
        role="admin",
        agent_session=agent_session,
    )
    plugin = SimpleNamespace(context=context)
    task_result = {
        "task_id": "task-430",
        "image_paths": [str(image_path)],
        "image_urls": ["data:image/png;base64,aGVsbG8="],
        "text_content": "图片 data:image/png;base64,aGVsbG8= 已生成",
    }

    delivered = await background_notify.notify_llm_background_result(
        plugin,
        event,
        task_result,
        notice="后台图片生成任务已完成",
        scene="test-430",
    )

    assert delivered is True
    wake.assert_awaited_once()
    assert wake.await_args.args == (context,)
    kwargs = wake.await_args.kwargs
    assert kwargs["umo"] == event.unified_msg_origin
    assert kwargs["source"] == "background_task"
    assert kwargs["source_id"] == "task-430"
    assert kwargs["role"] == "admin"
    assert kwargs["agent_session"] is agent_session
    assert kwargs["source_event"] is event
    assert str(image_path) in kwargs["user_prompt"]
    assert "send_message_to_user" in kwargs["user_prompt"]
    assert "data:image" not in kwargs["user_prompt"]
    bump.assert_awaited_once_with(plugin, task_result, scene="test-430")


@pytest.mark.asyncio
async def test_notify_keeps_astrbot_429_fallback(monkeypatch):
    class LegacySession:
        @classmethod
        def from_str(cls, umo):
            return SimpleNamespace(message_type="friend")

    class LegacyCronEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self._has_send_oper = True

    class LegacyRequest:
        def __init__(self):
            self.conversation = None
            self.contexts = []
            self.system_prompt = ""
            self.prompt = ""
            self.func_tool = None

    class LegacyToolSet:
        def __init__(self):
            self.tools = []

        def add_tool(self, tool):
            self.tools.append(tool)

    class LegacyRunner:
        max_steps = None

        async def step_until_done(self, max_steps):
            self.max_steps = max_steps
            yield None

    runner = LegacyRunner()
    build = AsyncMock(return_value=SimpleNamespace(agent_runner=runner))
    get_conv = AsyncMock(
        return_value=SimpleNamespace(
            history='[{"role": "user", "content": "previous"}]'
        )
    )
    persist = AsyncMock()
    bump = AsyncMock()
    provider_settings = {"stream": True}
    context = SimpleNamespace(
        conversation_manager=object(),
        get_config=lambda umo: {
            "provider_settings": provider_settings,
            "agent_runner": {"config": {"misc": {"max_steps": 7}}},
        },
        get_llm_tool_manager=lambda: SimpleNamespace(
            get_builtin_tool=lambda tool_type: "send-tool"
        ),
    )
    event = SimpleNamespace(unified_msg_origin="test:FriendMessage:user", role="member")
    plugin = SimpleNamespace(context=context)
    task_result = {"task_id": "task-429", "result": "完成"}

    monkeypatch.setattr(background_notify, "wake_main_agent", None)
    monkeypatch.setattr(background_notify, "_OFFICIAL_API_READY", True)
    monkeypatch.setattr(background_notify, "ToolSet", LegacyToolSet)
    monkeypatch.setattr(
        background_notify,
        "MainAgentBuildConfig",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setattr(background_notify, "_get_session_conv", get_conv)
    monkeypatch.setattr(background_notify, "build_main_agent", build)
    monkeypatch.setattr(
        background_notify,
        "BACKGROUND_TASK_RESULT_WOKE_SYSTEM_PROMPT",
        "Result: {background_task_result}",
    )
    monkeypatch.setattr(background_notify, "CronMessageEvent", LegacyCronEvent)
    monkeypatch.setattr(background_notify, "MessageSession", LegacySession)
    monkeypatch.setattr(background_notify, "ProviderRequest", LegacyRequest)
    monkeypatch.setattr(background_notify, "SendMessageToUserTool", object())
    monkeypatch.setattr(background_notify, "persist_agent_history", persist)
    monkeypatch.setattr(
        background_notify, "get_session_tool_timeout", lambda context, umo: 15
    )
    monkeypatch.setattr(background_notify, "_bump_delivery_count", bump)

    delivered = await background_notify.notify_llm_background_result(
        plugin,
        event,
        task_result,
        notice="旧版通知",
        scene="test-429",
    )

    assert delivered is True
    build.assert_awaited_once()
    kwargs = build.await_args.kwargs
    cron_event = kwargs["event"]
    req = kwargs["req"]
    assert cron_event.role == "member"
    assert kwargs["config"].tool_call_timeout == 15
    assert kwargs["config"].streaming_response is True
    assert kwargs["config"].provider_settings is provider_settings
    assert req.contexts == [{"role": "user", "content": "previous"}]
    assert "task-429" in req.system_prompt
    assert req.prompt == background_notify._BACKGROUND_DELIVERY_PROMPT
    assert req.func_tool.tools == ["send-tool"]
    assert runner.max_steps == 7
    persist.assert_awaited_once_with(
        context.conversation_manager,
        event=cron_event,
        req=req,
        summary_note="[BackgroundTask] 旧版通知",
    )
    bump.assert_awaited_once_with(plugin, task_result, scene="test-429")
