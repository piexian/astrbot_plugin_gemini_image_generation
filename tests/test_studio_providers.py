from __future__ import annotations

import asyncio
import copy
import json
import threading
from types import SimpleNamespace

import pytest

from tl.provider_runtime import ProviderRuntime, ProviderRuntimeBusy
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
async def test_get_reads_all_raw_entries_and_masks_secrets_and_unknown_values():
    svc = service()
    raw = svc.raw_config["provider_settings"]
    raw["provider_overrides"][0].update(
        api_base=FAKE_URL,
        legacy_secret={"token": "fake-hidden-legacy"},
        model_alias={"token": "fake-hidden-malformed-known-field"},
    )
    raw["provider_overrides"].extend(
        [
            {
                "__template_key": "legacy_vendor",
                "api_keys": ["fake-unknown-key"],
                "legacy": "fake-hidden-unknown",
            },
            {"__template_key": "doubao", "enabled": False},
            "fake-malformed-entry-secret",
        ]
    )
    before = copy.deepcopy(raw)
    result = await svc.get_config()
    encoded = json.dumps(result)
    for hidden in (
        FAKE_URL,
        "fake-hidden-legacy",
        "fake-hidden-unknown",
        "fake-private-common",
        "fake-unknown-key",
        "fake-hidden-malformed-known-field",
        "fake-malformed-entry-secret",
        "fake-password",
        "fake-token",
    ):
        assert hidden not in encoded
    assert len(result["entries"]) == 5
    first, second, unknown, incomplete, malformed = result["entries"]
    assert first["secrets"]["api_keys"] == {"present": True, "count": 1}
    assert result["key_values_visible"] is True
    assert first["values"]["api_keys"] == [FAKE_KEY]
    assert second["values"]["api_keys"] == ["fake-second-key"]
    assert first["secrets"]["api_base"] == {"present": True, "preview": "已配置"}
    assert first["unknown_fields"] == ["legacy_secret"]
    assert "model_alias" not in first["values"]
    assert not second["values"]["enabled"]
    assert unknown["values"] == {} and not unknown["supported"]
    assert not incomplete["values"]["enabled"]
    assert not malformed["supported"]
    assert result["common"]["secrets"]["proxy"]["present"]
    assert "proxy" not in result["common"]["values"]
    assert raw == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid/?fake-query",
        "https://example.invalid/#fake-fragment",
        "https://fake-user@example.invalid",
        "http://fake-user:fake-password@example.invalid",
        "socks5://fake-user:fake-password@example.invalid:1080",
        "fake-user:fake-password@example.invalid:1080",
        "//fake-user:fake-password@example.invalid:1080",
        "  //fake-user:fake-password@example.invalid:1080",
        "https://example.invalid/?",
        "https://example.invalid/#",
        "https://[invalid",
    ],
)
async def test_sensitive_urls_are_masked_for_entries_and_common(url):
    svc = service(
        {
            "provider_overrides": [
                {"__template_key": "google", "api_base": url, "proxy": url}
            ],
            "proxy": url,
        }
    )
    result = await svc.get_config()
    assert url not in json.dumps(result)
    assert set(result["entries"][0]["secrets"]) == {"api_keys", "api_base", "proxy"}
    assert result["common"]["secrets"]["proxy"] == {
        "present": True,
        "preview": "已配置",
    }


@pytest.mark.asyncio
async def test_schema_is_complete_and_unconstrained_models_remain_free_text():
    svc = service()
    result = await svc.get_config()
    assert len(result["templates"]) == 14
    assert set(result["common_fields"]) == {
        "proxy",
        "vision_model",
        "vision_provider_id",
    }
    fields = result["templates"]["openai_images"]["fields"]
    assert fields["custom_size"]["condition"] == {"size_mode": "custom"}
    assert fields["output_compression"]["slider"] == {"min": 0, "max": 100, "step": 1}
    assert "options" not in fields["model"]
    assert result["templates"]["stepfun"]["fields"]["resolution"]["options"] == ["1K"]
    assert result["templates"]["minimax"]["fields"]["style_weight"]["type"] == "float"
    assert "api_base" not in result["entries"][0]["values"]
    assert result["templates"]["google"]["fields"]["api_base"]["default"]
    result["templates"]["google"]["fields"]["model"]["default"] = "mutated"
    assert (await svc.get_config())["templates"]["google"]["fields"]["model"][
        "default"
    ] != "mutated"


@pytest.mark.asyncio
async def test_reordering_uses_revision_bound_ids_without_mixing_keys_or_unknowns():
    svc = service()
    raw = svc.raw_config["provider_settings"]
    raw["provider_overrides"][0]["legacy"] = {"private": "fake-private-entry"}
    svc.test_state.current = copy.deepcopy(raw)
    body = await payload(svc)
    body["entries"].reverse()
    body["entries"][0]["values"]["model"] = "free-model-not-in-enum"
    body["provider_polling"] = ["doubao"]
    result = await svc.save_config(body)
    saved = svc.raw_config.disk["provider_settings"]
    assert saved["provider_overrides"][0]["api_keys"] == ["fake-second-key"]
    assert saved["provider_overrides"][1]["api_keys"] == [FAKE_KEY]
    assert saved["provider_overrides"][1]["legacy"] == {"private": "fake-private-entry"}
    assert saved["provider_overrides"][1]["priority"] == 4
    assert saved["legacy_common"] == raw["legacy_common"]
    assert saved["proxy"] == FAKE_URL
    assert saved["provider_polling"] == ["doubao"]
    assert svc.raw_config.disk["limit_settings"] == {"unchanged": True}
    assert result["revision"] != body["revision"]
    assert result["entries"][0]["id"] not in {entry["id"] for entry in body["entries"]}
    assert result["entries"][1]["values"]["api_keys"] == [FAKE_KEY]
    assert not result["busy"] and not result["requires_reload"]
    assert svc.test_state.current == saved


@pytest.mark.asyncio
async def test_secret_replace_clear_keep_and_new_defaults():
    svc = service()
    body = await payload(svc)
    body["entries"][0]["secret_actions"] = {
        "api_keys": {"mode": "replace", "value": ["fake-new-key", "fake-new-key-two"]},
        "api_base": {"mode": "replace", "value": FAKE_URL},
        "proxy": {"mode": "clear"},
    }
    body["entries"][1]["secret_actions"] = {"api_keys": {"mode": "clear"}}
    body["entries"].append(
        {
            "id": None,
            "api_type": "minimax",
            "values": {"enabled": False, "model": ""},
            "secret_actions": {"api_keys": {"mode": "keep"}},
        }
    )
    body["common"]["secret_actions"] = {"proxy": {"mode": "clear"}}
    result = await svc.save_config(body)
    saved = svc.raw_config["provider_settings"]["provider_overrides"]
    assert saved[0]["api_keys"] == ["fake-new-key", "fake-new-key-two"]
    assert saved[0]["api_base"] == FAKE_URL and saved[0]["proxy"] == ""
    assert saved[1]["api_keys"] == []
    assert (
        saved[2]["api_keys"] == []
        and saved[2]["api_base"] == "https://api.minimaxi.com"
    )
    assert svc.raw_config["provider_settings"]["proxy"] == ""
    assert result["entries"][0]["secrets"]["api_keys"]["count"] == 2
    assert result["entries"][0]["values"]["api_keys"] == [
        "fake-new-key",
        "fake-new-key-two",
    ]


@pytest.mark.asyncio
async def test_unknown_and_malformed_entries_can_be_kept_or_explicitly_deleted():
    original = [
        {"__template_key": "old_vendor", "token": {"nested": [FAKE_KEY]}},
        {"model": "fake-incomplete", "legacy": [FAKE_KEY]},
        "fake-malformed",
    ]
    svc = service({"provider_overrides": original})
    body = await payload(svc)
    await svc.save_config(body)
    assert svc.raw_config["provider_settings"]["provider_overrides"] == original
    body = await payload(svc)
    body["entries"].pop(1)
    await svc.save_config(body)
    assert svc.raw_config["provider_settings"]["provider_overrides"] == [
        original[0],
        original[2],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("priority", True),
        ("priority", 1.2),
        ("enabled", "false"),
        ("model", "x" * 16385),
        ("model", {}),
        ("resolution", "unknown-enum"),
        ("api_keys", FAKE_KEY),
        ("invented_private_field", FAKE_KEY),
    ],
)
async def test_invalid_known_values_are_rejected_without_mutating_or_echoing(
    field, value
):
    svc = service()
    before = copy.deepcopy(dict(svc.raw_config))
    body = await payload(svc)
    body["entries"][0]["values"][field] = value
    with pytest.raises(StudioServiceError) as exc:
        await svc.save_config(body)
    assert exc.value.status_code == 400
    assert "entries[1]" in exc.value.message and FAKE_KEY not in exc.value.message
    assert not svc.raw_config.calls and svc.raw_config == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        {"mode": "invalid"},
        {"mode": {}},
        {"mode": []},
        {"mode": "replace"},
        {"mode": "replace", "value": FAKE_KEY},
        {"mode": "replace", "value": [True]},
        {"mode": "replace", "value": ["x" * 8193]},
        {"mode": "replace", "value": [FAKE_KEY] * 201},
        {"mode": "keep", "value": FAKE_KEY},
        {"mode": "clear", "value": []},
        [],
        None,
    ],
)
async def test_invalid_secret_actions(action):
    svc = service()
    body = await payload(svc)
    body["entries"][0]["secret_actions"]["api_keys"] = action
    with pytest.raises(StudioServiceError) as exc:
        await svc.save_config(body)
    assert exc.value.status_code == 400 and FAKE_KEY not in exc.value.message
    assert not svc.raw_config.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["entries"].append(copy.deepcopy(p["entries"][0])),
        lambda p: p["entries"][0].update(id="google#1"),
        lambda p: p["entries"][0].update(api_type="doubao"),
        lambda p: p["entries"].append({"id": None, "api_type": "unknown"}),
        lambda p: p.update(entries=[{"id": None, "api_type": "google"}] * 101),
        lambda p: p.update(provider_polling=["google", "google"]),
        lambda p: p.update(provider_polling=["unknown"]),
        lambda p: p.update(provider_polling=[{}]),
        lambda p: p["common"]["values"].update(proxy=""),
        lambda p: p["common"]["secret_actions"].update(api_keys={"mode": "clear"}),
        lambda p: p["common"]["values"].update(limits={}),
    ],
)
async def test_identity_polling_and_common_boundaries(mutation):
    svc = service()
    body = await payload(svc)
    mutation(body)
    with pytest.raises(StudioServiceError) as exc:
        await svc.save_config(body)
    assert exc.value.status_code == 400 and not svc.raw_config.calls


@pytest.mark.asyncio
async def test_protected_url_requires_actions_and_values_actions_cannot_overlap():
    svc = service()
    body = await payload(svc)
    body["entries"][0]["values"]["api_base"] = "https://example.invalid"
    body["entries"][0]["secret_actions"]["api_base"] = {"mode": "keep"}
    with pytest.raises(StudioServiceError):
        await svc.save_config(body)
    body = await payload(svc)
    body["common"]["values"]["proxy"] = ""
    body["common"]["secret_actions"]["proxy"] = {"mode": "clear"}
    with pytest.raises(StudioServiceError):
        await svc.save_config(body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("n", 10),
        ("n", 0),
        ("style_weight", float("nan")),
        ("style_weight", float("inf")),
        ("style_weight", True),
    ],
)
async def test_sliders_and_finite_numbers(field, value):
    svc = service()
    body = await payload(svc)
    body["entries"] = [{"id": None, "api_type": "minimax", "values": {field: value}}]
    with pytest.raises(StudioServiceError):
        await svc.save_config(body)
    assert not svc.raw_config.calls


@pytest.mark.asyncio
async def test_unknown_provider_edit_is_rejected():
    svc = service(
        {
            "provider_overrides": [
                {"__template_key": "old_vendor", "api_keys": [FAKE_KEY]}
            ]
        }
    )
    body = await payload(svc)
    body["entries"][0]["values"] = {"enabled": False}
    with pytest.raises(StudioServiceError):
        await svc.save_config(body)
    body["entries"][0]["values"] = {}
    body["entries"][0]["secret_actions"] = {"api_keys": {"mode": "clear"}}
    with pytest.raises(StudioServiceError):
        await svc.save_config(body)


@pytest.mark.asyncio
async def test_revision_and_busy_conflicts_are_distinguishable():
    svc = service()
    body = await payload(svc)
    with svc.runtime.operation():
        assert (await svc.get_config())["busy"]
        with pytest.raises(StudioServiceError) as exc:
            await svc.save_config(body)
        assert exc.value.status_code == 409 and exc.value.data == {"reason": "busy"}
        assert "生成任务正在运行" in exc.value.message
    stale = copy.deepcopy(body)
    stale["revision"] = "stale"
    with pytest.raises(StudioServiceError) as exc:
        await svc.save_config(stale)
    assert exc.value.status_code == 409 and exc.value.data == {"reason": "revision"}
    assert not svc.raw_config.calls
    await svc.save_config(body)


@pytest.mark.asyncio
async def test_shared_lock_serializes_editors_and_preserves_limits():
    lock = asyncio.Lock()
    svc = service(lock=lock)
    one, two = await payload(svc), await payload(svc)
    one["entries"][0]["values"]["model"] = "first"
    two["entries"][0]["values"]["model"] = "second"
    async with lock:
        task = asyncio.create_task(svc.save_config(one))
        await asyncio.sleep(0)
        assert not svc.raw_config.calls and not svc.runtime.updating
        svc.raw_config["limit_settings"] = {"updated_elsewhere": True}
    results = await asyncio.gather(task, svc.save_config(two), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    conflict = next(
        result for result in results if isinstance(result, StudioServiceError)
    )
    assert conflict.data == {"reason": "revision"}
    assert svc.raw_config.disk["limit_settings"] == {"updated_elsewhere": True}


@pytest.mark.asyncio
async def test_cancelled_save_keeps_gate_closed_until_disk_and_runtime_finish():
    svc = service()
    svc.raw_config.release = asyncio.Event()
    body = await payload(svc)
    body["entries"][0]["values"]["model"] = "changed"
    task = asyncio.create_task(svc.save_config(body))
    await svc.raw_config.started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and svc.runtime.updating
    with pytest.raises(ProviderRuntimeBusy):
        with svc.runtime.operation():
            pytest.fail("generation must not enter")
    reader = asyncio.create_task(svc.get_config())
    await asyncio.sleep(0)
    assert not reader.done()
    svc.raw_config.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not svc.runtime.updating
    assert (await reader)["entries"][0]["values"]["model"] == "changed"
    assert svc.raw_config.disk["provider_settings"] == svc.test_state.current


@pytest.mark.asyncio
async def test_prepare_failure_never_persists_or_echoes_raw_exception():
    svc = service()

    async def prepare(_settings):
        raise ValueError(FAKE_URL)

    svc.prepare = prepare
    with pytest.raises(StudioServiceError) as exc:
        await svc.save_config(await payload(svc))
    assert exc.value.status_code == 400 and FAKE_URL not in str(exc.value)
    assert not svc.raw_config.calls and not svc.runtime.updating


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
@pytest.mark.parametrize("rollback_failure", [None, "disk", "restore"])
async def test_apply_failure_rolls_back_disk_and_runtime_or_closes_admission(
    rollback_failure,
):
    svc = service()
    before = copy.deepcopy(dict(svc.raw_config))
    body = await payload(svc)
    body["entries"][0]["values"]["model"] = "changed"

    def fail_apply(prepared):
        svc.test_state.current = prepared["new"]
        raise RuntimeError(FAKE_KEY)

    svc.apply = fail_apply
    if rollback_failure == "disk":
        svc.raw_config.behaviors = [None, OSError(FAKE_URL)]
    elif rollback_failure == "restore":

        def fail_restore(_prepared):
            raise RuntimeError(FAKE_KEY)

        svc.restore = fail_restore
    with pytest.raises(StudioServiceError) as exc:
        await svc.save_config(body)
    assert exc.value.status_code == 500
    assert FAKE_KEY not in str(exc.value) and FAKE_URL not in str(exc.value)
    assert len(svc.raw_config.calls) == 2
    assert svc.raw_config == before
    if rollback_failure != "disk":
        assert svc.raw_config.disk == before
    if rollback_failure != "restore":
        assert svc.test_state.current == before["provider_settings"]
        assert svc.test_state.restored[0] is svc.test_state.prepared[0]
    assert svc.runtime.failed == bool(rollback_failure)
    assert (await svc.get_config())["requires_reload"] == bool(rollback_failure)
    if rollback_failure:
        with pytest.raises(ProviderRuntimeBusy):
            with svc.runtime.operation():
                pytest.fail("failed runtime must reject generation")


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
async def test_sync_host_save_runs_off_event_loop():
    svc = service()
    loop_thread = threading.get_ident()

    class SyncConfig(dict):
        def save_config(self, patch):
            assert threading.get_ident() != loop_thread
            self.update(copy.deepcopy(patch))
            self.disk = copy.deepcopy(dict(self))

    svc.raw_config = SyncConfig(svc.raw_config)
    svc.raw_config.disk = copy.deepcopy(dict(svc.raw_config))
    svc.apply = lambda prepared: setattr(svc.test_state, "current", prepared["new"])
    await svc.save_config(await payload(svc))
    assert svc.raw_config.disk["provider_settings"] == svc.test_state.current


@pytest.mark.asyncio
async def test_read_only_closed_failed_and_background_busy_hosts():
    svc = service()
    svc.raw_config = dict(svc.raw_config)
    with pytest.raises(StudioServiceError) as exc:
        await svc.save_config(await payload(svc))
    assert exc.value.status_code == 503
    for flag in ("closed", "failed"):
        svc = service()
        setattr(svc.runtime, flag, True)
        with pytest.raises(StudioServiceError) as exc:
            await svc.save_config(await payload(svc))
        assert exc.value.status_code == 503 and not svc.raw_config.calls
    svc = service(runtime=ProviderRuntime(lambda: True))
    with pytest.raises(StudioServiceError) as exc:
        await svc.save_config(await payload(svc))
    assert exc.value.data == {"reason": "busy"}


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


@pytest.mark.asyncio
async def test_gate_already_holds_during_prepare_and_host_change_aborts_before_write():
    svc = service()
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(_settings):
        entered.set()
        await release.wait()
        return object()

    svc.prepare = prepare
    task = asyncio.create_task(svc.save_config(await payload(svc)))
    await entered.wait()
    assert svc.runtime.updating
    with pytest.raises(ProviderRuntimeBusy):
        with svc.runtime.operation():
            pytest.fail("preparation must block generation admission")
    svc.raw_config["provider_settings"]["vision_model"] = "external-host-model"
    release.set()
    with pytest.raises(StudioServiceError) as exc:
        await task
    assert exc.value.status_code == 409 and exc.value.data["reason"] == "revision"
    assert not svc.raw_config.calls and svc.runtime.failed


@pytest.mark.asyncio
async def test_committed_false_even_with_matching_memory_does_not_apply_or_rollback():
    svc = service()
    svc.raw_config.behaviors = [lambda raw: False]
    body = await payload(svc)
    body["entries"][0]["values"]["model"] = "uncommitted"
    with pytest.raises(StudioServiceError) as exc:
        await svc.save_config(body)
    assert exc.value.status_code == 409 and len(svc.raw_config.calls) == 1
    assert svc.test_state.current["provider_overrides"][0]["model"] == "fake-model"
    assert svc.runtime.failed and not svc.test_state.restored


@pytest.mark.asyncio
async def test_disk_exception_with_newer_host_revision_does_not_restore_old_snapshot():
    svc = service()

    def host_save(raw):
        raw._save_revision += 1
        raw["limit_settings"] = {"host_updated": True}
        raw.disk = copy.deepcopy(dict(raw))
        raise OSError(FAKE_KEY)

    svc.raw_config.behaviors = [host_save]
    body = await payload(svc)
    body["entries"][0]["values"]["model"] = "host-also-kept-this-update"
    with pytest.raises(StudioServiceError) as exc:
        await svc.save_config(body)
    assert exc.value.status_code == 409 and exc.value.data["reason"] == "revision"
    assert svc.raw_config == svc.raw_config.disk
    assert len(svc.raw_config.calls) == 1 and svc.runtime.failed


@pytest.mark.asyncio
async def test_secret_limits_allow_exact_maxima_and_logging_never_receives_secrets(
    monkeypatch,
):
    import tl.studio_providers as module

    logged = []

    def record(message, *args, **kwargs):
        logged.append((message, args, kwargs))

    monkeypatch.setattr(module, "logger", SimpleNamespace(info=record, warning=record))
    svc = service()
    body = await payload(svc)
    body["entries"][0]["secret_actions"]["api_keys"] = {
        "mode": "replace",
        "value": [str(index).zfill(3) + "k" * 8189 for index in range(200)],
    }
    body["entries"][0]["values"]["model"] = "m" * 16384
    result = await svc.save_config(body)
    assert result["entries"][0]["secrets"]["api_keys"]["count"] == 200
    svc.raw_config.behaviors = [OSError(FAKE_URL + FAKE_KEY)]
    body = await payload(svc)
    body["entries"][0]["secret_actions"]["api_keys"] = {
        "mode": "replace",
        "value": [FAKE_KEY],
    }
    with pytest.raises(StudioServiceError):
        await svc.save_config(body)
    encoded = json.dumps(logged)
    assert (
        FAKE_KEY not in encoded
        and FAKE_URL not in encoded
        and "k" * 8192 not in encoded
    )
    assert all(not kwargs.get("exc_info") for _, _, kwargs in logged)


@pytest.mark.asyncio
async def test_string_typed_api_keys_round_trips_as_string():
    """vertex 单凭证 api_keys（string 型）在工作台快照与保存中保持字符串形态。"""
    settings = {
        "provider_polling": [],
        "provider_overrides": [
            {
                "__template_key": "vertex",
                "api_keys": FAKE_KEY,
                "model": "gemini-3-pro-image",
            },
            {
                "__template_key": "vertex",
                "service_account_files": ["files/vertex/sa.json"],
                "model": "gemini-3-pro-image",
            },
        ],
    }
    svc = service(settings)
    snapshot = await svc.get_config()
    vertex_entries = [e for e in snapshot["entries"] if e["api_type"] == "vertex"]
    assert vertex_entries[0]["values"]["api_keys"] == FAKE_KEY
    assert isinstance(vertex_entries[0]["values"]["api_keys"], str)
    assert "api_keys" not in vertex_entries[1]["values"]

    body = await payload(svc)
    body["entries"][0]["values"]["api_keys"] = "fake-replaced-key"
    result = await svc.save_config(body)
    saved = result["entries"][0]["values"]["api_keys"]
    assert saved == "fake-replaced-key" and isinstance(saved, str)
    assert (
        svc.raw_config.calls[-1]["provider_settings"]["provider_overrides"][0][
            "api_keys"
        ]
        == "fake-replaced-key"
    )

    # 列表型 api_keys 传字符串仍然拒绝（类型不匹配不回显）
    google_settings = {
        "provider_polling": [],
        "provider_overrides": [
            {
                "__template_key": "google",
                "api_keys": [FAKE_KEY],
                "model": "fake-model",
            }
        ],
    }
    google_svc = service(google_settings)
    google_snapshot = await google_svc.get_config()
    google_body = {
        "revision": google_snapshot["revision"],
        "provider_polling": google_snapshot["provider_polling"],
        "entries": [
            {
                "id": google_snapshot["entries"][0]["id"],
                "api_type": "google",
                "values": {"api_keys": FAKE_KEY},
                "secret_actions": {},
            }
        ],
        "common": {"values": {}, "secret_actions": {}},
    }
    with pytest.raises(StudioServiceError) as exc:
        await google_svc.save_config(google_body)
    assert exc.value.status_code == 400
    assert FAKE_KEY not in exc.value.message
