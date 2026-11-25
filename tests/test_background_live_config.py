"""Synthetic regressions for inherited message configuration during callbacks."""

import asyncio
import contextvars
from types import SimpleNamespace

import pytest
from packaging.version import Version

from tl import astrbot_compat, background_notify

_UMO = "synthetic:GroupMessage:room"


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_setting", ["session", "llm"])
async def test_modern_guard_reads_changes_outside_inherited_message_scope(
    monkeypatch, disabled_setting
):
    original = {
        "platform_settings": {"id_blacklist": []},
        "provider_settings": {"enable": True},
    }
    current = {
        "platform_settings": {
            "id_blacklist": [_UMO] if disabled_setting == "session" else []
        },
        "provider_settings": {"enable": disabled_setting != "llm"},
    }
    # Model the host's per-message snapshot while its original turn is active.
    inherited = contextvars.ContextVar("synthetic_message_config", default=None)
    scope = SimpleNamespace(umo=_UMO, config=original, active=True)
    calls = []

    def get_config(*, umo):
        calls.append(umo)
        active_scope = inherited.get()
        if active_scope is not None and active_scope.active and active_scope.umo == umo:
            return active_scope.config
        return current

    monkeypatch.setattr(astrbot_compat, "ASTRBOT_VERSION", Version("4.30.0b1"))
    monkeypatch.setattr(background_notify, "SessionServiceManager", None)
    context = SimpleNamespace(get_config=get_config)
    event = SimpleNamespace(unified_msg_origin=_UMO, get_group_id=lambda: "room")
    token = inherited.set(scope)
    try:
        # A background task inherits the original active message's context.
        allowed = await asyncio.create_task(
            background_notify._background_delivery_allowed(
                event, require_llm=True, context=context
            )
        )
        assert allowed is False
        assert calls == [_UMO]
        assert inherited.get() is scope
        assert scope.active is True
        assert original["provider_settings"]["enable"] is True
        assert original["platform_settings"]["id_blacklist"] == []
    finally:
        inherited.reset(token)


@pytest.mark.asyncio
async def test_modern_failure_guard_keeps_llm_independent_session_check(monkeypatch):
    calls = []

    def get_config(*, umo):
        calls.append(umo)
        return {
            "platform_settings": {"id_blacklist": []},
            "provider_settings": {"enable": False},
        }

    monkeypatch.setattr(astrbot_compat, "ASTRBOT_VERSION", Version("4.30.0b1"))
    monkeypatch.setattr(background_notify, "SessionServiceManager", None)
    event = SimpleNamespace(unified_msg_origin=_UMO, get_group_id=lambda: "room")
    assert await background_notify._background_delivery_allowed(
        event, require_llm=False, context=SimpleNamespace(get_config=get_config)
    )
    assert calls == [_UMO]
