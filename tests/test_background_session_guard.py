"""Synthetic session-state changes; no model, backend or real send is used."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tl import background_notify

_UMO = "synthetic:FriendMessage:requester"


def services(monkeypatch, *, session=True, llm=True):
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
    services(monkeypatch)
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
    services(monkeypatch, llm=False)
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
