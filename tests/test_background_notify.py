from types import SimpleNamespace
from unittest.mock import AsyncMock

from packaging.version import Version
import pytest

from tl import astrbot_compat, background_notify

_UMO = "test:FriendMessage:user"


@pytest.fixture(autouse=True)
def enabled_session_services(monkeypatch):
    monkeypatch.setattr(astrbot_compat, "ASTRBOT_VERSION", Version("4.29.4"))
    monkeypatch.setattr(
        background_notify,
        "SessionServiceManager",
        SimpleNamespace(
            is_session_enabled=AsyncMock(return_value=True),
            is_llm_enabled_for_session=AsyncMock(return_value=True),
        ),
    )


class _ModernRun:
    def __init__(self, *, tool_text=None, has_send_oper=False):
        self._event = SimpleNamespace(_has_send_oper=has_send_oper)
        self.result = AsyncMock(return_value=SimpleNamespace(status="completed"))
        self.tool_text = tool_text

    async def events(self):
        if self.tool_text is None:
            return
        yield SimpleNamespace(
            type="tool.started",
            payload=SimpleNamespace(execution_id="exec-1", name="send_message_to_user"),
        )
        yield SimpleNamespace(
            type="tool.finished",
            payload=SimpleNamespace(
                execution_id="exec-1",
                status="completed",
                content=[SimpleNamespace(text=self.tool_text)],
            ),
        )


def _modern_harness(monkeypatch, run):
    class ModernInput:
        def __init__(self, text):
            self.text = text

    service = SimpleNamespace(run_turn=AsyncMock(return_value=run))
    config = {
        "platform_settings": {"id_blacklist": []},
        "provider_settings": {"enable": True},
    }
    plugin = SimpleNamespace(
        context=SimpleNamespace(agent=service, get_config=lambda umo: config)
    )
    event = SimpleNamespace(
        unified_msg_origin=_UMO,
        role="admin",
        agent_session=SimpleNamespace(session_id="conversation-1"),
        get_group_id=lambda: None,
    )
    bump = AsyncMock()
    monkeypatch.setattr(background_notify, "PlainInput", ModernInput)
    monkeypatch.setattr(background_notify, "_OFFICIAL_API_READY", True)
    monkeypatch.setattr(background_notify, "SessionServiceManager", None)
    monkeypatch.setattr(astrbot_compat, "ASTRBOT_VERSION", Version("4.30.0b1"))
    monkeypatch.setattr(background_notify, "_bump_delivery_count", bump)
    return plugin, event, service, bump


@pytest.mark.asyncio
async def test_notify_uses_astrbot_430_agent_service(monkeypatch, tmp_path):
    run = _ModernRun(tool_text=f"Message sent to session {_UMO}")
    plugin, event, service, bump = _modern_harness(monkeypatch, run)
    image_path = tmp_path / "result.png"
    image_path.write_bytes(b"image")
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
    service.run_turn.assert_awaited_once()
    kwargs = service.run_turn.await_args.kwargs
    assert kwargs["umo"] == _UMO
    assert kwargs["send_to_user"] is True
    assert kwargs["source_event"] is event
    assert kwargs["session_id"] == "conversation-1"
    prompt = kwargs["input_message"][0].text
    assert str(image_path) in prompt
    assert "send_message_to_user" in prompt
    assert "data:image" not in prompt
    run.result.assert_awaited_once()
    bump.assert_awaited_once_with(plugin, task_result, scene="test-430")


@pytest.mark.asyncio
async def test_notify_430_ignores_automatic_reply_without_image_delivery(monkeypatch):
    run = _ModernRun(has_send_oper=True)
    plugin, event, _, bump = _modern_harness(monkeypatch, run)
    task_result = {"task_id": "task-430", "image_paths": ["/tmp/result.png"]}

    delivered = await background_notify.notify_llm_background_result(
        plugin,
        event,
        task_result,
        notice="后台图片生成任务已完成",
        scene="test-430",
    )

    assert delivered is False
    run.result.assert_awaited_once()
    bump.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_430_failure_falls_back_to_direct_send(monkeypatch):
    run = _ModernRun(has_send_oper=False)
    plugin, event, _, bump = _modern_harness(monkeypatch, run)
    plugin.cfg = SimpleNamespace(background_failure_notify_llm=True)
    event.plain_result = lambda text: text
    event.send = AsyncMock()

    delivered = await background_notify.report_background_failure(
        plugin,
        event,
        "上游详细错误",
        scene="test-430",
        task_id="task-failure",
    )

    assert delivered is True
    event.send.assert_awaited_once_with(
        "后台图片生成任务（任务号 task-failure）失败，请稍后重试。"
    )
    bump.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_430_failure_accepts_confirmed_send(monkeypatch):
    run = _ModernRun(has_send_oper=True)
    plugin, event, _, bump = _modern_harness(monkeypatch, run)
    plugin.cfg = SimpleNamespace(background_failure_notify_llm=True)
    event.plain_result = lambda text: text
    event.send = AsyncMock()

    delivered = await background_notify.report_background_failure(
        plugin,
        event,
        "上游详细错误",
        scene="test-430",
        task_id="task-failure",
    )

    assert delivered is True
    event.send.assert_not_awaited()
    bump.assert_awaited_once_with(
        plugin,
        {"task_id": "task-failure", "result": "上游详细错误"},
        scene="test-430",
    )


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
            self.tools = [t for t in self.tools if t.name != tool.name]
            self.tools.append(tool)

        def get_tool(self, name):
            return next((t for t in self.tools if t.name == name), None)

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
    builtin_send = SimpleNamespace(
        name="send_message_to_user", handler=None, call=AsyncMock()
    )
    context = SimpleNamespace(
        conversation_manager=object(),
        get_config=lambda umo: {
            "provider_settings": provider_settings,
            "agent_runner": {"config": {"misc": {"max_steps": 7}}},
        },
        get_llm_tool_manager=lambda: SimpleNamespace(
            get_builtin_tool=lambda tool_type: builtin_send
        ),
    )
    event = SimpleNamespace(unified_msg_origin="test:FriendMessage:user", role="member")
    plugin = SimpleNamespace(context=context)
    task_result = {"task_id": "task-429", "result": "完成"}

    monkeypatch.setattr(background_notify, "PlainInput", None)
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
    assert len(req.func_tool.tools) == 1
    assert req.func_tool.tools[0].name == "send_message_to_user"
    assert req.func_tool.tools[0] is not builtin_send
    assert req.func_tool.tools[0].call is not builtin_send.call
    assert runner.max_steps == 7
    persist.assert_awaited_once_with(
        context.conversation_manager,
        event=cron_event,
        req=req,
        summary_note="[BackgroundTask] 旧版通知",
    )
    bump.assert_awaited_once_with(plugin, task_result, scene="test-429")
