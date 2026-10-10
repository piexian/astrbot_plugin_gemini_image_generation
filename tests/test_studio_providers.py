from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from tl.provider_runtime import ProviderRuntime
from tl.studio_providers import ProviderConfigService
from tl.web_studio_service import StudioServiceError

FAKE_KEY = "fake-test-key-only"
FAKE_URL = (
    "https://fake-user:fake-password@example.invalid/v1?token=fake-token#fake-fragment"
)


class Config(dict):
    def __init__(self, settings):
        super().__init__(
            provider_settings=copy.deepcopy(settings),
            limit_settings={"unchanged": True},
        )
        self.disk = copy.deepcopy(dict(self))
        self.calls = []
        self._save_revision = 0
        self.started = asyncio.Event()
        self.release = None
        self.behaviors = []

    async def save_config_async(self, patch):
        assert set(patch) == {"provider_settings"}
        self.calls.append(copy.deepcopy(patch))
        self.update(copy.deepcopy(patch))
        self._save_revision += 1
        self.started.set()
        if self.release:
            await self.release.wait()
        behavior = self.behaviors.pop(0) if self.behaviors else None
        if isinstance(behavior, Exception):
            raise behavior
        if callable(behavior):
            return behavior(self)
        self.disk = copy.deepcopy(dict(self))
        return True


def service(settings=None, *, runtime=None, lock=None):
    if settings is None:
        settings = {
            "provider_polling": [],
            "provider_overrides": [
                {
                    "__template_key": "google",
                    "api_keys": [FAKE_KEY],
                    "model": "fake-model",
                    "priority": 4,
                },
                {
                    "__template_key": "google",
                    "api_keys": ["fake-second-key"],
                    "enabled": False,
                },
            ],
            "proxy": FAKE_URL,
            "legacy_common": {"private": "fake-private-common"},
        }
    raw = Config(settings)
    state = SimpleNamespace(current=copy.deepcopy(settings), prepared=[], restored=[])
    gate = runtime or ProviderRuntime()

    async def prepare(merged):
        assert gate.updating
        assert raw["provider_settings"] == state.current
        prepared = {"old": copy.deepcopy(state.current), "new": copy.deepcopy(merged)}
        state.prepared.append(prepared)
        return prepared

    def apply(prepared):
        assert gate.updating
        assert raw.disk["provider_settings"] == prepared["new"]
        state.current = copy.deepcopy(prepared["new"])

    def restore(prepared):
        assert gate.updating
        state.current = copy.deepcopy(prepared["old"])
        state.restored.append(prepared)

    svc = ProviderConfigService(
        raw,
        SimpleNamespace(),
        config_lock=lock or asyncio.Lock(),
        runtime=gate,
        prepare=prepare,
        apply=apply,
        restore=restore,
    )
    svc.test_state = state
    return svc


async def payload(svc):
    snapshot = await svc.get_config()
    return {
        "revision": snapshot["revision"],
        "provider_polling": snapshot["provider_polling"],
        "entries": [
            {
                "id": entry["id"],
                "api_type": entry["api_type"],
                "values": {},
                "secret_actions": {},
            }
            for entry in snapshot["entries"]
        ],
        "common": {"values": {}, "secret_actions": {}},
    }


@pytest.mark.asyncio
async def test_disk_failure_restores_memory_without_touching_runtime():
    svc = service()
    before = copy.deepcopy(dict(svc.raw_config))
    svc.raw_config.behaviors = [OSError(FAKE_KEY)]
    body = await payload(svc)
    body["entries"][0]["values"]["model"] = "changed"
    with pytest.raises(StudioServiceError) as exc:
        await svc.save_config(body)
    assert exc.value.status_code == 500 and FAKE_KEY not in str(exc.value)
    assert svc.raw_config == svc.raw_config.disk == before
    assert svc.test_state.current == before["provider_settings"]
    assert not svc.runtime.failed and not svc.runtime.updating


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [True, False])
async def test_host_newer_revision_is_never_overwritten(committed):
    svc = service()

    def host_save(raw):
        raw["provider_settings"] = {
            "provider_overrides": [],
            "proxy": "http://host.invalid",
        }
        raw["limit_settings"] = {"host_version": True}
        raw._save_revision += 1
        raw.disk = copy.deepcopy(dict(raw))
        return committed

    svc.raw_config.behaviors = [host_save]
    with pytest.raises(StudioServiceError) as exc:
        await svc.save_config(await payload(svc))
    assert exc.value.status_code == 409 and exc.value.data == {"reason": "revision"}
    assert len(svc.raw_config.calls) == 1
    assert svc.raw_config.disk == svc.raw_config
    assert svc.raw_config["provider_settings"]["proxy"] == "http://host.invalid"
    assert svc.runtime.failed and not svc.test_state.restored


@pytest.mark.asyncio
async def test_vision_providers_only_use_public_metadata_not_credentials():
    svc = service()
    provider = SimpleNamespace(
        meta=lambda: SimpleNamespace(
            id="fake-vision-provider", model="fake-vision-model"
        )
    )
    svc.context.get_all_providers = lambda: [provider]
    svc.context.get_config = lambda: {
        "provider": [
            {
                "id": "fake-vision-provider",
                "model": "fake-vision-model",
                "provider_type": "chat_completion",
                "key": ["fake-core-secret"],
            }
        ]
    }
    result = await svc.get_config()
    assert result["vision_providers"] == [
        {
            "id": "fake-vision-provider",
            "label": "fake-vision-provider",
            "model": "fake-vision-model",
            "source_id": "",
            "available": True,
        }
    ]
    assert "fake-core-secret" not in json.dumps(result)
    svc.context.get_config = lambda: (_ for _ in ()).throw(RuntimeError(FAKE_KEY))
    assert (await svc.get_config())["vision_providers"] == []
