from __future__ import annotations

import asyncio
import copy
import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tl.limit_config import apply_limits, normalize_limits
from tl.rate_limiter import RateLimiter
from tl.studio_limits import StudioLimitsService
from tl.web_studio_service import StudioServiceError

UMO = "a:GroupMessage:1"


class Config(dict):
    def __init__(self, limits):
        super().__init__(
            limit_settings=copy.deepcopy(limits),
            provider_settings={"api_keys": ["test-secret"]},
        )
        self.disk = copy.deepcopy(dict(self))
        self.fail = False
        self.started = asyncio.Event()
        self.release = None

    async def save_config_async(self, patch):
        self.update(patch)
        self.started.set()
        if self.release:
            await self.release.wait()
        if self.fail:
            raise OSError("disk full")
        self.disk = copy.deepcopy(dict(self))
        return True


def service(tmp_path, *, rules=None):
    limits = normalize_limits(
        {
            "group_limit_mode": "blacklist",
            "group_limit_list": ["blocked"],
            "default_rate_limit": {"enabled": True, "max_requests": 2},
            "rate_limit_rules": rules or [],
        }
    )
    cfg = SimpleNamespace(group_limit_mode="none", group_limit_list=set())
    apply_limits(cfg, limits)
    raw = Config(
        {**limits, "group_limit_mode": "blacklist", "group_limit_list": ["blocked"]}
    )
    limiter = RateLimiter(cfg)
    return StudioLimitsService(raw, cfg, limiter, SimpleNamespace(), tmp_path)


def payload(svc):
    snapshot = svc.get_limits()
    return {"revision": snapshot["revision"], "limits": snapshot["limits"]}


@pytest.mark.asyncio
async def test_save_is_narrow_immediate_and_retains_existing_counts(tmp_path):
    svc = service(tmp_path)
    await svc.limiter.acquire(UMO)
    body = payload(svc)
    body["limits"]["default_rate_limit"]["max_requests"] = 1
    result = await svc.save_limits(body)
    assert result["revision"] != body["revision"]
    assert not (await svc.limiter.acquire(UMO)).allowed
    assert svc.raw_config.disk["limit_settings"]["group_limit_list"] == ["blocked"]
    assert svc.raw_config.disk["provider_settings"]["api_keys"] == ["test-secret"]
    backup = (tmp_path / "rate_limit_config_backup.json").read_text()
    assert "test-secret" not in backup
    assert json.loads(backup)["default_rate_limit"]["max_requests"] == 2
    assert "provider_settings" not in result
    with pytest.raises(StudioServiceError) as exc:
        await svc.save_limits(body)
    assert exc.value.status_code == 409
    assert svc.config.default_rate_limit["max_requests"] == 1


@pytest.mark.asyncio
async def test_failed_save_restores_raw_and_runtime_config(tmp_path):
    svc = service(tmp_path)
    old = copy.deepcopy(dict(svc.raw_config))
    body = payload(svc)
    body["limits"]["global_rate_limit"]["enabled"] = True
    svc.raw_config.fail = True
    with pytest.raises(StudioServiceError) as exc:
        await svc.save_limits(body)
    assert exc.value.status_code == 500
    assert svc.raw_config == svc.raw_config.disk == old
    assert not svc.config.global_rate_limit["enabled"]


@pytest.mark.asyncio
async def test_cancelled_request_finishes_config_transaction(tmp_path):
    svc = service(tmp_path)
    svc.raw_config.release = asyncio.Event()
    body = payload(svc)
    body["limits"]["global_rate_limit"]["enabled"] = True
    task = asyncio.create_task(svc.save_limits(body))
    await svc.raw_config.started.wait()
    task.cancel()
    svc.raw_config.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert svc.config.global_rate_limit["enabled"]
    assert svc.raw_config.disk["limit_settings"]["global_rate_limit"]["enabled"]


@pytest.mark.asyncio
async def test_simultaneous_editors_conflict_without_losing_unrelated_config(tmp_path):
    svc = service(tmp_path)
    first, second = payload(svc), payload(svc)
    first["limits"]["global_rate_limit"]["enabled"] = True
    second["limits"]["default_rate_limit"]["max_requests"] = 100
    results = await asyncio.gather(
        svc.save_limits(first), svc.save_limits(second), return_exceptions=True
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    conflict = next(
        result for result in results if isinstance(result, StudioServiceError)
    )
    assert conflict.status_code == 409
    assert svc.raw_config.disk["provider_settings"]["api_keys"] == ["test-secret"]


@pytest.mark.asyncio
async def test_legacy_rules_require_explicit_migration_and_keep_backup(tmp_path):
    svc = service(tmp_path, rules=[{"group_ids": ["123"]}])
    assert svc.get_limits()["migration"]["pending"]
    with pytest.raises(StudioServiceError):
        await svc.save_limits(payload(svc))
    body = payload(svc)
    body["limits"]["rate_limit_rules"][0].update(umos=[UMO], group_ids=[])
    result = await svc.save_limits(body)
    assert not result["migration"]["pending"]
    saved_rule = svc.raw_config.disk["limit_settings"]["rate_limit_rules"][0]
    assert saved_rule["__template_key"] == "rule"
    assert json.loads((tmp_path / "rate_limit_config_backup.json").read_text())[
        "rate_limit_rules"
    ][0]["group_ids"] == ["123"]


@pytest.mark.asyncio
async def test_readonly_host_and_closed_limiter_cannot_save(tmp_path):
    svc = service(tmp_path)
    svc.raw_config = dict(svc.raw_config)
    with pytest.raises(StudioServiceError) as exc:
        await svc.save_limits(payload(svc))
    assert exc.value.status_code == 503
    svc = service(tmp_path)
    await svc.limiter.close()
    with pytest.raises(StudioServiceError):
        await svc.save_limits(payload(svc))


@pytest.mark.asyncio
async def test_sessions_use_distinct_umo_and_alias_union_without_history(
    tmp_path, monkeypatch
):
    svc = service(tmp_path)
    column = object()

    class Selection:
        def distinct(self):
            return self

    def select(value):
        assert value is column
        return Selection()

    monkeypatch.setitem(sys.modules, "sqlmodel", SimpleNamespace(select=select))
    monkeypatch.setitem(
        sys.modules,
        "astrbot.core.db.po",
        SimpleNamespace(ConversationV2=SimpleNamespace(user_id=column)),
    )
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                fetchall=lambda: [
                    (UMO,),
                    (UMO,),
                    ("b:FriendMessage:u:t",),
                    ("not-umo",),
                ]
            )
        )
    )

    @asynccontextmanager
    async def get_db():
        yield session

    db = SimpleNamespace(
        get_db=get_db,
        get_umo_aliases=AsyncMock(
            return_value=[
                SimpleNamespace(umo=UMO, user_alias="管理员别名", auto_name="群名"),
                SimpleNamespace(
                    umo="c:GroupMessage:3", user_alias="", auto_name="仅有名称的群"
                ),
            ]
        ),
    )
    svc.context.get_db = lambda: db
    query = {
        "page": 1,
        "page_size": 1,
        "search": "",
        "message_type": "all",
        "platform": "",
    }
    first = await svc.sessions(**query)
    assert first["total"] == 3
    assert first["sessions"][0]["display_name"] == "管理员别名"
    second = await svc.sessions(**{**query, "page": 2})
    assert second["sessions"][0]["umo"] == "b:FriendMessage:u:t"
    found = await svc.sessions(
        **{**query, "search": "仅有名称", "message_type": "group", "platform": "c"}
    )
    assert found["total"] == 1
    assert session.execute.await_count == db.get_umo_aliases.await_count == 1


@pytest.mark.asyncio
async def test_session_failure_is_recoverable_and_manual_input_remains_possible(
    tmp_path,
):
    svc = service(tmp_path)
    svc._load_sessions = AsyncMock(side_effect=RuntimeError("unavailable"))
    query = {
        "page": 1,
        "page_size": 20,
        "search": "",
        "message_type": "all",
        "platform": "",
    }
    assert not (await svc.sessions(**query))["available"]
    svc._load_sessions.side_effect = None
    svc._load_sessions.return_value = []
    assert (await svc.sessions(**query))["available"]
    body = payload(svc)
    body["limits"]["rate_limit_rules"] = [{"rule_name": "手填", "umos": [UMO]}]
    assert (await svc.save_limits(body))["limits"]["rate_limit_rules"][0]["umos"] == [
        UMO
    ]
