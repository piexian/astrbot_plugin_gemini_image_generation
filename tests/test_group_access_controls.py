"""群名单与限流统一编辑的边界和热更新回归。"""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from tests.test_studio_limits import payload, service
from tl.limit_config import normalize_limits
from tl.plugin_config import ConfigLoader, PluginConfig
from tl.web_studio_service import StudioServiceError


def group_event(group_id="123"):
    return SimpleNamespace(
        group_id=group_id, unified_msg_origin=f"bot:GroupMessage:{group_id}"
    )


@pytest.mark.asyncio
async def test_access_snapshot_save_backup_and_live_decisions(tmp_path):
    svc = service(tmp_path)
    body = payload(svc)
    assert body["limits"]["group_limit_mode"] == "blacklist"
    assert body["limits"]["group_limit_list"] == ["blocked"]
    body["limits"].update(
        group_limit_mode="whitelist", group_limit_list=[" 123 ", "123", "", "abc"]
    )
    await svc.save_limits(body)
    assert svc.config.group_limit_list == {"123", "abc"}
    assert svc.limiter.allows_group(group_event())
    assert not svc.limiter.allows_group(group_event("other"))
    assert svc.limiter.allows_group(SimpleNamespace(group_id=""))
    assert (await svc.limiter.acquire(None)).allowed
    assert svc.raw_config.disk["limit_settings"]["group_limit_list"] == ["123", "abc"]
    backup = json.loads((tmp_path / "group_access_config_backup.json").read_text())
    assert backup == {"group_limit_mode": "blacklist", "group_limit_list": ["blocked"]}
    assert "provider_settings" not in backup
    body = payload(svc)
    body["limits"]["group_limit_mode"] = "none"
    await svc.save_limits(body)
    assert svc.config.group_limit_list == {"123", "abc"}
    assert svc.limiter.allows_group(group_event("other"))
    assert (
        json.loads((tmp_path / "group_access_config_backup.json").read_text()) == backup
    )


@pytest.mark.asyncio
async def test_old_client_omission_preserves_group_settings(tmp_path):
    svc = service(tmp_path)
    body = payload(svc)
    body["limits"].pop("group_limit_mode")
    body["limits"].pop("group_limit_list")
    body["limits"]["global_rate_limit"]["enabled"] = True
    await svc.save_limits(body)
    assert svc.config.group_limit_mode == "blacklist"
    assert svc.config.group_limit_list == {"blocked"}
    assert svc.raw_config.disk["limit_settings"]["group_limit_list"] == ["blocked"]
    assert not (tmp_path / "group_access_config_backup.json").exists()


@pytest.mark.asyncio
async def test_failed_group_save_rolls_back_both_runtime_and_disk(tmp_path):
    svc = service(tmp_path)
    original = copy.deepcopy(dict(svc.raw_config))
    body = payload(svc)
    body["limits"].update(group_limit_mode="whitelist", group_limit_list=["123"])
    svc.raw_config.fail = True
    with pytest.raises(StudioServiceError):
        await svc.save_limits(body)
    assert svc.config.group_limit_mode == "blacklist"
    assert svc.config.group_limit_list == {"blocked"}
    assert svc.raw_config == svc.raw_config.disk == original


@pytest.mark.asyncio
async def test_group_permission_is_rechecked_after_waiting_for_config_lock(tmp_path):
    svc = service(tmp_path)
    observed = asyncio.Event()
    original_check = svc.limiter.allows_group

    def check(event):
        observed.set()
        return original_check(event)

    svc.limiter.allows_group = check
    async with svc.limiter._rate_limit_lock:
        task = asyncio.create_task(svc.limiter.check_and_consume(group_event()))
        await observed.wait()
        svc.config.group_limit_list = {"123"}
    assert await task == (False, None)
    assert not svc.limiter._rate_limit_buckets


@pytest.mark.parametrize(
    "value",
    [
        {"group_limit_mode": "none"},
        {"group_limit_list": []},
        {"group_limit_mode": "invalid", "group_limit_list": []},
        {"group_limit_mode": [], "group_limit_list": []},
        {"group_limit_mode": "none", "group_limit_list": "123"},
        {"group_limit_mode": "none", "group_limit_list": [123]},
        {"group_limit_mode": "none", "group_limit_list": [True]},
        {"group_limit_mode": "none", "group_limit_list": ["a\nb"]},
        {"group_limit_mode": "none", "group_limit_list": ["x"] * 1001},
    ],
)
def test_invalid_access_payload_is_rejected(value):
    with pytest.raises(ValueError):
        normalize_limits(value)


def test_legacy_group_config_loads_and_empty_whitelist_keeps_original_semantics(
    tmp_path,
):
    cfg = PluginConfig()
    ConfigLoader(
        {
            "limit_settings": {
                "group_limit_mode": "Whitelist",
                "group_limit_list": [123, " 456 "],
            }
        }
    )._load_limit_settings(cfg)
    assert cfg.group_limit_mode == "whitelist"
    assert cfg.group_limit_list == {"123", "456"}
    svc = service(tmp_path)
    svc.config.group_limit_mode = "whitelist"
    svc.config.group_limit_list = set()
    assert svc.limiter.allows_group(group_event())
