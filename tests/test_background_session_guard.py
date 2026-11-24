"""Synthetic session-state changes; no model, backend or real send is used."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from packaging.version import Version

from tl import astrbot_compat, background_notify
from tl.core.states import JobState

_UMO = "synthetic:FriendMessage:requester"


def services(monkeypatch, *, session=True, llm=True):
    monkeypatch.setattr(astrbot_compat, "ASTRBOT_VERSION", Version("4.29.4"))
    manager = SimpleNamespace(
        is_session_enabled=AsyncMock(return_value=session),
        is_llm_enabled_for_session=AsyncMock(return_value=llm),
    )
    monkeypatch.setattr(background_notify, "SessionServiceManager", manager)
    return manager


def event(*, stopped=False, umo=_UMO):
    return SimpleNamespace(
        unified_msg_origin=umo,
        is_stopped=lambda: stopped,
        send=AsyncMock(),
        plain_result=lambda text: text,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("require_llm", [False, True])
@pytest.mark.parametrize("enabled", [False, None, 1, "true"])
async def test_session_requires_explicit_native_true(monkeypatch, require_llm, enabled):
    services(monkeypatch, session=enabled)
    assert not await background_notify._background_delivery_allowed(
        event(), require_llm=require_llm
    )


@pytest.mark.asyncio
async def test_llm_disabled_blocks_wake_but_allows_direct_notice(monkeypatch):
    services(monkeypatch, llm=False)
    assert not await background_notify._background_delivery_allowed(
        event(), require_llm=True
    )
    assert await background_notify._background_delivery_allowed(
        event(), require_llm=False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_service_missing_or_lookup_failure_is_closed(monkeypatch, missing):
    manager = services(monkeypatch)
    if missing:
        monkeypatch.setattr(background_notify, "SessionServiceManager", None)
    else:
        manager.is_session_enabled.side_effect = RuntimeError("synthetic unavailable")
    assert not await background_notify._background_delivery_allowed(
        event(), require_llm=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped,umo", [(True, _UMO), (False, ""), (False, None)])
async def test_stopped_or_missing_scope_does_not_query_services(
    monkeypatch, stopped, umo
):
    manager = services(monkeypatch)
    assert not await background_notify._background_delivery_allowed(
        event(stopped=stopped, umo=umo), require_llm=True
    )
    manager.is_session_enabled.assert_not_awaited()


@pytest.mark.asyncio
async def test_stopped_during_service_lookup_is_rechecked(monkeypatch):
    source = event()
    manager = services(monkeypatch)

    async def pause(_umo):
        source.is_stopped = lambda: True
        return True

    manager.is_llm_enabled_for_session.side_effect = pause
    assert not await background_notify._background_delivery_allowed(
        source, require_llm=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "disable_llm", "stop_event"])
async def test_pause_while_tool_waits_blocks_final_send_without_mutating_shared_tool(
    monkeypatch, change
):
    manager = services(monkeypatch)
    source = event()
    core = SimpleNamespace(send_message=AsyncMock(return_value=True))
    wrapper = SimpleNamespace(context=SimpleNamespace(context=core, event=source))
    entered, release = asyncio.Event(), asyncio.Event()

    async def original_call(context, **kwargs):
        entered.set()
        await release.wait()
        return await context.context.context.send_message(_UMO, "synthetic-result")

    shared = SimpleNamespace(
        name="send_message_to_user", handler=None, call=original_call, active=True
    )
    guarded = background_notify._guard_background_send_tool(shared, source)
    pending = asyncio.create_task(guarded.call(wrapper, messages=[]))
    await entered.wait()
    if change == "pause":
        manager.is_session_enabled.return_value = False
    elif change == "disable_llm":
        manager.is_llm_enabled_for_session.return_value = False
    else:
        source.is_stopped = lambda: True
    release.set()
    assert await pending is False
    core.send_message.assert_not_awaited()
    assert shared.call is original_call
    assert wrapper.context.context is core
    assert guarded is not shared and guarded.active is True


@pytest.mark.asyncio
async def test_enabled_source_and_target_send_through_original_boundary(monkeypatch):
    manager = services(monkeypatch)
    monkeypatch.setattr(astrbot_compat, "ASTRBOT_VERSION", Version("4.10.4"))
    manager.is_session_enabled = lambda umo: True
    manager.is_llm_enabled_for_session = lambda umo: True
    source = event()
    core = SimpleNamespace(send_message=AsyncMock(return_value=True))
    boundary = background_notify._BackgroundSendContext(core, source)
    assert await boundary.send_message(_UMO, "synthetic-result") is True
    core.send_message.assert_awaited_once_with(_UMO, "synthetic-result")


@pytest.mark.asyncio
async def test_admin_other_target_still_checks_target_pause(monkeypatch):
    manager = services(monkeypatch)
    manager.is_session_enabled.side_effect = lambda umo: umo == _UMO
    core = SimpleNamespace(send_message=AsyncMock(return_value=True))
    boundary = background_notify._BackgroundSendContext(core, event())
    assert (
        await boundary.send_message("synthetic:GroupMessage:paused", "result") is False
    )
    core.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("llm_enabled", [False, True])
async def test_paused_failure_cannot_use_direct_fallback(monkeypatch, llm_enabled):
    services(monkeypatch, session=False)
    source = event()
    plugin = SimpleNamespace(
        cfg=SimpleNamespace(background_failure_notify_llm=llm_enabled)
    )
    assert not await background_notify.report_background_failure(
        plugin, source, "synthetic failure", scene="guard-test"
    )
    source.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_between_failed_agent_and_direct_fallback_is_rechecked(monkeypatch):
    manager = services(monkeypatch)
    source = event()
    plugin = SimpleNamespace(cfg=SimpleNamespace(background_failure_notify_llm=True))

    async def failed_agent(*args, **kwargs):
        manager.is_session_enabled.return_value = False
        return False

    monkeypatch.setattr(
        background_notify, "notify_llm_background_failure", failed_agent
    )
    assert not await background_notify.report_background_failure(
        plugin, source, "synthetic failure", scene="guard-test"
    )
    source.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_llm_disabled_failure_can_use_checked_non_llm_notice(monkeypatch):
    manager = services(monkeypatch, llm=False)
    monkeypatch.setattr(astrbot_compat, "ASTRBOT_VERSION", Version("4.10.4"))
    manager.is_session_enabled = lambda umo: True
    manager.is_llm_enabled_for_session = lambda umo: False
    source = event()
    plugin = SimpleNamespace(cfg=SimpleNamespace(background_failure_notify_llm=True))
    assert await background_notify.report_background_failure(
        plugin, source, "synthetic failure", scene="guard-test"
    )
    source.send.assert_awaited_once()


def test_unverified_handler_interface_is_rejected():
    tool = SimpleNamespace(handler=AsyncMock(), call=AsyncMock())
    with pytest.raises(RuntimeError):
        background_notify._guard_background_send_tool(tool, event())


@pytest.mark.parametrize("kind", ["agent", "signal"])
def test_event_stopped_reads_agent_and_shared_signal(kind):
    signal = asyncio.Event()
    extras = {
        "agent_stop_requested": kind == "agent",
        "_background_stop_signal": signal,
    }
    if kind == "signal":
        signal.set()
    source = SimpleNamespace(is_stopped=lambda: False, get_extra=extras.get)

    assert background_notify._event_stopped(source)


@pytest.mark.asyncio
async def test_background_job_registration_is_disabled_without_registry(monkeypatch):
    monkeypatch.setattr(background_notify, "active_event_registry", None)
    service = SimpleNamespace(
        wait=AsyncMock(), get_status=AsyncMock(), cancel=AsyncMock()
    )

    assert (
        await background_notify.register_background_job_stop(
            event(), service, "job-disabled"
        )
        is None
    )
    service.wait.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "should_cancel"),
    [
        (JobState.RUNNING, True),
        (JobState.RESULT_READY, False),
        (JobState.DELIVERY_PENDING, False),
        (JobState.SUCCEEDED, False),
    ],
)
async def test_background_job_stop_only_cancels_generation(
    monkeypatch, state, should_cancel
):
    registered = {}
    registry = SimpleNamespace(
        register_background_task=lambda _event, task: registered.setdefault(
            "task", task
        )
    )
    monkeypatch.setattr(background_notify, "active_event_registry", registry)
    started = asyncio.Event()

    async def wait(_job_id):
        started.set()
        await asyncio.Event().wait()

    service = SimpleNamespace(
        wait=wait,
        get_status=AsyncMock(return_value=SimpleNamespace(state=state)),
        cancel=AsyncMock(),
    )
    task = await background_notify.register_background_job_stop(
        event(), service, "job-stop"
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    registered["task"].cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert service.cancel.await_count == int(should_cancel)


@pytest.mark.asyncio
async def test_registration_cancelled_before_start_still_cancels_generation(
    monkeypatch,
):
    registry = SimpleNamespace(
        register_background_task=lambda _event, task: task.cancel()
    )
    monkeypatch.setattr(background_notify, "active_event_registry", registry)
    service = SimpleNamespace(
        wait=AsyncMock(),
        get_status=AsyncMock(return_value=SimpleNamespace(state=JobState.RUNNING)),
        cancel=AsyncMock(),
    )

    task = await background_notify.register_background_job_stop(
        event(), service, "job-early-stop"
    )

    assert task is not None and task.cancelled()
    service.wait.assert_not_awaited()
    service.cancel.assert_awaited_once_with("job-early-stop")


@pytest.mark.asyncio
async def test_supervisor_task_handles_wait_exception_without_leaking(monkeypatch):
    registry = SimpleNamespace(register_background_task=lambda _event, _task: None)
    monkeypatch.setattr(background_notify, "active_event_registry", registry)
    service = SimpleNamespace(
        wait=AsyncMock(side_effect=RuntimeError("store closed")),
        get_status=AsyncMock(),
        cancel=AsyncMock(),
    )

    task = await background_notify.register_background_job_stop(
        event(), service, "job-wait-error"
    )

    assert task is not None
    assert await task is None
    service.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_modern_config_handles_none_sections_and_none_group_id(monkeypatch):
    monkeypatch.setattr(astrbot_compat, "ASTRBOT_VERSION", Version("4.30.0b1"))
    context = SimpleNamespace(
        get_config=lambda umo: {
            "platform_settings": {"id_blacklist": ["None", None, ""]},
            "provider_settings": None,
        }
    )
    source = SimpleNamespace(
        unified_msg_origin=_UMO,
        is_stopped=lambda: False,
        get_group_id=lambda: None,
    )

    assert await background_notify._background_delivery_allowed(
        source, context=context, require_llm=True
    )

    context_null_platform = SimpleNamespace(
        get_config=lambda umo: {"platform_settings": None, "provider_settings": None}
    )
    assert await background_notify._background_delivery_allowed(
        source, context=context_null_platform, require_llm=True
    )
