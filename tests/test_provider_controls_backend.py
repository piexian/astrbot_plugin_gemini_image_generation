from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_provider_application import (
    config_service,
    entry,
    plugin_factory,
    settings,
    usage,
)
from test_studio_providers import payload, service

from tl.studio_vision_providers import VisionProviderDirectory

# Re-export the existing fixture without requiring that pytest collect its module.
__all__ = ["plugin_factory"]

FAKE_OLD_KEY = "fake-controls-old-key"
FAKE_NEW_KEY = "fake-controls-draft-key"
BASE = "https://catalog.example.invalid/v1beta"
MODEL_RESULT = {
    "models": [{"id": "fake-image-model", "label": "fake-image-model"}],
    "warning": "",
    "truncated": False,
}


@pytest.fixture(autouse=True)
def isolated_network_and_proxy(monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    # All catalog transports/providers below are fake. Fail closed if a regression
    # accidentally takes the production HTTP path, even with an invalid test host.
    monkeypatch.setattr(
        "tl.model_catalog.aiohttp.ClientSession",
        Mock(side_effect=AssertionError("real HTTP is forbidden in these tests")),
    )


def catalog_service(**values):
    row = entry(FAKE_OLD_KEY, api_base=BASE, proxy="")
    row.update(values)
    return service(settings(row, proxy=""))


async def model_payload(svc):
    body = await payload(svc)
    return {
        "kind": "entry",
        "revision": body["revision"],
        "entry": body["entries"][0],
        "common": body["common"],
    }


def host_context(*, use_manager=True):
    sources = [
        {
            "id": "chat-source",
            "provider_type": "chat_completion",
            "model": "source-model",
            "api_key": "fake-host-source-secret",
            "api_base": "https://fake-host:fake-password@host.invalid/v1",
        },
        {"id": "embedding-source", "provider_type": "embedding"},
        {"id": "legacy-source", "model": "legacy-source-model"},
    ]
    rows = [
        {
            "id": "loaded",
            "enable": True,
            "provider_type": "chat_completion",
            "model": "row-model",
            "api_keys": ["fake-host-row-secret"],
            "proxy": "http://fake-proxy-user:fake-proxy-password@proxy.invalid",
        },
        {"id": "pending", "enable": True, "provider_source_id": "chat-source"},
        {"id": "legacy", "provider_source_id": "legacy-source"},
        {"id": "inherited-embedding", "provider_source_id": "embedding-source"},
        {
            "id": "override-chat",
            "provider_source_id": "embedding-source",
            "provider_type": "chat_completion",
        },
        {
            "id": "override-embedding",
            "provider_source_id": "chat-source",
            "provider_type": "embedding",
        },
        {"id": "disabled", "provider_type": "chat_completion", "enable": False},
        {"id": "speech", "provider_type": "text_to_speech"},
        {"id": "no-type"},
        {"id": "loaded", "provider_type": "chat_completion", "model": "duplicate"},
        {"id": "", "provider_type": "chat_completion"},
        None,
    ]
    loaded = SimpleNamespace(meta=lambda: SimpleNamespace(id="loaded"))
    orphan = SimpleNamespace(meta=lambda: SimpleNamespace(id="instance-only-orphan"))
    context = SimpleNamespace(
        get_config=Mock(return_value={"provider": rows, "provider_sources": sources}),
        get_all_providers=Mock(return_value=[loaded, orphan]),
        get_provider_by_id=Mock(return_value=loaded),
    )
    if use_manager:
        context.provider_manager = SimpleNamespace(
            providers_config=rows,
            provider_sources_config=sources,
            inst_map={
                "loaded": loaded,
                "pending": None,
                "instance-only-orphan": orphan,
            },
        )
    return context


@pytest.mark.asyncio
async def test_get_config_exposes_editable_plugin_keys_without_host_credentials():
    svc = catalog_service()
    svc.vision_directory = VisionProviderDirectory(host_context())
    before = copy.deepcopy(dict(svc.raw_config))
    snapshot = await svc.get_config()
    assert snapshot["key_values_visible"] is True
    assert snapshot["entries"][0]["values"]["api_keys"] == [FAKE_OLD_KEY]
    assert snapshot["entries"][0]["secrets"]["api_keys"] == {
        "present": True,
        "count": 1,
    }
    assert "fake-host" not in json.dumps(snapshot)
    assert dict(svc.raw_config) == before and svc.raw_config.calls == []
    snapshot["entries"][0]["values"]["api_keys"].append("fake-local-edit")
    assert dict(svc.raw_config) == before
    body = await payload(svc)
    body["entries"][0]["values"]["api_keys"] = [f" {FAKE_NEW_KEY} ", "", FAKE_NEW_KEY]
    saved = await svc.save_config(body)
    assert saved["entries"][0]["values"]["api_keys"] == [FAKE_NEW_KEY]
    assert svc.raw_config["provider_settings"]["provider_overrides"][0]["api_keys"] == [
        FAKE_NEW_KEY
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("edit_mode", ["append", "plaintext"])
async def test_key_edit_through_provider_application_preserves_real_usage(
    plugin_factory, edit_mode
):
    plugin, kv = plugin_factory(settings(entry(FAKE_OLD_KEY, daily_limit_per_key=3)))
    old_manager = plugin.key_manager
    assert await old_manager.get_available_key("google#1") == FAKE_OLD_KEY
    assert await old_manager.get_available_key("google#1") == FAKE_OLD_KEY
    assert usage(old_manager, "google#1") == 2
    svc = config_service(plugin)
    body = await payload(svc)
    target = body["entries"][0]
    if edit_mode == "append":
        target["secret_actions"]["api_keys"] = {
            "mode": "append",
            "value": [f" {FAKE_OLD_KEY} ", FAKE_NEW_KEY, " ", FAKE_NEW_KEY],
        }
    else:
        target["values"]["api_keys"] = [
            FAKE_OLD_KEY,
            f" {FAKE_NEW_KEY} ",
            FAKE_NEW_KEY,
            "",
        ]
    result = await svc.save_config(body)
    assert result["entries"][0]["values"]["api_keys"] == [FAKE_OLD_KEY, FAKE_NEW_KEY]
    assert plugin.cfg.provider_candidates[0].api_keys == [FAKE_OLD_KEY, FAKE_NEW_KEY]
    assert plugin.key_manager is not old_manager
    assert usage(plugin.key_manager, "google#1", 0) == 2
    assert usage(plugin.key_manager, "google#1", 1) == 0
    assert usage(old_manager, "google#1") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["address", "entry-proxy", "common-proxy"])
async def test_existing_key_changed_destination_returns_success_confirmation_before_network(
    change,
):
    svc = catalog_service()
    svc.catalog.fetch = AsyncMock(return_value=MODEL_RESULT)
    body = await model_payload(svc)
    body["entry"]["values"]["api_keys"] = [FAKE_OLD_KEY]
    if change == "address":
        body["entry"]["values"]["api_base"] = (
            "https://changed.invalid/private-path?token=fake-url-secret"
        )
        expected_target = "https://changed.invalid"
    else:
        scope = "entry" if change == "entry-proxy" else "common"
        body[scope]["values"]["proxy"] = (
            "http://fake-user:fake-proxy-secret@changed-proxy.invalid:8080"
        )
        expected_target = "https://catalog.example.invalid"
    before = copy.deepcopy(dict(svc.raw_config))
    # This must be ordinary result data: the real bridge drops error envelope data.
    assert await svc.fetch_models(body) == {
        "confirmation_required": True,
        "target": expected_target,
    }
    svc.catalog.fetch.assert_not_awaited()
    body["confirmed_target"] = False
    assert (await svc.fetch_models(body))["confirmation_required"] is True
    svc.catalog.fetch.assert_not_awaited()
    body["confirmed_target"] = True
    assert await svc.fetch_models(body) == MODEL_RESULT
    svc.catalog.fetch.assert_awaited_once()
    assert svc.catalog.fetch.await_args.args[0].headers == {
        "x-goog-api-key": FAKE_OLD_KEY
    }
    assert dict(svc.raw_config) == before and svc.raw_config.calls == []
