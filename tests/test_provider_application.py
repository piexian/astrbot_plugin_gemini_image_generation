from __future__ import annotations

import asyncio
import copy
import importlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tl.key_manager import KeyManager
from tl.plugin_config import ConfigLoader
from tl.provider_application import ProviderApplication
from tl.provider_runtime import ProviderRuntime
from tl.studio_providers import ProviderConfigService
from tl.tl_api import GeminiAPIClient
from tl.web_studio_service import StudioServiceError


def load_llm_tools(monkeypatch):
    """Extend conftest's logger stub only at the SDK boundary, not tool code."""
    if "tl.llm_tools" in sys.modules:
        return sys.modules["tl.llm_tools"]

    if "mcp" not in sys.modules:
        mcp = types.ModuleType("mcp")
        mcp.types = types.ModuleType("mcp.types")
        monkeypatch.setitem(sys.modules, "mcp", mcp)
        monkeypatch.setitem(sys.modules, "mcp.types", mcp.types)

    class GenericHostType:
        @classmethod
        def __class_getitem__(cls, item):
            return cls

    for name in (
        "astrbot.core",
        "astrbot.core.agent",
        "astrbot.core.agent.run_context",
        "astrbot.core.agent.tool",
        "astrbot.core.astr_agent_context",
    ):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    for module, name in (
        ("astrbot.core.agent.run_context", "ContextWrapper"),
        ("astrbot.core.agent.tool", "FunctionTool"),
        ("astrbot.core.agent.tool", "ToolExecResult"),
        ("astrbot.core.astr_agent_context", "AstrAgentContext"),
    ):
        if not hasattr(sys.modules[module], name):
            monkeypatch.setattr(
                sys.modules[module], name, GenericHostType, raising=False
            )
    return importlib.import_module("tl.llm_tools")


class MemoryKV:
    def __init__(self):
        self.value = None
        self.reads = 0
        self.writes = []
        self.read_error = False
        self.write_error = False

    async def get(self, name, default=None):
        assert name == KeyManager.KV_KEY
        self.reads += 1
        if self.read_error:
            raise OSError("fake KV read failure")
        return copy.deepcopy(self.value) if self.value is not None else default

    async def put(self, name, value):
        assert name == KeyManager.KV_KEY
        if self.write_error:
            raise OSError("fake KV write failure")
        self.writes.append(copy.deepcopy(value))
        self.value = copy.deepcopy(value)


class MemoryConfig(dict):
    def __init__(self, settings):
        super().__init__(provider_settings=copy.deepcopy(settings))
        self.saved = []

    async def save_config_async(self, patch):
        self.saved.append(copy.deepcopy(patch))
        self.update(copy.deepcopy(patch))
        return True


def entry(key="fake-key-old", *, api_type="google", **values):
    return {
        "__template_key": api_type,
        "api_keys": [key],
        "model": "fake-model",
        "daily_limit_per_key": 3,
        **values,
    }


def settings(*entries, **common):
    return {"provider_overrides": list(entries), "provider_polling": [], **common}


def config_for(value):
    return ConfigLoader({"provider_settings": copy.deepcopy(value)}).load()


def key_manager(value, kv):
    return KeyManager(config_for(value), get_kv=kv.get, put_kv=kv.put)


def usage(manager, scope, index=0):
    return manager.get_key_status(scope)["keys"][index]["usage_today"]


@pytest.fixture
def plugin_factory(monkeypatch):
    llm = load_llm_tools(monkeypatch)
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)

    def make(initial=None, *, client=True):
        if initial is None:
            initial = settings(
                entry(),
                vision_provider_id="old-vision",
                vision_model="old-vision-model",
                proxy="http://old-proxy.invalid:8080",
            )
        cfg = config_for(initial)
        cfg.total_timeout = 317
        cfg.group_limit_list = {"unchanged-group"}
        cfg.quick_mode_overrides = {"avatar": ("2K", "1:1")}
        kv = MemoryKV()
        manager = KeyManager(cfg, get_kv=kv.get, put_kv=kv.put)
        api = GeminiAPIClient(["fake-key-old"]) if client else None
        runtime = ProviderRuntime()
        if api:
            api.proxy = api._default_proxy = cfg.proxy
            api.set_provider_candidates(cfg.provider_candidates)
            api.set_key_manager(manager)
            api.provider_runtime = runtime
        modules = [SimpleNamespace(api_client=api) for _ in range(4)]
        vision = modules[2]
        vision.vision_provider_id = cfg.vision_provider_id
        vision.vision_model = cfg.vision_model

        def update_vision(**values):
            for name, value in values.items():
                setattr(vision, name, value)

        vision.update_config = update_vision
        plugin = SimpleNamespace(
            cfg=cfg,
            raw_config=MemoryConfig(initial),
            key_manager=manager,
            api_client=api,
            provider_runtime=runtime,
            image_handler=modules[0],
            image_generator=modules[1],
            vision_handler=vision,
            web_studio_service=modules[3],
            llm_image_tool=None,
        )

        def update_modules():
            for module in modules:
                module.api_client = plugin.api_client

        plugin._update_modules_api_client = update_modules
        plugin.llm_image_tool = llm.GeminiImageGenerationTool(plugin=plugin)
        plugin.llm_image_tool.refresh_from_plugin()
        return plugin, kv

    return make


def config_service(plugin):
    application = ProviderApplication(plugin)
    return ProviderConfigService(
        plugin.raw_config,
        SimpleNamespace(),
        config_lock=asyncio.Lock(),
        runtime=plugin.provider_runtime,
        prepare=application.prepare,
        apply=application.apply,
        restore=application.restore,
    )


async def update_payload(service):
    snapshot = await service.get_config()
    return {
        "revision": snapshot["revision"],
        "provider_polling": snapshot["provider_polling"],
        "entries": [
            {
                "id": item["id"],
                "api_type": item["api_type"],
                "values": {"model_alias": "edited-alias"},
                "secret_actions": {},
            }
            for item in snapshot["entries"]
        ],
        "common": {"values": {}, "secret_actions": {}},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read", "write"])
async def test_key_kv_failure_prevents_config_save_and_runtime_mutation(
    plugin_factory, failure
):
    plugin, kv = plugin_factory()
    setattr(kv, f"{failure}_error", True)
    service = config_service(plugin)
    before = copy.deepcopy(dict(plugin.raw_config))
    old_keys = plugin.key_manager
    close = AsyncMock(wraps=plugin.api_client.close)
    plugin.api_client.close = close
    with pytest.raises(StudioServiceError) as error:
        await service.save_config(await update_payload(service))
    assert error.value.status_code == 503
    assert "存储恢复" in str(error.value)
    assert plugin.raw_config.saved == []
    assert dict(plugin.raw_config) == before
    assert plugin.key_manager is old_keys
    assert plugin.cfg.provider_candidates[0].model_alias is None
    close.assert_not_awaited()
    assert not plugin.provider_runtime.updating
    assert not plugin.provider_runtime.failed


@pytest.mark.asyncio
async def test_failed_lazy_kv_read_cannot_be_bypassed_by_later_hot_save(plugin_factory):
    plugin, kv = plugin_factory(settings(entry(daily_limit_per_key=0)))
    kv.value = {
        "__shared_keys_v1": {
            "google": {
                "fake-key-old": {
                    "usage_count": 2,
                    "last_reset_date": plugin.key_manager._get_today_date(),
                }
            }
        }
    }
    original_usage = copy.deepcopy(kv.value)
    kv.read_error = True
    # Generation tolerates a KV outage, but this must not authorize overwriting
    # unknown persisted usage during a later configuration transaction.
    assert await plugin.key_manager.get_available_key("google#1") == "fake-key-old"
    assert kv.value == original_usage
    service = config_service(plugin)
    try:
        await service.save_config(await update_payload(service))
    except StudioServiceError:
        pass
    else:
        saved_usage = kv.value["__shared_keys_v1"]["google"]["fake-key-old"][
            "usage_count"
        ]
        pytest.fail(
            f"hot save bypassed failed KV read: config writes={len(plugin.raw_config.saved)}, "
            f"persisted usage changed from 2 to {saved_usage}"
        )
    assert plugin.raw_config.saved == []
    assert kv.writes == []
    assert kv.value == original_usage


@pytest.mark.asyncio
async def test_canonical_usage_survives_same_type_reorder_removal_readdition_and_restart():
    kv = MemoryKV()
    first = entry("fake-key-A", model="model-A", priority=0)
    second = entry("fake-key-B", model="model-B", priority=9)
    shared = entry("fake-key-A", model="shared-model", priority=1)
    initial = settings(first, second, shared)
    manager = key_manager(initial, kv)
    assert [item.id for item in manager.config.provider_candidates] == [
        "google#2",
        "google#3",
        "google#1",
    ]
    assert await manager.get_available_key("google#1") == "fake-key-A"
    assert await manager.get_available_key("google#3") == "fake-key-A"
    assert usage(manager, "google#1") == usage(manager, "google#3") == 2
    assert await manager.get_available_key("google#2") == "fake-key-B"
    reordered = settings(second, shared, first)
    reordered_manager = await manager.clone_for_config(config_for(reordered))
    assert usage(reordered_manager, "google#1") == 1
    assert (
        usage(reordered_manager, "google#2")
        == usage(reordered_manager, "google#3")
        == 2
    )
    assert await reordered_manager.get_available_key("google#2") == "fake-key-A"
    assert await reordered_manager.get_available_key("google#3") is None
    # Clone records must not alias the old runtime needed by rollback.
    assert usage(manager, "google#1") == 2
    removed = await reordered_manager.clone_for_config(config_for(settings(second)))
    assert removed.key_count("google#2") == 0
    await removed.clone_for_config(config_for(settings(second)))
    assert kv.value["__shared_keys_v1"]["google"]["fake-key-A"]["usage_count"] == 3
    restarted_removed = key_manager(settings(second), kv)
    readded = await restarted_removed.clone_for_config(config_for(initial))
    assert usage(readded, "google#1") == usage(readded, "google#3") == 3
    assert await readded.get_available_key("google#1") is None
    assert usage(readded, "google#2") == 1
    restarted_reordered = key_manager(reordered, kv)
    await restarted_reordered._load_from_kv(strict=True)
    assert usage(restarted_reordered, "google#1") == 1
    assert usage(restarted_reordered, "google#2") == 3
    assert usage(restarted_reordered, "google#3") == 3


@pytest.mark.asyncio
async def test_canonical_usage_isolated_by_type_but_shared_under_different_candidate_limits():
    kv = MemoryKV()
    manager = key_manager(
        settings(
            entry("fake-shared-key", daily_limit_per_key=1),
            entry("fake-shared-key", daily_limit_per_key=3),
            entry("fake-shared-key", api_type="xai", model="grok-imagine-image"),
        ),
        kv,
    )
    assert await manager.get_available_key("google#1") == "fake-shared-key"
    assert await manager.get_available_key("google#1") is None
    assert await manager.get_available_key("google#2") == "fake-shared-key"
    assert usage(manager, "google#1") == usage(manager, "google#2") == 2
    assert usage(manager, "xai#1") == 0
    assert await manager.get_available_key("xai#1") == "fake-shared-key"
    assert usage(manager, "xai#1") == 1
    restarted = KeyManager(manager.config, get_kv=kv.get, put_kv=kv.put)
    await restarted._load_from_kv(strict=True)
    assert usage(restarted, "google#1") == 2
    assert usage(restarted, "xai#1") == 1
