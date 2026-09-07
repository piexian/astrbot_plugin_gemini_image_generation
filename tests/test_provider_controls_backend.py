from __future__ import annotations

import asyncio
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
from test_web_api import _api, _Context, _match

from tl.studio_vision_providers import VisionProviderDirectory
from tl.web_studio_service import StudioServiceError

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


@pytest.mark.parametrize("use_manager", [True, False])
def test_vision_directory_projects_enabled_host_config_not_instance_inventory(
    use_manager,
):
    context = host_context(use_manager=use_manager)
    result = VisionProviderDirectory(context).snapshot()
    assert result["vision_providers_available"] is True
    assert result["vision_providers_warning"] == ""
    rows = result["vision_providers"]
    assert [row["id"] for row in rows] == [
        "loaded",
        "pending",
        "legacy",
        "override-chat",
    ]
    assert rows[0] == {
        "id": "loaded",
        "label": "loaded",
        "model": "row-model",
        "source_id": "",
        "available": True,
    }
    assert rows[1] == {
        "id": "pending",
        "label": "pending（chat-source）",
        "model": "source-model",
        "source_id": "chat-source",
        "available": False,
    }
    assert rows[2]["model"] == "legacy-source-model"
    assert all(
        set(row) == {"id", "label", "model", "source_id", "available"} for row in rows
    )
    encoded = json.dumps(result)
    for secret in ("fake-host", "fake-password", "fake-proxy", "api_key", "api_base"):
        assert secret not in encoded
    if use_manager:
        context.get_config.assert_not_called()
        context.get_all_providers.assert_not_called()
    else:
        context.get_config.assert_called_once_with()
    context.get_provider_by_id.assert_not_called()


def test_vision_directory_config_failure_does_not_fall_back_to_orphan_instances():
    context = host_context(use_manager=False)
    context.get_config.side_effect = RuntimeError("fake-host-private-error")
    result = VisionProviderDirectory(context).snapshot()
    assert result["vision_providers"] == []
    assert result["vision_providers_available"] is False
    assert result["vision_providers_warning"]
    assert "fake-host-private-error" not in json.dumps(result)
    context.get_all_providers.assert_not_called()


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
@pytest.mark.parametrize("mode", ["keep", "clear", "replace", "append"])
@pytest.mark.parametrize("operation", ["save", "models"])
async def test_plaintext_and_secret_actions_for_same_key_are_mutually_exclusive(
    mode, operation
):
    svc = catalog_service()
    svc.catalog.fetch = AsyncMock(return_value=MODEL_RESULT)
    body = await payload(svc) if operation == "save" else await model_payload(svc)
    target = body["entries"][0] if operation == "save" else body["entry"]
    target["values"]["api_keys"] = [FAKE_OLD_KEY]
    target["secret_actions"]["api_keys"] = {"mode": mode}
    if mode in {"replace", "append"}:
        target["secret_actions"]["api_keys"]["value"] = [FAKE_NEW_KEY]
    with pytest.raises(StudioServiceError) as error:
        await (svc.save_config(body) if operation == "save" else svc.fetch_models(body))
    assert error.value.status_code == 400
    assert svc.raw_config.calls == []
    svc.catalog.fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,field", [("entry", "api_base"), ("entry", "proxy"), ("common", "proxy")]
)
async def test_connection_values_and_actions_are_mutually_exclusive(scope, field):
    svc = catalog_service()
    svc.catalog.fetch = AsyncMock()
    body = await model_payload(svc)
    body[scope]["values"][field] = BASE if field == "api_base" else ""
    body[scope]["secret_actions"][field] = {"mode": "keep"}
    with pytest.raises(StudioServiceError) as error:
        await svc.fetch_models(body)
    assert error.value.status_code == 400
    svc.catalog.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_append_preserves_old_order_and_deduplicates_trimmed_nonempty_keys():
    svc = catalog_service(
        api_keys=[f" {FAKE_OLD_KEY} ", "", FAKE_OLD_KEY, "fake-second-old"]
    )
    body = await payload(svc)
    body["entries"][0]["secret_actions"]["api_keys"] = {
        "mode": "append",
        "value": [
            " ",
            f" {FAKE_OLD_KEY} ",
            FAKE_NEW_KEY,
            f" {FAKE_NEW_KEY} ",
            "fake-last",
        ],
    }
    result = await svc.save_config(body)
    assert result["entries"][0]["values"]["api_keys"] == [
        FAKE_OLD_KEY,
        "fake-second-old",
        FAKE_NEW_KEY,
        "fake-last",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old_count,additions,valid",
    [
        (199, ["fake-key-0", "fake-added", " fake-added "], True),
        (200, ["fake-key-0", " "], True),
        (200, ["fake-added"], False),
        (1, ["fake-duplicate"] * 201, False),
        (1, ["x" * 8193], False),
        (1, [None], False),
    ],
)
async def test_append_validates_input_and_combined_key_limits(
    old_count, additions, valid
):
    svc = catalog_service(api_keys=[f"fake-key-{index}" for index in range(old_count)])
    before = copy.deepcopy(dict(svc.raw_config))
    body = await payload(svc)
    body["entries"][0]["secret_actions"]["api_keys"] = {
        "mode": "append",
        "value": additions,
    }
    if valid:
        result = await svc.save_config(body)
        assert len(result["entries"][0]["values"]["api_keys"]) == 200
    else:
        with pytest.raises(StudioServiceError) as error:
            await svc.save_config(body)
        assert error.value.status_code == 400
        assert dict(svc.raw_config) == before and svc.raw_config.calls == []


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
    assert kv.value["__shared_keys_v1"]["google"][FAKE_OLD_KEY]["usage_count"] == 2
    assert len(plugin.raw_config.saved) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,value,status",
    [
        (("revision",), "stale-revision", 409),
        (("entry", "id"), "forged-entry-id", 400),
        (("entry", "id"), 0, 400),
        (("entry", "api_type"), "openai", 400),
        (("entry", "api_type"), "unknown-provider", 400),
        (("entry", "values", "model"), "not-a-connection-field", 400),
        (("entry", "values", "api_keys"), "not-a-list", 400),
        (("entry", "values", "api_keys"), [], 400),
        (("entry", "values", "api_base"), "file:///fake-catalog", 400),
        (("entry", "values", "api_base"), "https://catalog.invalid/#fragment", 400),
        (("entry", "values", "proxy"), "http://proxy.invalid/path", 400),
        (("entry", "values", "proxy"), 17, 400),
        (("entry", "extra"), True, 400),
        (("common", "values", "vision_model"), "not-a-connection-field", 400),
        (("common", "values", "proxy"), False, 400),
        (("confirmed_target",), "true", 400),
        (("confirmed_target",), 1, 400),
        (("extra",), True, 400),
    ],
)
async def test_entry_model_query_rejects_invalid_revision_identity_and_connection(
    path, value, status
):
    svc = catalog_service()
    svc.catalog.fetch = AsyncMock(return_value=MODEL_RESULT)
    body = await model_payload(svc)
    target = body
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    before = copy.deepcopy(dict(svc.raw_config))
    with pytest.raises(StudioServiceError) as error:
        await svc.fetch_models(body)
    assert error.value.status_code == status
    svc.catalog.fetch.assert_not_awaited()
    assert dict(svc.raw_config) == before and svc.raw_config.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry_proxy,common_proxy,env,expected",
    [
        (
            " http://entry.invalid:8001 ",
            "http://common.invalid:8002",
            {"HTTPS_PROXY": "http://env.invalid:8003"},
            "http://entry.invalid:8001",
        ),
        (
            " ",
            " http://common.invalid:8002 ",
            {"HTTPS_PROXY": "http://env.invalid:8003"},
            "http://common.invalid:8002",
        ),
        (
            "",
            " ",
            {
                "HTTPS_PROXY": "http://upper.invalid",
                "https_proxy": "http://lower.invalid",
                "HTTP_PROXY": "http://http.invalid",
            },
            "http://upper.invalid",
        ),
        (
            "",
            "",
            {
                "https_proxy": "http://lower.invalid",
                "HTTP_PROXY": "http://http.invalid",
            },
            "http://lower.invalid",
        ),
        (
            "",
            "",
            {
                "HTTP_PROXY": "http://http.invalid",
                "http_proxy": "http://lower-http.invalid",
            },
            "http://http.invalid",
        ),
        (
            "",
            "",
            {"http_proxy": "http://lower-http.invalid"},
            "http://lower-http.invalid",
        ),
        ("", "", {}, None),
    ],
)
async def test_entry_catalog_uses_first_draft_key_and_proxy_precedence(
    monkeypatch, entry_proxy, common_proxy, env, expected
):
    svc = catalog_service()
    svc.catalog.fetch = AsyncMock(return_value=MODEL_RESULT)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    body = await model_payload(svc)
    body["entry"]["values"] = {
        "api_keys": [" ", f" {FAKE_NEW_KEY} ", "fake-unused-second-key"],
        "api_base": "https://draft.invalid/gateway/v1beta/models",
        "proxy": entry_proxy,
    }
    body["common"]["values"]["proxy"] = common_proxy
    before = copy.deepcopy(dict(svc.raw_config))
    assert await svc.fetch_models(body) == MODEL_RESULT
    query = svc.catalog.fetch.await_args.args[0]
    assert query.headers == {"x-goog-api-key": FAKE_NEW_KEY}
    assert query.url == "https://draft.invalid/gateway/v1beta/models"
    assert query.proxy == expected
    assert dict(svc.raw_config) == before and svc.raw_config.calls == []


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


@pytest.mark.asyncio
async def test_confirmed_target_does_not_override_revision_change():
    svc = catalog_service()
    svc.catalog.fetch = AsyncMock(return_value=MODEL_RESULT)
    body = await model_payload(svc)
    body["entry"]["values"]["api_base"] = "https://changed.invalid/v1beta"
    assert (await svc.fetch_models(body))["confirmation_required"] is True
    svc.raw_config["provider_settings"]["proxy"] = "http://edited.invalid:8080"
    body["confirmed_target"] = True
    with pytest.raises(StudioServiceError) as error:
        await svc.fetch_models(body)
    assert error.value.status_code == 409
    assert error.value.data == {"reason": "revision"}
    svc.catalog.fetch.assert_not_awaited()
    assert svc.raw_config.calls == []


@pytest.mark.asyncio
async def test_unchanged_existing_key_and_new_unsaved_entry_need_no_confirmation():
    svc = catalog_service()
    svc.catalog.fetch = AsyncMock(return_value=MODEL_RESULT)
    body = await model_payload(svc)
    assert await svc.fetch_models(body) == MODEL_RESULT
    assert svc.catalog.fetch.await_args.args[0].headers == {
        "x-goog-api-key": FAKE_OLD_KEY
    }
    body["entry"]["id"] = None
    body["entry"]["values"] = {"api_base": BASE, "api_keys": [FAKE_NEW_KEY]}
    assert await svc.fetch_models(body) == MODEL_RESULT
    assert svc.catalog.fetch.await_args.args[0].headers == {
        "x-goog-api-key": FAKE_NEW_KEY
    }
    assert svc.raw_config.calls == []


@pytest.mark.asyncio
async def test_catalog_network_does_not_hold_config_lock_save_or_consume_real_quota(
    plugin_factory,
):
    plugin, kv = plugin_factory(
        settings(entry(FAKE_OLD_KEY, api_base=BASE, proxy=""), proxy="")
    )
    assert await plugin.key_manager.get_available_key("google#1") == FAKE_OLD_KEY
    svc = config_service(plugin)
    body = await model_payload(svc)
    before = copy.deepcopy(dict(plugin.raw_config))
    before_kv = (kv.reads, copy.deepcopy(kv.writes), copy.deepcopy(kv.value))
    old_manager = plugin.key_manager
    quota = AsyncMock(wraps=old_manager.get_available_key)
    old_manager.get_available_key = quota
    started, release = asyncio.Event(), asyncio.Event()

    async def fetch(query):
        assert query.headers == {"x-goog-api-key": FAKE_OLD_KEY}
        assert not svc.config_lock.locked()
        started.set()
        await release.wait()
        return MODEL_RESULT

    svc.catalog.fetch = AsyncMock(side_effect=fetch)
    task = asyncio.create_task(svc.fetch_models(body))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        snapshot = await asyncio.wait_for(svc.get_config(), timeout=1)
        assert snapshot["revision"] == body["revision"]
        assert not task.done()
    finally:
        release.set()
        result = await asyncio.wait_for(task, timeout=1)
    assert result == MODEL_RESULT
    quota.assert_not_awaited()
    assert usage(plugin.key_manager, "google#1") == 1
    assert plugin.key_manager is old_manager
    assert dict(plugin.raw_config) == before and plugin.raw_config.saved == []
    assert (kv.reads, kv.writes, kv.value) == before_kv
    assert not plugin.provider_runtime.busy and not plugin.provider_runtime.updating


@pytest.mark.asyncio
async def test_vision_models_borrow_only_actual_enabled_provider_without_reconfiguration():
    svc = catalog_service()
    context = host_context()
    provider = SimpleNamespace(
        get_models=AsyncMock(return_value=["fake-vision-model", "fake-vision-model"]),
        update_config=Mock(),
        set_model=Mock(),
        initialize=AsyncMock(),
        terminate=AsyncMock(),
        close=AsyncMock(),
    )
    context.get_provider_by_id.return_value = provider
    svc.vision_directory = VisionProviderDirectory(context)
    svc.catalog.fetch = AsyncMock()
    before = copy.deepcopy(dict(svc.raw_config))
    result = await svc.fetch_models({"kind": "vision", "provider_id": "loaded"})
    assert result["models"] == [
        {"id": "fake-vision-model", "label": "fake-vision-model"}
    ]
    context.get_provider_by_id.assert_called_once_with("loaded")
    provider.get_models.assert_awaited_once_with()
    svc.catalog.fetch.assert_not_awaited()
    await svc.close()
    provider.update_config.assert_not_called()
    provider.set_model.assert_not_called()
    provider.initialize.assert_not_awaited()
    provider.terminate.assert_not_awaited()
    provider.close.assert_not_awaited()
    assert dict(svc.raw_config) == before and svc.raw_config.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_id,status,looked_up",
    [
        ("disabled", 400, False),
        ("instance-only-orphan", 400, False),
        ("inherited-embedding", 400, False),
        ("pending", 503, True),
        (None, 400, False),
    ],
)
async def test_vision_query_rejects_unconfigured_disabled_and_unloaded_providers(
    provider_id, status, looked_up
):
    svc = catalog_service()
    context = host_context()
    context.get_provider_by_id.return_value = None
    svc.vision_directory = VisionProviderDirectory(context)
    svc.catalog.fetch_vision = AsyncMock()
    with pytest.raises(StudioServiceError) as error:
        await svc.fetch_models({"kind": "vision", "provider_id": provider_id})
    assert error.value.status_code == status
    assert context.get_provider_by_id.call_count == int(looked_up)
    svc.catalog.fetch_vision.assert_not_awaited()
    assert svc.raw_config.calls == []


@pytest.mark.asyncio
async def test_vision_query_does_not_accept_plugin_connection_overrides():
    svc = catalog_service()
    context = host_context()
    svc.vision_directory = VisionProviderDirectory(context)
    svc.catalog.fetch_vision = AsyncMock()
    with pytest.raises(StudioServiceError) as error:
        await svc.fetch_models(
            {"kind": "vision", "provider_id": "loaded", "api_keys": [FAKE_NEW_KEY]}
        )
    assert error.value.status_code == 400
    context.get_provider_by_id.assert_not_called()
    svc.catalog.fetch_vision.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route,method,handler,operation",
    [
        ("vision-providers", "GET", "vision_providers", "get_vision_providers"),
        ("providers/models", "POST", "provider_models", "fetch_models"),
    ],
)
async def test_new_web_routes_keep_envelope_no_store_and_close_guard(
    tmp_path, monkeypatch, route, method, handler, operation
):
    import tl.web_api as module

    api = _api(tmp_path, monkeypatch)
    context = _Context()
    api.register(context)
    registered = _match(
        context.registered_web_apis,
        f"astrbot_plugin_gemini_image_generation/webui/{route}",
        method,
    )
    assert registered is not None and registered[0] == getattr(api, handler)
    result = (
        {"confirmation_required": True, "target": "https://changed.invalid"}
        if method == "POST"
        else {"vision_providers": []}
    )
    action = AsyncMock(return_value=result)
    api.providers_service = SimpleNamespace(**{operation: action})
    body = {"kind": "entry", "revision": "fake-revision"}
    request = SimpleNamespace(method=method, json=AsyncMock(return_value=body))
    monkeypatch.setattr(module, "request", request)
    response = await registered[0]()
    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "ok", "data": result}
    assert response.headers["cache-control"] == "no-store"
    if method == "POST":
        action.assert_awaited_once_with(body)
    else:
        action.assert_awaited_once_with()
        request.json.assert_not_awaited()
    action.reset_mock()
    request.json.reset_mock()
    api._is_closed = lambda: True
    response = await registered[0]()
    assert response.status_code == 503
    assert json.loads(response.body)["status"] == "error"
    action.assert_not_awaited()
    request.json.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_models_real_service_preserves_http_200_confirmation_data(
    tmp_path, monkeypatch
):
    import tl.web_api as module

    api = _api(tmp_path, monkeypatch)
    svc = catalog_service()
    svc.catalog.fetch = AsyncMock(return_value=MODEL_RESULT)
    api.providers_service = svc
    body = await model_payload(svc)
    body["entry"]["values"]["api_base"] = (
        "https://changed.invalid/private?token=fake-token"
    )
    monkeypatch.setattr(
        module,
        "request",
        SimpleNamespace(
            method="POST", json=AsyncMock(side_effect=lambda **_: copy.deepcopy(body))
        ),
    )
    response = await api.provider_models()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert json.loads(response.body) == {
        "status": "ok",
        "data": {"confirmation_required": True, "target": "https://changed.invalid"},
    }
    svc.catalog.fetch.assert_not_awaited()
    body["confirmed_target"] = True
    response = await api.provider_models()
    assert json.loads(response.body) == {"status": "ok", "data": MODEL_RESULT}
    svc.catalog.fetch.assert_awaited_once()
    svc.raw_config["provider_settings"]["vision_model"] = "concurrent-edit"
    response = await api.provider_models()
    assert response.status_code == 409
    assert json.loads(response.body)["status"] == "error"
    assert svc.catalog.fetch.await_count == 1
    assert svc.raw_config.calls == []
