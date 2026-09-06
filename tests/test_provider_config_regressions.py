from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_provider_application import MemoryKV, entry, key_manager, settings, usage
from test_provider_config_frontend import _run
from test_studio_providers import service
from test_web_api import _api

from tl.plugin_config import ConfigLoader
from tl.web_studio_service import StudioServiceError


async def payload(svc):
    """回传页面展示过的字段，复现真实表单保存而非最小补丁。"""
    snapshot = await svc.get_config()
    return {
        "revision": snapshot["revision"],
        "provider_polling": snapshot["provider_polling"],
        "entries": [
            {
                "id": item["id"],
                "api_type": item["api_type"],
                "values": item["values"],
                "secret_actions": {},
            }
            for item in snapshot["entries"]
        ],
        "common": {"values": snapshot["common"]["values"], "secret_actions": {}},
    }


@pytest.mark.asyncio
async def test_polling_save_preserves_missing_fields_and_doubao_size_migration():
    original = {
        "__template_key": "doubao",
        "endpoint_id": "doubao-seedream-5-0-260128",
        "api_keys": ["fake-key-only"],
        "default_size": "4K",
    }
    svc = service({"provider_overrides": [original], "provider_polling": []})
    before = copy.deepcopy(svc.raw_config["provider_settings"])
    body = await payload(svc)
    assert "size" not in body["entries"][0]["values"]
    body["provider_polling"] = ["doubao"]
    await svc.save_config(body)
    saved = svc.raw_config["provider_settings"]
    assert saved["provider_overrides"] == before["provider_overrides"]
    loaded = ConfigLoader({"provider_settings": saved}).load()
    assert loaded.doubao_settings["size"] == "4K"


@pytest.mark.asyncio
async def test_legacy_scalars_and_polling_aliases_keep_effective_meaning():
    svc = service(
        {
            "provider_overrides": [
                {"__template_key": "google", "enabled": "false", "priority": "7"}
            ],
            "provider_polling": ["Google", "google", "removed-vendor"],
        }
    )
    snapshot = await svc.get_config()
    assert snapshot["entries"][0]["values"] == {"enabled": False, "priority": 7}
    assert snapshot["provider_polling"] == ["google", "removed_vendor"]
    await svc.save_config(await payload(svc))
    assert (
        svc.raw_config["provider_settings"]["provider_overrides"][0]["enabled"] is False
    )
    body = await payload(svc)
    body["provider_polling"].append("new-unknown-vendor")
    with pytest.raises(StudioServiceError):
        await svc.save_config(body)


@pytest.mark.asyncio
async def test_unchanged_legacy_enum_is_preserved_but_new_invalid_value_is_rejected():
    svc = service(
        {
            "provider_overrides": [
                {"__template_key": "doubao", "enabled": False, "endpoint_mode": "plan"}
            ],
            "provider_polling": [],
        }
    )
    await svc.save_config(await payload(svc))
    assert (
        svc.raw_config["provider_settings"]["provider_overrides"][0]["endpoint_mode"]
        == "plan"
    )
    body = await payload(svc)
    body["entries"][0]["values"]["endpoint_mode"] = "not-a-mode"
    with pytest.raises(StudioServiceError):
        await svc.save_config(body)
    body["entries"][0]["values"] = {"enabled": 0}
    with pytest.raises(StudioServiceError):
        await svc.save_config(body)


@pytest.mark.asyncio
async def test_kv_read_outage_blocks_limited_keys_and_recovers_without_zeroing_usage():
    kv = MemoryKV()
    manager = key_manager(settings(entry()), kv)
    kv.value = {
        "__shared_keys_v1": {
            "google": {
                "fake-key-old": {
                    "usage_count": 2,
                    "last_reset_date": manager._get_today_date(),
                }
            }
        }
    }
    previous = copy.deepcopy(kv.value)
    kv.read_error = True
    assert await manager.get_available_key("google#1") is None
    assert not manager._loaded
    assert kv.value == previous and not kv.writes
    kv.read_error = False
    assert await manager.get_available_key("google#1") == "fake-key-old"
    assert usage(manager, "google#1") == 3
    assert await manager.get_available_key("google#1") is None


@pytest.mark.asyncio
async def test_concurrent_initial_key_load_is_single_flight():
    kv = MemoryKV()
    manager = key_manager(settings(entry()), kv)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_get(*args):
        value = await kv.get(*args)
        entered.set()
        await release.wait()
        return value

    manager._get_kv = delayed_get
    first = asyncio.create_task(manager.get_available_key("google#1"))
    await entered.wait()
    second = asyncio.create_task(manager.get_available_key("google#1"))
    release.set()
    assert await asyncio.gather(first, second) == ["fake-key-old", "fake-key-old"]
    assert kv.reads == 1 and usage(manager, "google#1") == 2


@pytest.mark.asyncio
async def test_providers_api_uses_standard_envelope_and_sanitizes_unexpected_errors(
    tmp_path, monkeypatch
):
    import tl.web_api as module

    api = _api(tmp_path, monkeypatch)
    request = SimpleNamespace(
        method="GET", headers={}, json=AsyncMock(return_value={"revision": "one"})
    )
    monkeypatch.setattr(module, "request", request)
    assert (await api.providers()).status_code == 503
    api.providers_service = SimpleNamespace(
        get_config=AsyncMock(return_value={"revision": "one"}),
        save_config=AsyncMock(return_value={"revision": "two"}),
    )
    response = await api.providers()
    assert json.loads(response.body) == {"status": "ok", "data": {"revision": "one"}}
    request.method = "POST"
    response = await api.providers()
    api.providers_service.save_config.assert_awaited_once_with({"revision": "one"})
    assert json.loads(response.body)["data"]["revision"] == "two"
    api.providers_service.save_config.side_effect = RuntimeError("fake-private-error")
    response = await api.providers()
    assert response.status_code == 500
    assert b"fake-private-error" not in response.body
    api._web_closed = True
    assert (await api.providers()).status_code == 503


def test_frontend_allows_manual_vision_id_and_explains_safe_validation_failure():
    _run(r"""
const view = create(); await view.open(); selectTab('common');
assert.equal(field('vision_provider_id', root).tagName, 'select');
action('manual-vision').click();
assert.equal(field('vision_provider_id', root).tagName, 'input');
input(field('vision_provider_id', root), 'manual-unlisted-vision');
post = async () => { throw Object.assign(new Error('启用的供应商配置缺少有效模型或 API Key'), {status: 'error', statusCode: 400}); };
action('save').click(); await tick();
assert.match(view.message, /缺少有效模型或 API Key/);
assert.equal(view.dirty, true); assert.equal(view.conflict, false);
assert.equal(posts()[0].payload.common.values.vision_provider_id, 'manual-unlisted-vision');
post = async () => { throw Object.assign(new Error('供应商运行时不可用，请重载插件'), {status: 'error', statusCode: 503}); };
action('save').click(); await tick();
assert.equal(view.snapshot.requires_reload, true); assert.equal(action('save').disabled, true);
view.destroy();
""")


def test_frontend_does_not_force_rewriting_unchanged_legacy_enum():
    _run(r"""
server.entries[0].api_type = 'doubao';
server.entries[0].values = {enabled: false, endpoint_mode: 'plan'};
const view = create(); await view.open(); selectTab('common');
input(field('vision_model', root), 'new-vision-model');
action('save').click(); await tick();
assert.equal(posts().length, 1);
assert.equal(posts()[0].payload.entries[0].values.endpoint_mode, 'plan');
assert.equal(view.dirty, false); view.destroy();
""")
